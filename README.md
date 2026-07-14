# Bambu Farm Manager — Print Failure Data Engine

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
├── main.py               # Entry point: loads config, starts monitors, runs the Tkinter loop
├── mqtt_listener.py      # PrinterMonitor class — MQTT connection + state machine
├── database.py           # SQLite helpers: init_db, create_job, update_job_end, write_label
├── labeler_gui.py        # Tkinter LabelPopup window
├── config.json.template  # Safe template (no real credentials) — copy and fill in
├── requirements.txt      # paho-mqtt>=2.0
├── setup.bat             # Windows: pip install -r requirements.txt  (optional)
├── install_autostart.bat # Windows: registers a Task Scheduler job at login  (optional)
├── launcher.bat          # Windows: runs main.py with no console window  (optional)
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
