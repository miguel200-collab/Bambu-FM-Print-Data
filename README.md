# Bambu Farm Manager — Print Failure Data Engine

> **NOTE FOR NEW AGENTS / CHATS:** Read the **"Project context (start here)"**
> section just below before doing anything else. It explains the current state
> of the project, the decided workflow, what is built, and the one small TODO
> that remains. The rest of this README describes the original
> monitoring/labeling tool and the student-submission web workflow in detail.

---

## Project context (start here)

This repo is a **custom Python daemon** that monitors a fleet of Bambu Lab
printers over MQTT and labels print outcomes for an ML dataset. It is **not**
the official Bambu product called "Bambu Farm Manager" (BFM). Both run on the
same dedicated Windows laptop; they are separate pieces of software.

### What we are building (the goal)

A low-friction way for students to submit 3D prints from home and pick them up
in the lab:

1. **Student (from home)** opens the public Vercel website, enters their name,
   and uploads a sliced `.gcode.3mf` file.
2. **The dedicated laptop** automatically pulls that file down from Vercel Blob
   within ~10 seconds, renames it to `<name>_<filename>.gcode.3mf`, and drops it
   into a clearly-named **inbox folder on the laptop**, organized by student name
   (e.g. `C:\BambuSubmissions\<student name>\`).
3. **Student (in the lab)** walks up to the dedicated laptop, opens **Bambu Farm
   Manager**, clicks **Upload**, browses to their folder in the inbox
   (`C:\BambuSubmissions\<student name>\`), and selects their file. BFM ingests
   it into its **Files** tab. Then the student clicks **Create → Direct to
   Print** and picks a matching idle printer.

The "send to printer" step stays a deliberate, in-lab human action — students
never print from home. The only manual step is clicking BFM's Upload button and
selecting the file from the inbox folder, which is exactly the human "push my
print" action the user wants to preserve.

### Architecture (decided)

```
student (home) → Vercel (Next.js) site → /api/upload (handleUpload: issue client token)
                       ↓ browser uploads the .gcode.3mf directly to Vercel Blob (incoming/), bypassing the 4.5 MB function limit
Vercel Blob → upload-completed callback → /api/upload (onUploadCompleted)
                       ↓ /api/upload POSTs {pathname,url,downloadUrl} to station /api/blob-webhook (best-effort)
station laptop → file_watcher downloads the blob via the signed downloadUrl, renames to <name>_<file>.gcode.3mf → inbox folder (C:\BambuSubmissions\<name>\)
student (in lab) → BFM Client → Upload → select file from inbox → Files tab → Create / Direct to Print → printer (LAN)
```

- **Status + camera** on the Vercel site are read from a small FastAPI server on
  the laptop (`station/api_server.py`), exposed to the internet via a Cloudflare
  Tunnel (see `station/TUNNEL.md`). The laptop fetches printer telemetry over
  MQTT (existing `mqtt_listener.py`) and camera JPEGs over the LAN.
- **File submission** is async and decoupled through Vercel Blob. The site uses
  **client-side uploads** (`handleUpload`/`upload()` from `@vercel/blob/client`):
  the browser asks `/api/upload` for a short-lived client token, then uploads the
  file **directly to Vercel Blob** — the file never passes through the serverless
  function, so Vercel's 4.5 MB request body limit does not apply and real
  10–50 MB `.gcode.3mf` slices upload fine. When the upload completes, Vercel Blob
  calls back into `/api/upload` (`onUploadCompleted`), which best-effort POSTs
  `{pathname, url, downloadUrl}` to the laptop's `/api/blob-webhook` (over the
  Cloudflare Tunnel) so the laptop downloads the blob immediately — **no
  polling**. This keeps the laptop within Vercel Blob's free Hobby operation
  limits (a 10s poll loop would exhaust the advanced-ops quota; the webhook-driven
  design does only one catch-up `list` on startup). If the tunnel is down when a
  callback fires, the blob simply stays in Blob and the laptop grabs it via that
  single catch-up `list` on next startup. No tunnel needed for the upload path
  itself.
- **Private Blob store.** `bambu-submissions` is a Private store, so blobs are not
  readable via their plain URL. The laptop downloads each blob via the signed
  `downloadUrl` (valid ≤ 7 days) and deletes it via the authenticated canonical
  `url`. The `file_watcher` deletes each blob after processing, which keeps Blob
  storage well under the 1 GB Hobby limit under normal operation.
- **The "into BFM" step is manual** (human clicks Upload + selects the file).
  This is the decided approach ("Pivot A") — it works today and needs no BFM
  reverse-engineering. Fully automating that last click is an *optional* future
  upgrade, not a blocker (see the bottom of this section).

### What is already built and working

- `mqtt_listener.py` — added `snapshot()` + `camera_url()` + thread lock.
- `database.py` — added `uploads` table + helpers.
- `station/api_server.py` — FastAPI: `/api/printers`, `/api/printer/<serial>/camera`, `/api/health`, and `POST /api/blob-webhook` (enqueue submissions for the file watcher, shared-secret auth).
- `station/camera.py` — fetches a JPEG snapshot from a printer over the LAN.
- `station/file_watcher.py` — **webhook-driven**: receives `{pathname,url,downloadUrl}`
  from the station's `/api/blob-webhook` endpoint, downloads the blob via the
  signed `downloadUrl` (the store is Private, so the plain `url` isn't fetchable),
  renames it to `<name>_<filename>.gcode.3mf`, records it in the `uploads` table,
  uploads it to a printer, deletes the blob, and archives the renamed file into
  the organized inbox (`C:\BambuSubmissions\<sanitized name>\<name>_<filename>.gcode.3mf`
  by default; configurable via `web.inbox_dir`). Does a single catch-up `list` on
  startup for anything uploaded while offline; an optional rare fallback poll
  (`web.poll_interval_s`, default `0` = off) can be enabled as a safety net.
- `main.py` — starts the API server + file watcher in daemon threads (optional,
  only when the `web` config section is present).
- `config.json.template`, `requirements.txt`, `.gitignore` — extended.
- `web/` — a Cornell Tech-themed Next.js app (status grid, camera, upload form,
  `/api/upload` route, Vercel Blob integration).
- `station/inspect_bfm.py` — read-only inspection tool used to investigate BFM
  internals (kept for reference; see "Optional future upgrade" below).

### The ONE remaining TODO (done)

Make `station/file_watcher.py` archive finished submissions into an organized,
easy-to-find inbox folder instead of the current flat `processed/` dir. Suggested
layout:

```
C:\BambuSubmissions\
└── <sanitized student name>\
    └── <name>_<filename>.gcode.3mf
```

Implementation notes for whoever picks this up:

- In `file_watcher.py`, the `__init__` currently sets `self._processed =
  base_dir / "processed"`. Change the archive root to a configurable inbox path
  (add `inbox_dir` to the `web` section of `config.json.template`, default
  `C:\BambuSubmissions`), and create a per-student subfolder
  (`inbox_dir / sanitize(name)`) before moving the renamed file there.
- Keep the `uploads` table recording exactly as-is (it already logs
  `renamed_filename`, `student_name`, etc.) — the inbox folder is just the
  human-facing view of the same data.
- Create the inbox folder once on the laptop (admin may be needed for
  `C:\BambuSubmissions`; alternatively put it under the user's Desktop to
  avoid permission issues).
- Update this README's "Student (in the lab)" step and `web/README.md` to name
  the actual inbox path once chosen.

**Status: implemented.** `FileWatcher.__init__` now takes an `inbox_dir`
(defaulting to `base_dir / "BambuSubmissions"`), `config.json.template`
declares `web.inbox_dir` as `C:\BambuSubmissions`, `main.py` passes it through,
and `_process_blob` archives to `inbox_dir / sanitize(name) / renamed`. The
README "Student (in the lab)" step and `web/README.md` already name the path.

### What is deprecated / unused

- `station/printer_uploader.py` — uploads to a **printer's local HTTP API**,
  bypassing BFM. **Not used in the decided workflow.** Keep the file for
  reference but do NOT call it from `file_watcher.py`. (The human performs the
  upload via BFM's Upload button instead.)
- The web upload form's "target printer" dropdown (`web/components/UploadForm.tsx`,
  `web/lib/blob.ts`) is no longer a routing key — the human picks the printer in
  BFM. It can stay as optional metadata or be removed; it does not affect the
  laptop side.

### Optional future upgrade (NOT required to ship)

Fully automating the last "click Upload in BFM" step. Two viable routes; pick
based on a 2-minute check the user still needs to do:

1. **Sign into <https://bambulab.com> with the Bambu account used to activate
   BFM** and look at "My Files." If the files uploaded through BFM appear there
   too, then uploads go to the Bambu cloud account, and the laptop script can
   upload via the community Bambu cloud API (no BFM internals needed). The file
   would then appear in BFM's Files tab automatically.
2. If the files are NOT in the cloud, use **UI automation** (`pywinauto`) to
   drive BFM's Upload button + file dialog. No API or request-signing needed,
   but brittle to BFM UI changes.

Background on why this is optional: BFM's local server has no documented
upload API, and its `debug.log` shows no upload requests (the Client↔Server
REST calls logged are only `/license`, `/captain`, `/devices2`, `/tags`,
`/task`, `/users/current_user` — all polling/management, none file upload). The
`app.asar` strings show an `uploadToServer` path with
`application/vnd.adobe.partial-upload` and Bambu `x-bbl-sec-*` signing headers,
which means a direct API replication would require reproducing Bambu's request
signing — that's the hard route and is not worth it while the manual Upload
click is acceptable. `station/inspect_bfm.py` was written to investigate this
and can be re-run if we ever pursue the automated route.

### Quick links

- Official BFM docs: <https://wiki.bambulab.com/en/software/bambu-farm-manager>
- Tunnel setup: [`station/TUNNEL.md`](station/TUNNEL.md)
- Inspection script (reference): [`station/inspect_bfm.py`](station/inspect_bfm.py)
- Web app: [`web/README.md`](web/README.md)

---

Monitors a fleet of Bambu Lab printers over MQTT, logs every print job to a
local SQLite database, and prompts you to label each completed job as
**succeeded** or **failed**. Over time this builds a labeled dataset that can be
used to train a print-failure prediction model.

It works with any number of printers (the template ships with 4) and on any
desktop OS that runs Python — Windows, macOS, or Linux. The only thing you need
is the printer's IP address, serial number, and LAN access code.

---

## Why this exists

This project was built for a **MakerLAB** — a shared makerspace where several
Bambu Lab printers run unattended for long stretches. In that environment a few
problems show up fast:

- Prints fail silently while no one is watching, and nobody notices until a
  cold, tangled blob of filament is found hours later.
- There's no structured record of *which* prints failed, *on which printer*,
  with *which material* and settings — so it's impossible to spot patterns.
- Without labeled success/failure data, there's no way to ever predict failures
  before they happen.

This tool fixes that by passively watching every printer on the LAN and turning
each completed print into a quick yes/no question for the person nearby. The
result is a clean, growing dataset (printer, material, temperatures, file, and a
human label) that's ready for analysis or ML down the road.

---

## How it works

1. `main.py` reads `config.json` and starts one `PrinterMonitor` per printer in a
   background thread.
2. Each monitor connects to its printer over MQTT/TLS (port 8883) and watches
   for state transitions.
3. When a print starts (`RUNNING`), a new row is inserted into `print_dataset.db`.
4. When a print ends (`FINISH` or `FAILED`), a popup appears asking
   **"Did this print fail?"**
5. You click YES or NO — the label is written to the database alongside the job
   metadata.

---

## File structure

```
Bambu-FM-Print-Data/
├── main.py               # Entry point: loads config, starts monitors, runs the Tkinter loop,
│                         #   and (optionally) starts the station API server + file watcher
├── mqtt_listener.py      # PrinterMonitor class — MQTT connection + state machine + snapshot()
├── database.py           # SQLite helpers: print_jobs + uploads tables
├── labeler_gui.py        # Tkinter LabelPopup window
├── config.json.template  # Safe template (no real credentials) — copy and fill in
├── requirements.txt      # paho-mqtt, fastapi, uvicorn, httpx
├── setup.bat             # Windows: pip install -r requirements.txt  (optional)
├── install_autostart.bat # Windows: registers a Task Scheduler job at login  (optional)
├── launcher.bat          # Windows: runs main.py with no console window  (optional)
├── station/              # Runs on the dedicated Windows laptop
│   ├── api_server.py     # FastAPI: /api/printers, /api/printer/<serial>/camera, /api/health
│   ├── printer_uploader.py # Bambu HTTP chunked-upload client (writes files to a printer)
│   ├── file_watcher.py   # Polls Vercel Blob for submissions, renames, uploads to printer
│   ├── camera.py         # Fetches a JPEG snapshot from a printer over the LAN
│   └── TUNNEL.md         # Cloudflare Tunnel setup so the Vercel site can reach the station
├── web/                  # The Vercel Next.js site (Cornell Tech themed)
│   ├── app/              # App-router pages + /api/upload route
│   ├── components/       # PrinterCard, StatusBadge, UploadForm
│   ├── lib/              # station.ts (status/camera fetch), blob.ts (Vercel Blob put)
│   └── README.md         # Web app setup
└── README.md
```

> `config.json`, `print_dataset.db`, and `makerlab.log` are created at runtime
> next to the scripts and are **not** checked into the repo (see `.gitignore`).

---

## Setup

### 1. Install Python 3.11+

Any modern Python 3 will work. On Windows:

```
winget install Python.Python.3.11
```

Or download from [python.org](https://www.python.org/downloads/) and check
**"Add Python to PATH"**.

### 2. Get the code

```bash
git clone https://github.com/miguel200-collab/Bambu-FM-Print-Data.git
cd Bambu-FM-Print-Data
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

(On Windows you can just double-click `setup.bat` instead.)

### 4. Create `config.json`

Copy the template and fill in each printer's details:

```bash
cp config.json.template config.json
```

```json
{
  "printers": [
    {
      "name": "Printer 1 — Bambu X1C",
      "ip": "192.168.1.101",
      "serial": "01S00C123456789",
      "access_code": "12345678"
    }
  ]
}
```

You'll find the **serial number** and **LAN access code** in the Bambu printer's
on-device settings (Network / LAN section). The printer and this machine must be
on the same network, with LAN access enabled on the printer.

> `config.json` contains your printers' credentials — keep it private and never
> commit it. It's already in `.gitignore`.

### 5. Run it

```bash
python main.py
```

A background process starts and watches for print events. When a print finishes,
a popup asks whether it failed. Check `makerlab.log` for connection status.

### Try it without any printers (offline test)

```bash
python main.py --mock
```

This skips all MQTT connections and injects a fake `FINISH` event after 3
seconds, so you can test the full database → popup → label flow without a
printer on the network.

---

## Optional: auto-start on Windows

If you want the daemon to launch automatically whenever someone logs into a
Windows machine in the lab:

1. Double-click **`install_autostart.bat`** once. This registers a Task
   Scheduler job ("MakerLAB Daemon") that runs `launcher.bat` at login.
2. To start it immediately without logging out:
   ```
   schtasks /run /tn "MakerLAB Daemon"
   ```
3. Useful commands:
   - Stop: `schtasks /end /tn "MakerLAB Daemon"`
   - Remove: `schtasks /delete /tn "MakerLAB Daemon" /f`

On macOS/Linux, use your preferred process manager (`launchd`, `systemd --user`,
a cron `@reboot` entry, or just a terminal session).

---

## Updating after a pull

After pulling new code, just restart the daemon — there's nothing else to do.
Don't delete `config.json`, `print_dataset.db`, or `makerlab.log`; those hold
your local fleet config and collected data.

---

## Exporting the database

`print_dataset.db` is a standard SQLite3 database. Open it with
[DB Browser for SQLite](https://sqlitebrowser.org/) or query it with Python:

```python
import sqlite3, pandas as pd
conn = sqlite3.connect("print_dataset.db")
df = pd.read_sql("SELECT * FROM print_jobs WHERE user_label IS NOT NULL", conn)
print(df)
```

---

## Database schema

| Column          | Type    | Description                                      |
|-----------------|---------|--------------------------------------------------|
| `job_id`        | TEXT PK | UUID generated when print starts                 |
| `printer_serial`| TEXT    | Printer serial number                            |
| `printer_ip`    | TEXT    | Printer IP address                               |
| `subtask_name`  | TEXT    | Filename reported by printer (`.gcode.3mf`)      |
| `gcode_file`    | TEXT    | Internal printer path                            |
| `start_time`    | TEXT    | ISO-8601 timestamp when state → RUNNING          |
| `end_time`      | TEXT    | ISO-8601 timestamp when state → FINISH/FAILED    |
| `final_state`   | TEXT    | `"FINISH"` or `"FAILED"`                         |
| `nozzle_temper` | REAL    | Nozzle temperature at end of print               |
| `bed_temper`    | REAL    | Bed temperature at end of print                  |
| `filament_type` | TEXT    | Filament type (e.g. `PLA`, `PETG`)               |
| `user_label`    | INTEGER | `0` = succeeded, `1` = failed; NULL until labeled|
| `label_time`    | TEXT    | ISO-8601 timestamp when label was submitted      |
| `layer_height`  | REAL    | Reserved for future slicer data                  |
| `infill_density`| REAL    | Reserved for future slicer data                  |
| `wall_loops`    | INTEGER | Reserved for future slicer data                  |

---

## Notes

- If a printer goes offline mid-print, the MQTT monitor reconnects with
  exponential backoff (up to 60 s).
- If several printers finish within the same 250 ms poll window, labeling
  popups appear one at a time — each popup identifies which printer it belongs
  to.
- If the daemon is closed while a popup was open, those completed-but-unlabeled
  jobs are re-prompted the next time it starts.
- `layer_height`, `infill_density`, and `wall_loops` are reserved for future
  slicer (`.3mf`) metadata extraction.

---

## Roadmap — future workflow

The dataset today captures *metadata* about each print (printer, material,
temperatures, filename, outcome). The natural next step is to also keep the
**actual source file** so a failure can be investigated against the real
slicer settings, not just a row in a table.

Planned workflow:

1. **Fetch the file from the printer.** When a print ends, pull the
   `.gcode.3mf` file referenced by `subtask_name` / `gcode_file` off the
   printer (via the Bambu HTTP/file interface or MQTT) and hash it.
2. **Extract slicer settings.** Unzip the `.3mf` (it's a zip archive) and read
   the embedded `slice_info.config` / `metadata.json` to populate the reserved
   columns — `layer_height`, `infill_density`, `wall_loops`, and more.
3. **Archive to an external drive.** Copy the file to a mounted archive volume
   under a stable layout, e.g.:
   ```
   /Volumes/PrintArchive/
   └── 2026/
       └── 07/
           └── <job_id>__<printer_serial>__<subtask_name>.3mf
           └── <job_id>.json   # the matching dataset row + extracted slicer settings
           └── <job_id>.png    # the 3mf's thumbnail image (for quick visual review)
           └── ...
   ```
4. **Link it back to the dataset.** Store the archive path (and file hash) in a
   new `archive_path` / `file_sha256` column on `print_jobs`, so each labeled
   row points at the physical file on the external drive for later reference.

The result is a self-contained dataset: every row in `print_dataset.db` can be
traced back to the exact `.3mf` file on the archive drive, making it suitable
for reproducible analysis or model training. This is not implemented yet —
`layer_height`, `infill_density`, and `wall_loops` are placeholders for it.

---

## Student print submission (web + station)

In addition to passively labeling prints, the project now supports a
student-facing workflow: students visit a Vercel site, see live printer status
and camera snapshots, and submit a sliced `.gcode.3mf` file. The dedicated
laptop downloads it and pushes it to a printer's Files tab — no auto-start, the
student starts the print from the printer when physically ready.

### Components

- **`web/`** — a Next.js app (Cornell Tech themed). The status grid calls the
  station's `/api/printers`; each card shows a camera snapshot. The upload form
  uses **client-side uploads**: it asks `/api/upload` for a short-lived client
  token (`handleUpload`) and uploads the file directly to Vercel Blob at
  `incoming/<name>__<targetSerial>__<filename>.gcode.3mf` — bypassing Vercel's
  4.5 MB serverless body limit so real 10–50 MB slices upload fine. A progress
  bar reflects the direct upload.
- **`station/api_server.py`** — FastAPI server exposing `/api/printers`,
  `/api/printer/<serial>/camera`, and `/api/health`. Started by `main.py` in a
  daemon thread. Expose it to the internet with a Cloudflare Tunnel
  (`station/TUNNEL.md`). `POST /api/blob-webhook` accepts `{pathname, url,
  downloadUrl}` and enqueues it for the file watcher (shared-secret auth).
- **`station/file_watcher.py`** — **webhook-driven**. When the station's
  `/api/blob-webhook` endpoint is notified that a new submission landed in
  Vercel Blob's `incoming/` prefix, it downloads the blob via the signed
  `downloadUrl`, renames it to `<name>_<filename>.gcode.3mf`, and uploads it to
  the chosen (or any idle) printer via `station/printer_uploader.py`. Each
  submission is recorded in the `uploads` SQLite table. On startup it does one
  catch-up `list` to grab anything uploaded while offline; an optional rare
  fallback poll (`web.poll_interval_s`, default `0` = off) is a safety net —
  keep it rare on the Hobby plan.
- **`station/printer_uploader.py`** — the only module that writes to a printer:
  the 3-step Bambu local HTTP upload flow (credentials → OSS upload → land
  file).

### Setup (one-time)

1. Create a Vercel Blob store and copy its read/write token into
   `config.json` under `web.blob_token` and into `web/.env.local` (and the
   Vercel project env vars) as `BLOB_READ_WRITE_TOKEN`.
2. Generate a shared webhook secret (e.g. `openssl rand -hex 32`) and put it in
   `config.json` under `web.webhook_secret` and in the Vercel project env vars
   as `STATION_WEBHOOK_SECRET`. The two must match.
3. Deploy `web/` to Vercel. Set `STATION_API_URL` to the tunnel hostname.
4. Set up a Cloudflare Tunnel from the dedicated laptop to `localhost:8080`
   (`station/TUNNEL.md`). Put the tunnel hostname in `config.json` under
   `web.station_api_url`.
5. Restart the daemon on the laptop — `main.py` automatically starts the API
   server and the file watcher when the `web` section is present.

### Data flow

```
student → web (Vercel) → /api/upload (handleUpload: client token) → browser uploads direct to Vercel Blob (incoming/)
                                                                       ↓ Vercel Blob upload-completed callback → /api/upload (onUploadCompleted)
                                                                       ↓ /api/upload POSTs {pathname,url,downloadUrl} to station /api/blob-webhook (best-effort)
station → file_watcher downloads blob via signed downloadUrl → printer_uploader.upload_file() → printer Files tab
web (Vercel) → station /api/printers + /camera → printer (MQTT + snapshot)
```

### `uploads` table

| Column             | Type | Description                                              |
|--------------------|------|----------------------------------------------------------|
| `upload_id`        | TEXT | UUID generated when the station picks up the file       |
| `blob_key`         | TEXT | Vercel Blob pathname (`incoming/<name>__<serial>__<file>`|
| `student_name`     | TEXT | Sanitized student name                                   |
| `original_filename`| TEXT | Original uploaded filename                               |
| `renamed_filename` | TEXT | Final `<name>_<file>.gcode.3mf` sent to the printer       |
| `target_printer`   | TEXT | Requested printer serial, or `any`                       |
| `printer_serial`   | TEXT | Printer the file was actually uploaded to                |
| `upload_status`    | TEXT | `pending` / `uploading` / `done` / `failed`              |
| `error_message`    | TEXT | Failure detail, if any                                   |
| `received_at`      | TEXT | ISO-8601 when the station downloaded the file           |
| `uploaded_at`      | TEXT | ISO-8601 when the printer accepted the file             |
