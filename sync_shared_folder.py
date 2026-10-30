import os
import json
import time
import traceback
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = ['https://www.googleapis.com/auth/drive']

# Folder IDs
STRUCTURED_FOLDER_ID = os.environ.get('STRUCTURED_FOLDER_ID', '10GXi-A6ERI6_bpZXG1kUoBtE8ptGy0_b')
RAW_TRANSCRIPTS_FOLDER_ID = os.environ.get('RAW_TRANSCRIPTS_FOLDER_ID', '1obO_d69TfTOdJ4OMzsEDNCiza-DjRcrg')
SHARED_FOLDER_ID = os.environ.get('SHARED_FOLDER_ID', '1KI8Cw5scdk4LHOc4SQydG5cqH5UzMDLf')

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
            raise Exception("Google Drive credentials expired or invalid.")

    return build('drive', 'v3', credentials=creds)

def list_folder_files(service, folder_id):
    files = []
    page_token = None
    query = f"'{folder_id}' in parents and trashed = false"

    while True:
        resp = service.files().list(
            q=query,
            fields="nextPageToken, files(id, name, mimeType, modifiedTime, createdTime)",
            pageSize=1000,
            pageToken=page_token
        ).execute()

        files.extend(resp.get('files', []))
        page_token = resp.get('nextPageToken')
        if not page_token:
            break
    return files

def main():
    print("=== Supervised Drive -> Shared Folder Synchronization ===")
    service = get_drive_service()

    # 1. Fetch source files from both folders
    print("Scanning source folders...")
    structured_files = list_folder_files(service, STRUCTURED_FOLDER_ID)
    raw_files = list_folder_files(service, RAW_TRANSCRIPTS_FOLDER_ID)

    source_files = {}
    for f in structured_files:
        f['source_folder'] = 'Structured courses'
        source_files[f['name']] = f
    for f in raw_files:
        f['source_folder'] = 'Raw transcripts'
        source_files[f['name']] = f

    total_sources = len(source_files)
    print(f"Total unique source files: {total_sources} (Structured: {len(structured_files)}, Raw: {len(raw_files)})\n")

    # 2. Fetch destination files in the 'shared' folder
    print("Scanning destination 'shared' folder...")
    shared_files = list_folder_files(service, SHARED_FOLDER_ID)

    # Group by name to check existing copies and clean any duplicate copies
    shared_by_name = {}
    for f in shared_files:
        shared_by_name.setdefault(f['name'], []).append(f)

    cleaned_dups = 0
    active_shared = {}
    for name, flist in shared_by_name.items():
        if len(flist) > 1:
            # Sort newest first, keep index 0, delete duplicates
            flist.sort(key=lambda x: x.get('modifiedTime', ''), reverse=True)
            for dup in flist[1:]:
                try:
                    service.files().delete(fileId=dup['id']).execute()
                    cleaned_dups += 1
                except Exception as e:
                    print(f"  Warning: failed to delete duplicate copy: {e}")
        active_shared[name] = flist[0]

    if cleaned_dups > 0:
        print(f"Cleaned {cleaned_dups} duplicate copies in 'shared' folder.\n")

    print(f"Current unique items in 'shared' folder: {len(active_shared)}\n")

    # 3. Synchronize all source files to 'shared' folder
    print("Starting file synchronization check...")
    newly_copied = 0
    updated_count = 0
    already_synced = 0
    failed_count = 0

    for name, source_file in source_files.items():
        source_id = source_file['id']
        source_mod = source_file.get('modifiedTime', '')
        folder_origin = source_file['source_folder']
        masked = mask_title(name)

        target_file = active_shared.get(name)

        if not target_file:
            # Case 1: File is missing in shared folder -> Copy it
            print(f"  [COPYING NEW] '{masked}' ({folder_origin}) -> 'shared' folder...")
            try:
                service.files().copy(
                    fileId=source_id,
                    body={
                        'name': name,
                        'parents': [SHARED_FOLDER_ID],
                        'description': f"synced_from:{source_id}"
                    }
                ).execute()
                newly_copied += 1
                time.sleep(0.3)  # Gentle pacing
            except Exception as e:
                failed_count += 1
                print(f"    ERROR copying '{masked}': {e}")
        else:
            # Case 2: File exists -> Check if source has newer edits
            target_mod = target_file.get('modifiedTime', '')
            if source_mod > target_mod:
                print(f"  [UPDATING EDITS] '{masked}' (Source is newer) -> Re-copying to 'shared'...")
                try:
                    # Remove older copy and copy the latest edited version
                    service.files().delete(fileId=target_file['id']).execute()
                    service.files().copy(
                        fileId=source_id,
                        body={
                            'name': name,
                            'parents': [SHARED_FOLDER_ID],
                            'description': f"synced_from:{source_id}"
                        }
                    ).execute()
                    updated_count += 1
                    time.sleep(0.3)
                except Exception as e:
                    failed_count += 1
                    print(f"    ERROR updating '{masked}': {e}")
            else:
                already_synced += 1

    # 4. Final verification check
    print("\nVerifying final synchronization status...")
    final_shared = list_folder_files(service, SHARED_FOLDER_ID)
    final_names = {f['name'] for f in final_shared}

    missing_files = [name for name in source_files.keys() if name not in final_names]
    is_fully_synced = len(missing_files) == 0

    print("\n=== Synchronization & Health Check Summary ===")
    print(f"Total source files to sync    : {total_sources}")
    print(f"Already up-to-date            : {already_synced}")
    print(f"Newly copied to 'shared'      : {newly_copied}")
    print(f"Updated with newer edits      : {updated_count}")
    print(f"Failed operations             : {failed_count}")
    print(f"Total files in 'shared' folder: {len(final_shared)}")

    if is_fully_synced:
        print(f"STATUS: ALL {total_sources} FILES ARE 100% SYNCHRONIZED AND VERIFIED! (MATCH: {len(final_shared)}/{total_sources})")
    else:
        print(f"STATUS: WARNING: {len(missing_files)} file(s) are missing from 'shared' folder.")
    print("==============================================\n")

if __name__ == '__main__':
    main()
