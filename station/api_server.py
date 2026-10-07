"""
api_server.py — Small FastAPI server exposing live printer status + camera, and
the endpoints behind the website's "Label my print" page.

Endpoints
---------
GET  /api/health                  -> {"status": "ok", "printers": <count>}
GET  /api/printers                -> JSON array of per-printer snapshots
GET  /api/printer/<serial>/camera -> proxied JPEG from the printer (image/jpeg)
POST /api/blob-webhook            -> (x-station-key) enqueue an uploaded blob
GET  /api/my-prints?netid=        -> (x-station-key) a student's recent ended prints
POST /api/label-webhook           -> (x-station-key) record a human label for a print

The server holds a reference to the live list of PrinterMonitor instances owned
by main.py, so /api/printers always reflects current MQTT telemetry. Camera
fetches happen on the station (LAN-side) and are proxied out so the Vercel site
never needs direct LAN access to the printers.

The three x-station-key endpoints are only ever called by the Vercel *server*
(never the browser), so the shared secret never reaches a student. The two
labeling endpoints FAIL CLOSED: with no secret configured they answer 503
instead of running unauthenticated.

Run standalone for testing:
    uvicorn station.api_server:create_app --factory --port 8080
(In production main.py starts it in a daemon thread — see main.py.)
"""

from __future__ import annotations

import hmac
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Literal, Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import database as db
from mqtt_listener import PrinterMonitor
from station.camera import fetch_snapshot
from station.matching import normalize_netid, parse_ts

log = logging.getLogger(__name__)

# A "monitor registry" is anything that returns the current list of monitors.
# main.py passes a callable so the server always sees the live list even if it
# changes at runtime.
MonitorRegistry = Callable[[], Iterable[PrinterMonitor]]


class BlobWebhookPayload(BaseModel):
    """Body POSTed by the Vercel site after a successful upload to Blob."""
    pathname: str
    url: str
    # Signed URL for a private store (the canonical `url` is not fetchable without
    # auth). Optional — absent for public stores / older payloads.
    downloadUrl: Optional[str] = None


class LabelPayload(BaseModel):
    """Body POSTed by the Vercel site when a student labels a print."""
    job_id: str
    outcome: Literal["succeeded", "failed"]
    source: Literal["web", "email"] = "web"
    # Required for source="web": the NetID typed on the label page, which must
    # own the print. (Email links are authenticated by their signature instead.)
    netid: Optional[str] = None


def _key_ok(provided: str, secret: str) -> bool:
    # Compare as bytes: hmac.compare_digest raises TypeError on non-ASCII str.
    return hmac.compare_digest(provided.encode("utf-8"), secret.encode("utf-8"))


def create_app(
    get_monitors: MonitorRegistry,
    watcher: Optional[object] = None,
    webhook_secret: Optional[str] = None,
    email_labels_enabled: bool = False,
    now_fn: Optional[Callable[[], datetime]] = None,
) -> FastAPI:
    app = FastAPI(title="Bambu Farm Manager — Station API", version="1.0")
    now = now_fn or (lambda: datetime.now(timezone.utc))

    # The Vercel site lives on a different origin, so CORS is required for the
    # browser to call this API directly. Lock it down to the known site origin
    # in production via the CORS_ORIGINS env var (see main.py wiring).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # tightened by main.py via env in production
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    def require_key(request: Request) -> None:
        """Fail closed: no configured secret => 503, wrong/missing key => 401."""
        if not webhook_secret:
            raise HTTPException(status_code=503, detail="station key not configured")
        if not _key_ok(request.headers.get("x-station-key", ""), webhook_secret):
            raise HTTPException(status_code=401, detail="invalid station key")

    @app.get("/api/health")
    def health() -> dict:
        monitors = list(get_monitors())
        return {"status": "ok", "printers": len(monitors)}

    @app.get("/api/printers")
    def printers() -> list[dict]:
        return [m.snapshot() for m in get_monitors()]

    @app.get("/api/printer/{serial}/camera")
    def camera(serial: str) -> Response:
        monitor = _find_monitor(get_monitors, serial)
        if monitor is None:
            raise HTTPException(status_code=404, detail=f"unknown printer serial: {serial}")

        jpeg = fetch_snapshot(
            printer_ip=monitor.hostname,
            access_code=monitor._access_code,  # noqa: SLF001 — same package trust
            camera_url=monitor.camera_url(),
        )
        if jpeg is None:
            log.warning(
                "Camera 502 for %s: fetch_snapshot returned None "
                "(ip=%s, camera_url=%s)",
                serial, monitor.hostname, monitor.camera_url() or "none",
            )
            raise HTTPException(status_code=502, detail="camera not reachable")

        return Response(content=jpeg, media_type="image/jpeg")

    @app.post("/api/blob-webhook")
    def blob_webhook(payload: BlobWebhookPayload, request: Request) -> dict:
        """
        Notified by the Vercel site after a student upload lands in Blob.

        The site sends the blob's `pathname` and `url`; we enqueue it on the
        FileWatcher for download + processing. Auth is a shared secret sent in
        the `x-station-key` header (the tunnel is HTTPS, so it isn't sniffable).
        """
        if webhook_secret:
            if not _key_ok(request.headers.get("x-station-key", ""), webhook_secret):
                raise HTTPException(status_code=401, detail="invalid station key")
        if watcher is None:
            raise HTTPException(status_code=503, detail="file watcher not configured")

        blob = {"pathname": payload.pathname, "url": payload.url}
        if payload.downloadUrl:
            blob["downloadUrl"] = payload.downloadUrl
        watcher.enqueue_blob(blob)  # type: ignore[attr-defined]
        return {"ok": True, "enqueued": payload.pathname}

    @app.get("/api/my-prints")
    def my_prints(request: Request, netid: str = "") -> dict:
        """A student's recently ended prints, for the "Label my print" page."""
        require_key(request)
        key = normalize_netid(netid)
        if not key:
            raise HTTPException(status_code=400, detail="netid is required")

        names = {m.serial: m.name for m in get_monitors()}
        prints = db.list_jobs_for_netid(key, days=db.LABEL_WINDOW_DAYS, now=now())
        for p in prints:
            p["printer_name"] = names.get(p["printer_serial"], p["printer_serial"])
        return {"netid": key, "window_days": db.LABEL_WINDOW_DAYS, "prints": prints}

    @app.post("/api/label-webhook")
    def label_webhook(payload: LabelPayload, request: Request) -> dict:
        """Record a human label (succeeded / failed) for one ended print."""
        require_key(request)
        if payload.source == "email" and not email_labels_enabled:
            raise HTTPException(status_code=503, detail="email labeling is disabled")

        job = db.get_job(payload.job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown print")
        if job["final_state"] is None:
            raise HTTPException(status_code=409, detail="that print has not finished yet")

        ended = parse_ts(job["end_time"])
        if ended is None or now() - ended > timedelta(days=db.LABEL_WINDOW_DAYS):
            raise HTTPException(
                status_code=410,
                detail=f"labeling closes {db.LABEL_WINDOW_DAYS} days after a print ends",
            )

        match = db.find_upload_for_job(job["subtask_name"], job["start_time"])
        if payload.source == "web":
            claimed = normalize_netid(payload.netid)
            if not claimed:
                raise HTTPException(status_code=400, detail="netid is required")
            if match.upload is None or normalize_netid(match.student_name) != claimed:
                # Also covers printer-initiated jobs and prints with no matching
                # submission: only the student who uploaded a file may label it.
                raise HTTPException(status_code=403, detail="that print is not yours")
        elif match.upload is None:
            raise HTTPException(status_code=404, detail="no submission matches that print")

        label = 1 if payload.outcome == "failed" else 0
        if not db.write_label(payload.job_id, label, now().isoformat(), payload.source):
            raise HTTPException(status_code=404, detail="unknown print")
        return {"ok": True, "job_id": payload.job_id, "label": label, "source": payload.source}

    return app


def _find_monitor(
    get_monitors: MonitorRegistry, serial: str
) -> PrinterMonitor | None:
    for m in get_monitors():
        if m.serial == serial:
            return m
    return None


__all__ = ["create_app"]
