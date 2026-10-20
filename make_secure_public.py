import base64
import json
import os
import subprocess
import time
import urllib.request
import urllib.error
from nacl import encoding, public

TOKEN_FILE = r"C:\Users\amine\Desktop\tools\github api.txt"
with open(TOKEN_FILE, "r") as f:
    GITHUB_TOKEN = f.read().strip()

REPO_NAME = "drive-file-splitter"
OWNER = "amineelmoufid"

HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "User-Agent": "SecureSetup",
    "X-GitHub-Api-Version": "2022-11-28"
}

def api_request(method, url, data=None):
    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(url, data=body, headers=HEADERS, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            content = resp.read().decode("utf-8")
            return json.loads(content) if content else {}
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8")
        print(f"HTTPError {e.code} on {url}: {err}")
        raise

# 1. Fetch public key for secrets
print("Fetching repository public key...")
pubkey_info = api_request("GET", f"https://api.github.com/repos/{OWNER}/{REPO_NAME}/actions/secrets/public-key")
key_id = pubkey_info["key_id"]
public_key_b64 = pubkey_info["key"]
public_key = public.PublicKey(public_key_b64.encode("utf-8"), encoding.Base64Encoder())
sealed_box = public.SealedBox(public_key)

def set_secret(secret_name, secret_value):
    encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
    encrypted_b64 = base64.b64encode(encrypted).decode("utf-8")
    payload = {"encrypted_value": encrypted_b64, "key_id": key_id}
    api_request("PUT", f"https://api.github.com/repos/{OWNER}/{REPO_NAME}/actions/secrets/{secret_name}", payload)
    print(f"Secret '{secret_name}' stored securely in GitHub Secrets.")

set_secret("SOURCE_FOLDER_ID", "1earjvkiZsK2PIB8KzoDFLrdJh3IURhZ2")
set_secret("OUTPUT_FOLDER_ID", "1iSfUarcsKNQP2Rk5yF0m6pO483e1YGke")

# 2. Re-create clean git history (erase old commits containing raw folder IDs)
print("\nCreating clean sanitized git history...")
subprocess.run(["git", "checkout", "--orphan", "clean-main"], check=True)
subprocess.run(["git", "add", "-A"], check=True)
subprocess.run(["git", "commit", "-m", "Initialize secure Drive file splitter workflow"], check=True)
subprocess.run(["git", "branch", "-D", "main"], check=True)
subprocess.run(["git", "branch", "-m", "main"], check=True)

remote_url = f"https://x-access-token:{GITHUB_TOKEN}@github.com/{OWNER}/{REPO_NAME}.git"
subprocess.run(["git", "remote", "set-url", "origin", remote_url], check=True)
subprocess.run(["git", "push", "-f", "origin", "main"], check=True)
safe_url = f"https://github.com/{OWNER}/{REPO_NAME}.git"
subprocess.run(["git", "remote", "set-url", "origin", safe_url], check=True)
print("Clean sanitized history pushed to GitHub!")

# 3. Change repository to PUBLIC
print("\nChanging repository visibility to PUBLIC...")
api_request("PATCH", f"https://api.github.com/repos/{OWNER}/{REPO_NAME}", {"private": False})
print("Repository is now PUBLIC: Unlimited GitHub Actions minutes enabled!")

# 4. Trigger test run
print("\nTriggering verification run on public repo...")
time.sleep(2)
api_request("POST", f"https://api.github.com/repos/{OWNER}/{REPO_NAME}/actions/workflows/split_schedule.yml/dispatches", {"ref": "main"})
print("Workflow run triggered! Check: https://github.com/amineelmoufid/drive-file-splitter/actions")
