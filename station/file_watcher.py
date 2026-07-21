"""
file_watcher.py — Bridge between Vercel Blob and the Bambu printers.

Processing is **webhook-driven** to stay within Vercel Blob's free Hobby operation
limits (a 10s polling loop would exhaust the 2,000 advanced-ops/month quota in
hours, since every `list` counts as an advanced operation). The flow:

  - The Vercel site does a server-side `put()` to Blob, then POSTs a small
    notification to this station's `/api/blob-webhook` endpoint (via the
    Cloudflare Tunnel) containing the blob's `pathname` and `url`.
  - The webhook handler (in api_server.py) verifies a shared secret and enqueues
    the blob onto the watcher's queue.
  - The watcher thread downloads the blob by its URL (a cheap read, not a
    `list`), renames it, uploads it to a printer, deletes the blob, and archives
    the local copy into the organized inbox.
  - On startup the watcher does ONE catch-up `list` to grab anything that was
    uploaded while the station was offline (or whose webhook was missed).
  - An optional low-frequency fallback poll (`fallback_poll_interval_s`,
    default 0 = disabled) can be enabled as a safety net. Keep it rare: each poll
    is one advanced operation, and Hobby only allows 2,000/month.

Per-blob steps:
  1. Parse student name, target printer, and original filename from the blob
     pathname (encoded by the Vercel upload route as
     "incoming/<name>__<targetSerial>__<originalFilename>").
  2. Download the blob to a local incoming/ dir.
  3. Rename to "<name>_<originalFilename>.gcode.3mf" (extension normalised to the
     printer's native .gcode.3mf format).
  4. Resolve the target printer (the chosen serial, or any idle printer if the
     student left it as "any").
  5. Push the file to the printer via printer_uploader.upload_file().
  6. On success: delete the blob (so it isn't reprocessed) and mark the uploads
     row done, and archive the renamed local copy into the organized inbox at
     ``<inbox_dir>/<sanitized student name>/<name>_<filename>.gcode.3mf`` so the
     student can find it from BFM's Upload dialog. On failure: mark failed and
     leave the blob for a backoff retry.

Vercel Blob has no metadata-update endpoint, so "processed" is signalled by
deleting the blob once the file has been handed to the printer. The uploads
table is the durable record.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

import httpx

import database as db
from mqtt_listener import PrinterMonitor
from station import printer_uploader

log = logging.getLogger(__name__)

_BLOB_BASE = "https://blob.vercel-storage.com"
_POLL_INTERVAL_S = 10.0
_CHUNK = 5 * 1024 * 1024
_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=120.0, pool=10.0)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize(value: str) -> str:
    """Make a string safe to embed in a filename / blob pathname."""
    cleaned = "".join(
        c if c.isalnum() or c in "-._" else "_" for c in value.strip()
    )
    return cleaned or "anon"


def _normalize_gcode_name(name: str) -> str:
    """Force a .gcode.3mf extension to match the printer's native format."""
    stem = name
    for ext in (".gcode.3mf", ".3mf.gcode", ".3mf", ".gcode"):
        if stem.lower().endswith(ext):
            stem = stem[: -len(ext)]
            break
    return f"{stem}.gcode.3mf"


class FileWatcher:
    """
    Polls Vercel Blob for new student submissions and uploads them to printers.

    Parameters
    ----------
    blob_token    : Vercel Blob read/write token.
    get_monitors  : Callable returning the live list of PrinterMonitor instances.
    base_dir      : Directory to use for the local incoming/ staging folder.
    inbox_dir     : Root of the organized inbox where finished submissions are
                    archived, one subfolder per sanitized student name. Defaults
                    to ``base_dir / "BambuSubmissions"`` when not provided.
    poll_interval : Seconds between fallback polls. ``0`` (default) disables
                    continuous polling — processing is webhook-driven, with a
                    single catch-up ``list`` on startup. Set to e.g. ``1800``
                    (30 min) to enable a rare safety-net poll. Keep it rare:
                    each poll is one advanced Blob operation, and the Hobby plan
                    allows only 2,000/month.
    """

    def __init__(
        self,
        blob_token: str,
        get_monitors: Callable[[], Iterable[PrinterMonitor]],
        base_dir: Path,
        inbox_dir: Optional[Path] = None,
        poll_interval: float = 0.0,
    ) -> None:
        self._token = blob_token
        self._get_monitors = get_monitors
        self._poll_interval = float(poll_interval or 0.0)
        self._incoming = base_dir / "incoming"
        self._inbox = Path(inbox_dir) if inbox_dir else base_dir / "BambuSubmissions"
        self._incoming.mkdir(parents=True, exist_ok=True)
        self._inbox.mkdir(parents=True, exist_ok=True)

        # Webhook-driven queue: items are blob dicts with at least {pathname, url}.
        self._queue: "queue.Queue[Optional[dict]]" = queue.Queue()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        # Catch up on anything uploaded while the station was offline (or whose
        # webhook was missed). This is the only `list` we do at startup.
        try:
            pending = self._list_pending_blobs()
            for blob in pending:
                if self._already_processed(blob.get("pathname", "")):
                    continue
                self._queue.put(blob)
            if pending:
                log.info("FileWatcher: %d pending blob(s) on startup.", len(pending))
        except Exception as exc:  # don't let startup catch-up kill the watcher
            log.exception("FileWatcher startup catch-up failed: %s", exc)
        self._thread = threading.Thread(
            target=self._run, name="file-watcher", daemon=True
        )
        self._thread.start()
        log.info(
            "FileWatcher started (webhook-driven; fallback poll every %.0fs)",
            self._poll_interval,
        )

    def enqueue_blob(self, blob: dict) -> None:
        """Enqueue a blob for processing. Called by the /api/blob-webhook handler."""
        self._queue.put(blob)
        log.info("FileWatcher: enqueued blob %s", blob.get("pathname"))

    def stop(self) -> None:
        self._stop_event.set()
        self._queue.put(None)  # unblock the queue getter
        if self._thread:
            self._thread.join(timeout=5.0)
        log.info("FileWatcher stopped.")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _run(self) -> None:
        # Webhook-driven: block on the queue, processing blobs as they arrive.
        # Optionally do a rare fallback `list` poll to catch anything missed by
        # webhooks (e.g. tunnel blips). The fallback interval is a safety net,
        # not the primary mechanism — keep it rare on the Hobby plan.
        next_poll = time.monotonic() + self._poll_interval if self._poll_interval else None
        while not self._stop_event.is_set():
            timeout = None
            if next_poll is not None:
                timeout = max(0.0, next_poll - time.monotonic())
            try:
                blob = self._queue.get(timeout=timeout)
            except queue.Empty:
                blob = None

            if blob is not None:
                try:
                    self._process_blob(blob)
                except Exception as exc:  # never let the loop die
                    log.exception("Failed to process blob %s: %s", blob.get("pathname"), exc)

            if next_poll is not None and time.monotonic() >= next_poll:
                try:
                    self._fallback_poll()
                except Exception as exc:
                    log.exception("FileWatcher fallback poll failed: %s", exc)
                next_poll = time.monotonic() + self._poll_interval

    def _fallback_poll(self) -> None:
        """Rare safety-net list. Enqueues any pending blobs not already handled."""
        blobs = self._list_pending_blobs()
        if not blobs:
            return
        log.info("FileWatcher fallback: %d pending blob(s).", len(blobs))
        for blob in blobs:
            if self._stop_event.is_set():
                return
            if self._already_processed(blob.get("pathname", "")):
                continue
            self._queue.put(blob)

    # ------------------------------------------------------------------
    # Vercel Blob REST API
    # ------------------------------------------------------------------

    def _list_pending_blobs(self) -> list[dict]:
        """List blobs under the 'incoming/' prefix via the Vercel Blob REST API."""
        headers = {"authorization": f"Bearer {self._token}"}
        params = {"prefix": "incoming/", "limit": "100"}
        out: list[dict] = []
        cursor: Optional[str] = None
        with httpx.Client(timeout=_TIMEOUT) as client:
            for _ in range(10):  # paginate, cap at 10 pages
                if cursor:
                    params["cursor"] = cursor
                resp = client.get(_BLOB_BASE, headers=headers, params=params)
                if resp.status_code != 200:
                    log.error("Blob list failed: HTTP %d %s", resp.status_code, resp.text[:200])
                    return out
                data = resp.json()
                out.extend(data.get("blobs", []))
                cursor = data.get("cursor")
                if not data.get("hasMore") or not cursor:
                    break
        return out

    def _download_blob(self, blob: dict, dest: Path) -> None:
        url = blob.get("url") or blob.get("downloadUrl")
        if not url:
            raise printer_uploader.UploadError("blob has no download url")
        with httpx.Client(timeout=_TIMEOUT) as client:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                with dest.open("wb") as fh:
                    for chunk in resp.iter_bytes(chunk_size=_CHUNK):
                        fh.write(chunk)

    def _delete_blob(self, blob: dict) -> None:
        url = blob.get("url")
        if not url:
            return
        headers = {"authorization": f"Bearer {self._token}", "content-type": "application/json"}
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.request(
                "DELETE", _BLOB_BASE, headers=headers, json={"urls": [url]}
            )
        if resp.status_code not in (200, 204):
            log.warning("Blob delete returned HTTP %d for %s", resp.status_code, url)

    # ------------------------------------------------------------------
    # Per-blob processing
    # ------------------------------------------------------------------

    def _process_blob(self, blob: dict) -> None:
        pathname: str = blob.get("pathname", "")
        blob_key = pathname

        # Skip if we've already handled this blob (it might still be listing
        # before the delete propagates).
        if self._already_processed(blob_key):
            return

        name, target_serial, original_filename = self._parse_pathname(pathname)
        log.info(
            "Processing blob %s: name=%s target=%s file=%s",
            pathname, name, target_serial, original_filename,
        )

        renamed = _normalize_gcode_name(f"{name}_{original_filename}")
        local_path = self._incoming / f"{uuid.uuid4().hex}_{renamed}"

        upload_id = str(uuid.uuid4())
        received_at = _now()
        db.create_upload(
            upload_id=upload_id,
            blob_key=blob_key,
            student_name=name,
            original_filename=original_filename,
            renamed_filename=renamed,
            target_printer=target_serial,
            received_at=received_at,
            upload_status="pending",
        )

        try:
            self._download_blob(blob, local_path)

            monitor = self._resolve_monitor(target_serial)
            if monitor is None:
                raise printer_uploader.UploadError(
                    f"no available printer for target '{target_serial}'"
                )

            printer_uploader.upload_file(
                printer_ip=monitor.hostname,
                access_code=monitor._access_code,  # noqa: SLF001
                local_path=local_path,
                remote_filename=renamed,
                auto_start=False,
            )

            db.mark_uploaded(
                upload_id=upload_id,
                printer_serial=monitor.serial,
                uploaded_at=_now(),
                status="done",
            )
            self._delete_blob(blob)
            # Archive the local copy into the per-student inbox subfolder.
            student_folder = self._inbox / _sanitize(name)
            student_folder.mkdir(parents=True, exist_ok=True)
            archive = student_folder / renamed
            local_path.replace(archive)
            log.info(
                "Uploaded %s -> %s and archived to %s", renamed, monitor.name, archive
            )

        except Exception as exc:
            log.error("Upload failed for %s: %s", pathname, exc)
            db.mark_uploaded(
                upload_id=upload_id,
                printer_serial=None,
                uploaded_at=_now(),
                status="failed",
                error_message=str(exc),
            )
            if local_path.exists():
                try:
                    local_path.unlink()
                except OSError:
                    pass
            # Blob is left in place for a future retry pass.

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_pathname(pathname: str) -> tuple[str, str, str]:
        """
        Decode 'incoming/<name>__<targetSerial>__<originalFilename>'.

        Missing parts fall back to sensible defaults.
        """
        body = pathname
        if body.startswith("incoming/"):
            body = body[len("incoming/"):]
        parts = body.split("__", 2)
        if len(parts) == 3:
            name, target, filename = parts
        elif len(parts) == 2:
            name, filename = parts
            target = "any"
        else:
            name = parts[0] if parts else "anon"
            target = "any"
            filename = "submission.gcode.3mf"
        return name or "anon", (target or "any"), (filename or "submission.gcode.3mf")

    def _already_processed(self, blob_key: str) -> bool:
        rows = db.list_recent_uploads(limit=500)
        for row in rows:
            if row["blob_key"] == blob_key and row["upload_status"] in ("done", "uploading"):
                return True
        return False

    def _resolve_monitor(self, target_serial: str) -> Optional[PrinterMonitor]:
        monitors = list(self._get_monitors())
        if not monitors:
            return None
        if target_serial and target_serial != "any":
            for m in monitors:
                if m.serial == target_serial:
                    return m
            # fall through to auto-pick if the requested one vanished
        # Auto-pick: prefer an idle/finished printer, else the first one.
        for m in monitors:
            snap = m.snapshot()
            state = (snap.get("state") or "").upper()
            if state in ("", "IDLE", "FINISH", "FAILED"):
                return m
        return monitors[0]


__all__ = ["FileWatcher"]
