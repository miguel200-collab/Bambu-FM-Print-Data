"""
mqtt_listener.py — Per-printer MQTT monitor for the MakerLAB data engine.

Each PrinterMonitor instance:
  - Opens one TLS MQTT connection to a Bambu printer (port 8883, self-signed cert)
  - Runs the network loop in a background thread via loop_start()
  - Tracks gcode_state transitions and writes to the database
  - Pushes a PrintEndEvent onto the shared queue when a job ends
  - Reconnects automatically with exponential backoff if the printer goes offline

All database writes happen inside the MQTT callback thread; SQLite handles
concurrent access safely because each call opens its own connection.
"""

import json
import logging
import queue
import ssl
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import paho.mqtt.client as mqtt

import database as db

log = logging.getLogger(__name__)

# States that mean "a print is actively in progress"
_ACTIVE_STATES = {"RUNNING", "PREPARE"}

# States that mean "the print job has ended"
_TERMINAL_STATES = {"FINISH", "FAILED"}


@dataclass
class PrintEndEvent:
    """Dropped onto the shared queue when a print job completes or is aborted."""
    job_id: str
    printer_name: str
    subtask_name: Optional[str]
    final_state: str  # "FINISH" or "FAILED"


class PrinterMonitor:
    """
    Manages a single MQTT connection to one Bambu printer.

    Parameters
    ----------
    hostname     : IP address of the printer (e.g. "192.168.1.101")
    serial       : Printer serial number (e.g. "01S00C123456789")
    access_code  : LAN access code shown on the printer screen
    name         : Human-readable label (e.g. "Printer 1 — Bambu X1C")
    event_queue  : Shared queue; PrintEndEvent objects are put here on job end
    """

    _PORT = 8883
    _KEEPALIVE = 60

    def __init__(
        self,
        hostname: str,
        serial: str,
        access_code: str,
        name: str,
        event_queue: queue.Queue,
    ) -> None:
        self._hostname = hostname
        self._serial = serial
        self._access_code = access_code
        self._name = name
        self._event_queue = event_queue

        # State-machine tracking (mutated only inside MQTT callbacks)
        self._last_state: Optional[str] = None
        self._current_job_id: Optional[str] = None
        # Whether we've recorded the printer's initial state. The first state we
        # see is just a baseline — we must NOT treat a pre-existing FINISH (left
        # over from a previous print) as a fresh completion and pop up a dialog.
        self._baselined: bool = False

        # Latest telemetry snapshot (updated on every incoming message)
        self._subtask_name: Optional[str] = None
        self._gcode_file: Optional[str] = None
        self._nozzle_temper: Optional[float] = None
        self._bed_temper: Optional[float] = None
        self._filament_type: Optional[str] = None
        self._print_error: Optional[int] = None
        self._gcode_state: Optional[str] = None
        # Camera JPEG URL reported by the printer (from the ipcam topic).
        self._camera_url: Optional[str] = None

        # Guards telemetry reads/writes across the MQTT thread vs. the API
        # server thread that calls snapshot() / camera_url().
        self._lock = threading.Lock()

        self._client = self._build_client()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Connect and start the background network loop."""
        log.info("[%s] Connecting to %s:%d …", self._name, self._hostname, self._PORT)
        self._client.connect_async(self._hostname, self._PORT, keepalive=self._KEEPALIVE)
        self._client.loop_start()

    def stop(self) -> None:
        """Gracefully stop the background loop and disconnect."""
        log.info("[%s] Stopping monitor.", self._name)
        self._client.loop_stop()
        self._client.disconnect()

    def is_connected(self) -> bool:
        """True if the MQTT client currently has a live connection to the printer."""
        return self._client.is_connected()

    @property
    def serial(self) -> str:
        return self._serial

    @property
    def name(self) -> str:
        return self._name

    @property
    def hostname(self) -> str:
        return self._hostname

    def snapshot(self) -> dict:
        """
        Thread-safe copy of the latest telemetry for the status API.

        Returns a plain dict so it can be JSON-serialised by the FastAPI server
        without holding the MQTT-thread lock during serialisation.
        """
        with self._lock:
            return {
                "name": self._name,
                "serial": self._serial,
                "ip": self._hostname,
                "connected": self._client.is_connected(),
                "state": self._gcode_state,
                "subtask_name": self._subtask_name,
                "gcode_file": self._gcode_file,
                "nozzle_temper": self._nozzle_temper,
                "bed_temper": self._bed_temper,
                "filament_type": self._filament_type,
                "camera_url": self._camera_url,
            }

    def camera_url(self) -> Optional[str]:
        """Latest camera JPEG URL reported by the printer, if any."""
        with self._lock:
            return self._camera_url

    # ------------------------------------------------------------------
    # MQTT client construction
    # ------------------------------------------------------------------

    def _build_client(self) -> mqtt.Client:
        client_id = f"makerlab_{self._serial}"
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
        )

        # Bambu printers use a self-signed TLS cert — disable verification.
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        client.tls_set_context(ssl_ctx)

        client.username_pw_set("bblp", self._access_code)

        # Exponential back-off: 1 s → 2 s → 4 s … up to 60 s between retries.
        client.reconnect_delay_set(min_delay=1, max_delay=60)

        client.on_connect    = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message    = self._on_message

        return client

    # ------------------------------------------------------------------
    # MQTT callbacks (run in the paho background thread)
    # ------------------------------------------------------------------

    def _on_connect(self, client, userdata, connect_flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            log.error("[%s] Connection refused: %s", self._name, reason_code)
            return

        topic = f"device/{self._serial}/report"
        client.subscribe(topic, qos=0)
        log.info("[%s] Connected. Subscribed to %s", self._name, topic)

        # Ask the printer for a full state dump so we pick up any in-progress print.
        self._request_pushall(client)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            log.warning(
                "[%s] Unexpected disconnect (%s). Reconnecting with back-off…",
                self._name, reason_code,
            )
        else:
            log.info("[%s] Disconnected cleanly.", self._name)

    def _on_message(self, client, userdata, message) -> None:
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            log.debug("[%s] Unparseable message: %s", self._name, exc)
            return

        print_data = payload.get("print")
        if not isinstance(print_data, dict):
            # The ipcam topic (sibling of "print") carries the camera JPEG URL.
            ipcam = payload.get("ipcam")
            if isinstance(ipcam, dict):
                url = ipcam.get("ipcam_url")
                if url:
                    with self._lock:
                        self._camera_url = url
            return

        self._update_telemetry(print_data)

        new_state: Optional[str] = print_data.get("gcode_state")
        if new_state is None:
            return

        with self._lock:
            self._gcode_state = new_state

        # First state seen for this printer: record a baseline. Only treat it as
        # a print start if the printer is already actively printing; an existing
        # FINISH/FAILED/IDLE is just leftover state and must not trigger a popup.
        if not self._baselined:
            self._baselined = True
            self._last_state = new_state
            if new_state in _ACTIVE_STATES:
                log.info("[%s] Baseline state %s — print already in progress.", self._name, new_state)
                self._on_print_start()
            else:
                log.info("[%s] Baseline state %s — idle, no action.", self._name, new_state)
            return

        if new_state == self._last_state:
            return

        log.info("[%s] State transition: %s → %s", self._name, self._last_state, new_state)
        self._handle_state_transition(new_state)
        self._last_state = new_state

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    def _handle_state_transition(self, new_state: str) -> None:
        entering_active = (
            new_state in _ACTIVE_STATES and self._last_state not in _ACTIVE_STATES
        )
        if entering_active:
            self._on_print_start()

        elif new_state in _TERMINAL_STATES:
            if self._current_job_id:
                self._on_print_end(new_state)
            else:
                # Terminal state with no job we were tracking — this is leftover
                # state from before we observed a start, so ignore it (no popup).
                log.info(
                    "[%s] Terminal state %s with no tracked job — ignoring.", self._name, new_state
                )

    def _on_print_start(self) -> None:
        self._current_job_id = str(uuid.uuid4())
        self._print_error = None  # reset per-job error tracking
        now = datetime.now(timezone.utc).isoformat()
        log.info("[%s] Job started: %s  file=%s", self._name, self._current_job_id, self._subtask_name)

        db.create_job(
            job_id=self._current_job_id,
            printer_serial=self._serial,
            printer_ip=self._hostname,
            start_time=now,
            subtask_name=self._subtask_name,
            gcode_file=self._gcode_file,
            nozzle_temper=self._nozzle_temper,
            bed_temper=self._bed_temper,
            filament_type=self._filament_type,
        )

    def _on_print_end(self, final_state: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        log.info("[%s] Job ended: %s  state=%s  file=%s", self._name, self._current_job_id, final_state, self._subtask_name)

        db.update_job_end(
            job_id=self._current_job_id,
            end_time=now,
            final_state=final_state,
            subtask_name=self._subtask_name,
            gcode_file=self._gcode_file,
            nozzle_temper=self._nozzle_temper,
            bed_temper=self._bed_temper,
            filament_type=self._filament_type,
            print_error=self._print_error,
        )

        self._event_queue.put(PrintEndEvent(
            job_id=self._current_job_id,
            printer_name=self._name,
            subtask_name=self._subtask_name,
            final_state=final_state,
        ))

        self._current_job_id = None

    # ------------------------------------------------------------------
    # Telemetry helpers
    # ------------------------------------------------------------------

    def _update_telemetry(self, print_data: dict) -> None:
        """
        Accumulate the latest telemetry values from an incoming message.
        Only overwrites a field when the new value is non-empty/non-None.
        """
        def _pick(key: str) -> Optional[str]:
            val = print_data.get(key)
            return val if val else None

        with self._lock:
            if _pick("subtask_name"):
                self._subtask_name = _pick("subtask_name")
            if _pick("gcode_file"):
                self._gcode_file = _pick("gcode_file")
            if "nozzle_temper" in print_data:
                self._nozzle_temper = print_data["nozzle_temper"]
            if "bed_temper" in print_data:
                self._bed_temper = print_data["bed_temper"]

            # Capture a non-zero print_error so a future team can see why a job failed.
            err = print_data.get("print_error")
            if err:
                try:
                    self._print_error = int(err)
                except (TypeError, ValueError):
                    pass

            # Filament type: try vt_tray (non-AMS) first, then first AMS tray.
            self._filament_type = self._extract_filament(print_data) or self._filament_type

    @staticmethod
    def _extract_filament(print_data: dict) -> Optional[str]:
        """Best-effort extraction of the active filament type from a print payload."""
        vt = print_data.get("vt_tray")
        if isinstance(vt, dict):
            ft = vt.get("tray_type")
            if ft:
                return str(ft)

        ams_obj = print_data.get("ams")
        if isinstance(ams_obj, dict):
            for ams_unit in ams_obj.get("ams", []):
                for tray in ams_unit.get("tray", []):
                    ft = tray.get("tray_type")
                    if ft:
                        return str(ft)

        return None

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def _request_pushall(self, client: mqtt.Client) -> None:
        """
        Send a pushall command so the printer immediately broadcasts its full state.
        This lets us detect any in-progress print that started before the daemon launched.
        """
        topic = f"device/{self._serial}/request"
        payload = json.dumps({
            "pushing": {"sequence_id": "1", "command": "pushall"},
            "user_id": "makerlab",
        })
        client.publish(topic, payload, qos=0)
        log.debug("[%s] pushall sent.", self._name)
