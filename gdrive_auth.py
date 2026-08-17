#!/usr/bin/env python3
# gdrive_auth.py — run interactively ONCE to produce token.json

import os
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ['https://www.googleapis.com/auth/drive.file']
CFG = os.path.expanduser('~/.config/gdrive_uploader')
CRED = os.path.join(CFG, 'credentials.json')
TOK  = os.path.join(CFG, 'token.json')

# 1. Create the config folder if it doesn't exist
os.makedirs(CFG, exist_ok=True)

# 2. Load the credentials.json you downloaded from Google Cloud Console
flow = InstalledAppFlow.from_client_secrets_file(CRED, SCOPES)

# 3. THIS IS THE LINE THAT OPENS THE BROWSER!
# It starts a local server, opens your browser, asks you to login to Gmail,
# and waits for you to click "Allow".
creds = flow.run_local_server(port=0)

# 4. Save the result as token.json so the uploader script can use it later
with open(TOK, 'w') as f:
    f.write(creds.to_json())

print(f"Success! Token saved to {TOK}")
print("You can now run gdrive_uploader.py without needing a browser.")
