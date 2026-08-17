#!/usr/bin/env python3
"""
gdrive_uploader.py
Bulletproof version 2: Handles bad folders, deleted files, rate limits (429/403),
revoked tokens, and session expirations.
"""

import os
import sys
import time
import re
import argparse
import logging
import subprocess

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
SCOPES          = ['https://www.googleapis.com/auth/drive.file']
TOKEN_FILE      = os.path.expanduser('~/.config/gdrive_uploader/token.json')
MAX_RETRIES     = 5
RETRY_BACKOFF   = 5
CHUNK_SIZE      = 10 * 1024 * 1024
MAX_FILE_BYTES  = 2 * 1024 * 1024 * 1024

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    stream=sys.stderr,
)
log = logging.getLogger('gdrive')

# --------------------------------------------------------------------------- #
# Notification
# --------------------------------------------------------------------------- #
def notify_user(title: str, message: str) -> None:
    sys.stderr.write(f"\n*** {title} ***\n{message}\n\n")
    for cmd in (['notify-send', '-u', 'critical', title, message],
                ['wall', f"{title}: {message}"]):
        try:
            subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue

# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #
def get_credentials() -> Credentials:
    if not os.path.exists(TOKEN_FILE):
        sys.stderr.write(f"Token file not found: {TOKEN_FILE}\nRun the auth script first.\n")
        sys.exit(2)

    creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                with open(TOKEN_FILE, 'w') as f:
                    f.write(creds.to_json())
            except RefreshError as e:
                msg = "Google Drive token is invalid or revoked. Please re-run the auth script."
                sys.stderr.write(f"{msg}\nError: {e}\n")
                notify_user("Drive Auth Failed", msg)
                sys.exit(2)
        else:
            sys.stderr.write("Token is invalid and cannot be refreshed.\n")
            sys.exit(2)
    return creds

# --------------------------------------------------------------------------- #
# Upload with retry & Advanced Edge Cases
# --------------------------------------------------------------------------- #
def upload_file(file_path: str,
                parent_folder_id: str | None = None,
                description: str | None = None,
                properties: dict | None = None) -> str:

    creds   = get_credentials()
    service = build('drive', 'v3', credentials=creds, cache_discovery=False)

    size   = os.path.getsize(file_path)
    name   = os.path.basename(file_path)
    log.info("Uploading %s (%.2f MiB)", name, size / (1024 * 1024))

    if size > MAX_FILE_BYTES:
        raise ValueError(f"File is larger than the 2 GB limit ({size} bytes).")

    # EDGE CASE: Pre-flight check for read permissions
    if not os.access(file_path, os.R_OK):
        raise PermissionError(f"Cannot read file: {file_path}. Check permissions.")

    media = MediaFileUpload(
        file_path,
        mimetype='application/octet-stream',
        resumable=True,
        chunksize=CHUNK_SIZE,
    )

    body = {'name': name}
    if description: body['description'] = description
    if properties:  body['appProperties'] = properties
    if parent_folder_id: body['parents'] = [parent_folder_id]

    request = service.files().create(body=body, media_body=media, fields='id')

    last_error = None
    response = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            while response is None:
                status, response = request.next_chunk()
                if status:
                    log.info("Progress: %5.1f%%", status.progress() * 100)

            log.info("Upload complete. File ID: %s", response['id'])
            return response['id']

        except HttpError as e:
            last_error = e
            status_code = e.resp.status

            # EDGE CASE 1: Rate limits (403 and 429)
            if status_code in [403, 429]:
                log.warning("Attempt %d/%d: Hit API Rate Limit (%d). Sleeping longer...", attempt, MAX_RETRIES, status_code)
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF * attempt * 3) # Triple the backoff
                    continue

            # EDGE CASE 2: 404/410 Differentiation (Bad Folder vs Expired Session)
            elif status_code in [404, 410]:
                # If it's the FIRST chunk, the session hasn't started yet.
                # A 404 here means the Parent Folder ID is invalid!
                if response is None and status_code == 404:
                    err_reason = e.error_details[0].get('message', '') if e.error_details else str(e)
                    raise RuntimeError(f"Failed to start upload. Is the --folder-id valid? Reason: {err_reason}")

                # If we already sent chunks, the session just expired. Safe to recreate.
                log.warning("Upload session expired (%d). Restarting session...", status_code)
                request = service.files().create(body=body, media_body=media, fields='id')
                response = None
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF * attempt)

            # EDGE CASE 3: Bad Request (400) - Usually properties format or file changing size
            elif status_code == 400:
                err_reason = e.error_details[0].get('message', '') if e.error_details else str(e)
                if "mediaUploadSize" in str(err_reason):
                     raise RuntimeError(f"File '{name}' changed size during upload. Failing to avoid corruption.")
                raise RuntimeError(f"Google rejected the request (400). Check properties format. Reason: {err_reason}")

            else:
                log.warning("Attempt %d/%d failed (HTTP %d): %s", attempt, MAX_RETRIES, status_code, e)
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF * attempt)

        except (OSError, ConnectionError, TimeoutError) as e:
            last_error = e
            # EDGE CASE 4: File deleted or moved mid-upload
            if not os.path.exists(file_path):
                raise RuntimeError(f"File '{name}' was deleted or moved during the upload process.")

            # EDGE CASE 5: Permissions changed mid-upload
            if isinstance(e, PermissionError):
                raise RuntimeError(f"Read permissions for '{name}' were revoked during upload.")

            log.warning("Attempt %d/%d failed (Network/OS): %s", attempt, MAX_RETRIES, e)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)

        except Exception as e:
            last_error = e
            log.exception("Unexpected error on attempt %d/%d", attempt, MAX_RETRIES)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)

    raise RuntimeError(f"All {MAX_RETRIES} upload attempts failed. Last error: {last_error}")

# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_properties(items: list[str]) -> dict:
    props = {}
    valid_key_regex = re.compile(r'^[a-zA-Z0-9_-]+$')

    for raw in items or []:
        if '=' not in raw:
            sys.stderr.write(f"Invalid --property '{raw}'. Expected key=value.\n")
            sys.exit(1)
        k, v = raw.split('=', 1)
        k = k.strip()
        v = v.strip()

        if not valid_key_regex.match(k):
            sys.stderr.write(f"Invalid property key '{k}'. Only letters, numbers, hyphens, and underscores are allowed.\n")
            sys.exit(1)

        props[k] = v
    return props

def main() -> int:
    p = argparse.ArgumentParser(description="Upload a file to Google Drive (headless).")
    p.add_argument('file', help='Path to the local file.')
    p.add_argument('-f', '--folder-id', default=None, help='Drive folder ID to upload into (optional).')
    p.add_argument('-d', '--description', default=None, help='File description (optional).')
    p.add_argument('-p', '--property', action='append', default=[], help='Custom property as key=value (repeatable).')
    args = p.parse_args()

    if not os.path.isfile(args.file):
        sys.stderr.write(f"Error: file not found: {args.file}\n")
        return 1

    props = parse_properties(args.property)

    try:
        file_id = upload_file(
            file_path=args.file,
            parent_folder_id=args.folder_id,
            description=args.description,
            properties=props or None,
        )
        sys.stdout.write(file_id + '\n')
        return 0
    except Exception as e:
        notify_user(
            "Google Drive upload failed",
            (f"File: {args.file}\nAttempts: {MAX_RETRIES}\nReason: {e}"),
        )
        return 1

if __name__ == '__main__':
    sys.exit(main())
