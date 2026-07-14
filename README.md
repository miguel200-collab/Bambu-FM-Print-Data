# Bambu MakerLAB Print Failure Data Engine

Monitors a fleet of up to 4 Bambu Lab printers via MQTT, logs every print job to a local SQLite database, and prompts lab staff to label each completed job as succeeded or failed — building a dataset for future print-failure prediction.

---

## How It Works

1. `main.py` reads `config.json` and starts one `PrinterMonitor` per printer in a background thread.
2. Each monitor connects to the printer over MQTT/TLS (port 8883) and watches for state transitions.
3. When a print starts (`RUNNING`), a new row is inserted in `print_dataset.db`.
4. When a print ends (`FINISH` or `FAILED`), a popup appears asking: **"Did this print fail?"**
5. Staff clicks YES or NO — the label is written to the database alongside job metadata.

---

## File Structure

```
Bambu Farm Manager Script/
├── config.json           # Fleet config — edit this on Windows only
├── config.json.template  # Safe template (no real credentials) — copy and rename on Windows
├── main.py               # Entry point: loads config, starts monitors, runs Tkinter loop
├── mqtt_listener.py      # PrinterMonitor class — MQTT connection + state machine
├── database.py           # SQLite helpers: init_db, create_job, update_job_end, write_label
├── labeler_gui.py        # Tkinter LabelPopup window
├── requirements.txt      # paho-mqtt>=2.0
├── setup.bat             # Windows: pip install -r requirements.txt
├── install_autostart.bat # Windows: registers Task Scheduler job at login
├── launcher.bat          # Windows: pythonw main.py (no console window)
├── print_dataset.db      # SQLite database — lives only on Windows
└── makerlab.log          # Rotating log — lives only on Windows
```

---

## Windows Setup (first time)

### 1. Install Python 3.11

```
winget install Python.Python.3.11
```

Or download from [python.org](https://www.python.org/downloads/) and check **"Add Python to PATH"**.

### 2. Copy files from USB

Copy everything **except** `print_dataset.db` and `makerlab.log` to `C:\MakerLAB\`.

### 3. Create `config.json`

Copy `config.json.template` to `config.json` and fill in real values:

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

> **Never copy `config.json` back to your Mac** — it contains printer credentials.

### 4. Install dependencies

Double-click `setup.bat` (or run in a terminal):

```bat
setup.bat
```

### 5. Register auto-start

Double-click `install_autostart.bat` once. This registers the daemon with Windows Task Scheduler so it starts automatically at login.

### 6. Run manually (optional first test)

```bat
launcher.bat
```

A system-tray-style process starts silently. Check `makerlab.log` for connection status.

---

## Update Cycle (after USB copy)

1. Copy new `.py` files from USB to `C:\MakerLAB\` (do **not** overwrite `config.json`, `print_dataset.db`, or `makerlab.log`).
2. Restart the daemon:

```bat
schtasks /run /tn "MakerLAB Daemon"
```

Or reboot the laptop.

---

## Exporting the Database

Copy `C:\MakerLAB\print_dataset.db` to your Mac for analysis. The file is a standard SQLite3 database — open it with [DB Browser for SQLite](https://sqlitebrowser.org/) or query it with Python:

```python
import sqlite3, pandas as pd
conn = sqlite3.connect("print_dataset.db")
df = pd.read_sql("SELECT * FROM print_jobs WHERE user_label IS NOT NULL", conn)
print(df)
```

---

## Database Schema

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

## Development Notes

- All `.py` files flow **one-way**: Mac → USB → Windows. Never edit them on Windows.
- `config.json` is **Windows-only**. Keep real credentials off your Mac.
- `print_dataset.db` and `makerlab.log` stay on Windows; copy them to Mac periodically for analysis.
- If a printer goes offline mid-print, the MQTT monitor reconnects with exponential backoff (up to 60 s).
- If two printers finish within the same 250 ms poll window, labeling popups appear one at a time — each popup identifies which printer it belongs to.
