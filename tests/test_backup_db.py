import sqlite3
from datetime import datetime, timedelta

import pytest

from analysis import backup_db
from tests.conftest import make_job


def _count(path, table="print_jobs"):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def test_backup_creates_verified_snapshot(tmp_db, tmp_path):
    make_job("A", "a")
    make_job("B", "b")
    dest = tmp_path / "backups"
    result = backup_db.backup(tmp_db, dest, now=datetime(2026, 10, 7, 3, 0, 0))

    snap = dest / "db" / "2026-10-07_030000" / "print_dataset.db"
    assert result["snapshot"] == str(snap)
    assert snap.exists()
    assert _count(snap) == 2
    assert result["row_counts"]["print_jobs"] == 2


def test_backup_is_consistent_while_source_connection_is_open(tmp_db, tmp_path):
    """The daemon keeps connections open; the online-backup API must still work."""
    make_job("A", "a")
    live = sqlite3.connect(tmp_db)
    live.execute("BEGIN")
    live.execute("SELECT COUNT(*) FROM print_jobs").fetchone()
    try:
        backup_db.backup(tmp_db, tmp_path / "b", now=datetime(2026, 10, 7, 3, 0, 0))
    finally:
        live.close()
    snap = tmp_path / "b" / "db" / "2026-10-07_030000" / "print_dataset.db"
    assert _count(snap) == 1


def test_backup_does_not_modify_source(tmp_db, tmp_path):
    make_job("A", "a")
    before = _count(tmp_db)
    backup_db.backup(tmp_db, tmp_path / "b")
    assert _count(tmp_db) == before


def test_missing_database_raises(tmp_path):
    with pytest.raises(backup_db.BackupError):
        backup_db.backup(tmp_path / "missing.db", tmp_path / "b")


def test_archive_sync_is_additive(tmp_db, tmp_path):
    archive = tmp_path / "inbox"
    (archive / "ab123").mkdir(parents=True)
    (archive / "ab123" / "one.gcode.3mf").write_bytes(b"PK1")
    dest = tmp_path / "b"

    r1 = backup_db.backup(tmp_db, dest, archive, now=datetime(2026, 10, 7, 3, 0, 0))
    assert r1["archive"] == {"copied": 1, "skipped": 0}

    # Second run: nothing changed -> skipped. Add one new file -> copied.
    (archive / "ab123" / "two.gcode.3mf").write_bytes(b"PK22")
    r2 = backup_db.backup(tmp_db, dest, archive, now=datetime(2026, 10, 8, 3, 0, 0))
    assert r2["archive"] == {"copied": 1, "skipped": 1}

    # If the source file is deleted, the backup copy must survive.
    (archive / "ab123" / "one.gcode.3mf").unlink()
    backup_db.backup(tmp_db, dest, archive, now=datetime(2026, 10, 9, 3, 0, 0))
    assert (dest / "BambuSubmissions" / "ab123" / "one.gcode.3mf").read_bytes() == b"PK1"


def test_changed_archive_file_is_recopied(tmp_db, tmp_path):
    archive = tmp_path / "inbox"
    archive.mkdir()
    f = archive / "x.gcode.3mf"
    f.write_bytes(b"old")
    dest = tmp_path / "b"
    backup_db.backup(tmp_db, dest, archive, now=datetime(2026, 10, 7, 3, 0, 0))
    f.write_bytes(b"new-and-longer")
    backup_db.backup(tmp_db, dest, archive, now=datetime(2026, 10, 8, 3, 0, 0))
    assert (dest / "BambuSubmissions" / "x.gcode.3mf").read_bytes() == b"new-and-longer"


def test_missing_archive_dir_is_not_an_error(tmp_db, tmp_path):
    r = backup_db.backup(tmp_db, tmp_path / "b", tmp_path / "does-not-exist")
    assert r["archive"] is None


def test_prune_keeps_recent_and_minimum(tmp_path):
    root = tmp_path / "db"
    now = datetime(2026, 10, 7, 3, 0, 0)
    # 12 daily snapshots, the oldest 11 days back.
    for i in range(12):
        stamp = (now - timedelta(days=i)).strftime("%Y-%m-%d_%H%M%S")
        (root / stamp).mkdir(parents=True)
    (root / "not-a-snapshot").mkdir()

    removed = backup_db.prune_snapshots(root, keep_days=3, now=now)

    remaining = sorted(p.name for p in root.iterdir())
    # The 7 newest are always kept even though only 4 are within keep_days...
    assert len([r for r in remaining if r != "not-a-snapshot"]) == backup_db.MIN_SNAPSHOTS_KEPT
    assert len(removed) == 5
    # ...and unrelated folders are never touched.
    assert "not-a-snapshot" in remaining


def test_cli_requires_destination(tmp_db, capsys, monkeypatch):
    monkeypatch.setattr(backup_db, "_load_backup_config", lambda: {})
    assert backup_db.main(["--db", str(tmp_db)]) == 2
    assert "no destination" in capsys.readouterr().err


def test_cli_success_and_failure(tmp_db, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(backup_db, "_load_backup_config", lambda: {})
    make_job("A", "a")
    assert backup_db.main(["--db", str(tmp_db), "--dest", str(tmp_path / "b")]) == 0
    assert "Backup OK" in capsys.readouterr().out
    assert backup_db.main(["--db", str(tmp_path / "nope.db"), "--dest", str(tmp_path / "b")]) == 1
    assert "BACKUP FAILED" in capsys.readouterr().err
