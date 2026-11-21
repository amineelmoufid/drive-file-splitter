import asyncio
import os
import re
import shutil
import subprocess
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

CANONICAL_PREFIX = {
    'Fiscality': 'fisc',
    'Gestion': 'gestion',
    'Comptability': 'compta',
    'Finance': 'finance',
    'Econometry': 'eco',
    'Marche': 'marche',
    'Adm': 'adm'
}

def parse_audio_file(name):
    """
    Parses a filename from OUTPUT_FOLDER_ID into canonical lesson metadata.
    Handles:
      - Raw parts: 'adm 2_part1.opus' -> ('adm 2.opus', sub=0, part=1)
      - Sub-lessons: 'marche 4.1.opus' -> ('marche 4.opus', sub=1, part=1)
      - Both: 'gestion 7.2_part1.opus' -> ('gestion 7.opus', sub=2, part=1)
      - Spelling variations: 'Fiscalité 2.2.opus' -> ('fisc 2.opus', sub=2, part=1)
    """
    base = re.sub(r'\.opus$', '', name, flags=re.I)

    # 1. Check for _part(\d+)
    part_match = re.search(r'_part(\d+)$', base, re.I)
    if part_match:
        part_idx = int(part_match.group(1))
        base = base[:part_match.start()]
    else:
        part_idx = 1

    # 2. Check for sub-lesson dot notation (e.g. 'marche 4.1' or 'gestion 7.2')
    dot_match = re.match(r'^(.*?)\s*(\d+)\.(\d+)$', base)
    if dot_match:
        raw_subj = dot_match.group(1).strip()
        lesson_num = int(dot_match.group(2))
        sub_idx = int(dot_match.group(3))
    else:
        std_match = re.match(r'^(.*?)\s*(\d+)$', base)
        if std_match:
            raw_subj = std_match.group(1).strip()
            lesson_num = int(std_match.group(2))
            sub_idx = 0
        else:
            raw_subj = base.strip()
            lesson_num = None
            sub_idx = 0

    # 3. Match subject category
    clean_subj = raw_subj.lower()
    matched_class = None
    for c_name, info in NOTEBOOKS.items():
        if any(kw in clean_subj for kw in info['keywords']):
            matched_class = c_name
            break

    # 4. Generate canonical file name
    if matched_class and lesson_num is not None:
        prefix = CANONICAL_PREFIX.get(matched_class, raw_subj.lower())
        canonical_name = f"{prefix} {lesson_num}.opus"
    elif lesson_num is not None:
        canonical_name = f"{raw_subj.lower()} {lesson_num}.opus"
    else:
        canonical_name = f"{raw_subj}.opus"

    return canonical_name, sub_idx, part_idx, matched_class

def group_output_files(files):
    """
    Groups output files by canonical lesson name with sub-lesson and part structure.
    Returns:
      {
        canonical_name: {
          'class': matched_class,
          'sub_lessons': { sub_idx: [ (part_idx, file_dict) ] },
          'raw_files': [ file_dict ]
        }
      }
    """
    lessons = {}
    for f in files:
        canonical_name, sub_idx, part_idx, c_class = parse_audio_file(f['name'])
        if canonical_name not in lessons:
            lessons[canonical_name] = {
                'class': c_class,
                'sub_lessons': {},
                'raw_files': []
            }
        lessons[canonical_name]['raw_files'].append(f)
        lessons[canonical_name]['sub_lessons'].setdefault(sub_idx, []).append((part_idx, f))

    # Sort parts within each sub-lesson
    for c_name in lessons:
        subs = lessons[c_name]['sub_lessons']
        for s_idx in subs:
            subs[s_idx].sort(key=lambda x: x[0])

    return lessons

def merge_lesson_audio(drive_service, lesson_info, canonical_name, local_merged_path):
    """
    Downloads and merges all parts/sub-lessons for a canonical lesson.
    - Raw parts within a sub-lesson are concatenated binary.
    - Multiple sub-lessons (e.g. marche 4.1 + marche 4.2) are stitched with ffmpeg concat.
    """
    subs = lesson_info['sub_lessons']
    sorted_sub_keys = sorted(subs.keys())
    temp_files = []

    try:
        sub_merged_paths = []
        for s_idx in sorted_sub_keys:
            parts = subs[s_idx]
            if len(parts) == 1:
                # Single part for this sub-lesson
                f_info = parts[0][1]
                part_path = os.path.join(TEMP_AUDIO_DIR, f"temp_{s_idx}_{f_info['name']}")
                temp_files.append(part_path)
                print(f"  Downloading {mask_title(f_info['name'])}...")
                download_file(drive_service, f_info['id'], part_path)
                sub_merged_paths.append(part_path)
            else:
                # Multiple binary parts for this sub-lesson (e.g. _part1 + _part2)
                part_paths = []
                for p_num, p_info in parts:
                    part_file = os.path.join(TEMP_AUDIO_DIR, f"temp_{s_idx}_{p_info['name']}")
                    temp_files.append(part_file)
                    print(f"  Downloading part {p_num}: {mask_title(p_info['name'])}...")
                    download_file(drive_service, p_info['id'], part_file)
                    part_paths.append(part_file)

                sub_out_path = os.path.join(TEMP_AUDIO_DIR, f"temp_sub_{s_idx}_{canonical_name}")
                temp_files.append(sub_out_path)
                print(f"  Binary concatenating {len(part_paths)} raw part(s) for sub-lesson {s_idx}...")
                with open(sub_out_path, 'wb') as outfile:
                    for p_path in part_paths:
                        with open(p_path, 'rb') as infile:
                            outfile.write(infile.read())
                sub_merged_paths.append(sub_out_path)

        # Now combine sub-lessons into the final merged file
        if len(sub_merged_paths) == 1:
            shutil.copyfile(sub_merged_paths[0], local_merged_path)
        else:
            print(f"  Stitching {len(sub_merged_paths)} sub-lesson(s) into '{mask_title(canonical_name)}'...")
            # Try ffmpeg concat demuxer
            concat_list = os.path.join(TEMP_AUDIO_DIR, f"concat_list_{canonical_name}.txt")
            temp_files.append(concat_list)
            with open(concat_list, 'w', encoding='utf-8') as f:
                for sp in sub_merged_paths:
                    escaped_sp = os.path.abspath(sp).replace('\\', '/')
                    f.write(f"file '{escaped_sp}'\n")

            cmd = ['ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', concat_list, '-c', 'copy', local_merged_path]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                print(f"  Warning: ffmpeg failed ({proc.stderr[:150] if proc.stderr else 'no err'}), falling back to binary concatenation...")
                with open(local_merged_path, 'wb') as outfile:
                    for sp in sub_merged_paths:
                        with open(sp, 'rb') as infile:
                            outfile.write(infile.read())

        return True
    finally:
        for tf in temp_files:
            if os.path.exists(tf):
                try:
                    os.remove(tf)
                except Exception:
                    pass

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
    print(f"Total canonical consolidated lessons: {len(grouped_lessons)}\n")

    print("Scanning destination 'audio 3' Google Drive folder...")
    existing_audio3_files = list_all_files(drive_service, AUDIO3_FOLDER_ID)
    existing_drive_map = {f['name']: f['id'] for f in existing_audio3_files}
    print(f"Existing files in 'audio 3' folder: {len(existing_drive_map)}\n")

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
                sources_map = {s.title: s.id for s in sources if getattr(s, 'title', None)}
                notebook_sources_cache[nb_id] = {
                    'name': nb_name,
                    'sources_map': sources_map
                }
                print(f"Loaded NotebookLM [{nb_name} - V2]: {len(sources)} existing source(s).")
            except Exception as e:
                print(f"Warning: Failed to fetch sources for [{nb_name} - V2]: {e}")
                notebook_sources_cache[nb_id] = {'name': nb_name, 'sources_map': {}}

        print("\nStarting Audio Merge, Drive Upload, and NotebookLM Ingestion...\n")

        total_drive_uploaded = 0
        total_notebooklm_uploaded = 0
        total_skipped = 0
        total_failed = 0
        total_cleaned = 0

        processed_count = 0
        for canonical_name, lesson_info in sorted(grouped_lessons.items()):
            nb_name = lesson_info['class']
            if args.class_name and (not nb_name or nb_name.lower() != args.class_name.lower()):
                continue

            if args.limit and processed_count >= args.limit:
                print(f"\nReached limit of {args.limit} lessons. Stopping.")
                break

            masked = mask_title(canonical_name)
            if not nb_name or nb_name not in NOTEBOOKS:
                print(f"[SKIP UNCLASSIFIED] {masked}")
                continue

            nb_id = NOTEBOOKS[nb_name]['id']
            cache = notebook_sources_cache.get(nb_id, {})
            sources_map = cache.get('sources_map', {})

            canonical_lower = canonical_name.lower()
            drive_match_name = next((k for k in existing_drive_map if k.lower() == canonical_lower), None)
            need_drive_upload = drive_match_name is None

            nb_match_name = next((k for k in sources_map if k.lower() == canonical_lower), None)
            need_notebooklm_upload = nb_match_name is None

            # Sub-part names that should be cleaned up if they exist as separate obsolete files
            # (e.g. 'marche 4.1.opus' and 'marche 4.2.opus' when 'marche 4.opus' is created)
            fragment_names = set()
            if len(lesson_info['sub_lessons']) > 1:
                for f in lesson_info['raw_files']:
                    clean_name = re.sub(r'_part\d+', '', f['name'])
                    if clean_name != canonical_name:
                        fragment_names.add(clean_name)
                        fragment_names.add(f['name'])

            if not need_drive_upload and not need_notebooklm_upload:
                # Even if already merged, check if any old fragmented files need cleanup
                for frag in fragment_names:
                    frag_lower = frag.lower()
                    drive_frag_key = next((k for k in existing_drive_map if k.lower() == frag_lower and k.lower() != canonical_lower), None)
                    if drive_frag_key:
                        try:
                            print(f"  Cleaning obsolete fragmented file from Drive 'audio 3': {mask_title(drive_frag_key)}")
                            drive_service.files().delete(fileId=existing_drive_map[drive_frag_key]).execute()
                            del existing_drive_map[drive_frag_key]
                            total_cleaned += 1
                        except Exception as ce:
                            print(f"  Notice: could not delete obsolete Drive file {drive_frag_key}: {ce}")

                    nb_frag_key = next((k for k in sources_map if k.lower() == frag_lower and k.lower() != canonical_lower), None)
                    if nb_frag_key:
                        try:
                            print(f"  Cleaning obsolete fragmented source from NotebookLM: {mask_title(nb_frag_key)}")
                            await client.sources.delete(nb_id, sources_map[nb_frag_key])
                            del sources_map[nb_frag_key]
                            total_cleaned += 1
                        except Exception as ce:
                            print(f"  Notice: could not delete obsolete NotebookLM source {nb_frag_key}: {ce}")

                total_skipped += 1
                continue

            sub_count = len(lesson_info['sub_lessons'])
            total_file_count = len(lesson_info['raw_files'])
            print(f"\n[PROCESSING] '{masked}' ({sub_count} sub-lesson(s), {total_file_count} total file(s)) -> Target: [{nb_name} - V2]")
            processed_count += 1
            local_merged_path = os.path.join(TEMP_AUDIO_DIR, canonical_name)

            try:
                # Step A: Download & Merge locally
                merge_lesson_audio(drive_service, lesson_info, canonical_name, local_merged_path)
                merged_size_mb = os.path.getsize(local_merged_path) / (1024 * 1024)
                print(f"  Complete consolidated file ready: {merged_size_mb:.2f} MB")

                # Step B: Upload to Google Drive 'audio 3' folder if not already there
                if need_drive_upload:
                    print(f"  Uploading to Google Drive 'audio 3' folder...")
                    upload_file(drive_service, canonical_name, local_merged_path, AUDIO3_FOLDER_ID)
                    existing_drive_map[canonical_name] = "uploaded"
                    total_drive_uploaded += 1
                    print(f"  Uploaded '{masked}' to Drive 'audio 3' successfully.")
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
                                title=canonical_name,
                                wait=False
                            )
                            sources_map[canonical_name] = source.id
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

                # Step D: Cleanup old unmerged fragmented files from Drive 'audio 3' and NotebookLM
                for frag in fragment_names:
                    frag_lower = frag.lower()
                    drive_frag_key = next((k for k in existing_drive_map if k.lower() == frag_lower and existing_drive_map[k] != "uploaded"), None)
                    if drive_frag_key:
                        try:
                            print(f"  Cleaning obsolete fragmented file from Drive 'audio 3': {mask_title(drive_frag_key)}")
                            drive_service.files().delete(fileId=existing_drive_map[drive_frag_key]).execute()
                            del existing_drive_map[drive_frag_key]
                            total_cleaned += 1
                        except Exception as ce:
                            print(f"  Notice: could not delete obsolete Drive file {drive_frag_key}: {ce}")

                    nb_frag_key = next((k for k in sources_map if k.lower() == frag_lower and k.lower() != canonical_lower), None)
                    if nb_frag_key:
                        try:
                            print(f"  Cleaning obsolete fragmented source from NotebookLM: {mask_title(nb_frag_key)}")
                            await client.sources.delete(nb_id, sources_map[nb_frag_key])
                            del sources_map[nb_frag_key]
                            total_cleaned += 1
                        except Exception as ce:
                            print(f"  Notice: could not delete obsolete NotebookLM source {nb_frag_key}: {ce}")

            except Exception as e:
                total_failed += 1
                print(f"  ERROR processing '{masked}': {e}")
                traceback.print_exc()

            finally:
                if os.path.exists(local_merged_path):
                    try:
                        os.remove(local_merged_path)
                    except Exception:
                        pass

    print("\n=== Audio Processing & Export Summary ===")
    print(f"Total canonical lessons      : {len(grouped_lessons)}")
    print(f"Newly uploaded to Drive      : {total_drive_uploaded}")
    print(f"Newly exported to NotebookLM : {total_notebooklm_uploaded}")
    print(f"Obsolete fragments cleaned   : {total_cleaned}")
    print(f"Already up-to-date (skipped) : {total_skipped}")
    print(f"Failed                       : {total_failed}")
    print("=========================================\n")

if __name__ == '__main__':
    asyncio.run(main())
