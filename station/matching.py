"""
matching.py — Pure helpers for matching an uploaded file to the print job that ran it.

No database access here, so the daemon (database.find_upload_for_job), the
station API and the offline audit (analysis/audit.py) all share exactly the same
matching rules.

A student upload is renamed by the file watcher to
``<netid>_<original>.gcode.3mf``. When that file is printed from Bambu Farm
Manager, the printer reports a ``subtask_name`` that is (usually) the file name
without its extension. Matching levels, strongest first:

  exact       identical after stripping the extension
  normalized  identical after lower-casing and dropping every non-alphanumeric
              character (handles spaces vs underscores, case, punctuation)
  ambiguous   the best level matched uploads from DIFFERENT students
  none        no match

Time windows (a job can only run a file that already exists):
  * the job must start no earlier than ``START_SLACK`` before the upload arrived
    (clock skew tolerance)
  * ``exact`` matches may start up to ``EXACT_WINDOW`` after the upload
  * ``normalized`` matches are weaker evidence, so only ``NORMALIZED_WINDOW``
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, NamedTuple, Optional

# Longest extensions first so ".gcode.3mf" wins over ".3mf".
_EXTENSIONS = (".gcode.3mf", ".3mf.gcode", ".3mf", ".gcode")

# Jobs the printer starts by itself (calibration, HMS checks, ...). They never
# correspond to a student upload and must be excluded from analysis.
_CALIBRATION_PREFIXES = ("auto_hms", "auto_cali", "auto_bed_level")

EXACT = "exact"
NORMALIZED = "normalized"
AMBIGUOUS = "ambiguous"
NONE = "none"

START_SLACK = timedelta(hours=1)
EXACT_WINDOW = timedelta(days=30)
NORMALIZED_WINDOW = timedelta(hours=48)


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

def strip_ext(name: Optional[str]) -> str:
    """Remove a trailing .gcode.3mf / .3mf / .gcode (case-insensitive)."""
    stem = (name or "").strip()
    lower = stem.lower()
    for ext in _EXTENSIONS:
        if lower.endswith(ext):
            return stem[: -len(ext)]
    return stem


def normalize_name(name: Optional[str]) -> str:
    """Lower-case and keep only letters/digits, after dropping the extension."""
    return re.sub(r"[^a-z0-9]", "", strip_ext(name).lower())


def normalize_netid(netid: Optional[str]) -> str:
    """
    Canonical form of a student identifier: lower-case, with anything outside
    ``[a-z0-9._-]`` collapsed to ``_`` — the same shape the website produces
    when it builds the upload pathname — and capped at 64 characters.
    """
    cleaned = re.sub(r"[^a-z0-9._-]+", "_", (netid or "").strip().lower()).strip("_")
    return cleaned[:64]


def sanitize_name(value: str) -> str:
    """
    Make a string safe to use as a file/folder name: keep letters, digits and
    ``-._``, replace everything else with ``_`` ("anon" if nothing is left).
    The inbox folder for a student is ``sanitize_name(student_name)``. Names made
    only of dots become "anon" so they can never traverse out of the inbox.
    """
    cleaned = "".join(c if c.isalnum() or c in "-._" else "_" for c in (value or "").strip())
    # "", ".", ".." and "..." are path components, not names: ".." would make the
    # inbox folder the inbox's PARENT (and Windows silently drops trailing dots).
    if not cleaned.strip("."):
        return "anon"
    return cleaned


def is_calibration_job(name: Optional[str]) -> bool:
    """True for printer-initiated jobs such as ``auto_hms_check_for_user.gcode``."""
    lower = (name or "").strip().lower()
    return lower.startswith(_CALIBRATION_PREFIXES)


def match_confidence(job_name: Optional[str], renamed_filename: Optional[str]) -> str:
    """Return ``exact``, ``normalized`` or ``none`` for a job/upload name pair."""
    if not job_name or not renamed_filename:
        return NONE
    if is_calibration_job(job_name):
        return NONE
    if strip_ext(job_name) == strip_ext(renamed_filename):
        return EXACT
    a, b = normalize_name(job_name), normalize_name(renamed_filename)
    if a and a == b:
        return NORMALIZED
    return NONE


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------

def parse_ts(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp to an aware UTC datetime (naive input is taken as UTC)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Join: job -> upload
# ---------------------------------------------------------------------------

class JobMatch(NamedTuple):
    """Result of matching one print job to the uploads table."""
    upload: Optional[Mapping[str, Any]]
    confidence: str

    @property
    def upload_id(self) -> Optional[str]:
        return self.upload["upload_id"] if self.upload is not None else None

    @property
    def student_name(self) -> Optional[str]:
        return self.upload["student_name"] if self.upload is not None else None


NO_MATCH = JobMatch(upload=None, confidence=NONE)


def candidate_uploads(
    job_name: Optional[str],
    job_start: Optional[datetime],
    uploads: Iterable[Mapping[str, Any]],
) -> list[tuple[str, Optional[datetime], Mapping[str, Any]]]:
    """
    Every upload that could plausibly have produced this job, as
    ``(level, received_at, upload)`` triples, in input order. Each upload mapping
    needs ``renamed_filename`` and ``received_at``.
    """
    if not job_name or is_calibration_job(job_name):
        return []
    out = []
    for up in uploads:
        level = match_confidence(job_name, up["renamed_filename"])
        if level == NONE:
            continue
        received = parse_ts(up["received_at"])
        if job_start is not None and received is not None:
            age = job_start - received
            if age < -START_SLACK:
                continue  # the job started before the file existed
            window = EXACT_WINDOW if level == EXACT else NORMALIZED_WINDOW
            if age > window:
                continue
        out.append((level, received, up))
    return out


def pick_upload(
    job_name: Optional[str],
    job_start: Optional[datetime],
    uploads: Iterable[Mapping[str, Any]],
) -> JobMatch:
    """
    Choose the upload a job most likely ran: strongest level first, then the most
    recent upload. If that level matched uploads from different students the
    result is ``ambiguous`` (the most recent one is still returned).

    The same student re-uploading an identical filename is NOT ambiguous — the
    latest upload simply wins.
    """
    cands = candidate_uploads(job_name, job_start, uploads)
    if not cands:
        return NO_MATCH
    best_level = EXACT if any(c[0] == EXACT for c in cands) else NORMALIZED
    top = [c for c in cands if c[0] == best_level]
    top.sort(key=lambda c: c[1].timestamp() if c[1] else float("-inf"), reverse=True)
    students = {normalize_netid(c[2]["student_name"]) for c in top}
    confidence = AMBIGUOUS if len(students) > 1 else best_level
    return JobMatch(upload=top[0][2], confidence=confidence)
