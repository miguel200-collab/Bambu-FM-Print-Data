"""
main.py — Entry point for the Bambu Farm Manager Print Failure Data Engine.

Normal mode (connects to real printers on the LAN):
    python main.py
    pythonw main.py        # same, but with no console window (Windows)

Mock mode (offline testing, no printers required):
    python main.py --mock

    Skips all MQTT connections and injects a fake FINISH event after 3 seconds
    so the full database → popup → label flow can be tested without printers.
"""

import argparse
import json
import logging
import logging.handlers
import queue
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import tkinter as tk

import database as db
from labeler_gui import LabelPopup
from mqtt_listener import PrintEndEvent, PrinterMonitor

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_BASE_DIR   = Path(__file__).parent
_LOG_PATH   = _BASE_DIR / "makerlab.log"
_CONFIG_PATH = _BASE_DIR / "config.json"

# How often the heartbeat line is written to the log (5 minutes).
_HEARTBEAT_MS = 5 * 60 * 1000


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _setup_logging() -> None:
    fmt = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    file_handler = logging.handlers.RotatingFileHandler(
        _LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=2, encoding="utf-8"
    )
    console_handler = logging.StreamHandler()
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[file_handler, console_handler],
    )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_config() -> list[dict]:
    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"\nconfig.json not found at {_CONFIG_PATH}\n"
            "  → Copy config.json.template to config.json\n"
            "  → Fill in each printer's ip, serial, and access_code\n"
            "  → Or run with --mock to test without printers"
        )
    with _CONFIG_PATH.open(encoding="utf-8") as f:
        data = json.load(f)
    printers = data.get("printers", [])
    if not printers:
        raise ValueError("config.json contains no printers. Add at least one entry.")
    return printers


# ---------------------------------------------------------------------------
# Mock mode helpers
# ---------------------------------------------------------------------------

def _run_mock(event_queue: queue.Queue) -> None:
    """
    Creates a fake in-progress job in the database, waits 3 seconds, then
    pushes a FINISH event as if a real printer had just completed a print.
    """
    log = logging.getLogger("mock")
    mock_job_id = str(uuid.uuid4())

    db.create_job(
        job_id=mock_job_id,
        printer_serial="MOCK_SERIAL_001",
        printer_ip="0.0.0.0",
        start_time=datetime.now(timezone.utc).isoformat(),
        subtask_name="test_cube.gcode.3mf",
        gcode_file="/data/Metadata/plate_1.gcode",
        nozzle_temper=220.0,
        bed_temper=60.0,
        filament_type="PLA",
    )
    log.info("Mock job created: %s", mock_job_id)
    log.info("Mock FINISH event fires in 3 seconds …")

    time.sleep(3)

    event_queue.put(PrintEndEvent(
        job_id=mock_job_id,
        printer_name="Mock Printer — Bambu X1C",
        subtask_name="test_cube.gcode.3mf",
        final_state="FINISH",
    ))
    log.info("Mock FINISH event queued.")


# ---------------------------------------------------------------------------
# Tkinter queue poller
# ---------------------------------------------------------------------------

def _requeue_unlabeled(event_queue: queue.Queue, serial_to_name: dict[str, str]) -> None:
    """Put a PrintEndEvent back on the queue for every completed-but-unlabeled job."""
    log = logging.getLogger("main")
    rows = db.get_unlabeled_completed_jobs()
    if not rows:
        return
    log.info("Found %d unlabeled completed job(s) from a previous run — re-prompting.", len(rows))
    for row in rows:
        serial = row["printer_serial"]
        name = serial_to_name.get(serial, serial)
        event_queue.put(PrintEndEvent(
            job_id=row["job_id"],
            printer_name=name,
            subtask_name=row["subtask_name"],
            final_state=row["final_state"],
        ))


def _make_heartbeat(root: tk.Tk, monitors: list):
    """Returns a function that periodically logs daemon health."""
    log = logging.getLogger("main")

    def beat() -> None:
        if monitors:
            connected = sum(1 for m in monitors if m.is_connected())
            log.info("Heartbeat: %d/%d printer(s) connected.", connected, len(monitors))
        else:
            log.info("Heartbeat: running (no live monitors / mock mode).")
        root.after(_HEARTBEAT_MS, beat)

    return beat


def _make_poller(root: tk.Tk, event_queue: queue.Queue):
    """
    Returns a function that shows ONE LabelPopup at a time. If a popup is already
    open, queued events wait until it is dismissed. This avoids overlapping
    grab_set() windows when several printers finish at once. Always runs on the
    main thread.
    """
    log = logging.getLogger("gui")
    state = {"popup_open": False}

    def show_next() -> None:
        if state["popup_open"]:
            return
        try:
            event: PrintEndEvent = event_queue.get_nowait()
        except queue.Empty:
            return

        state["popup_open"] = True
        log.info(
            "Popup: printer=%s  state=%s  job=%s",
            event.printer_name, event.final_state, event.job_id,
        )

        def on_label(user_label: int, label_time: str) -> None:
            db.write_label(event.job_id, user_label, label_time)
            label_word = "FAILED" if user_label else "SUCCEEDED"
            log.info(
                "Label saved: job=%s  label=%d (%s)",
                event.job_id, user_label, label_word,
            )

        def on_close() -> None:
            state["popup_open"] = False
            # Immediately try to show the next queued popup, if any.
            show_next()

        LabelPopup(
            parent=root,
            printer_name=event.printer_name,
            subtask_name=event.subtask_name,
            on_label=on_label,
            on_close=on_close,
        )

    def poll() -> None:
        show_next()
        root.after(250, poll)

    return poll


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="MakerLAB Print Failure Data Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python main.py           # production: connect to all printers in config.json\n"
            "  python main.py --mock    # offline test: fake FINISH event after 3 s\n"
        ),
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Skip real MQTT connections; inject a fake FINISH event after 3 s for local testing.",
    )
    args = parser.parse_args()

    _setup_logging()
    log = logging.getLogger("main")
    log.info("=== MakerLAB Data Engine starting ===")

    db.init_db()

    event_queue: queue.Queue = queue.Queue()
    monitors: list[PrinterMonitor] = []
    serial_to_name: dict[str, str] = {}

    if args.mock:
        log.info("MOCK MODE — no real printers will be contacted.")
        threading.Thread(
            target=_run_mock,
            args=(event_queue,),
            daemon=True,
            name="mock-injector",
        ).start()
    else:
        printers = _load_config()
        log.info("Loaded %d printer(s) from config.json.", len(printers))
        for p in printers:
            serial_to_name[p["serial"]] = p["name"]
            monitor = PrinterMonitor(
                hostname=p["ip"],
                serial=p["serial"],
                access_code=p["access_code"],
                name=p["name"],
                event_queue=event_queue,
            )
            monitor.start()
            monitors.append(monitor)

    # Re-prompt for any completed print that never got labeled (e.g. the daemon
    # was closed while a popup was open). Their rows already exist in the DB.
    _requeue_unlabeled(event_queue, serial_to_name)

    # ------------------------------------------------------------------
    # Tkinter main loop (must run on the main thread)
    # ------------------------------------------------------------------
    root = tk.Tk()
    root.withdraw()  # root window is invisible; only Toplevel popups are shown

    poll = _make_poller(root, event_queue)
    root.after(250, poll)

    heartbeat = _make_heartbeat(root, monitors)
    root.after(_HEARTBEAT_MS, heartbeat)

    log.info("GUI loop started. Waiting for print events …")
    try:
        root.mainloop()
    except KeyboardInterrupt:
        log.info("Interrupted by user.")
    finally:
        for monitor in monitors:
            monitor.stop()
        log.info("=== MakerLAB Data Engine stopped ===")


if __name__ == "__main__":
    main()
