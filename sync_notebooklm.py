import asyncio
import json
import os
import re
import sys
import traceback
from pathlib import Path
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# Check hermes-agent path if not already in sys.path
HERMES_LIB = r"C:\Users\amine\AppData\Local\hermes\hermes-agent\venv\Lib\site-packages"
if os.path.exists(HERMES_LIB) and HERMES_LIB not in sys.path:
    sys.path.append(HERMES_LIB)

from notebooklm import NotebookLMClient
from notebooklm._app.source_listing import fetch_sources
from notebooklm._app.source_mutations import SourceAddDrivePlan, execute_source_add_drive

SCOPES = ['https://www.googleapis.com/auth/drive']

# Folder IDs
STRUCTURED_FOLDER_ID = os.environ.get('STRUCTURED_FOLDER_ID', '10GXi-A6ERI6_bpZXG1kUoBtE8ptGy0_b')
RAW_TRANSCRIPTS_FOLDER_ID = os.environ.get('RAW_TRANSCRIPTS_FOLDER_ID', '1obO_d69TfTOdJ4OMzsEDNCiza-DjRcrg')

# NotebookLM Target Notebooks Mapping
NOTEBOOKS = {
    'Fiscality': {
        'id': '1ba0a12f-9607-4b0b-a51f-0fcefca93c26',
        'keywords': ['fiscalite', 'fiscale', 'fiscal', 'fisc']
    },
    'Gestion': {
        'id': '2da45a06-2036-4e0a-a4c4-bf00535eb51e',
        'keywords': ['gestion']
    },
    'Comptability': {
        'id': '888a0b4d-d7b3-47c4-a5c4-300e9b46da03',
        'keywords': ['comptabilite', 'comptability', 'compta']
    },
    'Finance': {
        'id': 'f2059b83-6536-4686-b388-213c088d2f1d',
        'keywords': ['finance', 'fin']
    },
    'Econometry': {
        'id': '6b0f93d4-8bb1-4f00-9c02-ce4a88270b57',
        'keywords': ['econometrie', 'econometry', 'econo', 'eco']
    },
    'Marche': {
        'id': '9376c9e2-7d9e-4a65-8f37-487f15da8fb0',
        'keywords': ['marche', 'bours']
    },
    'Adm': {
        'id': '33f71335-5cb2-436b-b36f-390ccc47949b',
        'keywords': ['admin', 'adm']
    }
}

def mask_title(title):
    """Anonymize filenames in logs for privacy."""
    name, ext = os.path.splitext(title)
    if len(name) <= 4:
        return f"***{ext}"
    return f"{name[:2]}***{name[-2:]}{ext}"

def setup_notebooklm_storage():
    """Ensure storage_state.json exists from environment or local path."""
    storage_env = os.environ.get("NOTEBOOKLM_STORAGE_STATE_JSON")
    default_dir = Path.home() / ".notebooklm" / "profiles" / "default"
    default_dir.mkdir(parents=True, exist_ok=True)
    target_path = default_dir / "storage_state.json"

    if storage_env:
        with open(target_path, "w", encoding="utf-8") as f:
            f.write(storage_env)
        print("Restored NotebookLM session storage from environment secret.")
    elif not target_path.exists():
        raise FileNotFoundError(
            f"NotebookLM storage_state.json not found at {target_path} and secret NOTEBOOKLM_STORAGE_STATE_JSON not set."
        )

def get_drive_service():
    """Authenticate and create Google Drive API client."""
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
    """List all non-trashed files in a Google Drive folder."""
    files = []
    page_token = None
    query = f"'{folder_id}' in parents and trashed = false"

    while True:
        resp = service.files().list(
            q=query,
            fields="nextPageToken, files(id, name, mimeType, modifiedTime)",
            pageSize=1000,
            pageToken=page_token
        ).execute()

        files.extend(resp.get('files', []))
        page_token = resp.get('nextPageToken')
        if not page_token:
            break
    return files

def classify_file(filename):
    """Classify a filename into its target notebook using prioritized keyword regex."""
    lower = filename.lower()
    for nb_name, data in NOTEBOOKS.items():
        for kw in data['keywords']:
            pattern = r'(?:\b|_)' + re.escape(kw) + r'(?:\b|_|\d|\s)'
            if re.search(pattern, lower) or kw in lower:
                return nb_name, data['id']
    return None, None

async def main():
    print("=== Supervised Drive -> NotebookLM Synchronization ===\n")
    setup_notebooklm_storage()
    drive_service = get_drive_service()

    # 1. Scan the two Google Drive folders
    print("Scanning Google Drive folders...")
    structured_files = list_folder_files(drive_service, STRUCTURED_FOLDER_ID)
    raw_files = list_folder_files(drive_service, RAW_TRANSCRIPTS_FOLDER_ID)

    all_files = []
    for f in structured_files:
        f['source_folder'] = 'Structured courses'
        all_files.append(f)
    for f in raw_files:
        f['source_folder'] = 'Raw transcripts'
        all_files.append(f)

    print(f"Total discovered files: {len(all_files)} (Structured: {len(structured_files)}, Raw: {len(raw_files)})\n")

    # 2. Connect to NotebookLM client
    async with NotebookLMClient.from_storage() as client:
        # Pre-fetch existing sources for all 7 notebooks to check duplicates
        notebook_sources_cache = {}
        for nb_name, nb_data in NOTEBOOKS.items():
            nb_id = nb_data['id']
            try:
                sources = await fetch_sources(client, nb_id, label_filter=None, label_resolver=None)
                existing_drive_ids = {
                    s.drive_document_id for s in sources if getattr(s, 'drive_document_id', None)
                }
                existing_titles = {
                    s.title for s in sources if getattr(s, 'title', None)
                }
                notebook_sources_cache[nb_id] = {
                    'name': nb_name,
                    'drive_ids': existing_drive_ids,
                    'titles': existing_titles
                }
                print(f"Loaded NotebookLM [{nb_name}]: {len(sources)} existing source(s).")
            except Exception as e:
                print(f"Warning: Failed to fetch sources for notebook '{nb_name}' ({nb_id}): {e}")
                notebook_sources_cache[nb_id] = {
                    'name': nb_name,
                    'drive_ids': set(),
                    'titles': set()
                }

        print("\nStarting document verification & ingestion...\n")

        total_imported = 0
        total_already_present = 0
        total_unclassified = 0
        total_failed = 0

        for file in all_files:
            file_name = file['name']
            file_id = file['id']
            folder_origin = file['source_folder']
            masked = mask_title(file_name)

            nb_name, nb_id = classify_file(file_name)
            if not nb_id:
                print(f"[UNCLASSIFIED] '{masked}' from {folder_origin} -> Skipped (No subject match).")
                total_unclassified += 1
                continue

            cache = notebook_sources_cache.get(nb_id)
            existing_drive_ids = cache['drive_ids'] if cache else set()

            # STRICT ANTI-DUPLICATE CHECK
            if file_id in existing_drive_ids:
                total_already_present += 1
                continue

            # Add new document to target notebook
            print(f"[ADDING] '{masked}' ({folder_origin}) -> Target Notebook: [{nb_name}]...")
            try:
                plan = SourceAddDrivePlan(
                    notebook_id=nb_id,
                    file_id=file_id,
                    title=file_name,
                    mime_type="google-doc"
                )
                await execute_source_add_drive(client, plan)
                existing_drive_ids.add(file_id)
                total_imported += 1
                print(f"  Successfully imported '{masked}' into [{nb_name}].")
                # Polite pacing to avoid Google RPC rate limiting
                await asyncio.sleep(1.5)
            except Exception as e:
                total_failed += 1
                print(f"  ERROR: Failed to add '{masked}' to [{nb_name}]: {e}")
                traceback.print_exc()

    print("\n=== Synchronization Summary ===")
    print(f"Total files examined     : {len(all_files)}")
    print(f"Already present (skipped): {total_already_present}")
    print(f"Newly imported           : {total_imported}")
    print(f"Unclassified             : {total_unclassified}")
    print(f"Failed                   : {total_failed}")
    print("================================\n")

if __name__ == '__main__':
    asyncio.run(main())
