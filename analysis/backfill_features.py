"""
backfill_features.py — One offline pass over the existing submission archive:
extract features from every archived ``.gcode.3mf`` and assign design families.

New uploads get this automatically (station/file_watcher.py). This script is for
everything archived BEFORE that pipeline existed. It is idempotent: uploads that
already have features are skipped, so it is safe to re-run.

    python analysis/backfill_features.py                       # inbox from config.json
    python analysis/backfill_features.py --inbox C:\\BambuSubmissions
    python analysis/backfill_features.py --db path/to/print_dataset.db

Take a backup first (run_backup.bat). The script only ADDS data: it fills the
file_features / design_families tables and the new identity columns on uploads.

Details
-------
* Files are processed oldest-first, so design families grow in the order students
  actually iterated.
* A file on disk maps to the uploads row with the same student folder + file name.
  If the same name was uploaded several times the file on disk is the LATEST
  version, so only the latest row receives its features.
* A file with no uploads row at all (archived before the table existed) is
  "adopted": a row is created with ``blob_key='backfill:<path>'``, the file's
  modification time as ``received_at``, and **era 1** (quarantined).
* Everything backfilled is era 1 — it predates the label page.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Allow `python analysis/backfill_features.py` from the repo root without installing.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import database as db  # noqa: E402
from station import gcode_features  # noqa: E402
from station.matching import parse_ts, sanitize_name  # noqa: E402

ERA_BACKFILL = 1


def _iso_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()


def backfill(inbox: Path) -> dict[str, Any]:
    """Process every archived .3mf under ``inbox``. Returns a summary dict."""
    inbox = Path(inbox)
    if not inbox.is_dir():
        raise FileNotFoundError(f"inbox folder not found: {inbox}")

    db.init_db()  # applies additive migrations (new tables/columns) if needed

    # (student folder, file name) -> upload rows, oldest first
    by_path: dict[tuple[str, str], list[Any]] = {}
    for row in db.list_uploads_for_backfill():
        key = (sanitize_name(row["student_name"] or ""), row["renamed_filename"] or "")
        by_path.setdefault(key, []).append(row)

    # (received_at, path, upload_id or None)
    work: list[tuple[datetime, Path, Optional[str]]] = []
    summary = {"files": 0, "processed": 0, "skipped": 0, "adopted": 0, "unreadable": 0}
    for path in sorted(p for p in inbox.rglob("*") if p.is_file() and p.name.lower().endswith(".3mf")):
        summary["files"] += 1
        rows = by_path.get((path.parent.name, path.name))
        if rows:
            latest = rows[-1]
            if latest["file_sha256"]:
                summary["skipped"] += 1
                continue
            received = parse_ts(latest["received_at"]) or datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            work.append((received, path, latest["upload_id"]))
        else:
            work.append((datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc), path, None))

    work.sort(key=lambda w: (w[0], str(w[1])))
    for received, path, upload_id in work:
        sha256 = gcode_features.sha256_file(path)
        if upload_id is None:
            upload_id = f"backfill-{sha256[:16]}"
            db.create_upload(
                upload_id=upload_id,
                blob_key=f"backfill:{path.parent.name}/{path.name}",
                student_name=path.parent.name,
                original_filename=path.name,
                renamed_filename=path.name,
                target_printer="any",
                received_at=received.isoformat(),
                upload_status="done",
                era=ERA_BACKFILL,
            )
            summary["adopted"] += 1
        features = db.get_file_features(sha256)
        if features is None:
            try:
                features = gcode_features.extract_features(path)
            except gcode_features.FeatureError as exc:
                summary["unreadable"] += 1
                features = gcode_features.failed_result(str(exc))
        db.record_upload_features(upload_id, sha256, features)
        summary["processed"] += 1
    return summary


def main(argv: Optional[list[str]] = None) -> int:
    repo = Path(__file__).resolve().parent.parent
    cfg: dict[str, Any] = {}
    cfg_path = repo / "config.json"
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cfg = {}

    parser = argparse.ArgumentParser(description="Backfill file features + design families")
    parser.add_argument("--inbox", default=(cfg.get("web") or {}).get("inbox_dir"),
                        help="submission archive (default: config.json web.inbox_dir)")
    parser.add_argument("--db", default=None, help="database path (default: ./print_dataset.db)")
    args = parser.parse_args(argv)

    if not args.inbox:
        print("error: no inbox folder. Pass --inbox or set web.inbox_dir in config.json.", file=sys.stderr)
        return 2
    if args.db:
        db.DB_PATH = Path(args.db)
    try:
        summary = backfill(Path(args.inbox))
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("Backfill complete: " + ", ".join(f"{k}={v}" for k, v in summary.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
