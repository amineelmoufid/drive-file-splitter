import asyncio
import base64
import datetime
import json
import os
import sys
import urllib.request
from pathlib import Path
from nacl import encoding, public

HERMES_LIB = r"C:\Users\amine\AppData\Local\hermes\hermes-agent\venv\Lib\site-packages"
if os.path.exists(HERMES_LIB) and HERMES_LIB not in sys.path:
    sys.path.append(HERMES_LIB)

from notebooklm import NotebookLMClient

TOKEN_FILE = r"C:\Users\amine\Desktop\tools\github api.txt"
REPO_NAME = "drive-file-splitter"
OWNER = "amineelmoufid"
STORAGE_FILE = Path.home() / ".notebooklm" / "profiles" / "default" / "storage_state.json"
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notebooklm_refresh.log")

def log(msg):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{timestamp}] {msg}"
    print(formatted, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(formatted + "\n")
    except Exception:
        pass

async def refresh_and_update_secret():
    log("Starting NotebookLM session refresh...")
    if not os.path.exists(TOKEN_FILE):
        log(f"ERROR: GitHub token file not found at {TOKEN_FILE}")
        return False

    with open(TOKEN_FILE, "r") as f:
        github_token = f.read().strip()

    # 1. Connect to NotebookLM to trigger internal session refresh
    try:
        async with NotebookLMClient.from_storage() as client:
            email = await client.get_account_email()
            log(f"Connected to NotebookLM as: {email}")
    except Exception as e:
        log(f"ERROR: Failed to connect/refresh NotebookLM locally: {e}")
        return False

    if not STORAGE_FILE.exists():
        log(f"ERROR: Storage state not found at {STORAGE_FILE}")
        return False

    with open(STORAGE_FILE, "r", encoding="utf-8") as f:
        storage_json_str = f.read()

    # 2. Upload to GitHub Secrets
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "NotebookLMAutoRefresh",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{OWNER}/{REPO_NAME}/actions/secrets/public-key",
            headers=headers
        )
        with urllib.request.urlopen(req) as resp:
            pubkey_info = json.loads(resp.read().decode("utf-8"))

        key_id = pubkey_info["key_id"]
        public_key_b64 = pubkey_info["key"]

        # Encrypt with PyNaCl
        public_key = public.PublicKey(public_key_b64.encode("utf-8"), encoding.Base64Encoder())
        sealed_box = public.SealedBox(public_key)
        encrypted = sealed_box.encrypt(storage_json_str.encode("utf-8"))
        encrypted_b64 = base64.b64encode(encrypted).decode("utf-8")

        # Put secret
        payload = json.dumps({"encrypted_value": encrypted_b64, "key_id": key_id}).encode("utf-8")
        put_req = urllib.request.Request(
            f"https://api.github.com/repos/{OWNER}/{REPO_NAME}/actions/secrets/NOTEBOOKLM_STORAGE_STATE_JSON",
            data=payload,
            headers=headers,
            method="PUT"
        )
        with urllib.request.urlopen(put_req) as resp:
            pass

        log("SUCCESS: 'NOTEBOOKLM_STORAGE_STATE_JSON' refreshed and updated in GitHub Secrets!")
        return True
    except Exception as e:
        log(f"ERROR: Failed to update GitHub Secret: {e}")
        return False

if __name__ == '__main__':
    success = asyncio.run(refresh_and_update_secret())
    sys.exit(0 if success else 1)
