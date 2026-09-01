#!/usr/bin/env python3
"""gdrive_auth.py — run interactively ONCE to produce token.json."""

from __future__ import annotations

import os
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
CFG = os.path.expanduser("~/.config/gdrive_uploader")
CRED = os.path.join(CFG, "credentials.json")
TOK = os.path.join(CFG, "token.json")

os.makedirs(CFG, exist_ok=True)

if not os.path.isfile(CRED):
    sys.stderr.write(
        f"Missing {CRED}\n"
        "Download an OAuth Desktop-app client JSON from Google Cloud Console\n"
        "and place it at that path (chmod 600).\n"
    )
    sys.exit(2)

flow = InstalledAppFlow.from_client_secrets_file(CRED, SCOPES)
creds = flow.run_local_server(port=0)

with open(TOK, "w") as handle:
    handle.write(creds.to_json())
os.chmod(TOK, 0o600)

print(f"Success! Token saved to {TOK}")
print("You can now run gdrive_uploader.py without needing a browser.")
