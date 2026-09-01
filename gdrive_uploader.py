#!/usr/bin/env python3
"""
gdrive_uploader.py
Headless Google Drive uploader for cron / systemd backups.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import subprocess
import sys
import time

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from gdrive_logic import (
    BAD_REQUEST,
    EXPIRED_SESSION,
    INVALID_PARENT,
    MAX_RETRIES,
    RATE_LIMIT,
    classify_resumable_http_error,
    retry_delay_seconds,
)

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
TOKEN_FILE = os.path.expanduser("~/.config/gdrive_uploader/token.json")
CHUNK_SIZE = 10 * 1024 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("gdrive")


def notify_user(title: str, message: str) -> None:
    sys.stderr.write(f"\n*** {title} ***\n{message}\n\n")
    for cmd in (
        ["notify-send", "-u", "critical", title, message],
        ["wall", f"{title}: {message}"],
    ):
        try:
            subprocess.run(
                cmd,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue


def http_error_message(error: HttpError) -> str:
    details = getattr(error, "error_details", None)
    if details:
        try:
            return details[0].get("message", "") or str(error)
        except (IndexError, AttributeError, TypeError):
            pass
    return str(error)


def get_credentials() -> Credentials:
    if not os.path.exists(TOKEN_FILE):
        sys.stderr.write(
            f"Token file not found: {TOKEN_FILE}\nRun the auth script first.\n"
        )
        sys.exit(2)

    creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                with open(TOKEN_FILE, "w") as handle:
                    handle.write(creds.to_json())
            except RefreshError as exc:
                msg = (
                    "Google Drive token is invalid or revoked. "
                    "Please re-run the auth script."
                )
                sys.stderr.write(f"{msg}\nError: {exc}\n")
                notify_user("Drive Auth Failed", msg)
                sys.exit(2)
        else:
            sys.stderr.write("Token is invalid and cannot be refreshed.\n")
            sys.exit(2)
    return creds


def _new_resumable_request(service, body: dict, file_path: str):
    """Fresh MediaFileUpload + files().create so a restarted session starts at byte 0."""
    media = MediaFileUpload(
        file_path,
        mimetype="application/octet-stream",
        resumable=True,
        chunksize=CHUNK_SIZE,
    )
    return service.files().create(body=body, media_body=media, fields="id")


def upload_file(
    file_path: str,
    parent_folder_id: str | None = None,
    description: str | None = None,
    properties: dict | None = None,
    *,
    service=None,
    sleeper=time.sleep,
) -> str:
    if service is None:
        creds = get_credentials()
        service = build("drive", "v3", credentials=creds, cache_discovery=False)

    size = os.path.getsize(file_path)
    name = os.path.basename(file_path)
    log.info("Uploading %s (%.2f MiB)", name, size / (1024 * 1024))

    if size > MAX_FILE_BYTES:
        raise ValueError(f"File is larger than the 2 GB limit ({size} bytes).")

    if not os.access(file_path, os.R_OK):
        raise PermissionError(f"Cannot read file: {file_path}. Check permissions.")

    body: dict = {"name": name}
    if description:
        body["description"] = description
    if properties:
        body["appProperties"] = properties
    if parent_folder_id:
        body["parents"] = [parent_folder_id]

    request = _new_resumable_request(service, body, file_path)
    last_error = None
    response = None
    chunks_accepted = 0

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            while response is None:
                status, response = request.next_chunk()
                chunks_accepted += 1
                if status:
                    log.info("Progress: %5.1f%%", status.progress() * 100)

            log.info("Upload complete. File ID: %s", response["id"])
            return response["id"]

        except HttpError as exc:
            last_error = exc
            status_code = exc.resp.status
            kind = classify_resumable_http_error(status_code, chunks_accepted)

            if kind == RATE_LIMIT:
                log.warning(
                    "Attempt %d/%d: API rate limit (%d). Exponential backoff × 3.",
                    attempt,
                    MAX_RETRIES,
                    status_code,
                )
                if attempt < MAX_RETRIES:
                    sleeper(retry_delay_seconds(attempt, rate_limited=True))
                    continue

            elif kind == INVALID_PARENT:
                reason = http_error_message(exc)
                raise RuntimeError(
                    "Failed to start upload. Is the --folder-id valid? "
                    f"Reason: {reason}"
                ) from exc

            elif kind == EXPIRED_SESSION:
                log.warning(
                    "Upload session expired (HTTP %d) after %d accepted chunk(s). "
                    "Starting a new resumable session.",
                    status_code,
                    chunks_accepted,
                )
                request = _new_resumable_request(service, body, file_path)
                response = None
                chunks_accepted = 0
                if attempt < MAX_RETRIES:
                    sleeper(retry_delay_seconds(attempt))
                    continue

            elif kind == BAD_REQUEST:
                reason = http_error_message(exc)
                if "mediaUploadSize" in str(reason):
                    raise RuntimeError(
                        f"File '{name}' changed size during upload. "
                        "Failing to avoid corruption."
                    ) from exc
                raise RuntimeError(
                    "Google rejected the request (400). "
                    f"Check properties format. Reason: {reason}"
                ) from exc

            else:
                log.warning(
                    "Attempt %d/%d failed (HTTP %d): %s",
                    attempt,
                    MAX_RETRIES,
                    status_code,
                    exc,
                )
                if attempt < MAX_RETRIES:
                    sleeper(retry_delay_seconds(attempt))
                    continue

        except (OSError, ConnectionError, TimeoutError) as exc:
            last_error = exc
            if not os.path.exists(file_path):
                raise RuntimeError(
                    f"File '{name}' was deleted or moved during the upload process."
                ) from exc
            if isinstance(exc, PermissionError):
                raise RuntimeError(
                    f"Read permissions for '{name}' were revoked during upload."
                ) from exc
            log.warning(
                "Attempt %d/%d failed (Network/OS): %s",
                attempt,
                MAX_RETRIES,
                exc,
            )
            if attempt < MAX_RETRIES:
                sleeper(retry_delay_seconds(attempt))
                continue

        except Exception as exc:
            last_error = exc
            log.exception("Unexpected error on attempt %d/%d", attempt, MAX_RETRIES)
            if attempt < MAX_RETRIES:
                sleeper(retry_delay_seconds(attempt))
                continue

    raise RuntimeError(
        f"All {MAX_RETRIES} upload attempts failed. Last error: {last_error}"
    )


def parse_properties(items: list[str]) -> dict:
    props = {}
    valid_key_regex = re.compile(r"^[a-zA-Z0-9_-]+$")

    for raw in items or []:
        if "=" not in raw:
            sys.stderr.write(f"Invalid --property '{raw}'. Expected key=value.\n")
            sys.exit(1)
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not valid_key_regex.match(key):
            sys.stderr.write(
                f"Invalid property key '{key}'. "
                "Only letters, numbers, hyphens, and underscores are allowed.\n"
            )
            sys.exit(1)
        props[key] = value
    return props


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Upload a file to Google Drive (headless)."
    )
    parser.add_argument("file", help="Path to the local file.")
    parser.add_argument(
        "-f",
        "--folder-id",
        default=None,
        help="Drive folder ID to upload into (optional).",
    )
    parser.add_argument(
        "-d", "--description", default=None, help="File description (optional)."
    )
    parser.add_argument(
        "-p",
        "--property",
        action="append",
        default=[],
        help="Custom property as key=value (repeatable).",
    )
    args = parser.parse_args()

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
        sys.stdout.write(file_id + "\n")
        return 0
    except Exception as exc:
        notify_user(
            "Google Drive upload failed",
            f"File: {args.file}\nAttempts: {MAX_RETRIES}\nReason: {exc}",
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
