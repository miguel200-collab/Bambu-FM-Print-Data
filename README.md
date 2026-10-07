# Bambu Farm Manager — Print Failure Data Engine

A Python daemon for a shared makerspace (Cornell Tech MakerLAB). It runs
headless (no window) on a dedicated Windows laptop and does two things:

1. **Print logging + labeling** — monitors a fleet of Bambu Lab printers over
   MQTT/TLS and logs every print job to a local SQLite database. When a print
   ends, the student who ran it tells us how it turned out on the website's
   **"Label my print"** page. Over time this builds a labeled dataset for a
   print-failure prediction study.
2. **Student submission** — students visit a public Vercel website, upload a
   sliced `.gcode.3mf` file, and the laptop pulls it down, archives it into an
   organized inbox folder and extracts the slicer settings from it. The student
   then walks into the lab, opens the official **Bambu Farm Manager (BFM)** app,
   and clicks Upload to send the file to a printer. The "send to printer" step
   stays a deliberate, in-lab human action — students never print from home.

This daemon is **not** the official Bambu product called "Bambu Farm Manager"
(BFM). Both run on the same laptop; they are separate pieces of software.

---

## Why this exists

In a shared makerspace where several Bambu Lab printers run unattended for long
stretches, a few problems show up fast:

- Prints fail silently while no one is watching, and nobody notices until a
  cold, tangled blob of filament is found hours later.
- There's no structured record of *which* prints failed, *on which printer*,
  with *which material* and settings — so it's impossible to spot patterns.
- Without labeled success/failure data, there's no way to ever predict failures
  before they happen.

This tool fixes that by passively watching every printer on the LAN, keeping the
sliced file each student submitted, and asking the student — the person who sees
the physical print — how it turned out. The result is a growing dataset
(printer, material, temperatures, **slicer settings**, and a human label).

---

## How it works

1. `main.py` reads `config.json` and starts one `PrinterMonitor` per printer in a
   background thread. Each connects over MQTT/TLS (port 8883) and watches for
   state transitions.
2. When a print starts (`RUNNING`), a row is inserted into `print_dataset.db`.
   When it ends (`FINISH` or `FAILED`, including the printer's own `print_error`
   code) the row is completed. A paused print that is resumed stays **one** job.
3. The student opens the website's **Label my print** page, enters their NetID,
   sees their recent prints and taps **Print succeeded** / **Print failed**. The
   label (`0` = succeeded, `1` = failed) is written to the row with
   `label_source = 'web'`.
4. The job is tied to the student's upload by file name (`<netid>_<file>`) plus a
   time window, so every labeled print can be joined to the slicer settings of
   the file that produced it.

The daemon has **no popup**. Earlier versions asked "How did this print turn
out?" in a Tkinter window; those labels were routinely mass-clicked, which is why
they are now quarantined as **era 1** (see below).

---

## Architecture

### Print logging

```
printer (LAN) ──MQTT/TLS──▶ mqtt_listener.py ──▶ print_dataset.db (print_jobs)
                                             └─▶ headless event loop (main.py)
                                                     └─▶ LabelNotifier (email reminder: FUTURE, disabled)
```

### Student submission

```
student (home) → Vercel (Next.js) site → /api/upload (handleUpload: issue client token; .gcode.3mf only)
                     ↓ browser uploads the .gcode.3mf directly to Vercel Blob (incoming/), bypassing the 4.5 MB function limit
Vercel Blob → upload-completed callback → /api/upload (onUploadCompleted)
                     ↓ POSTs {pathname,url,downloadUrl} to station /api/blob-webhook (best-effort)
station laptop → file_watcher downloads the blob (Bearer auth), renames to <name>_<file>.gcode.3mf,
                  ARCHIVES it to the inbox, deletes the blob, then extracts features + design family
student (in lab) → BFM Client → Upload → select file from inbox → Files tab → Create / Direct to Print → printer (LAN)
```

### Labeling

```
student → /label page → /api/my-prints (Vercel proxy, adds x-station-key) → station GET /api/my-prints?netid=
                     → /api/label     (Vercel proxy, adds x-station-key) → station POST /api/label-webhook → print_jobs.user_label
```

Key design points:

- **Webhook-driven, not polling.** Vercel Blob counts every `list` as an
  advanced operation (2,000/month max on the free Hobby plan); a 10s poll loop
  would exhaust the quota in hours. The laptop processes from a queue fed by
  the webhook endpoint, does ONE catch-up `list` on startup for anything
  uploaded while offline, and has an optional rare fallback poll
  (`web.poll_interval_s`, default `0` = off). Do not reintroduce continuous
  polling.
- **Client-side uploads.** The browser uploads directly to Vercel Blob with a
  short-lived client token, so the file never passes through the serverless
  function and Vercel's 4.5 MB request body limit doesn't apply. Free on Hobby.
- **Private Blob store.** `bambu-submissions` is a Private store; the laptop
  sends `Authorization: Bearer <BLOB_READ_WRITE_TOKEN>` on every blob GET.
- **Archive first, then delete.** The file is archived to the inbox *before* the
  blob is deleted, and a failed delete never fails an archived submission.
- **Only a sliced `.gcode.3mf` is accepted** (see "Student file requirement").
- **Secrets stay server-side.** The shared `x-station-key` is attached by the
  Vercel *server* routes; a browser never sees it. The label/my-prints endpoints
  fail closed (503) if no secret is configured.
- **Status + camera** on the Vercel site are read from a small FastAPI server on
  the laptop (`station/api_server.py`), exposed via **Tailscale Funnel** (free,
  no domain — see `station/TUNNEL.md`); Cloudflare Tunnel is the alternative.
- **The "into BFM" step is manual.** Fully automating that last click is an
  optional future upgrade (see below).

---

## Data eras (read before analysing anything)

Every `print_jobs` and `uploads` row carries an `era`:

| Era | What it is | Use |
|-----|------------|-----|
| **1** | Everything collected **before** the "Label my print" page went live. Its popup labels were mass-clicked (e.g. 7 labels in 20 seconds). | **Quarantined.** Only for the label-reliability audit and pipeline tests. Never train or evaluate on it. |
| **2** | Everything after launch: human labels from the web page plus the printer's own verdict. | The study data. |

Existing rows are backfilled to era 1 the first time the new code starts. New
rows get the era in `config.json` → `labels.era` (default `1`, so nothing is ever
counted as clean by accident). **Set it to `2` only at the moment the label page
is deployed** and record that date.

Because students re-upload near-copies of the same design (a tweaked setting, a
fixed model, a new name), uploads are grouped:

- `file_sha256` — the exact same file (features stored once).
- `gcode_md5` — the same sliced toolpath under a different name (read from the
  archive's `.gcode.md5`).
- `design_family_id` — "the same thing, tweaked": same student, same object
  name or footprint/height within 5%, within 14 days of the family's latest
  upload. Each upload gets an `attempt_index` and a `config_diff` (the settings
  that changed since the previous attempt).

When modelling, cross-validate **grouped by `design_family_id` and student**, or
near-copies of one design will leak between train and test.

---

## File structure

```
Bambu-FM-Print-Data/
├── main.py               # Entry point: loads config, starts monitors, runs the headless event loop,
│                         #   and (when config has a "web" section) the station API server + file watcher
├── mqtt_listener.py      # PrinterMonitor — MQTT connection + state machine + snapshot()
├── database.py           # SQLite: print_jobs, uploads, file_features, design_families (additive migrations only)
├── config.json.template  # Safe template (no real credentials) — copy and fill in
├── requirements.txt      # runtime deps: paho-mqtt, fastapi, uvicorn, httpx
├── requirements-dev.txt  # + pytest, pyflakes
├── pytest.ini
├── setup.bat / launcher.bat / install_autostart.bat   # Windows helpers (optional)
├── run_backup.bat / install_backup_task.bat           # nightly dataset backup (see "Backups")
├── analysis/             # Offline tools (read the DB; run anywhere)
│   ├── audit.py            # read-only dataset + label-reliability audit
│   ├── backup_db.py        # verified SQLite snapshot + additive archive mirror
│   └── backfill_features.py # extract features for files archived before the pipeline existed
├── station/              # Runs on the dedicated Windows laptop
│   ├── api_server.py     # FastAPI: /api/printers, /api/printer/<serial>/camera, /api/health,
│   │                     #   /api/blob-webhook, /api/my-prints, /api/label-webhook
│   ├── file_watcher.py   # Webhook-driven Blob→inbox bridge: download, archive, delete blob, features
│   ├── gcode_features.py # extract_features(): slicer settings + print statistics from a .gcode.3mf
│   ├── families.py       # near-duplicate upload grouping (pure)
│   ├── matching.py       # job ↔ upload filename matching rules (pure; shared by DB, API, audit)
│   ├── label_notifier.py # email reminder — FUTURE FEATURE, disabled
│   ├── printer_uploader.py # Bambu HTTP chunked-upload client (best-effort convenience)
│   ├── camera.py         # JPEG snapshot from a printer over the LAN
│   └── TUNNEL.md         # Tunnel setup (Tailscale Funnel / Cloudflare Tunnel)
├── web/                  # The Vercel Next.js site
│   ├── app/              # pages (/, /label) + API routes (upload, printers, my-prints, label, notify-print-end)
│   ├── components/       # PrinterGrid, UploadForm, LabelPrints, LabelByLink, ...
│   ├── lib/              # blob.ts (validation), labeling.ts, labelLinks.ts, email.ts, station.ts, stationServer.ts
│   └── README.md         # Web app setup
├── tests/                # pytest suite (Python)
└── README.md
```

> `config.json`, `print_dataset.db`, `makerlab.log`, `backup.log` and `SECRETS.md`
> are local-only and **not** checked in (see `.gitignore`).

### Notes on `printer_uploader.py`

This module uploads to a **printer's local HTTP API**, bypassing BFM. It is not
the primary path — the human performs the upload via BFM's Upload button.
`file_watcher.py` still calls it as a **best-effort, non-fatal** convenience: if
the push fails (or no printer is available) the daemon logs a warning but still
archives the file and deletes the blob.

---

## Setup

### Part 1 — Run the daemon

1. **Install Python 3.11+** (`winget install Python.Python.3.11` on Windows; tick
   "Add Python to PATH" if using python.org).
2. **Get the code and dependencies**
   ```bash
   git clone https://github.com/miguel200-collab/Bambu-FM-Print-Data.git
   cd Bambu-FM-Print-Data
   python -m pip install -r requirements.txt
   ```
   (Use `python -m pip`, not `pip`, so packages land in the interpreter that runs
   `main.py`. On Windows `setup.bat` does this.)
3. **Create `config.json`** from the template and fill in each printer:
   ```bash
   cp config.json.template config.json
   ```
   The **serial number** and **LAN access code** are in the printer's on-device
   Network settings. The printer and this machine must be on the same network
   with LAN access enabled. `config.json` holds credentials — never commit it.
4. **Run it**
   ```bash
   python main.py        # pythonw main.py = no console window (Windows)
   ```
   You should see each printer connect, then `Daemon running (headless)`. Stop it
   with Ctrl+C (clean shutdown). Check `makerlab.log` for details.

#### Try it without any printers

```bash
python main.py --mock
```

Injects a fake `FINISH` event after 3 seconds so the database → event loop flow
can be exercised. Mock mode writes to **`print_dataset_mock.db`**, never to the
real dataset, and starts no HTTP server.

### Part 2 — Student submission (web + laptop)

#### A. Web side (Vercel) — once per deployment

1. **Create a Vercel Blob store** named `bambu-submissions` with **Private**
   access (cannot be changed later). Note the store ID and `BLOB_READ_WRITE_TOKEN`.
2. **Generate a shared webhook secret:** `openssl rand -hex 32` — the same value
   goes in Vercel and in the laptop's `config.json`.
3. **Deploy `web/`:** `cd web && npx vercel --prod`.
4. **Set Vercel env vars** (Production + Preview): `BLOB_READ_WRITE_TOKEN`,
   `BLOB_STORE_ID`, `BLOB_WEBHOOK_PUBLIC_KEY`, `STATION_WEBHOOK_SECRET`,
   `STATION_API_URL` (the tunnel URL; can be set after step B-5).
5. **Redeploy** (build cache off) so the env vars take effect.

The email-reminder variables in `web/.env.example` stay **unset** — that feature
is disabled (see "Future: email reminders").

#### B. Laptop side (the dedicated Windows machine in the lab)

1. Install Python 3.11+ and Git (`winget install Python.Python.3.11 Git.Git`).
2. Clone to `C:\Bambu-FM-Print-Data` and `python -m pip install -r requirements.txt`.
3. Create `config.json` (printers array + the sections below):
   ```json
   {
     "printers": [ { "name", "ip", "serial", "access_code" } ],
     "web": {
       "blob_token":       "<BLOB_READ_WRITE_TOKEN from Vercel>",
       "station_api_url":  "https://<laptop>.ts.net",
       "webhook_secret":   "<same as Vercel STATION_WEBHOOK_SECRET>",
       "api_host":         "127.0.0.1",
       "api_port":         8080,
       "poll_interval_s":   0,
       "inbox_dir":        "C:\\BambuSubmissions"
     },
     "labels": { "era": 1, "email_enabled": false, "notify_url": "", "email_domain": "cornell.edu" },
     "backup": { "dest": "C:\\Users\\<you>\\OneDrive\\BambuBackups", "keep_days": 30 }
   }
   ```
   `api_host` `127.0.0.1` means only the tunnel reaches the API. Keep
   `poll_interval_s` at `0`. Leave `labels.email_enabled` `false`.
4. `mkdir C:\BambuSubmissions` (the daemon also creates it).
5. **Tunnel — Tailscale Funnel (free, no domain).** Install Tailscale, `tailscale
   up`, enable Funnel (Access controls → JSON editor → "Add Funnel to policy"),
   `tailscale funnel --bg 8080`, then put the printed URL in `web.station_api_url`
   **and** the Vercel `STATION_API_URL`, and redeploy. (`station/TUNNEL.md` covers
   Cloudflare Tunnel as the alternative.)
6. **Start the daemon:** `python main.py`. Expect printers connecting, `Station API
   listening on http://127.0.0.1:8080` and `FileWatcher started (webhook-driven …)`.
7. **End-to-end test:** upload a real sliced `.gcode.3mf` from the site. Within
   seconds the laptop logs `Archived <renamed> … to C:\BambuSubmissions\<netid>\`
   and the blob disappears from `incoming/`. Open BFM → Upload → pick the file.

### Student file requirement (important)

Students **must upload a sliced `.gcode.3mf`** — the bundled 3MF that Bambu Studio
exports (a ZIP package holding the G-code + plate metadata). A plain `.3mf` model
or a plain `.gcode` does not print: renaming it doesn't change its bytes, and BFM
rejects it with a generic "file error". The upload form and server now accept
**only** `.gcode.3mf`, and the browser also checks the ZIP signature. In Bambu
Studio: **File → Export → Export plate sliced file**.

The laptop double-checks: a download with no ZIP header, or a package containing
no G-code, is still archived but logged as a warning and recorded
(`file_features.is_sliced = 0`).

---

## Going live with the label page (era 2)

Do these in order, on the day you deploy:

1. **Back up the laptop's data** (`run_backup.bat`) and confirm `backup.log` says OK.
2. `git pull` on the laptop (don't touch `config.json` / `print_dataset.db`).
3. Edit `config.json` → `"labels": { "era": 2, ... }`. **This date is the start of
   era 2 — write it down** (AGENTS.md / your lab notebook).
4. Restart the daemon. The first start adds the new columns and backfills every
   existing row to era 1.
5. `npx vercel --prod` from `web/`.
6. Optional: `python analysis/backfill_features.py` to extract features for files
   already in the inbox (idempotent; adopted files are era 1).

---

## Backups, audit and backfill

The dataset lives only on the laptop, so back it up.

- **Backup** — `python analysis/backup_db.py --dest <synced folder>` takes a
  consistent SQLite snapshot (safe while the daemon runs), verifies it
  (`integrity_check` + row counts) and additively mirrors the submission archive;
  old snapshots are pruned but the newest 7 are always kept. Set `backup.dest` in
  `config.json` (use a OneDrive / Google Drive folder), then run
  `install_backup_task.bat` once as administrator to schedule it nightly at 03:00.
  **Restore:** stop the daemon, copy the newest `db/<stamp>/print_dataset.db` over
  the live one (delete any `-wal`/`-shm` next to it), start the daemon, run the audit.
- **Audit** — `python analysis/audit.py [path/to/db] [--immutable] [--json]` opens
  the database **read-only** and reports volume, human-vs-machine label agreement
  (percent + Cohen's kappa), mass-click bursts, and the upload↔job match rate.
- **Backfill** — `python analysis/backfill_features.py` (see above).

---

## Optional: auto-start on Windows

1. Double-click **`install_autostart.bat`** once (registers the Task Scheduler job
   "MakerLAB Daemon" that runs `launcher.bat` at login).
2. Start now: `schtasks /run /tn "MakerLAB Daemon"`; stop: `schtasks /end /tn
   "MakerLAB Daemon"`; remove: `schtasks /delete /tn "MakerLAB Daemon" /f`.

For a dedicated always-on laptop, [NSSM](https://nssm.cc/) is the most robust
choice: it wraps `python main.py` as a real Windows service (no window, survives
reboot, auto-restarts on crash).

---

## Updating after a pull

Restart the daemon — nothing else to do. Never delete `config.json`,
`print_dataset.db` or `makerlab.log`; they are gitignored, so `git pull` never
touches them. If `git pull` complains about local changes: `git stash`, `git pull`,
`git stash drop`.

---

## Testing

```bash
python -m pip install -r requirements-dev.txt
python -m pytest              # Python: database, MQTT state machine, file watcher, API, features,
                              #   families, audit, backup, notifier, end-to-end, `main.py --mock` smoke test
cd web && npm install && npm test   # web: validators, signed links, API routes (Vercel SDK mocked)
cd web && npm run build       # type-checks everything, including the tests
```

All tests use throw-away databases (`database.DB_PATH` is redirected per test), so
they can never touch `print_dataset.db`. `tests/test_static.py` runs pyflakes over
the project to catch undefined-name bugs before they reach a background thread.

---

## Exporting the database

`print_dataset.db` is standard SQLite3. For analysis, join job outcome → upload →
features, and keep era 2 only:

```python
import sqlite3, pandas as pd
conn = sqlite3.connect("file:print_dataset.db?mode=ro", uri=True)   # read-only
df = pd.read_sql("""
    SELECT j.job_id, j.printer_serial, j.final_state, j.print_error, j.user_label, j.label_source,
           u.student_name, u.design_family_id, u.attempt_index, f.*
    FROM print_jobs j
    JOIN uploads u ON u.upload_id = (
        SELECT upload_id FROM uploads
        WHERE upload_status = 'done' AND replace(lower(renamed_filename), '.gcode.3mf', '') = lower(j.subtask_name)
        ORDER BY received_at DESC LIMIT 1)
    JOIN file_features f ON f.file_sha256 = u.file_sha256
    WHERE j.era = 2
""", conn)
```

(The exact matching rules, with time windows, are in `station/matching.py`; this
query is a quick approximation.)

---

## Database schema

### `print_jobs`

| Column           | Type    | Description |
|------------------|---------|-------------|
| `job_id`         | TEXT PK | UUID generated when the print starts |
| `printer_serial` / `printer_ip` | TEXT | Which printer |
| `subtask_name`   | TEXT    | File name reported by the printer |
| `gcode_file`     | TEXT    | Internal printer path |
| `start_time` / `end_time` | TEXT | ISO-8601 UTC |
| `final_state`    | TEXT    | `"FINISH"` or `"FAILED"` (NULL = never ended) |
| `print_error`    | INTEGER | The printer's own error code, if any |
| `nozzle_temper` / `bed_temper` | REAL | Temperatures at end of print |
| `filament_type`  | TEXT    | e.g. `PLA`, `PETG` |
| `user_label`     | INTEGER | `0` succeeded, `1` failed; NULL until labeled |
| `label_time`     | TEXT    | When the label was given |
| `label_source`   | TEXT    | `popup` (era 1), `web`, or `email` (future) |
| `era`            | INTEGER | `1` quarantined, `2` study data |
| `layer_height`, `infill_density`, `wall_loops` | | Reserved/unused — join `file_features` instead |

### `uploads`

| Column | Description |
|--------|-------------|
| `upload_id`, `blob_key` | UUID; Vercel Blob pathname `incoming/<netid>__<serial>__<file>` |
| `student_name` | Sanitized NetID (the inbox folder name) |
| `original_filename`, `renamed_filename` | As uploaded; `<netid>_<file>.gcode.3mf` |
| `target_printer`, `printer_serial` | Requested / pushed-to printer (metadata only) |
| `upload_status`, `error_message` | `pending` / `uploading` / `done` / `failed` |
| `received_at`, `uploaded_at` | ISO-8601 |
| `era` | As above |
| `file_sha256`, `gcode_md5` | Exact-file and toolpath identity |
| `design_family_id`, `attempt_index`, `config_diff` | Near-duplicate grouping; JSON of changed settings vs. the previous attempt |
| `blob_deleted` | `1` once the Vercel blob was deleted after archiving |

### `file_features` (one row per distinct file, keyed by `file_sha256`)

~50 promoted columns (time estimate, layers, filament grams, footprint, layer
height, infill, walls, speeds, temperatures, supports, G-code travel/extrusion
statistics, …) plus `raw_json` holding every slicer setting (~460 keys) —
see `station/gcode_features.py` (`CURATED_COLUMNS`).

### `design_families`

One row per (student, design): fingerprint of its latest upload, `upload_count`.

---

## Notes

- If a printer goes offline mid-print, the MQTT monitor reconnects with
  exponential backoff (up to 60 s).
- Printer-initiated jobs (e.g. `auto_hms_check_for_user.gcode`) are excluded from
  matching and from the audit.
- Typing a NetID on the label page is **not authentication** (anyone who knows a
  NetID could label that student's prints). Accepted for v1; each label records
  its source and time, and the future email flow replaces NetID entry with signed links.

---

## Future: email reminders (built but disabled)

The architecture for emailing a student when their print ends is in place but
**switched off**, pending a Cornell sender address:

`station/label_notifier.py` → website `/api/notify-print-end` → email with a
signed 14-day link → `/label?t=…` (a button press is still required, so mail
scanners can't label by prefetching) → `/api/label` → `label_source = 'email'`.

Turning it on needs **all** of: (1) a consent checkbox on the upload form stored on
the upload row, and `LabelNotifier._has_consent` implemented — until then **nothing
is ever sent**, even with the flag on; (2) a Cornell sender address + Resend API
key; (3) Vercel env `EMAIL_LABELING_ENABLED=true`, `LABEL_LINK_SECRET`,
`RESEND_API_KEY`, `EMAIL_FROM` (see `web/.env.example`); (4) `config.json`
`labels.email_enabled: true` and `labels.notify_url`.

### Slicer metadata and the file archive

Implemented: `station/gcode_features.py` extracts the slicer settings from every
submission (see above). Not implemented: pulling the file back off the printer when
a print ends, or archiving to an external drive.

### Automating the last BFM click

1. **Bambu cloud API** — if files uploaded through BFM appear under "My Files" on
   <https://bambulab.com>, the laptop could upload via the community cloud API.
2. **UI automation** (`pywinauto`) to drive BFM's Upload button + file dialog
   (brittle to BFM UI changes).

BFM's local server has no documented upload API; replicating Bambu's request
signing isn't worth it while the manual click is acceptable. (The old
`station/inspect_bfm.py` investigation tool was removed; it is in git history.)

---

## Quick links

- Official BFM docs: <https://wiki.bambulab.com/en/software/bambu-farm-manager>
- Tunnel setup: [`station/TUNNEL.md`](station/TUNNEL.md)
- Web app: [`web/README.md`](web/README.md)
