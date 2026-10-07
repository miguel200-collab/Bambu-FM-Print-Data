"""
database.py — SQLite helpers for the MakerLAB print-failure dataset.

All SQL lives here; no other module should touch the database directly.

Migrations are strictly ADDITIVE (new columns/tables only — never drop, rename
or rewrite existing data), because print_dataset.db exists only on the lab laptop.

Data eras
---------
Every print job and upload is stamped with an ``era``:

  era 1  everything collected before the "Label my print" web page went live.
         Its popup labels were routinely mass-clicked, so it is quarantined:
         used for the audit write-up and pipeline tests, never for training.
  era 2  everything collected after launch (human labels via the web page, plus
         the printer's own verdict).

Old rows are backfilled to era 1 when the column is first added. New rows get the
era set with ``set_current_era`` (main.py reads it from config ``labels.era``);
it defaults to 1 so nothing is ever counted as "clean" by accident.
"""

import json
import sqlite3
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from station import families
from station.gcode_features import CURATED_COLUMNS, EXTRACTOR_VERSION
from station.matching import JobMatch, parse_ts, pick_upload, is_calibration_job, normalize_netid

log = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "print_dataset.db"

# Era stamped on newly created rows (see module docstring).
_current_era = 1

# A finished print can be labeled for this long after it ended.
LABEL_WINDOW_DAYS = 14

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
    label_source    TEXT,            -- 'popup' | 'web' | 'email'
    era             INTEGER,         -- 1 = quarantined, 2 = clean era
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
    uploaded_at         TEXT,
    era                 INTEGER,
    -- identity of the submitted file (filled by the feature pipeline)
    file_sha256         TEXT,            -- exact same file
    gcode_md5           TEXT,            -- same sliced toolpath
    design_family_id    TEXT,            -- "same thing, tweaked" (per student)
    attempt_index       INTEGER,         -- 1 = first upload of the family
    config_diff         TEXT             -- JSON: settings changed vs. previous attempt
);
"""

_CREATE_DESIGN_FAMILIES = """
CREATE TABLE IF NOT EXISTS design_families (
    family_id        TEXT PRIMARY KEY,
    student_name     TEXT NOT NULL,      -- normalized NetID: families never span students
    created_at       TEXT NOT NULL,
    last_upload_at   TEXT NOT NULL,
    fingerprint_json TEXT NOT NULL,      -- names/area/bbox/max_z of the LATEST upload
    upload_count     INTEGER NOT NULL
);
"""


def _create_file_features_sql() -> str:
    # One row per distinct file (keyed by sha256); the promoted columns come from
    # gcode_features.CURATED_COLUMNS, everything else lives in raw_json.
    cols = ",\n    ".join(f"{name} {sql_type}" for name, sql_type in CURATED_COLUMNS.items())
    return f"""
CREATE TABLE IF NOT EXISTS file_features (
    file_sha256        TEXT PRIMARY KEY,
    gcode_md5          TEXT,
    extractor_version  INTEGER NOT NULL,
    extracted_at       TEXT NOT NULL,
    is_sliced          INTEGER NOT NULL,
    error              TEXT,
    plate_count        INTEGER,
    {cols},
    raw_json           TEXT
);
"""

# Columns added after the first release. (table -> {column: sqlite type})
_COLUMN_MIGRATIONS: dict[str, dict[str, str]] = {
    "print_jobs": {
        "print_error": "INTEGER",
        "label_source": "TEXT",
        "era": "INTEGER",
    },
    "uploads": {
        "era": "INTEGER",
        "file_sha256": "TEXT",
        "gcode_md5": "TEXT",
        "design_family_id": "TEXT",
        "attempt_index": "INTEGER",
        "config_diff": "TEXT",
    },
    "file_features": dict(CURATED_COLUMNS),
}

# One-off backfills, run ONLY at the moment the column is first added, so they
# can never overwrite data written later.
_BACKFILLS: dict[tuple[str, str], str] = {
    ("print_jobs", "label_source"):
        "UPDATE print_jobs SET label_source = 'popup' WHERE user_label IS NOT NULL",
    ("print_jobs", "era"): "UPDATE print_jobs SET era = 1",
    ("uploads", "era"): "UPDATE uploads SET era = 1",
}


def set_current_era(era: int) -> None:
    """Set the era stamped on newly created jobs/uploads (from config ``labels.era``)."""
    global _current_era
    _current_era = int(era)
    log.info("Data era for new rows: %d", _current_era)


def get_current_era() -> int:
    return _current_era


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
        conn.execute(_CREATE_DESIGN_FAMILIES)
        conn.execute(_create_file_features_sql())
        _migrate(conn)
    log.info("Database initialised at %s", DB_PATH)


def _migrate(conn: sqlite3.Connection) -> None:
    """Add any columns missing from older databases (idempotent, additive only)."""
    for table, wanted in _COLUMN_MIGRATIONS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for col, col_type in wanted.items():
            if col in existing:
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
            log.info("Migrated database: added column %s.%s", table, col)
            backfill = _BACKFILLS.get((table, col))
            if backfill:
                conn.execute(backfill)
                log.info("Migrated database: backfilled %s.%s", table, col)


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
             subtask_name, gcode_file, nozzle_temper, bed_temper, filament_type, era)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    with _connect() as conn:
        conn.execute(sql, (
            job_id, printer_serial, printer_ip, start_time,
            subtask_name, gcode_file, nozzle_temper, bed_temper, filament_type,
            _current_era,
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


def get_job(job_id: str) -> Optional[sqlite3.Row]:
    """Return one print job row, or None."""
    with _connect() as conn:
        return conn.execute("SELECT * FROM print_jobs WHERE job_id = ?", (job_id,)).fetchone()


def write_label(job_id: str, user_label: int, label_time: str, source: str) -> bool:
    """
    Write the human label for a completed print job.

    Parameters
    ----------
    job_id      : UUID of the job row.
    user_label  : 0 = print succeeded, 1 = print failed.
    label_time  : ISO-8601 timestamp when the label was given.
    source      : where the label came from: 'popup' | 'web' | 'email'.

    Returns True if a job row was updated, False if the job_id doesn't exist.
    """
    sql = """
        UPDATE print_jobs
        SET user_label = ?, label_time = ?, label_source = ?
        WHERE job_id = ?
    """
    with _connect() as conn:
        updated = conn.execute(sql, (user_label, label_time, source, job_id)).rowcount
    log.info("Label written: job=%s  label=%d  source=%s  updated=%d",
             job_id, user_label, source, updated)
    return updated > 0


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
    era: Optional[int] = None,
) -> None:
    """Insert a row for a file the station just pulled from Vercel Blob.

    ``era`` defaults to the current era; the backfill passes 1 explicitly.
    """
    sql = """
        INSERT OR IGNORE INTO uploads
            (upload_id, blob_key, student_name, original_filename,
             renamed_filename, target_printer, upload_status, received_at, era)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    with _connect() as conn:
        conn.execute(sql, (
            upload_id, blob_key, student_name, original_filename,
            renamed_filename, target_printer, upload_status, received_at,
            _current_era if era is None else era,
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
        SELECT upload_id, blob_key, student_name, original_filename, renamed_filename,
               target_printer, printer_serial, upload_status, error_message,
               received_at, uploaded_at, era, file_sha256, gcode_md5,
               design_family_id, attempt_index
        FROM uploads
        ORDER BY received_at DESC
        LIMIT ?
    """
    with _connect() as conn:
        return conn.execute(sql, (limit,)).fetchall()


# ---------------------------------------------------------------------------
# Join: print job <-> upload   (rules live in station/matching.py)
# ---------------------------------------------------------------------------

_UPLOAD_MATCH_COLUMNS = "upload_id, student_name, renamed_filename, received_at"


def _done_uploads(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    # Only uploads that were fully archived can have been printed from the inbox.
    return conn.execute(
        f"SELECT {_UPLOAD_MATCH_COLUMNS} FROM uploads WHERE upload_status = 'done'"
    ).fetchall()


def find_upload_for_job(
    subtask_name: Optional[str], start_time: Optional[str]
) -> JobMatch:
    """
    Find the upload a print job most likely ran, by filename + time window.

    Returns a ``JobMatch`` whose ``confidence`` is ``exact`` / ``normalized`` /
    ``ambiguous`` / ``none`` (see station/matching.py). Printer-initiated
    calibration jobs never match.
    """
    with _connect() as conn:
        uploads = _done_uploads(conn)
    return pick_upload(subtask_name, parse_ts(start_time), uploads)


def list_jobs_for_netid(
    netid: str, days: int = LABEL_WINDOW_DAYS, now: Optional[datetime] = None
) -> list[dict[str, Any]]:
    """
    A student's recently ENDED print jobs (newest first), found by joining each
    job to the uploads table by filename. Used by the "Label my print" page.
    """
    key = normalize_netid(netid)
    if not key:
        return []
    now = now or datetime.now(timezone.utc)
    cutoff = now.timestamp() - days * 86400

    with _connect() as conn:
        uploads = _done_uploads(conn)
        jobs = conn.execute(
            "SELECT * FROM print_jobs WHERE final_state IS NOT NULL ORDER BY end_time DESC"
        ).fetchall()

    out: list[dict[str, Any]] = []
    for job in jobs:
        ended = parse_ts(job["end_time"])
        if ended is None or ended.timestamp() < cutoff:
            continue
        if is_calibration_job(job["subtask_name"]):
            continue
        match = pick_upload(job["subtask_name"], parse_ts(job["start_time"]), uploads)
        if match.upload is None or normalize_netid(match.student_name) != key:
            continue
        out.append({
            "job_id": job["job_id"],
            "subtask_name": job["subtask_name"],
            "filename": match.upload["renamed_filename"],
            "printer_serial": job["printer_serial"],
            "start_time": job["start_time"],
            "end_time": job["end_time"],
            "final_state": job["final_state"],
            "user_label": job["user_label"],
            "label_source": job["label_source"],
            "match_confidence": match.confidence,
        })
    return out


# ---------------------------------------------------------------------------
# File features + design families (see station/gcode_features.py, families.py)
# ---------------------------------------------------------------------------

def get_file_features(file_sha256: str) -> Optional[dict[str, Any]]:
    """
    The stored extraction for a file, or None if it was never extracted (or was
    extracted by an older extractor version and should be redone).
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT extractor_version, raw_json FROM file_features WHERE file_sha256 = ?",
            (file_sha256,),
        ).fetchone()
    if row is None or row["extractor_version"] != EXTRACTOR_VERSION or not row["raw_json"]:
        return None
    return json.loads(row["raw_json"])


def _save_file_features(conn: sqlite3.Connection, file_sha256: str, features: dict[str, Any]) -> None:
    columns = list(CURATED_COLUMNS)
    curated = features.get("curated") or {}
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        f"""INSERT OR REPLACE INTO file_features
            (file_sha256, gcode_md5, extractor_version, extracted_at, is_sliced, error,
             plate_count, {", ".join(columns)}, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, {placeholders}, ?)""",
        (
            file_sha256,
            features.get("gcode_md5"),
            features.get("extractor_version", EXTRACTOR_VERSION),
            datetime.now(timezone.utc).isoformat(),
            1 if features.get("is_sliced") else 0,
            features.get("error"),
            features.get("plate_count"),
            *[curated.get(c) for c in columns],
            json.dumps(features, default=str),
        ),
    )


def _assign_family(
    conn: sqlite3.Connection, upload: sqlite3.Row, features: dict[str, Any]
) -> tuple[str, int, Optional[dict]]:
    """Put an upload into the student's closest design family (or a new one)."""
    student = normalize_netid(upload["student_name"])
    received = parse_ts(upload["received_at"]) or datetime.now(timezone.utc)
    fingerprint = families.fingerprint(features)

    candidates = []
    for row in conn.execute(
        "SELECT family_id, fingerprint_json, last_upload_at FROM design_families WHERE student_name = ?",
        (student,),
    ):
        last = parse_ts(row["last_upload_at"])
        if last is not None:
            candidates.append((row["family_id"], json.loads(row["fingerprint_json"]), last))

    family_id = families.choose_family(fingerprint, received, candidates)
    now_iso = received.isoformat()

    if family_id is None:
        family_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO design_families
               (family_id, student_name, created_at, last_upload_at, fingerprint_json, upload_count)
               VALUES (?, ?, ?, ?, ?, 1)""",
            (family_id, student, now_iso, now_iso, json.dumps(fingerprint)),
        )
        return family_id, 1, None

    previous = conn.execute(
        """SELECT u.attempt_index AS attempt_index, ff.raw_json AS raw_json
           FROM uploads u LEFT JOIN file_features ff ON ff.file_sha256 = u.file_sha256
           WHERE u.design_family_id = ? AND u.upload_id != ?
           ORDER BY u.attempt_index DESC, u.received_at DESC LIMIT 1""",
        (family_id, upload["upload_id"]),
    ).fetchone()
    attempt = ((previous["attempt_index"] or 0) + 1) if previous else 2
    diff: Optional[dict] = None
    if previous is not None and previous["raw_json"]:
        previous_config = json.loads(previous["raw_json"]).get("config") or {}
        diff = families.config_diff(previous_config, features.get("config") or {})

    conn.execute(
        """UPDATE design_families
           SET fingerprint_json = ?, upload_count = upload_count + 1,
               last_upload_at = CASE WHEN last_upload_at < ? THEN ? ELSE last_upload_at END
           WHERE family_id = ?""",
        (json.dumps(fingerprint), now_iso, now_iso, family_id),
    )
    return family_id, attempt, diff


def record_upload_features(
    upload_id: str, file_sha256: str, features: dict[str, Any]
) -> dict[str, Any]:
    """
    Attach an extraction to an upload: store the features (once per distinct
    file), record the file's sha256 / gcode md5 on the upload, and assign it to a
    design family. Idempotent: an upload that already has a family keeps it.

    Returns ``{"design_family_id", "attempt_index", "config_diff"}``.
    """
    with _connect() as conn:
        upload = conn.execute(
            """SELECT upload_id, student_name, received_at, design_family_id,
                      attempt_index, config_diff
               FROM uploads WHERE upload_id = ?""",
            (upload_id,),
        ).fetchone()
        if upload is None:
            raise KeyError(f"unknown upload: {upload_id}")

        _save_file_features(conn, file_sha256, features)

        if upload["design_family_id"] is not None:        # already assigned
            conn.execute(
                "UPDATE uploads SET file_sha256 = ?, gcode_md5 = ? WHERE upload_id = ?",
                (file_sha256, features.get("gcode_md5"), upload_id),
            )
            return {
                "design_family_id": upload["design_family_id"],
                "attempt_index": upload["attempt_index"],
                "config_diff": json.loads(upload["config_diff"]) if upload["config_diff"] else None,
            }

        family_id: Optional[str] = None
        attempt: Optional[int] = None
        diff: Optional[dict] = None
        if features.get("is_sliced") and not features.get("error"):
            family_id, attempt, diff = _assign_family(conn, upload, features)

        conn.execute(
            """UPDATE uploads
               SET file_sha256 = ?, gcode_md5 = ?, design_family_id = ?,
                   attempt_index = ?, config_diff = ?
               WHERE upload_id = ?""",
            (
                file_sha256, features.get("gcode_md5"), family_id, attempt,
                json.dumps(diff) if diff is not None else None, upload_id,
            ),
        )
    log.info("Upload %s: sha256=%s family=%s attempt=%s", upload_id, file_sha256[:12], family_id, attempt)
    return {"design_family_id": family_id, "attempt_index": attempt, "config_diff": diff}


def list_uploads_for_backfill() -> list[sqlite3.Row]:
    """All archived uploads (oldest first) with whether features were recorded yet."""
    with _connect() as conn:
        return conn.execute(
            """SELECT upload_id, student_name, renamed_filename, received_at, file_sha256
               FROM uploads WHERE upload_status = 'done'
               ORDER BY received_at ASC, upload_id ASC"""
        ).fetchall()
