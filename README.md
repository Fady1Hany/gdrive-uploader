## Google Drive uploader for automated backups.

Two scripts, two responsibilities:

| Script | When to run | What it does |
|--------|-------------|--------------|
| `gdrive_auth.py` | Once, interactively | Opens a browser, logs you in, saves `token.json`. |
| `gdrive_uploader.py` | On schedule (cron, systemd, CI) | Reads `token.json` and uploads a file silently. |

The uploader is designed for unattended servers and laptops that run nightly backups. It must never stop to ask for a login instead it reads a saved OAuth token, refreshes it when needed, and exits with a non-zero code (and a desktop notification) only when something genuinely requires human attention.

---

## Features

- **Headless by design** — once `token.json` exists, no browser is ever opened again.
- **Resumable chunked uploads** — 10 MiB chunks, restartable after network drops.
- **2 GiB ceiling** — refuses files larger than 2 GiB so a runaway backup cannot consume all of your Drive quota.
- **Automatic token refresh** — silently renews expired tokens (7-day expiry) in the background.
- **Smart retry with backoff** — 5 attempts, exponential backoff (5 s, 10 s, 15 s, 20 s).
- **Triple backoff on rate limits** — HTTP 403 and HTTP 429 both trigger an aggressive 3× wait.
- **Bad-folder detection** — distinguishes a 404 on chunk 1 (invalid `--folder-id`) from a 404 mid-upload (expired session) and fails fast instead of looping uselessly.
- **File-deleted detection** — if another process deletes the source file mid-upload, the script aborts immediately rather than burning through 5 retries.
- **Permission-revocation detection** — if read permissions are pulled mid-upload, aborts instead of retrying.
- **File-size-change detection** — refuses to continue if the source file changes size during upload, preventing corruption.
- **Desktop notification on hard failure** — uses `notify-send` (Linux) and `wall` as fallbacks so you actually see when a backup broke.
- **Custom Drive properties** — tag uploads with `key=value` metadata for later search via the Drive API.
- **Clean exit codes** — `0` on success, `1` on upload failure, `2` on auth failure, so cron can route alerts correctly.

---

## Architecture

```
            ┌──────────────────────┐
            │  gdrive_auth.py      │  run once, interactively
            │  (opens browser)     │
            └──────────┬───────────┘
                       │ writes
                       ▼
            ┌──────────────────────┐
            │  ~/.config/          │
            │  gdrive_uploader/    │
            │   ├─ credentials.json│  from Google Cloud Console
            │   └─ token.json      │  refreshed in place
            └──────────┬───────────┘
                       │ reads
                       ▼
            ┌──────────────────────┐
            │  gdrive_uploader.py  │  run from cron / systemd
            │  (silent, headless)  │
            └──────────┬───────────┘
                       │ HTTPS resumable upload
                       ▼
                  Google Drive API v3
```

The two-script split is the core design decision. Authentication requires an interactive browser round-trip that is fundamentally incompatible with a cron job. By isolating that step in `gdrive_auth.py`, the uploader can stay non-interactive forever after the only thing that ever forces you to re-run the auth script is a manually revoked refresh token.

---

## Installation

```bash
git clone https://github.com/Fady1Hany/gdrive-uploader.git
cd gdrive-uploader
pip install -r requirements.txt
```

Python 3.10+ is required (the code uses `str | None` PEP 604 unions).

### Get `credentials.json` from Google

1. Open the [Google Cloud Console](https://console.cloud.google.com/).
2. Create a project (or pick an existing one).
3. **APIs & Services → Library →** enable **Google Drive API**.
4. **APIs & Services → OAuth consent screen** choose *External*, add yourself as a test user.
5. **APIs & Services → Credentials → Create credentials → OAuth client ID.**
   - Application type: *Desktop app*.
   - Download the JSON.
6. Place it at `~/.config/gdrive_uploader/credentials.json`:

```bash
mkdir -p ~/.config/gdrive_uploader
mv ~/Downloads/client_secret_*.json \
   ~/.config/gdrive_uploader/credentials.json
chmod 600 ~/.config/gdrive_uploader/credentials.json
```

---

## One-time authentication

```bash
python3 gdrive_auth.py
```

What happens next:

1. A browser window opens to `http://localhost:PORT/` (random port).
2. Log in with the Google account that owns the Drive.
3. Google shows a scary *“Google hasn’t verified this app”* screen click **Advanced → Go to app (unsafe)**. This is normal for personal-use OAuth clients.
4. Click **Allow** on the consent screen.
5. The browser tab closes itself and the terminal prints:
   ```
   Success! Token saved to /home/you/.config/gdrive_uploader/token.json
   You can now run gdrive_uploader.py without needing a browser.
   ```

From this point on, `token.json` is the only credential the uploader needs.

---

## Usage

### Basic upload

```bash
python3 gdrive_uploader.py /var/backups/db_2024_08_18.sql.gz
# prints the new Drive file ID to stdout
1a2B3c4D5e6F7g8H9i0J...
```

### Upload into a specific folder

```bash
python3 gdrive_uploader.py \
    ~/backups/photos.tar.zst \
    --folder-id 0AOu...k9P \
    --description "Nightly photo archive"
```

Find a folder ID in the Drive web UI: open the folder, copy the last segment of the URL.

### Tag uploads with custom properties

```bash
python3 gdrive_uploader.py snapshot.img \
    --property host=prod-db-01 \
    --property kind=pg_basebackup \
    --property retention_days=30
```

These become searchable via `appProperties` in the Drive API:

```
'appProperties' has { 'host' = 'prod-db-01' }
```

### Cron example

```cron
# Nightly at 02:30 upload the latest DB dump
30 2 * * *  /home/you/bin/gdrive_uploader.py \
    /var/backups/db-$(date +\%F).sql.gz \
    --folder-id 0AOu...k9P \
    >> /var/log/gdrive-uploader.log 2>&1
```

---

## Edge cases handled

The uploader was hardened against five real-world failure modes that the naive “call `service.files().create()` and hope” approach gets wrong.

### 1. Bad parent folder (logic bug → fail fast)

If you pass a wrong / deleted / forbidden `--folder-id`, the Drive API returns **404** on the *first* chunk. A naive retry loop sees 404, thinks *“session expired”*, creates a new session, gets 404 again, and burns all 5 retries before telling you something useful. Our implementation distinguishes a 404 on chunk 1 (invalid folder) from a 404 on chunk *N* (expired session) and aborts immediately with a clear message.

### 2. File deleted mid-upload

If a cleanup script removes the source file while the uploader is in the middle of streaming it, `MediaFileUpload` raises `FileNotFoundError` (an `OSError`). Without protection, the retry loop kicks in and wastes 5 attempts. The handler checks `os.path.exists(file_path)` immediately on any `OSError` and aborts with a precise diagnostic instead.

### 3. Rate limits (HTTP 429)

Modern Google API quotas return **429 Too Many Requests** rather than **403** for rate-limit throttling. The retry logic catches both codes and applies a triple backoff (`RETRY_BACKOFF × attempt × 3`) to let the quota window recover.

### 4. Source file changes size mid-upload

If the source file is being actively written to (e.g. a log file being rotated), the Drive API returns **400** with a `mediaUploadSize` mismatch. The uploader detects this and refuses to continue rather than producing a corrupt remote file.

### 5. Token revoked

If a user manually revokes access via the Google account security page, `creds.refresh()` raises `RefreshError`. The uploader surfaces a desktop notification (`Drive Auth Failed`) and exits with code `2` so cron can route to the right alert channel.

---

## Exit codes

| Code | Meaning | Action required |
|------|---------|-----------------|
| `0`  | Upload succeeded | none |
| `1`  | Upload failed after 5 retries | check logs; possibly rerun |
| `2`  | Auth failed (token missing / revoked) | rerun `gdrive_auth.py` |

---

## License

MIT see `LICENSE`.
