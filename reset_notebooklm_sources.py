import asyncio
import argparse
import os
import sys
import time

HERMES_LIB = r"C:\Users\amine\AppData\Local\hermes\hermes-agent\venv\Lib\site-packages"
if os.path.exists(HERMES_LIB) and HERMES_LIB not in sys.path:
    sys.path.append(HERMES_LIB)

from notebooklm import NotebookLMClient
from sync_notebooklm import NOTEBOOKS

async def reset_notebooks(target_name=None, dry_run=True):
    print("=== NotebookLM Sources Reset Utility ===")
    if dry_run:
        print(">>> MODE: DRY-RUN (No changes will be made) <<<\n")
    else:
        print(">>> MODE: PURGE / DELETE SOURCES <<<\n")

    async with NotebookLMClient.from_storage() as client:
        email = await client.get_account_email()
        print(f"Connected as: {email}\n")

        total_purged = 0

        for name, data in NOTEBOOKS.items():
            if target_name and name.lower() != target_name.lower():
                continue

            nb_id = data['id']
            print(f"Checking Notebook [{name}] (ID: {nb_id})...")

            try:
                sources = await client.sources.list(nb_id)
            except Exception as e:
                print(f"  ERROR: Could not list sources for [{name}]: {e}\n")
                continue

            if not sources:
                print(f"  [{name}] is already empty (0 sources).\n")
                continue

            print(f"  Found {len(sources)} source(s) in [{name}].")

            if dry_run:
                for s in sources:
                    print(f"    - Would delete: '{getattr(s, 'title', 'Untitled')}' (ID: {getattr(s, 'id', 'unknown')})")
                print()
            else:
                source_ids = [s.id for s in sources]
                print(f"  Purging {len(source_ids)} source(s) from [{name}]...")
                try:
                    await client.sources.delete_many(nb_id, source_ids)
                    total_purged += len(source_ids)
                    print(f"  Purged successfully.")
                except Exception as e:
                    print(f"  ERROR: Failed to delete sources from [{name}]: {e}")

                # Verify clean state
                await asyncio.sleep(1.0)
                try:
                    remaining = await client.sources.list(nb_id)
                    print(f"  Post-verification: {len(remaining)} source(s) remaining in [{name}].\n")
                except Exception:
                    pass

                # Brief pause between notebooks for polite RPC pacing
                await asyncio.sleep(1.5)

        if dry_run:
            print("Dry-run complete. Run with --confirm to execute the purge.")
        else:
            print(f"Purge complete. Total sources removed across notebooks: {total_purged}.")

def main():
    parser = argparse.ArgumentParser(description="Purge all sources inside target NotebookLM projects.")
    parser.add_argument("--confirm", action="store_true", help="Confirm execution of source deletions.")
    parser.add_argument("--notebook", type=str, default=None, help="Specific notebook name to reset (e.g. Fiscality).")
    args = parser.parse_args()

    is_dry_run = not args.confirm
    asyncio.run(reset_notebooks(target_name=args.notebook, dry_run=is_dry_run))

if __name__ == '__main__':
    main()
