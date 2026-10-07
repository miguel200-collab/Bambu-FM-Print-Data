"""
matching.py — Pure helpers for matching an uploaded file to the print job that ran it.

No database access here, so both the daemon (database.find_upload_for_job) and
the offline audit (analysis/audit.py) share exactly the same matching rules.

A student upload is renamed by the file watcher to
``<netid>_<original>.gcode.3mf``. When that file is printed from Bambu Farm
Manager, the printer reports a ``subtask_name`` that is (usually) the file name
without its extension. Matching levels, strongest first:

  exact       identical after stripping the extension
  normalized  identical after lower-casing and dropping every non-alphanumeric
              character (handles spaces vs underscores, case, punctuation)
  none        no match
"""

from __future__ import annotations

import re
from typing import Optional

# Longest extensions first so ".gcode.3mf" wins over ".3mf".
_EXTENSIONS = (".gcode.3mf", ".3mf.gcode", ".3mf", ".gcode")

# Jobs the printer starts by itself (calibration, HMS checks, ...). They never
# correspond to a student upload and must be excluded from analysis.
_CALIBRATION_PREFIXES = ("auto_hms", "auto_cali", "auto_bed_level")

EXACT = "exact"
NORMALIZED = "normalized"
NONE = "none"


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
