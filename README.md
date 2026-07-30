# Bambu Farm Manager — Print Failure Data Engine

A Python daemon for a shared makerspace (Cornell Tech MakerLAB). It does two
things on a dedicated Windows laptop:

1. **Print labeling** — monitors a fleet of Bambu Lab printers over MQTT/TLS,
   logs every print job to a local SQLite database, and pops up a prompt asking
   "How did this print turn out?" when each job ends. Over time this builds a
   labeled dataset for a future print-failure prediction model.
2. **Student submission** — students visit a public Vercel website, upload a
   sliced `.gcode.3mf` file, and the laptop pulls it down and archives it into
   an organized inbox folder. The student then walks into the lab, opens the
   official **Bambu Farm Manager (BFM)** app, and clicks Upload to send the
   file to a printer. The "send to printer" step stays a deliberate, in-lab
   human action — students never print from home.

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

This tool fixes that by passively watching every printer on the LAN and turning
each completed print into a quick question for the person nearby. The result is
a clean, growing dataset (printer, material, temperatures, file, and a human
label) that's ready for analysis or ML down the road.

The student-submission half then makes it easy for someone to send a sliced
file from home and pick it up in the lab, without ever needing physical media
or remote printer access.

---

## How print labeling works

1. `main.py` reads `config.json` and starts one `PrinterMonitor` per printer in a
   background thread.
2. Each monitor connects to its printer over MQTT/TLS (port 8883) and watches
   for state transitions.
3. When a print starts (`RUNNING`), a new row is inserted into `print_dataset.db`.
4. When a print ends (`FINISH` or `FAILED`), a popup appears asking
   **"How did this print turn out?"**
5. You click **Print Succeeded** or **Print Failed** — the label (`0` =
   succeeded, `1` = failed) is written to the database alongside the job
   metadata.

---

## Architecture

### Print labeling

```
printer (LAN) ──MQTT/TLS──▶ mqtt_listener.py ──▶ print_dataset.db (print_jobs)
                                              └─▶ labeler_gui.py popup on FINISH/FAILED
```

### Student submission

```
student (home) → Vercel (Next.js) site → /api/upload (handleUpload: issue client token)
                     ↓ browser uploads the .gcode.3mf directly to Vercel Blob (incoming/), bypassing the 4.5 MB function limit
Vercel Blob → upload-completed callback → /api/upload (onUploadCompleted)
                     ↓ /api/upload POSTs {pathname,url,downloadUrl} to station /api/blob-webhook (best-effort)
station laptop → file_watcher downloads the blob (Bearer auth on the GET; prefers signed downloadUrl),
                  renames to <name>_<file>.gcode.3mf, archives to the inbox, deletes the blob
student (in lab) → BFM Client → Upload → select file from inbox → Files tab → Create / Direct to Print → printer (LAN)
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
  short-lived client token (`handleUpload`/`upload()` from `@vercel/blob/client`),
  so the file never passes through the serverless function and Vercel's 4.5 MB
  request body limit doesn't apply — real 10–50 MB `.gcode.3mf` slices upload
  fine. Stays free on the Hobby plan.
- **Private Blob store.** `bambu-submissions` is a Private store, so blobs are
  not readable via their plain URL. The laptop sends
  `Authorization: Bearer <BLOB_READ_WRITE_TOKEN>` on every blob GET (and
  prefers the signed `downloadUrl` when present), so both the signed URL and
  the plain-URL fallback work. The `file_watcher` deletes each blob after
  processing, which keeps Blob storage well under the 1 GB Hobby limit under
  normal operation.
- **Status + camera** on the Vercel site are read from a small FastAPI server
  on the laptop (`station/api_server.py`), exposed to the internet via a
  tunnel. The live deployment uses **Tailscale Funnel** (free, no domain
  needed — see `station/TUNNEL.md`); Cloudflare Tunnel is the documented
  alternative. The laptop fetches printer telemetry over MQTT and camera JPEGs
  over the LAN.
- **The "into BFM" step is manual** (a human clicks Upload + selects the file).
  This works today and needs no BFM reverse-engineering. Fully automating that
  last click is an optional future upgrade, not a blocker (see below).

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
│   ├── api_server.py     # FastAPI: /api/printers, /api/printer/<serial>/camera, /api/health, /api/blob-webhook
│   ├── file_watcher.py   # Webhook-driven Blob→inbox bridge: downloads, renames, archives, deletes blob
│   ├── printer_uploader.py # Bambu HTTP chunked-upload client (best-effort convenience; see notes)
│   ├── camera.py         # Fetches a JPEG snapshot from a printer over the LAN
│   └── TUNNEL.md         # Tunnel setup (Tailscale Funnel / Cloudflare Tunnel)
├── web/                  # The Vercel Next.js site (Cornell Tech themed)
│   ├── app/              # App-router pages + /api/upload route
│   ├── components/       # PrinterCard, StatusBadge, UploadForm
│   ├── lib/              # station.ts (status/camera fetch), blob.ts (Vercel Blob helpers)
│   └── README.md         # Web app setup
└── README.md
```

> `config.json`, `print_dataset.db`, and `makerlab.log` are created at runtime
> next to the scripts and are **not** checked into the repo (see `.gitignore`).

### Notes on `printer_uploader.py`

This module uploads to a **printer's local HTTP API**, bypassing BFM. It is not
the primary path — the human performs the upload via BFM's Upload button.
`file_watcher.py` still calls it as a **best-effort, non-fatal** convenience:
if the push fails (or no printer is available), the daemon logs a warning but
still archives the file to the inbox and deletes the blob. So a printer-push
failure never blocks the actual goal (file landing in the inbox for the
student to pick via BFM). The web upload form's "target printer" dropdown is
optional metadata only — the human picks the printer in BFM, not from the
website.

---

## Setup

### Part 1 — Run the print-labeling daemon

#### 1. Install Python 3.11+

Any modern Python 3 will work. On Windows:

```
winget install Python.Python.3.11
```

Or download from [python.org](https://www.python.org/downloads/) and check
**"Add Python to PATH"**.

#### 2. Get the code

```bash
git clone https://github.com/miguel200-collab/Bambu-FM-Print-Data.git
cd Bambu-FM-Print-Data
```

#### 3. Install dependencies

```bash
pip install -r requirements.txt
```

(On Windows you can just double-click `setup.bat` instead.)

#### 4. Create `config.json`

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

#### 5. Run it

```bash
python main.py
```

A background process starts and watches for print events. When a print finishes,
a popup asks how it turned out. Check `makerlab.log` for connection status.

#### Try it without any printers (offline test)

```bash
python main.py --mock
```

This skips all MQTT connections and injects a fake `FINISH` event after 3
seconds, so you can test the full database → popup → label flow without a
printer on the network.

### Part 2 — Set up student submission (web + laptop)

This is the full end-to-end setup, in order. It splits into a **web side**
(Vercel, done once) and a **laptop side** (the dedicated machine in the lab).

#### A. Web side (Vercel) — once per deployment

1. **Create a Vercel Blob store** named `bambu-submissions` with
   **Private** access (access mode cannot be changed after creation — pick
   Private). Note the **store ID** and the **`BLOB_READ_WRITE_TOKEN`** (the
   Blob store's Settings → `.env.local` tab shows the token in plaintext).
2. **Generate a shared webhook secret** once:
   ```bash
   openssl rand -hex 32
   ```
   Save it — you'll put the same value in both the Vercel project env vars
   and the laptop's `config.json`.
3. **Deploy `web/` to Vercel** from the `web/` directory:
   ```bash
   cd web
   npx vercel --prod
   ```
4. **Set the Vercel project env vars** (Production + Preview) in the Vercel
   dashboard:
   - `BLOB_READ_WRITE_TOKEN` — from step 1.
   - `BLOB_STORE_ID` — from step 1.
   - `BLOB_WEBHOOK_PUBLIC_KEY` — from the Blob store settings (verifies the
     `onUploadCompleted` callback signature).
   - `STATION_WEBHOOK_SECRET` — from step 2 (must match the laptop).
   - `STATION_API_URL` — the laptop's public tunnel URL (set in step B-5
     after the tunnel is up; can be set later and redeployed).
5. **Redeploy** (Production, "Use existing Build Cache" unchecked) so the
   env vars take effect.

#### B. Laptop side (the dedicated Windows machine in the lab)

1. **Install Python 3.11+ and Git.** On Windows:
   ```cmd
   winget install Python.Python.3.11 Git.Git
   ```
2. **Clone the repo** and install dependencies:
   ```cmd
   git clone https://github.com/miguel200-collab/Bambu-FM-Print-Data.git C:\Bambu-FM-Print-Data
   cd C:\Bambu-FM-Print-Data
   python -m pip install -r requirements.txt
   ```
   Use `python -m pip` (not `pip`) so the packages land in the same
   interpreter that runs `main.py`. Verify with:
   ```cmd
   python -c "import httpx, fastapi, uvicorn; print('ok')"
   ```
3. **Create `config.json`** from the template and fill in the `printers`
   array (each printer's `name`, `ip`, `serial`, `access_code`) plus the
   `web` section:
   ```cmd
   copy config.json.template config.json
   ```
   ```json
   {
     "printers": [ { "name", "ip", "serial", "access_code" } ],
     "web": {
       "blob_token":       "<BLOB_READ_WRITE_TOKEN from Vercel>",
       "station_api_url":  "https://<laptop>.ts.net",
       "webhook_secret":   "<openssl rand -hex 32 output, same as Vercel>",
       "api_host":         "127.0.0.1",
       "api_port":         8080,
       "poll_interval_s":   0,
       "inbox_dir":        "C:\\BambuSubmissions"
     }
   }
   ```
   `api_host` `127.0.0.1` = only the tunnel reaches the API from outside
   (safer). `poll_interval_s` `0` = webhook-driven, no continuous polling
   (do not change — continuous polling exhausts the Hobby Blob quota).
4. **Create the inbox root:**
   ```cmd
   mkdir C:\BambuSubmissions
   ```
   (The daemon also auto-creates it on startup, but making it explicitly
   avoids any permission surprise.)
5. **Set up the tunnel — Tailscale Funnel (recommended, free, no domain).**
   - Create a Tailscale account, install Tailscale on the laptop, and
     `tailscale up`.
   - Enable Funnel in the admin console: **Access controls → JSON editor →
     "Add Funnel to policy"** (Funnel is ACL-gated; there's no toggle under
     Settings/DNS).
   - Test in the foreground: `tailscale funnel 8080` — it prints the public
     URL (`https://<device-name>.<tailnet>.ts.net`).
   - Make it persistent: `tailscale funnel --bg 8080` (registered with the
     `tailscaled` Windows service; survives reboot).
   - Copy that exact URL into `config.json` `web.station_api_url` **and**
     into the Vercel env var `STATION_API_URL`, then **redeploy** Vercel.
   - DNS propagation: the URL works from the tailnet immediately; public
     DNS (Vercel's servers) may take a few minutes the first time.
   - **Alternative:** Cloudflare Tunnel (`station/TUNNEL.md`) if you have a
     domain.
6. **Start the daemon:**
   ```cmd
   python main.py
   ```
   You should see all printers connect over MQTT, `Station API listening on
   http://127.0.0.1:8080`, `Uvicorn running`, `GUI loop started`, and
   `FileWatcher started (webhook-driven; fallback poll every 0s)`.
7. **End-to-end test:** upload a real sliced `.gcode.3mf` from the Vercel
   site. Within seconds the laptop should log
   `Archived <renamed> (target <printer>) to C:\BambuSubmissions\<name>\<renamed>`
   and the blob should disappear from the Vercel Blob dashboard's `incoming/`
   folder. Then open BFM → Upload → pick the file from
   `C:\BambuSubmissions\<name>\` → Create / Direct to Print.

### Student file requirement (important)

Students **must upload a sliced `.gcode.3mf` file** — the bundled 3MF format
Bambu Studio exports (a ZIP container with the G-code + plate metadata
inside). A **plain `.3mf`** (a 3D model with no G-code) or a **plain `.gcode`**
(text G-code) will be rejected by Bambu Farm Manager with a generic "file
error" even though the laptop accepts and archives it. In Bambu Studio:
**File → Export → Export plate sliced file as `.gcode.3mf`** (not "Export
plate as 3MF" and not a plain `.gcode`). Make sure this is communicated on the
upload form.

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

`python main.py` is a foreground process — closing its cmd window kills it
(the Tailscale Funnel survives as a Windows service, but the daemon does not).
Beyond the Task Scheduler option above, the most robust choice for a dedicated
always-on laptop is [NSSM](https://nssm.cc/), which wraps `python main.py` as a
real Windows service: no window, survives reboot, auto-restarts on crash, and
starts before login.

On macOS/Linux, use your preferred process manager (`launchd`, `systemd --user`,
a cron `@reboot` entry, or just a terminal session).

---

## Updating after a pull

After pulling new code, just restart the daemon — there's nothing else to do.
Don't delete `config.json`, `print_dataset.db`, or `makerlab.log`; those hold
your local fleet config and collected data.

If `git pull` reports "local changes to the following files would be
overwritten by merge", stash your local edits and pull:

```cmd
git stash
git pull
git stash drop
```

`config.json` and `print_dataset.db` are gitignored, so `git pull` never
touches them — your local config and data always survive an update.

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

### `print_jobs` (print labeling)

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

### `uploads` (student submissions)

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

## Future ideas

### Slicer metadata + file archive

The dataset today captures *metadata* about each print (printer, material,
temperatures, filename, outcome). A natural next step is to also keep the
**actual source file** so a failure can be investigated against the real slicer
settings, not just a row in a table:

1. **Fetch the file from the printer.** When a print ends, pull the
   `.gcode.3mf` file referenced by `subtask_name` / `gcode_file` off the
   printer (via the Bambu HTTP/file interface or MQTT) and hash it.
2. **Extract slicer settings.** Unzip the `.3mf` (it's a zip archive) and read
   the embedded `slice_info.config` / `metadata.json` to populate the reserved
   columns — `layer_height`, `infill_density`, `wall_loops`, and more.
3. **Archive to an external drive** under a stable layout, e.g.
   `/Volumes/PrintArchive/2026/07/<job_id>__<printer_serial>__<subtask_name>.3mf`
   plus matching `<job_id>.json` (the dataset row + slicer settings) and
   `<job_id>.png` (the 3mf's thumbnail).
4. **Link it back to the dataset** with new `archive_path` / `file_sha256`
   columns on `print_jobs`, so each labeled row points at the physical file.

This is not implemented yet — `layer_height`, `infill_density`, and `wall_loops`
are placeholders for it.

### Automating the last BFM click

Fully automating the manual "click Upload in BFM" step. Two viable routes:

1. **Bambu cloud API.** Sign into <https://bambulab.com> with the Bambu account
   used to activate BFM and check "My Files." If files uploaded through BFM
   appear there too, uploads go to the Bambu cloud account, and the laptop can
   upload via the community Bambu cloud API (no BFM internals needed). The file
   would then appear in BFM's Files tab automatically.
2. **UI automation** (`pywinauto`) to drive BFM's Upload button + file dialog.
   No API or request-signing needed, but brittle to BFM UI changes.

BFM's local server has no documented upload API, and its `debug.log` shows no
upload requests — a direct API replication would require reproducing Bambu's
request signing, which isn't worth it while the manual Upload click is
acceptable. `station/inspect_bfm.py` was written to investigate this and can be
re-run if the automated route is ever pursued.

---

## Quick links

- Official BFM docs: <https://wiki.bambulab.com/en/software/bambu-farm-manager>
- Tunnel setup: [`station/TUNNEL.md`](station/TUNNEL.md)
- Inspection script (reference): [`station/inspect_bfm.py`](station/inspect_bfm.py)
- Web app: [`web/README.md`](web/README.md)
