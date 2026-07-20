"""
api_server.py — Small FastAPI server exposing live printer status + camera.

Endpoints
---------
GET /api/health                -> {"status": "ok", "printers": <count>}
GET /api/printers              -> JSON array of per-printer snapshots
GET /api/printer/<serial>/camera -> proxied JPEG from the printer (image/jpeg)

The server holds a reference to the live list of PrinterMonitor instances owned
by main.py, so /api/printers always reflects current MQTT telemetry. Camera
fetches happen on the station (LAN-side) and are proxied out so the Vercel site
never needs direct LAN access to the printers.

Run standalone for testing:
    uvicorn station.api_server:create_app --factory --port 8080
(In production main.py starts it in a daemon thread — see main.py.)
"""

from __future__ import annotations

import logging
from typing import Callable, Iterable

from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware

from mqtt_listener import PrinterMonitor
from station.camera import fetch_snapshot

log = logging.getLogger(__name__)

# A "monitor registry" is anything that returns the current list of monitors.
# main.py passes a callable so the server always sees the live list even if it
# changes at runtime.
MonitorRegistry = Callable[[], Iterable[PrinterMonitor]]


def create_app(get_monitors: MonitorRegistry) -> FastAPI:
    app = FastAPI(title="Bambu Farm Manager — Station API", version="1.0")

    # The Vercel site lives on a different origin, so CORS is required for the
    # browser to call this API directly. Lock it down to the known site origin
    # in production via the CORS_ORIGINS env var (see main.py wiring).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # tightened by main.py via env in production
        allow_methods=["GET", "OPTIONS"],
        allow_headers=["*"],
    )

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
            raise HTTPException(status_code=502, detail="camera not reachable")

        return Response(content=jpeg, media_type="image/jpeg")

    return app


def _find_monitor(
    get_monitors: MonitorRegistry, serial: str
) -> PrinterMonitor | None:
    for m in get_monitors():
        if m.serial == serial:
            return m
    return None


__all__ = ["create_app"]
