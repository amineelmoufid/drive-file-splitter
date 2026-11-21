import asyncio
import os
import re
import sys
import time
import traceback
from pathlib import Path
import httpx

HERMES_LIB = r"C:\Users\amine\AppData\Local\hermes\hermes-agent\venv\Lib\site-packages"
if os.path.exists(HERMES_LIB) and HERMES_LIB not in sys.path:
    sys.path.append(HERMES_LIB)
sys.path.append(r"c:\Users\amine\Documents\antigravity\charming-newton")

from notebooklm import NotebookLMClient
from split_drive import get_drive_service, list_all_files, download_file, upload_file, mask_title, OUTPUT_FOLDER_ID
from sync_notebooklm import NOTEBOOKS, classify_file

AUDIO3_FOLDER_ID = os.environ.get('AUDIO3_FOLDER_ID', '1iqXltmHyKeAQuCcnMinnEIsV_OBV-uDJ')
TEMP_AUDIO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "temp_audio_merge")
os.makedirs(TEMP_AUDIO_DIR, exist_ok=True)

def group_output_files(files):
    """
    Groups output files by base lesson name.
    E.g. 'adm 2_part1.opus' and 'adm 2_part2.opus' -> 'adm 2.opus'
    """
    groups = {}
    for f in files:
        name = f['name']
        m = re.match(r'^(.*?)(?:_part(\d+))(\.[^.]+)$', name, re.IGNORECASE)
        if m:
            base_name = m.group(1) + m.group(3)
            part_num = int(m.group(2))
        else:
            base_name = name
            part_num = 1
        groups.setdefault(base_name, []).append((part_num, f))

    # Sort parts for each lesson by part number
    for base_name in groups:
        groups[base_name].sort(key=lambda x: x[0])
    return groups

async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="Limit number of lessons to process")
    parser.add_argument("--class-name", type=str, default=None, help="Process only a specific class")
    args = parser.parse_args()

    print("=== Consolidate Audios -> Google Drive 'audio 3' & Export to NotebookLM V2 ===\n")

    drive_service = get_drive_service()

    # 1. Inspect Google Drive folders
    print("Scanning Output Folder (parts)...")
    out_files = list_all_files(drive_service, OUTPUT_FOLDER_ID)
    grouped_lessons = group_output_files(out_files)
    print(f"Total parts in output folder: {len(out_files)}")
    print(f"Total unique consolidated lessons: {len(grouped_lessons)}\n")

    print("Scanning destination 'audio 3' Google Drive folder...")
    existing_audio3_files = list_all_files(drive_service, AUDIO3_FOLDER_ID)
    existing_drive_titles = {f['name'] for f in existing_audio3_files}
    print(f"Existing files in 'audio 3' folder: {len(existing_drive_titles)}\n")

    # 2. Connect to NotebookLM
    custom_timeout = httpx.Timeout(connect=30.0, read=300.0, write=300.0, pool=30.0)
    async with NotebookLMClient.from_storage(upload_timeout=custom_timeout) as client:
        print("Connected to NotebookLM.")
        email = await client.get_account_email()
        print(f"Account: {email}\n")

        # Pre-fetch existing sources for all 7 V2 notebooks
        notebook_sources_cache = {}
        for nb_name, nb_data in NOTEBOOKS.items():
            nb_id = nb_data['id']
            try:
                sources = await client.sources.list(nb_id)
                existing_titles = {s.title for s in sources if getattr(s, 'title', None)}
                notebook_sources_cache[nb_id] = {
                    'name': nb_name,
                    'titles': existing_titles
                }
                print(f"Loaded NotebookLM [{nb_name} - V2]: {len(sources)} existing source(s).")
            except Exception as e:
                print(f"Warning: Failed to fetch sources for [{nb_name} - V2]: {e}")
                notebook_sources_cache[nb_id] = {'name': nb_name, 'titles': set()}

        print("\nStarting Audio Merge, Drive Upload, and NotebookLM Ingestion...\n")

        total_drive_uploaded = 0
        total_notebooklm_uploaded = 0
        total_skipped = 0
        total_failed = 0

        processed_count = 0
        for base_name, parts in sorted(grouped_lessons.items()):
            if args.class_name:
                c, _ = classify_file(base_name)
                if not c or c.lower() != args.class_name.lower():
                    continue

            if args.limit and processed_count >= args.limit:
                print(f"\nReached limit of {args.limit} lessons. Stopping.")
                break

            masked = mask_title(base_name)
            nb_name, nb_id = classify_file(base_name)

            if not nb_id:
                print(f"[SKIP UNCLASSIFIED] {masked}")
                continue

            cache = notebook_sources_cache.get(nb_id, {})
            existing_nb_titles = cache.get('titles', set())

            need_drive_upload = base_name not in existing_drive_titles
            need_notebooklm_upload = base_name not in existing_nb_titles

            if not need_drive_upload and not need_notebooklm_upload:
                total_skipped += 1
                continue

            print(f"\n[PROCESSING] '{masked}' ({len(parts)} part(s)) -> Target: [{nb_name} - V2]")
            processed_count += 1
            local_merged_path = os.path.join(TEMP_AUDIO_DIR, base_name)
            temp_parts_to_clean = []

            try:
                # Step A: Download & Merge locally
                if len(parts) == 1:
                    f_info = parts[0][1]
                    print(f"  Downloading single file {masked}...")
                    download_file(drive_service, f_info['id'], local_merged_path)
                else:
                    part_paths = []
                    for p_num, p_info in parts:
                        part_file = os.path.join(TEMP_AUDIO_DIR, p_info['name'])
                        temp_parts_to_clean.append(part_file)
                        print(f"  Downloading part {p_num}: {mask_title(p_info['name'])}...")
                        download_file(drive_service, p_info['id'], part_file)
                        part_paths.append(part_file)

                    print(f"  Merging {len(part_paths)} parts into full audio...")
                    with open(local_merged_path, 'wb') as outfile:
                        for p_path in part_paths:
                            with open(p_path, 'rb') as infile:
                                outfile.write(infile.read())

                merged_size_mb = os.path.getsize(local_merged_path) / (1024 * 1024)
                print(f"  Complete merged file ready: {merged_size_mb:.2f} MB")

                # Step B: Upload to Google Drive 'audio 3' folder if not already there
                if need_drive_upload:
                    print(f"  Uploading to Google Drive 'audio 3' folder...")
                    upload_file(drive_service, base_name, local_merged_path, AUDIO3_FOLDER_ID)
                    existing_drive_titles.add(base_name)
                    total_drive_uploaded += 1
                    print(f"  Uploaded to Drive 'audio 3' successfully.")
                else:
                    print(f"  Already present in Drive 'audio 3', skipping Drive upload.")

                # Step C: Export/Upload to NotebookLM V2 project if not already there
                if need_notebooklm_upload:
                    max_retries = 3
                    uploaded_ok = False
                    for attempt in range(1, max_retries + 1):
                        try:
                            print(f"  Exporting audio to NotebookLM [{nb_name} - V2] (Attempt {attempt}/{max_retries})...", flush=True)
                            source = await client.sources.add_file(
                                nb_id,
                                local_merged_path,
                                title=base_name,
                                wait=False
                            )
                            existing_nb_titles.add(base_name)
                            total_notebooklm_uploaded += 1
                            print(f"  Successfully registered audio with NotebookLM [{nb_name} - V2]! (Source ID: {source.id})", flush=True)
                            uploaded_ok = True
                            await asyncio.sleep(2.0)
                            break
                        except Exception as upload_err:
                            print(f"  Warning: Attempt {attempt} failed for '{masked}': {upload_err}", flush=True)
                            if attempt < max_retries:
                                await asyncio.sleep(4.0 * attempt)
                            else:
                                raise upload_err
                else:
                    print(f"  Already present in NotebookLM [{nb_name} - V2], skipping.", flush=True)

            except Exception as e:
                total_failed += 1
                print(f"  ERROR processing '{masked}': {e}")
                traceback.print_exc()

            finally:
                # Cleanup temporary files
                if os.path.exists(local_merged_path):
                    try:
                        os.remove(local_merged_path)
                    except Exception:
                        pass
                for p_file in temp_parts_to_clean:
                    if os.path.exists(p_file):
                        try:
                            os.remove(p_file)
                        except Exception:
                            pass

    print("\n=== Audio Processing & Export Summary ===")
    print(f"Total consolidated lessons : {len(grouped_lessons)}")
    print(f"Newly uploaded to Drive    : {total_drive_uploaded}")
    print(f"Newly exported to NotebookLM: {total_notebooklm_uploaded}")
    print(f"Already up-to-date (skipped): {total_skipped}")
    print(f"Failed                     : {total_failed}")
    print("=========================================\n")

if __name__ == '__main__':
    asyncio.run(main())
