"""
audit.py — Read-only dataset + label audit for print_dataset.db (Phase 0).

Never writes to the database: it opens it with SQLite's read-only URI mode.

    python analysis/audit.py                      # audits ./print_dataset.db
    python analysis/audit.py path/to/copy.db      # audit a snapshot/backup
    python analysis/audit.py copy.db --immutable  # for a COPY on read-only media
    python analysis/audit.py --json               # machine-readable output

What it reports
---------------
1. Volume: total / ended / labeled / failed jobs, date range, per printer,
   per filament, per era (when the column exists). Printer-initiated
   calibration jobs are counted separately and excluded from everything else.
2. Label reliability: human label vs. the machine's own verdict
   (final_state / print_error) as a 2x2 table, percent agreement, Cohen's kappa,
   and a mass-click detector (bursts of labels only seconds apart).
3. Upload <-> job match rate (only if the ``uploads`` table exists): how many
   uploads can be tied to a print job by filename, and how many jobs have a
   matching upload.

Decision gate (see the plan): if humans flag many machine-"FINISH" prints as
failed, a human labeling path is essential; if agreement is high, machine
labels carry more of the weight.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Optional

# Allow `python analysis/audit.py` from the repo root without installing.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from station.matching import (  # noqa: E402
    NONE,
    candidate_uploads,
    is_calibration_job,
    parse_ts,
    pick_upload,
)

# Labels closer together than this many seconds count as one "burst".
BURST_GAP_S = 5.0
# A burst needs at least this many labels to be flagged as mass-clicking.
BURST_MIN_LABELS = 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def connect_readonly(path: Path | str, immutable: bool = False) -> sqlite3.Connection:
    """Open the database without any ability to modify it."""
    uri = Path(path).resolve().as_uri() + ("?immutable=1" if immutable else "?mode=ro")
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def cohen_kappa(a: int, b: int, c: int, d: int) -> Optional[float]:
    """
    Cohen's kappa for a 2x2 table.

        a = both say "failed"          b = machine failed, human success
        c = machine ok, human "failed" d = both say success/ok

    Returns None when kappa is undefined (empty table or chance agreement == 1).
    """
    n = a + b + c + d
    if n == 0:
        return None
    po = (a + d) / n
    pe = ((a + b) * (a + c) + (c + d) * (b + d)) / (n * n)
    if pe >= 1.0:
        return None
    return (po - pe) / (1.0 - pe)


def machine_verdict(final_state: Optional[str], print_error: Optional[int]) -> Optional[int]:
    """1 = machine says failed, 0 = machine says fine, None = unknown/unfinished."""
    if final_state == "FAILED" or (print_error not in (None, 0)):
        return 1
    if final_state == "FINISH":
        return 0
    return None


def find_label_bursts(label_times: list[str]) -> dict[str, int]:
    """
    Detect mass-clicking: runs of >= BURST_MIN_LABELS labels where each label is
    at most BURST_GAP_S after the previous one.
    """
    stamps = sorted(t for t in (parse_ts(x) for x in label_times) if t is not None)
    bursts: list[int] = []
    run = 1
    for prev, cur in zip(stamps, stamps[1:]):  # noqa: B905
        if (cur - prev).total_seconds() <= BURST_GAP_S:
            run += 1
        else:
            if run >= BURST_MIN_LABELS:
                bursts.append(run)
            run = 1
    if stamps and run >= BURST_MIN_LABELS:
        bursts.append(run)
    return {"bursts": len(bursts), "labels_in_bursts": sum(bursts)}


# ---------------------------------------------------------------------------
# Audit sections
# ---------------------------------------------------------------------------

def audit_volume(conn: sqlite3.Connection) -> dict[str, Any]:
    cols = _columns(conn, "print_jobs")
    rows = conn.execute("SELECT * FROM print_jobs").fetchall()
    calibration = [r for r in rows if is_calibration_job(r["subtask_name"])]
    real = [r for r in rows if not is_calibration_job(r["subtask_name"])]
    ended = [r for r in real if r["final_state"] is not None]
    labeled = [r for r in ended if r["user_label"] is not None]
    starts = sorted(r["start_time"] for r in real if r["start_time"])

    out: dict[str, Any] = {
        "total_rows": len(rows),
        "calibration_jobs_excluded": len(calibration),
        "jobs": len(real),
        "ended": len(ended),
        "never_ended": len(real) - len(ended),
        "labeled": len(labeled),
        "labeled_failed": sum(1 for r in labeled if r["user_label"] == 1),
        "machine_failed": sum(1 for r in ended if machine_verdict(r["final_state"], r["print_error"]) == 1),
        "first_start": starts[0] if starts else None,
        "last_start": starts[-1] if starts else None,
        "per_printer": dict(Counter(r["printer_serial"] for r in real)),
        "per_filament": dict(Counter(r["filament_type"] or "unknown" for r in real)),
    }
    if "era" in cols:
        out["per_era"] = dict(Counter(r["era"] for r in real))
    if "label_source" in cols:
        out["per_label_source"] = dict(Counter(r["label_source"] for r in labeled))
    return out


def audit_labels(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = [
        r for r in conn.execute("SELECT * FROM print_jobs").fetchall()
        if not is_calibration_job(r["subtask_name"])
        and r["user_label"] is not None
        and machine_verdict(r["final_state"], r["print_error"]) is not None
    ]
    a = b = c = d = 0
    for r in rows:
        machine = machine_verdict(r["final_state"], r["print_error"])
        human = 1 if r["user_label"] == 1 else 0
        if machine == 1 and human == 1:
            a += 1
        elif machine == 1 and human == 0:
            b += 1
        elif machine == 0 and human == 1:
            c += 1
        else:
            d += 1
    n = a + b + c + d
    all_label_times = [
        r["label_time"] for r in conn.execute(
            "SELECT label_time FROM print_jobs WHERE user_label IS NOT NULL AND label_time IS NOT NULL"
        )
    ]
    return {
        "n": n,
        "both_failed": a,
        "machine_failed_human_ok": b,
        "machine_ok_human_failed": c,
        "both_ok": d,
        "percent_agreement": (a + d) / n if n else None,
        "kappa": cohen_kappa(a, b, c, d),
        **find_label_bursts(all_label_times),
    }


def audit_matching(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
    """
    Match every job to an upload with the SAME rules the daemon uses
    (station/matching.py): exact / normalized filename + time window.
    """
    if "uploads" not in _tables(conn):
        return None
    uploads = conn.execute(
        "SELECT upload_id, student_name, renamed_filename, received_at"
        " FROM uploads WHERE upload_status = 'done'"
    ).fetchall()
    jobs = [
        r for r in conn.execute("SELECT job_id, subtask_name, start_time FROM print_jobs").fetchall()
        if not is_calibration_job(r["subtask_name"])
    ]

    printed_uploads: set[str] = set()
    by_confidence: Counter[str] = Counter()
    for job in jobs:
        started = parse_ts(job["start_time"])
        for _level, _received, up in candidate_uploads(job["subtask_name"], started, uploads):
            printed_uploads.add(up["upload_id"])
        by_confidence[pick_upload(job["subtask_name"], started, uploads).confidence] += 1

    matched_jobs = len(jobs) - by_confidence[NONE]
    return {
        "uploads": len(uploads),
        "uploads_with_a_job": len(printed_uploads),
        "upload_match_rate": len(printed_uploads) / len(uploads) if uploads else None,
        "jobs": len(jobs),
        "jobs_with_an_upload": matched_jobs,
        "job_coverage": matched_jobs / len(jobs) if jobs else None,
        "jobs_by_confidence": dict(by_confidence),
    }


def run_audit(db_path: Path | str, immutable: bool = False) -> dict[str, Any]:
    conn = connect_readonly(db_path, immutable=immutable)
    try:
        if "print_jobs" not in _tables(conn):
            raise ValueError(f"{db_path} has no print_jobs table — not a MakerLAB database.")
        return {
            "database": str(db_path),
            "volume": audit_volume(conn),
            "labels": audit_labels(conn),
            "matching": audit_matching(conn),
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

def _pct(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def format_report(result: dict[str, Any]) -> str:
    v, lab, m = result["volume"], result["labels"], result["matching"]
    lines = [
        f"MakerLAB dataset audit — {result['database']}",
        "=" * 60,
        "1. VOLUME",
        f"  jobs (excl. calibration): {v['jobs']}   (calibration excluded: {v['calibration_jobs_excluded']})",
        f"  ended: {v['ended']}   never ended: {v['never_ended']}",
        f"  labeled: {v['labeled']}   labeled failed: {v['labeled_failed']}   machine failed: {v['machine_failed']}",
        f"  date range: {v['first_start']}  ->  {v['last_start']}",
        f"  per printer:  {v['per_printer']}",
        f"  per filament: {v['per_filament']}",
    ]
    if "per_era" in v:
        lines.append(f"  per era: {v['per_era']}")
    if "per_label_source" in v:
        lines.append(f"  per label source: {v['per_label_source']}")
    lines += [
        "",
        "2. LABEL RELIABILITY (human label vs. machine verdict)",
        f"  compared jobs: {lab['n']}",
        "                       human FAILED   human OK",
        f"  machine FAILED       {lab['both_failed']:>12}   {lab['machine_failed_human_ok']:>8}",
        f"  machine OK (FINISH)  {lab['machine_ok_human_failed']:>12}   {lab['both_ok']:>8}",
        f"  percent agreement: {_pct(lab['percent_agreement'])}",
        "  Cohen's kappa: " + ("n/a" if lab["kappa"] is None else f"{lab['kappa']:.2f}"),
        f"  mass-click bursts (>= {BURST_MIN_LABELS} labels, <= {BURST_GAP_S:.0f}s apart): "
        f"{lab['bursts']} burst(s), {lab['labels_in_bursts']} label(s) involved",
        "",
        "3. UPLOAD <-> JOB MATCH RATE",
    ]
    if m is None:
        lines.append("  (no uploads table in this database)")
    else:
        lines += [
            f"  uploads: {m['uploads']}   with a matching job: {m['uploads_with_a_job']}"
            f"   ({_pct(m['upload_match_rate'])})",
            f"  jobs: {m['jobs']}   with a matching upload: {m['jobs_with_an_upload']}"
            f"   ({_pct(m['job_coverage'])})",
            f"  jobs by match confidence: {m['jobs_by_confidence']}",
        ]
    lines += [
        "",
        "Decision gate: many machine-OK prints flagged failed by humans => a human",
        "labeling path is essential; high agreement => machine labels carry more weight.",
    ]
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only audit of print_dataset.db")
    parser.add_argument(
        "db", nargs="?", default=str(Path(__file__).resolve().parent.parent / "print_dataset.db"),
        help="path to the database (default: ./print_dataset.db)",
    )
    parser.add_argument("--immutable", action="store_true",
                        help="open a COPY with immutable=1 (no -wal/-shm access needed)")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    args = parser.parse_args(argv)

    if not Path(args.db).exists():
        print(f"error: {args.db} not found", file=sys.stderr)
        return 1
    try:
        result = run_audit(args.db, immutable=args.immutable)
    except (sqlite3.Error, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, default=str) if args.json else format_report(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
