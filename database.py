"""
database.py — SQLite helpers for the MakerLAB print-failure dataset.

All SQL lives here; no other module should touch the database directly.
"""

import sqlite3
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "print_dataset.db"

_CREATE_PRINT_JOBS = """
CREATE TABLE IF NOT EXISTS print_jobs (
    job_id          TEXT PRIMARY KEY,
    printer_serial  TEXT NOT NULL,
    printer_ip      TEXT NOT NULL,
    subtask_name    TEXT,
    gcode_file      TEXT,
    start_time      TEXT,
    end_time        TEXT,
    final_state     TEXT,
    print_error     INTEGER,
    nozzle_temper   REAL,
    bed_temper      REAL,
    filament_type   TEXT,
    user_label      INTEGER,
    label_time      TEXT,
    layer_height    REAL,
    infill_density  REAL,
    wall_loops      INTEGER
);
"""

_CREATE_UPLOADS = """
CREATE TABLE IF NOT EXISTS uploads (
    upload_id           TEXT PRIMARY KEY,
    blob_key            TEXT NOT NULL,
    student_name        TEXT,
    original_filename   TEXT,
    renamed_filename    TEXT,
    target_printer      TEXT,
    printer_serial      TEXT,
    upload_status       TEXT NOT NULL,   -- 'pending' | 'uploading' | 'done' | 'failed'
    error_message       TEXT,
    received_at         TEXT,
    uploaded_at         TEXT
);
"""


def _connect() -> sqlite3.Connection:
    # timeout: how long sqlite waits on a locked DB before raising.
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    # WAL lets multiple printer threads write concurrently without "database is
    # locked" errors; busy_timeout is a second safety net at the engine level.
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def init_db() -> None:
    """Create the database and tables if they don't already exist, and apply
    lightweight column migrations for databases created by older versions."""
    with _connect() as conn:
        conn.execute(_CREATE_PRINT_JOBS)
        conn.execute(_CREATE_UPLOADS)
        _migrate(conn)
    log.info("Database initialised at %s", DB_PATH)


def _migrate(conn: sqlite3.Connection) -> None:
    """Add any columns missing from older databases (idempotent)."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(print_jobs)")}
    expected = {
        "print_error": "INTEGER",
    }
    for col, col_type in expected.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE print_jobs ADD COLUMN {col} {col_type}")
            log.info("Migrated database: added column %s", col)


def create_job(
    job_id: str,
    printer_serial: str,
    printer_ip: str,
    start_time: str,
    subtask_name: Optional[str] = None,
    gcode_file: Optional[str] = None,
    nozzle_temper: Optional[float] = None,
    bed_temper: Optional[float] = None,
    filament_type: Optional[str] = None,
) -> None:
    """Insert a new print job row when the printer transitions to RUNNING."""
    sql = """
        INSERT OR IGNORE INTO print_jobs
            (job_id, printer_serial, printer_ip, start_time,
             subtask_name, gcode_file, nozzle_temper, bed_temper, filament_type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    with _connect() as conn:
        conn.execute(sql, (
            job_id, printer_serial, printer_ip, start_time,
            subtask_name, gcode_file, nozzle_temper, bed_temper, filament_type,
        ))
    log.info("[%s] Job created: %s  file=%s", printer_serial, job_id, subtask_name)


def update_job_end(
    job_id: str,
    end_time: str,
    final_state: str,
    subtask_name: Optional[str] = None,
    gcode_file: Optional[str] = None,
    nozzle_temper: Optional[float] = None,
    bed_temper: Optional[float] = None,
    filament_type: Optional[str] = None,
    print_error: Optional[int] = None,
) -> None:
    """
    Fill in end-of-print fields when the printer transitions to FINISH or FAILED.

    Non-None values for the metadata fields overwrite whatever was captured at
    job creation, ensuring the most-recent telemetry snapshot is stored.
    """
    # Build a dynamic SET clause so we only overwrite columns that have a value.
    updates = {"end_time": end_time, "final_state": final_state}
    if subtask_name is not None:
        updates["subtask_name"] = subtask_name
    if gcode_file is not None:
        updates["gcode_file"] = gcode_file
    if nozzle_temper is not None:
        updates["nozzle_temper"] = nozzle_temper
    if bed_temper is not None:
        updates["bed_temper"] = bed_temper
    if filament_type is not None:
        updates["filament_type"] = filament_type
    if print_error is not None:
        updates["print_error"] = print_error

    set_clause = ", ".join(f"{col} = ?" for col in updates)
    values = list(updates.values()) + [job_id]

    sql = f"UPDATE print_jobs SET {set_clause} WHERE job_id = ?"
    with _connect() as conn:
        conn.execute(sql, values)
    log.info("Job ended: %s  state=%s", job_id, final_state)


def write_label(job_id: str, user_label: int, label_time: str) -> None:
    """
    Write the staff label for a completed print job.

    Parameters
    ----------
    job_id      : UUID of the job row.
    user_label  : 0 = print succeeded, 1 = print failed.
    label_time  : ISO-8601 timestamp when the staff clicked the button.
    """
    sql = """
        UPDATE print_jobs
        SET user_label = ?, label_time = ?
        WHERE job_id = ?
    """
    with _connect() as conn:
        conn.execute(sql, (user_label, label_time, job_id))
    log.info("Label written: job=%s  label=%d", job_id, user_label)


def get_unlabeled_completed_jobs() -> list[sqlite3.Row]:
    """
    Return all jobs that have ended (final_state set) but were never labeled.

    Used at startup to re-prompt for any print whose popup was missed because the
    daemon was closed or crashed while the popup was open.
    """
    sql = """
        SELECT job_id, printer_serial, subtask_name, final_state
        FROM print_jobs
        WHERE final_state IS NOT NULL AND user_label IS NULL
        ORDER BY end_time ASC
    """
    with _connect() as conn:
        return conn.execute(sql).fetchall()


# ---------------------------------------------------------------------------
# uploads table — student-submitted files pulled from Vercel Blob
# ---------------------------------------------------------------------------

def create_upload(
    upload_id: str,
    blob_key: str,
    student_name: Optional[str],
    original_filename: Optional[str],
    renamed_filename: Optional[str],
    target_printer: Optional[str],
    received_at: str,
    upload_status: str = "pending",
) -> None:
    """Insert a row for a file the station just pulled from Vercel Blob."""
    sql = """
        INSERT OR IGNORE INTO uploads
            (upload_id, blob_key, student_name, original_filename,
             renamed_filename, target_printer, upload_status, received_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """
    with _connect() as conn:
        conn.execute(sql, (
            upload_id, blob_key, student_name, original_filename,
            renamed_filename, target_printer, upload_status, received_at,
        ))
    log.info("Upload recorded: %s  blob=%s  file=%s", upload_id, blob_key, renamed_filename)


def mark_uploaded(
    upload_id: str,
    printer_serial: Optional[str],
    uploaded_at: str,
    status: str = "done",
    error_message: Optional[str] = None,
) -> None:
    """Update an upload row after the printer upload succeeded or failed."""
    sql = """
        UPDATE uploads
        SET printer_serial = ?, uploaded_at = ?, upload_status = ?, error_message = ?
        WHERE upload_id = ?
    """
    with _connect() as conn:
        conn.execute(sql, (printer_serial, uploaded_at, status, error_message, upload_id))
    log.info("Upload %s marked %s on %s", upload_id, status, printer_serial or "n/a")


def list_recent_uploads(limit: int = 50) -> list[sqlite3.Row]:
    """Return the most recent upload rows (for a future station UI / log view)."""
    sql = """
        SELECT upload_id, student_name, original_filename, renamed_filename,
               target_printer, printer_serial, upload_status, error_message,
               received_at, uploaded_at
        FROM uploads
        ORDER BY received_at DESC
        LIMIT ?
    """
    with _connect() as conn:
        return conn.execute(sql, (limit,)).fetchall()
