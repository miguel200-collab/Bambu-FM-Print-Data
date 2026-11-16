# Feature map

What a student or a lab operator can actually do. Internal tables (eras, design families, slicer features) exist to support these and are not separate screens.

## Website (Vercel)

### Printer list with live status

The home page shows one card per printer: name, serial, connected or not, gcode state, nozzle and bed temperature, filament, and the file name of the current job. The page loads status from the station, then the browser polls `/api/printers` every 20 seconds and again when the tab becomes visible. A connection pill counts how many printers are connected. If the laptop is unreachable, the grid says the station is offline and keeps the last cards it had.

Cards use a static illustration chosen from the printer name (X1C, P1S, A1, or generic). The site does not show a live camera. The station still exposes `GET /api/printer/<serial>/camera` for a LAN JPEG, and each status payload includes `camera_url`.

A theme toggle switches light and dark. The About section states that this site is not the official Bambu Farm Manager app.

### Submit a sliced file

The upload form asks for a NetID, an optional target printer ("any free printer" or one of the live printers), and a file. Only a `.gcode.3mf` is accepted. The browser checks the extension and that the bytes start with a ZIP signature. The file uploads from the browser straight to a private Vercel Blob at `incoming/<netid>__<target>__<file>.gcode.3mf`, with a progress bar. The serverless route only issues the upload token and, when Blob reports completion, POSTs `{pathname, url, downloadUrl}` to the station. A failed notify does not fail the upload; the blob stays until the laptop catches up. The student is told to open Bambu Farm Manager on the lab laptop and send the file to a printer from there. The website never starts a print.

### Label my print

`/label` asks for a NetID and lists that student's prints from the last 14 days that came from a file they uploaded. Each row can be marked succeeded or failed, and a later choice replaces the earlier one. The browser talks only to this site; the server routes attach `x-station-key` and call the laptop. With no shared secret configured, those routes fail closed.

Signed email links (`/label?t=…`) and the notify route are built and stay off. They answer as disabled until email labeling is explicitly turned on.

## Station (lab laptop)

### Print logging

`python main.py` connects to each printer in `config.json` over MQTT/TLS and records job start and end in `print_dataset.db` (state, temperatures, filament, the printer's own error code). A pause and resume stay one job. The process is headless and is the NSSM service. `python main.py --mock` exercises the same loop with a fake finish and writes `print_dataset_mock.db` instead.

### Upload intake and offline catch-up

`POST /api/blob-webhook` checks `x-station-key` and queues the blob. The watcher downloads it, archives `<netid>_<file>.gcode.3mf` under `web.inbox_dir/<netid>/`, extracts slicer features, and deletes the blob only after the archive succeeds. A best-effort push to the printer's own HTTP API may run; if it fails, the file is still archived. On startup the watcher does one Blob `list` for anything uploaded while the laptop was offline. `web.poll_interval_s` defaults to 0, so it does not poll.

### Nightly database backup

`analysis/backup_db.py` (scheduled by `install_backup_task.bat`, 03:00) copies `print_dataset.db` with SQLite's online backup API while the daemon can stay up, checks integrity and row counts, mirrors the inbox archive without deleting backup files, and prunes old snapshots while keeping the newest seven. Destination and retention come from `backup.dest` and `backup.keep_days`.

### Read-only audit

`analysis/audit.py` opens the database read-only and reports volume, human-versus-machine label agreement, mass-click bursts, and how many uploads match a print job. It does not write.
