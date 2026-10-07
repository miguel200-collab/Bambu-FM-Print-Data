"""
backup_db.py — Off-laptop backup of the dataset (print_dataset.db + submission archive).

The dataset lives only on the lab laptop, so this is the single most important
safety net in the project. It is safe to run while the daemon is running:

  * The database is copied with SQLite's online backup API (a consistent
    snapshot even while the daemon is writing — never a raw file copy).
  * The copy is verified (integrity_check + per-table row counts vs. the source).
  * The submission archive is mirrored additively: new/changed files are copied,
    nothing in the backup is ever deleted.
  * Old database snapshots are pruned, but the newest few are always kept.

Usage
-----
    python analysis/backup_db.py --dest "C:\\Users\\lab\\OneDrive\\BambuBackups"
    python analysis/backup_db.py            # uses "backup.dest" from config.json

Point --dest at a folder that OneDrive / Google Drive / Dropbox syncs, so the
copy leaves the laptop. Schedule it nightly with install_backup_task.bat.

Restore
-------
  1. Stop the daemon.
  2. Copy <dest>/db/<newest-stamp>/print_dataset.db over the laptop's
     print_dataset.db (delete any print_dataset.db-wal / -shm next to it).
  3. Copy <dest>/BambuSubmissions/* back into the inbox folder if needed.
  4. Start the daemon. Run `python analysis/audit.py` to confirm the row counts.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

_REPO_DIR = Path(__file__).resolve().parent.parent
_STAMP_FMT = "%Y-%m-%d_%H%M%S"
DB_NAME = "print_dataset.db"
# Never prune below this many snapshots, however old they are.
MIN_SNAPSHOTS_KEPT = 7


class BackupError(RuntimeError):
    """The backup could not be completed or did not verify."""


# ---------------------------------------------------------------------------
# Database snapshot
# ---------------------------------------------------------------------------

def _row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )]
    return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}


def snapshot_database(db_path: Path, snapshot_path: Path) -> dict[str, int]:
    """Online-backup db_path into snapshot_path and verify it. Returns row counts."""
    if not db_path.exists():
        raise BackupError(f"database not found: {db_path}")
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)

    src = sqlite3.connect(str(db_path), timeout=30.0)
    dst = sqlite3.connect(str(snapshot_path))
    try:
        src.backup(dst)
        dst.commit()
        src_counts = _row_counts(src)
        dst_counts = _row_counts(dst)
        integrity = dst.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        dst.close()
        src.close()

    if integrity != "ok":
        raise BackupError(f"backup failed integrity_check: {integrity}")
    # The daemon may insert rows between the copy and the count, so the source
    # can only be >= the backup, never smaller.
    for table, n in dst_counts.items():
        if src_counts.get(table, 0) < n:
            raise BackupError(f"table {table}: backup has {n} rows, source {src_counts.get(table)}")
    if not dst_counts:
        raise BackupError("backup contains no tables")
    return dst_counts


# ---------------------------------------------------------------------------
# Archive mirror
# ---------------------------------------------------------------------------

def sync_archive(archive_dir: Path, dest_dir: Path) -> dict[str, int]:
    """Additively mirror archive_dir into dest_dir (copy new/changed, never delete)."""
    copied = skipped = 0
    for src in archive_dir.rglob("*"):
        if not src.is_file():
            continue
        target = dest_dir / src.relative_to(archive_dir)
        if target.exists():
            s, t = src.stat(), target.stat()
            if s.st_size == t.st_size and s.st_mtime <= t.st_mtime + 1:
                skipped += 1
                continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        copied += 1
    return {"copied": copied, "skipped": skipped}


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------

def prune_snapshots(snapshots_root: Path, keep_days: int, now: datetime) -> list[str]:
    """Delete snapshot folders older than keep_days, always keeping the newest few."""
    if not snapshots_root.exists():
        return []
    dated: list[tuple[datetime, Path]] = []
    for child in snapshots_root.iterdir():
        if not child.is_dir():
            continue
        try:
            dated.append((datetime.strptime(child.name, _STAMP_FMT), child))
        except ValueError:
            continue  # not one of ours — leave it alone
    dated.sort(key=lambda x: x[0], reverse=True)
    cutoff = now - timedelta(days=keep_days)
    removed: list[str] = []
    for index, (stamp, folder) in enumerate(dated):
        if index < MIN_SNAPSHOTS_KEPT or stamp >= cutoff:
            continue
        shutil.rmtree(folder, ignore_errors=True)
        removed.append(folder.name)
    return removed


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def backup(
    db_path: Path,
    dest: Path,
    archive_dir: Optional[Path] = None,
    keep_days: int = 30,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    now = now or datetime.now()
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)

    snapshot = dest / "db" / now.strftime(_STAMP_FMT) / DB_NAME
    counts = snapshot_database(Path(db_path), snapshot)

    archive_result: Optional[dict[str, int]] = None
    if archive_dir is not None and Path(archive_dir).exists():
        archive_result = sync_archive(Path(archive_dir), dest / "BambuSubmissions")

    removed = prune_snapshots(dest / "db", keep_days, now)
    return {
        "snapshot": str(snapshot),
        "row_counts": counts,
        "archive": archive_result,
        "pruned": removed,
    }


def _load_backup_config() -> dict:
    cfg_path = _REPO_DIR / "config.json"
    if not cfg_path.exists():
        return {}
    try:
        with cfg_path.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def main(argv: Optional[list[str]] = None) -> int:
    cfg = _load_backup_config()
    backup_cfg = cfg.get("backup", {}) or {}
    web_cfg = cfg.get("web", {}) or {}

    parser = argparse.ArgumentParser(description="Back up print_dataset.db and the submission archive")
    parser.add_argument("--db", default=str(_REPO_DIR / DB_NAME), help="database to back up")
    parser.add_argument("--dest", default=backup_cfg.get("dest"),
                        help="backup root (default: config.json backup.dest)")
    parser.add_argument("--archive", default=web_cfg.get("inbox_dir"),
                        help="submission archive to mirror (default: config.json web.inbox_dir)")
    parser.add_argument("--keep-days", type=int, default=int(backup_cfg.get("keep_days", 30)),
                        help="prune snapshots older than this (default 30)")
    args = parser.parse_args(argv)

    if not args.dest:
        print("error: no destination. Pass --dest or set backup.dest in config.json.", file=sys.stderr)
        return 2
    try:
        result = backup(
            Path(args.db), Path(args.dest),
            Path(args.archive) if args.archive else None,
            keep_days=args.keep_days,
        )
    except (BackupError, sqlite3.Error, OSError) as exc:
        print(f"BACKUP FAILED: {exc}", file=sys.stderr)
        return 1

    print(f"Backup OK -> {result['snapshot']}")
    print(f"  rows: {result['row_counts']}")
    if result["archive"] is not None:
        print(f"  archive: {result['archive']}")
    if result["pruned"]:
        print(f"  pruned old snapshots: {result['pruned']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
