"""
camera.py — Fetch a JPEG snapshot from a Bambu Lab printer on the LAN.

Two strategies, tried in order:
  1. If the monitor captured an ipcam URL over MQTT, proxy that URL.
  2. Otherwise fall back to the printer's HTTP snapshot endpoint
     (https://<ip>/snapshot/recent_request) using bblp/<access_code> basic auth.

Both use the same self-signed TLS context as the rest of the project. Returns
the raw JPEG bytes (content-type image/jpeg) or None if the printer has no
reachable camera.
"""

from __future__ import annotations

import logging
import ssl
from typing import Optional

import httpx

log = logging.getLogger(__name__)

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)


def fetch_snapshot(
    printer_ip: str,
    access_code: str,
    camera_url: Optional[str] = None,
) -> Optional[bytes]:
    """
    Return JPEG bytes for the printer's current camera frame, or None.

    Parameters
    ----------
    printer_ip    : LAN IP of the printer.
    access_code    : LAN access code (basic auth password, username "bblp").
    camera_url     : Optional URL reported by the printer over MQTT (ipcam topic).
                     If provided and reachable, it is preferred over the snapshot
                     endpoint.
    """
    auth = ("bblp", access_code)

    candidates = []
    if camera_url:
        candidates.append(camera_url)
    candidates.append(f"https://{printer_ip}/snapshot/recent_request")

    with httpx.Client(verify=_SSL_CTX, timeout=_TIMEOUT, auth=auth) as client:
        for url in candidates:
            try:
                resp = client.get(url)
            except httpx.HTTPError as exc:
                log.debug("camera fetch failed for %s: %s", url, exc)
                continue
            if resp.status_code != 200:
                log.debug("camera %s returned HTTP %d", url, resp.status_code)
                continue
            ctype = resp.headers.get("content-type", "")
            if "image" not in ctype and "jpeg" not in ctype and "octet-stream" not in ctype:
                log.debug("camera %s returned non-image content-type %s", url, ctype)
                continue
            return resp.content

    log.debug("No reachable camera for printer %s", printer_ip)
    return None


__all__ = ["fetch_snapshot"]
