"""
main.py — Entry point for the Bambu Farm Manager Print Failure Data Engine.

Headless daemon: it has no window. It watches the printers over MQTT, records
every print job to SQLite, serves the status/label API, and archives student
submissions. Print outcomes are labeled by students on the website's
"Label my print" page (see station/api_server.py), not in a popup.

Normal mode (connects to real printers on the LAN):
    python main.py
    pythonw main.py        # same, but with no console window (Windows)

Mock mode (offline testing, no printers required):
    python main.py --mock

    Skips all MQTT connections and injects a fake FINISH event after 3 seconds
    so the full database -> event loop flow can be tested without printers.
    Mock mode writes to print_dataset_mock.db, never the real dataset.
"""

import argparse
import json
import logging
import logging.handlers
import queue
import signal
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import database as db
from mqtt_listener import PrintEndEvent, PrinterMonitor
from station.label_notifier import LabelNotifier

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_BASE_DIR   = Path(__file__).parent
_LOG_PATH   = _BASE_DIR / "makerlab.log"
_CONFIG_PATH = _BASE_DIR / "config.json"
_MOCK_DB_PATH = _BASE_DIR / "print_dataset_mock.db"

# How often the heartbeat line is written to the log (5 minutes).
_HEARTBEAT_S = 5 * 60


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


def _load_full_config() -> dict:
    """Load the entire config.json (printers + web section)."""
    if not _CONFIG_PATH.exists():
        return {}
    with _CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def _start_station_services(
    monitors: list[PrinterMonitor],
    full_config: dict,
) -> list:
    """
    Start the FastAPI status/camera/label server and the Vercel Blob file watcher.

    Both are optional: if the "web" section of config.json is missing, or the
    extra dependencies (fastapi/uvicorn/httpx) aren't installed, we log a notice
    and continue running just the print-logging daemon.

    Returns the list of stoppable service objects (for graceful shutdown).
    """
    log = logging.getLogger("main")
    services: list = []
    web_cfg = full_config.get("web", {}) or {}
    labels_cfg = full_config.get("labels", {}) or {}

    if not web_cfg:
        log.info("No 'web' section in config.json — station API and file watcher disabled.")
        return services

    # A registry the API server calls on every request to get the live monitors.
    def get_monitors() -> list[PrinterMonitor]:
        return monitors

    # ---- File watcher (Vercel Blob -> printer) ----
    watcher = None
    blob_token = web_cfg.get("blob_token")
    if blob_token:
        try:
            from station.file_watcher import FileWatcher
        except ImportError as exc:
            log.warning("file_watcher unavailable (missing dependency): %s", exc)
        else:
            watcher = FileWatcher(
                blob_token=blob_token,
                get_monitors=get_monitors,
                base_dir=_BASE_DIR,
                inbox_dir=Path(web_cfg["inbox_dir"]) if web_cfg.get("inbox_dir") else None,
                # 0 = webhook-driven (no continuous polling); set poll_interval_s
                # in config to enable a rare safety-net fallback poll.
                poll_interval=float(web_cfg.get("poll_interval_s", 0) or 0),
            )
            watcher.start()
            services.append(watcher)
    else:
        log.info("No 'web.blob_token' in config.json — file watcher disabled.")

    # ---- FastAPI status + camera + label server ----
    api_host = web_cfg.get("api_host", "0.0.0.0")
    api_port = int(web_cfg.get("api_port", 8080))
    try:
        import uvicorn
        from station.api_server import create_app
    except ImportError as exc:
        log.warning("Station API server unavailable (missing dependency): %s", exc)
        return services

    app = create_app(
        get_monitors,
        watcher=watcher,
        webhook_secret=web_cfg.get("webhook_secret") or None,
        email_labels_enabled=bool(labels_cfg.get("email_enabled", False)),
    )
    config = uvicorn.Config(app, host=api_host, port=api_port, log_level="info")
    server = uvicorn.Server(config)

    def _serve() -> None:
        try:
            server.run()
        except Exception as exc:  # pragma: no cover — daemon thread
            log.exception("Station API server crashed: %s", exc)

    threading.Thread(target=_serve, name="station-api", daemon=True).start()
    log.info("Station API listening on http://%s:%d (use a tunnel to expose it)", api_host, api_port)
    services.append(server)
    return services


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
# Headless event loop
# ---------------------------------------------------------------------------

def _make_heartbeat(monitors: list) -> Callable[[], None]:
    """Returns a function that logs daemon health (called periodically)."""
    log = logging.getLogger("main")

    def beat() -> None:
        if monitors:
            connected = sum(1 for m in monitors if m.is_connected())
            log.info("Heartbeat: %d/%d printer(s) connected.", connected, len(monitors))
        else:
            log.info("Heartbeat: running (no live monitors / mock mode).")

    return beat


def _run_event_loop(
    event_queue: queue.Queue,
    notifier: Optional[object],
    stop_event: threading.Event,
    heartbeat: Optional[Callable[[], None]] = None,
    heartbeat_interval_s: float = _HEARTBEAT_S,
    poll_s: float = 1.0,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """
    Block on the print-end queue until ``stop_event`` is set.

    For every PrintEndEvent: log it, then hand it to ``notifier.notify_print_end``
    (if a notifier is configured). A notifier failure is logged and swallowed —
    nothing a notifier does may ever stop the daemon. The job's outcome row is
    already in SQLite (mqtt_listener wrote it); labels arrive later via the
    website.
    """
    log = logging.getLogger("events")
    next_beat = clock() + heartbeat_interval_s
    while not stop_event.is_set():
        try:
            event: Optional[PrintEndEvent] = event_queue.get(timeout=poll_s)
        except queue.Empty:
            event = None

        if event is not None:
            log.info(
                "Print ended: printer=%s  state=%s  job=%s  file=%s",
                event.printer_name, event.final_state, event.job_id, event.subtask_name,
            )
            if notifier is not None:
                try:
                    notifier.notify_print_end(event)  # type: ignore[attr-defined]
                except Exception as exc:
                    log.exception("Notifier failed for job %s: %s", event.job_id, exc)

        if heartbeat is not None and clock() >= next_beat:
            try:
                heartbeat()
            except Exception as exc:  # pragma: no cover — defensive
                log.exception("Heartbeat failed: %s", exc)
            next_beat = clock() + heartbeat_interval_s


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

    if args.mock:
        # Never let fake jobs touch the real dataset.
        db.DB_PATH = _MOCK_DB_PATH
    db.init_db()

    event_queue: queue.Queue = queue.Queue()
    monitors: list[PrinterMonitor] = []
    full_config: dict = {}

    if args.mock:
        log.info("MOCK MODE — no real printers will be contacted (db: %s).", _MOCK_DB_PATH.name)
        threading.Thread(
            target=_run_mock,
            args=(event_queue,),
            daemon=True,
            name="mock-injector",
        ).start()
    else:
        printers = _load_config()
        full_config = _load_full_config()
        # Rows created from now on are stamped with this data era (default 1 =
        # quarantined; set labels.era to 2 when the label page goes live).
        db.set_current_era(int((full_config.get("labels") or {}).get("era", 1)))
        log.info("Loaded %d printer(s) from config.json.", len(printers))
        for p in printers:
            monitor = PrinterMonitor(
                hostname=p["ip"],
                serial=p["serial"],
                access_code=p["access_code"],
                name=p["name"],
                event_queue=event_queue,
            )
            monitor.start()
            monitors.append(monitor)

    # Start the optional station services (FastAPI status/camera/label server +
    # the Vercel Blob file watcher). No-ops if the "web" section is absent.
    services = _start_station_services(monitors, full_config)

    stop_event = threading.Event()
    try:
        signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
    except (ValueError, OSError):  # pragma: no cover — not in the main thread / unsupported
        pass

    log.info("Daemon running (headless). Waiting for print events …")
    try:
        _run_event_loop(
            event_queue,
            # Email reminders are a FUTURE feature: disabled unless
            # config.json labels.email_enabled is true (and still sends nothing
            # without recorded consent). See station/label_notifier.py.
            notifier=LabelNotifier.from_config(full_config),
            stop_event=stop_event,
            heartbeat=_make_heartbeat(monitors),
        )
    except KeyboardInterrupt:
        log.info("Interrupted by user.")
    finally:
        stop_event.set()
        for monitor in monitors:
            monitor.stop()
        for svc in services:
            stop = getattr(svc, "stop", None)
            if callable(stop):
                try:
                    stop()
                except Exception as exc:  # pragma: no cover
                    log.warning("Error stopping service: %s", exc)
        log.info("=== MakerLAB Data Engine stopped ===")


if __name__ == "__main__":
    main()
