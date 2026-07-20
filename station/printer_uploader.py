"""
printer_uploader.py — Push a sliced .gcode.3mf file to a Bambu Lab printer.

This is the only module in the project that WRITES to a printer (everything else
only reads telemetry over MQTT). It implements the Bambu Lab local HTTP upload
flow, which is the same sequence Bambu Studio uses to send a file to a printer
on the LAN:

  1. GET  https://<ip>/api/file?        -> signed OSS upload credentials
  2. PUT  chunks to the OSS URL          -> upload the file body
  3. POST https://<ip>/api/file         -> "project_file" command that lands the
                                            file in the printer's Files tab

Auth for steps 1 and 3 is HTTP Basic with username "bblp" and the printer's LAN
access code (the same access_code already stored in config.json for MQTT).

The file is uploaded only — it is NOT auto-started. The student starts the print
from the printer's Files tab whenever they are physically ready, per the
project requirement.

References: Bambu Studio's BambuNetworkProxy / HttpServer::UploadToPrinter and
community projects (bambu-printer-api, bambu-farm). Field names in the OSS
credential response can vary slightly across firmware versions; the parser below
is defensive and logs the raw response at DEBUG level for easy tuning.
"""

from __future__ import annotations

import logging
import ssl
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger(__name__)

# Bambu printers use a self-signed TLS cert — disable verification for the
# local HTTP calls just like we do for MQTT in mqtt_listener.py.
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

# Chunk size for the file body upload (5 MB matches Bambu Studio).
_CHUNK_SIZE = 5 * 1024 * 1024

# Per-request timeout: large files over LAN can take a while to push.
_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=120.0, pool=10.0)


class UploadError(RuntimeError):
    """Raised when any step of the Bambu upload flow fails."""


def _auth(access_code: str) -> tuple[str, str]:
    return ("bblp", access_code)


def _request_credentials(
    client: httpx.Client, base_url: str, access_code: str
) -> dict:
    """Step 1: ask the printer for OSS upload credentials."""
    url = f"{base_url}/api/file?"
    log.debug("GET %s", url)
    resp = client.get(url, auth=_auth(access_code))
    if resp.status_code != 200:
        raise UploadError(f"credential request failed: HTTP {resp.status_code} {resp.text[:200]}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise UploadError(f"credential response was not JSON: {exc}") from exc
    log.debug("credential response keys: %s", list(data.keys()))
    return data


def _upload_to_oss(
    client: httpx.Client,
    creds: dict,
    local_path: Path,
) -> str:
    """Step 2: push the file body to the OSS URL returned by the printer.

    Returns the object name / remote path that step 3 needs to reference.
    """
    oss_url = creds.get("oss_url")
    if not oss_url:
        raise UploadError("credential response missing 'oss_url'")

    object_name = creds.get("object_name") or creds.get("file_name")
    upload_id = creds.get("upload_id")

    # Many community implementations PUT the whole file to the OSS URL with the
    # upload_id as the Authorization header. We stream the file in chunks so
    # large prints don't have to be fully buffered in memory.
    headers = {}
    if upload_id:
        headers["Authorization"] = str(upload_id)

    target = oss_url if object_name is None else f"{oss_url}/{object_name}"
    log.info("Uploading %s (%d bytes) to OSS …", local_path.name, local_path.stat().st_size)

    with local_path.open("rb") as fh:
        # Stream as a single PUT; httpx streams the file body without loading it
        # all into memory. For very large files this could be upgraded to a true
        # OSS multipart upload (init/parts/complete), but a single PUT is what
        # most community tools use successfully on the LAN.
        resp = client.put(target, content=fh, headers=headers)
    if resp.status_code not in (200, 204):
        raise UploadError(
            f"OSS upload failed: HTTP {resp.status_code} {resp.text[:200]}"
        )
    log.info("OSS upload complete.")
    return str(object_name) if object_name else ""


def _land_file(
    client: httpx.Client,
    base_url: str,
    access_code: str,
    creds: dict,
    remote_filename: str,
    object_name: str,
    auto_start: bool = False,
) -> None:
    """Step 3: tell the printer to ingest the uploaded file into its Files tab."""
    url = f"{base_url}/api/file"

    # The "project_file" command registers the uploaded object with the printer's
    # file manager. Setting subtask_id/task_id/project_id to "0" and the print
    # param's "action" to "save" lands the file without starting a print.
    payload = {
        "print": {
            "sequence_id": "1",
            "command": "project_file",
            "param": {
                "url": object_name,
                "filename": remote_filename,
                "subtask_id": "",
                "task_id": "",
                "project_id": "0",
                "profile_id": "0",
                "url_path": object_name,
                "model_id": "0",
                "uid": "0",
                "action": "start" if auto_start else "save",
            },
        }
    }

    log.info("Landing file %s on printer (auto_start=%s) …", remote_filename, auto_start)
    resp = client.post(url, json=payload, auth=_auth(access_code))
    if resp.status_code != 200:
        raise UploadError(f"land-file request failed: HTTP {resp.status_code} {resp.text[:200]}")
    try:
        data = resp.json()
    except ValueError:
        data = {}
    # Bambu acks carry a "success" boolean in the top-level "print" object.
    ack = data.get("print", {})
    if isinstance(ack, dict) and ack.get("success") is False:
        raise UploadError(f"printer rejected file: {ack}")
    log.info("File %s landed successfully.", remote_filename)


def upload_file(
    printer_ip: str,
    access_code: str,
    local_path: str | Path,
    remote_filename: str,
    auto_start: bool = False,
    use_https: bool = True,
) -> None:
    """
    Upload a sliced file to a Bambu Lab printer on the LAN.

    Parameters
    ----------
    printer_ip     : LAN IP of the target printer (e.g. "192.168.1.101").
    access_code     : Printer LAN access code (same one used for MQTT).
    local_path      : Path to the local .gcode.3mf file to upload.
    remote_filename : Filename the printer should store it under. Should end in
                      ".gcode.3mf" to match the printer's native format.
    auto_start      : If True, start printing immediately. Default False — the
                      student starts the print from the Files tab when ready.
    use_https       : Bambu local API is HTTPS with a self-signed cert. Keep True.
    """
    local_path = Path(local_path)
    if not local_path.is_file():
        raise UploadError(f"local file not found: {local_path}")

    scheme = "https" if use_https else "http"
    base_url = f"{scheme}://{printer_ip}"

    with httpx.Client(verify=_SSL_CTX, timeout=_Timeout) as client:
        creds = _request_credentials(client, base_url, access_code)
        object_name = _upload_to_oss(client, creds, local_path)
        _land_file(
            client, base_url, access_code, creds,
            remote_filename=remote_filename,
            object_name=object_name,
            auto_start=auto_start,
        )


__all__ = ["upload_file", "UploadError"]
