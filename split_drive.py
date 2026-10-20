import io
import json
import os
import traceback
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

SCOPES = ['https://www.googleapis.com/auth/drive']

# Folder IDs loaded securely from environment / GitHub Secrets
SOURCE_FOLDER_ID = os.environ.get('SOURCE_FOLDER_ID')
OUTPUT_FOLDER_ID = os.environ.get('OUTPUT_FOLDER_ID')
LIMIT_SIZE = 10 * 1024 * 1024  # 10 MB

# Fallback for local testing if secrets not in environment
if not SOURCE_FOLDER_ID or not OUTPUT_FOLDER_ID:
    # Check if a local config exists
    local_cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "folders.json")
    if os.path.exists(local_cfg):
        with open(local_cfg, 'r') as cf:
            cfg = json.load(cf)
            SOURCE_FOLDER_ID = SOURCE_FOLDER_ID or cfg.get('SOURCE_FOLDER_ID')
            OUTPUT_FOLDER_ID = OUTPUT_FOLDER_ID or cfg.get('OUTPUT_FOLDER_ID')

TEMP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "temp_drive_sync")
os.makedirs(TEMP_DIR, exist_ok=True)

def mask_title(title):
    """Anonymizes filenames in public logs for maximum privacy."""
    name, ext = os.path.splitext(title)
    if len(name) <= 4:
        return f"***{ext}"
    return f"{name[:2]}***{name[-2:]}{ext}"

def get_drive_service():
    token_json_env = os.environ.get("GOOGLE_DRIVE_TOKEN_JSON")
    token_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'token.json')

    if token_json_env:
        info = json.loads(token_json_env)
        creds = Credentials.from_authorized_user_info(info, SCOPES)
    elif os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    else:
        raise FileNotFoundError("token.json not found and GOOGLE_DRIVE_TOKEN_JSON secret not set!")

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            if not token_json_env and os.path.exists(token_path):
                with open(token_path, 'w') as f:
                    f.write(creds.to_json())
        else:
            raise Exception("Credentials expired or invalid.")

    return build('drive', 'v3', credentials=creds)

def split_file(local_path, file_size):
    part1_size = file_size // 2
    part2_size = file_size - part1_size

    name, ext = os.path.splitext(local_path)
    part1_path = f"{name}_part1{ext}"
    part2_path = f"{name}_part2{ext}"

    with open(local_path, 'rb') as f:
        with open(part1_path, 'wb') as out1:
            out1.write(f.read(part1_size))
        with open(part2_path, 'wb') as out2:
            out2.write(f.read(part2_size))

    return part1_path, part2_path

def list_all_files(service, folder_id):
    files = []
    page_token = None
    query = f"'{folder_id}' in parents and trashed = false"

    while True:
        resp = service.files().list(
            q=query,
            fields="nextPageToken, files(id, name, size, modifiedTime)",
            pageSize=1000,
            pageToken=page_token
        ).execute()

        files.extend(resp.get('files', []))
        page_token = resp.get('nextPageToken')
        if not page_token:
            break
    return files

def download_file(service, file_id, dest_path):
    request = service.files().get_media(fileId=file_id)
    with open(dest_path, 'wb') as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

def upload_file(service, name, local_path, parent_folder_id):
    metadata = {'name': name, 'parents': [parent_folder_id]}
    media = MediaFileUpload(local_path, resumable=True)
    return service.files().create(body=metadata, media_body=media, fields='id').execute()

def main():
    if not SOURCE_FOLDER_ID or not OUTPUT_FOLDER_ID:
        raise ValueError("SOURCE_FOLDER_ID and OUTPUT_FOLDER_ID must be configured via Secrets.")

    service = get_drive_service()

    # 1. Deduplicate output folder
    print("Checking destination folder for duplicates...")
    output_files = list_all_files(service, OUTPUT_FOLDER_ID)

    files_by_title = {}
    for f in output_files:
        files_by_title.setdefault(f['name'], []).append(f)

    existing_files = set()
    cleaned_dups = 0
    for name, flist in files_by_title.items():
        existing_files.add(name)
        if len(flist) > 1:
            flist.sort(key=lambda x: x.get('modifiedTime', ''), reverse=True)
            for dup in flist[1:]:
                try:
                    service.files().delete(fileId=dup['id']).execute()
                    cleaned_dups += 1
                except Exception:
                    pass

    print(f"Destination check complete: {len(existing_files)} items verified ({cleaned_dups} duplicates cleaned).\n")

    # 2. Process source files
    print("Scanning source folder...")
    source_files = list_all_files(service, SOURCE_FOLDER_ID)
    print(f"Total source items: {len(source_files)}.\n")

    success_count = 0
    skipped_count = 0
    fail_count = 0

    for file in source_files:
        file_title = file['name']
        file_size = int(file.get('size', 0))
        file_id = file['id']
        masked = mask_title(file_title)

        if file_size == 0:
            continue

        temp_files_to_clean = []

        try:
            if file_size > LIMIT_SIZE:
                name, ext = os.path.splitext(file_title)
                p1_title = f"{name}_part1{ext}"
                p2_title = f"{name}_part2{ext}"

                if p1_title in existing_files and p2_title in existing_files:
                    skipped_count += 1
                    continue

                print(f"Processing (Split > 10MB): {masked} ({file_size / (1024*1024):.2f} MB)")
                local_temp_path = os.path.join(TEMP_DIR, file_title)
                temp_files_to_clean.append(local_temp_path)

                download_file(service, file_id, local_temp_path)
                p1_path, p2_path = split_file(local_temp_path, file_size)
                temp_files_to_clean.extend([p1_path, p2_path])

                if p1_title not in existing_files:
                    upload_file(service, p1_title, p1_path, OUTPUT_FOLDER_ID)
                    existing_files.add(p1_title)

                if p2_title not in existing_files:
                    upload_file(service, p2_title, p2_path, OUTPUT_FOLDER_ID)
                    existing_files.add(p2_title)

                print(f"  Successfully uploaded parts for {masked}")
                success_count += 1
            else:
                if file_title in existing_files:
                    skipped_count += 1
                    continue

                print(f"Processing (Direct <= 10MB): {masked} ({file_size / (1024*1024):.2f} MB)")
                local_temp_path = os.path.join(TEMP_DIR, file_title)
                temp_files_to_clean.append(local_temp_path)

                download_file(service, file_id, local_temp_path)
                upload_file(service, file_title, local_temp_path, OUTPUT_FOLDER_ID)
                existing_files.add(file_title)

                print(f"  Successfully uploaded {masked}")
                success_count += 1

        except Exception as e:
            fail_count += 1
            print(f"ERROR: Failed processing item {masked}: {e}")
        finally:
            for path in temp_files_to_clean:
                if os.path.exists(path):
                    try:
                        os.remove(path)
                    except Exception:
                        pass

    print(f"\nExecution summary: {success_count} processed, {skipped_count} skipped (up-to-date), {fail_count} failed.")

if __name__ == '__main__':
    main()
