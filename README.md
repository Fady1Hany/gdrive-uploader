## Google Drive uploader for automated backups.

Two scripts, two responsibilities:

| Script | When to run | What it does |
|--------|-------------|--------------|
| `gdrive_auth.py` | Once, interactively | Opens a browser, logs you in, saves `token.json`. |
| `gdrive_uploader.py` | On schedule (cron, systemd, CI) | Reads `token.json` and uploads a file silently. |

The uploader is designed for unattended servers and laptops that run nightly backups. It must never stop to ask for a login — it reads a saved OAuth token, refreshes it when needed, and exits with a non-zero code (and a desktop notification) only when something genuinely requires human attention.

---

## Features

- **Headless by design** — once `token.json` exists, no browser is ever opened again.
- **Resumable chunked uploads** — 10 MiB chunks. If the resumable session dies mid-file, a **new** session is started from byte 0 (the `MediaFileUpload` is recreated, not reused).
- **2 GiB ceiling** — refuses files larger than 2 GiB so a runaway backup cannot consume all of your Drive quota.
- **Automatic token refresh** — access tokens last about an hour; the uploader refreshes them in place from `token.json`. If the OAuth client is still in Google Cloud **Testing** mode, Google expires the *refresh* token after 7 days and you must re-run `gdrive_auth.py`.
- **Exponential backoff** — 5 attempts, delay `5 × 2^(attempt-1)` seconds: **5, 10, 20, 40, 80**, capped at 120 s. This is exponential, not linear.
- **Triple backoff on rate limits** — HTTP 403 and HTTP 429 wait 3× that delay: **15, 30, 60, 120, 120**.
- **Bad-folder vs expired-session** — a 404 is treated as an invalid `--folder-id` **only if zero chunks were accepted in the current session**. A 404/410 after any successful chunk is an expired resumable URI and triggers a new session. The code does **not** use `response is None` for this (Drive leaves `response` as `None` until the last chunk, so that heuristic was wrong).
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

The two-script split is the core design decision. Authentication requires an interactive browser round-trip that is fundamentally incompatible with a cron job. By isolating that step in `gdrive_auth.py`, the uploader can stay non-interactive forever after. The only thing that ever forces you to re-run the auth script is a revoked (or Testing-mode-expired) refresh token.

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
3. Google shows a scary *“Google hasn’t verified this app”* screen — click **Advanced → Go to app (unsafe)**. This is normal for personal-use OAuth clients.
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

### 1. Bad parent folder (fail fast)

If you pass a wrong / deleted / forbidden `--folder-id`, the Drive API returns **404** before any chunk is accepted. The uploader aborts immediately with a clear message. It does **not** spend 5 retries on this.

The discriminator is `chunks_accepted == 0` in the current resumable session, **not** `response is None`. Drive only fills `response` when the last chunk completes, so a 404 on chunk 2 of a 3-chunk file still has `response is None` — using that flag would mis-blame the folder id.

### 2. Expired upload session (retry with a new session)

If chunks have already been accepted and Drive then returns **404** or **410**, the resumable URI died (timeout, load balancer, etc.). The uploader:

1. Throws away the old `MediaFileUpload` (its stream position is not trustworthy).
2. Opens a new resumable session from byte 0.
3. Waits with exponential backoff and tries again.

### 3. File deleted mid-upload

If a cleanup script removes the source file while the uploader is streaming it, `MediaFileUpload` raises `FileNotFoundError`. The handler checks `os.path.exists` immediately and aborts with a precise diagnostic instead of retrying.

### 4. Rate limits (HTTP 429 / 403)

Both codes use triple exponential backoff (15 s, 30 s, 60 s, 120 s) so the quota window can recover.

### 5. Source file changes size mid-upload

If the source is being written to, Drive returns **400** with a `mediaUploadSize` mismatch. The uploader refuses to continue rather than producing a corrupt remote file.

### 6. Token revoked

If access is revoked, `creds.refresh()` raises `RefreshError`. The uploader surfaces a desktop notification and exits with code `2`.

---

## Tests

```bash
python3 test_gdrive_logic.py      # no Google libraries needed
pip install -r requirements.txt
python3 test_upload_loop.py       # mocked Drive client, no network
```

`test_gdrive_logic.py` pins the two contracts that previously drifted from the README: exponential delays, and 404 classification by accepted chunks.

---

## Exit codes

| Code | Meaning | Action required |
|------|---------|-----------------|
| `0`  | Upload succeeded | none |
| `1`  | Upload failed after 5 retries | check logs; possibly rerun |
| `2`  | Auth failed (token missing / revoked) | rerun `gdrive_auth.py` |

---

## License

MIT — see `LICENSE`.
