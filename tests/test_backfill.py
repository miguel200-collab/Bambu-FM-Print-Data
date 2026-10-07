import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import database as db
from analysis import backfill_features as bf
from tests.helpers import build_3mf

T0 = datetime(2026, 8, 1, 9, 0, 0, tzinfo=timezone.utc)


def seed_upload(upload_id, student, renamed, received):
    db.create_upload(upload_id, f"incoming/{upload_id}", student, renamed, renamed, "any",
                     received.isoformat(), era=1)
    db.mark_uploaded(upload_id, None, received.isoformat(), status="done")


def test_backfill_processes_archive_in_order_and_is_idempotent(tmp_db, tmp_path):
    inbox = tmp_path / "inbox"
    seed_upload("u1", "ab123", "ab123_bracket.gcode.3mf", T0)
    seed_upload("u2", "ab123", "ab123_bracket_v2.gcode.3mf", T0 + timedelta(days=1))
    build_3mf(inbox / "ab123" / "ab123_bracket.gcode.3mf", names=["Bracket"])
    build_3mf(inbox / "ab123" / "ab123_bracket_v2.gcode.3mf", names=["Bracket"],
              config_overrides={"wall_loops": "4"})

    first = bf.backfill(inbox)
    assert first == {"files": 2, "processed": 2, "skipped": 0, "adopted": 0, "unreadable": 0}
    rows = {r["upload_id"]: r for r in db.list_recent_uploads(10)}
    assert rows["u1"]["attempt_index"] == 1 and rows["u2"]["attempt_index"] == 2
    assert rows["u1"]["design_family_id"] == rows["u2"]["design_family_id"]
    assert rows["u1"]["era"] == 1

    second = bf.backfill(inbox)
    assert second["processed"] == 0 and second["skipped"] == 2
    families = sqlite3.connect(tmp_db).execute("SELECT upload_count FROM design_families").fetchall()
    assert families == [(2,)]


def test_files_without_an_uploads_row_are_adopted_as_era_1(tmp_db, tmp_path):
    inbox = tmp_path / "inbox"
    build_3mf(inbox / "Miguel_Ramirez" / "Miguel_old_print.gcode.3mf")
    db.set_current_era(2)       # even if the daemon is already in era 2, adopted rows are era 1

    summary = bf.backfill(inbox)

    assert summary["adopted"] == 1 and summary["processed"] == 1
    row = db.list_recent_uploads(10)[0]
    assert row["era"] == 1
    assert row["student_name"] == "Miguel_Ramirez"
    assert row["blob_key"].startswith("backfill:")
    assert row["design_family_id"] is not None
    assert bf.backfill(inbox)["adopted"] == 0                 # not adopted twice


def test_reuploaded_name_gives_features_to_the_latest_row_only(tmp_db, tmp_path):
    inbox = tmp_path / "inbox"
    seed_upload("old", "ab123", "ab123_x.gcode.3mf", T0)
    seed_upload("new", "ab123", "ab123_x.gcode.3mf", T0 + timedelta(days=2))   # overwrote the file
    build_3mf(inbox / "ab123" / "ab123_x.gcode.3mf")
    bf.backfill(inbox)
    rows = {r["upload_id"]: r for r in db.list_recent_uploads(10)}
    assert rows["new"]["file_sha256"] and rows["old"]["file_sha256"] is None


def test_unreadable_files_are_counted_and_do_not_stop_the_run(tmp_db, tmp_path):
    inbox = tmp_path / "inbox"
    seed_upload("u1", "ab123", "ab123_bad.gcode.3mf", T0)
    seed_upload("u2", "ab123", "ab123_good.gcode.3mf", T0 + timedelta(hours=1))
    (inbox / "ab123").mkdir(parents=True)
    (inbox / "ab123" / "ab123_bad.gcode.3mf").write_bytes(b"not a zip")
    build_3mf(inbox / "ab123" / "ab123_good.gcode.3mf")
    summary = bf.backfill(inbox)
    assert summary["unreadable"] == 1 and summary["processed"] == 2


def test_non_3mf_files_are_ignored(tmp_db, tmp_path):
    inbox = tmp_path / "inbox"
    (inbox / "ab123").mkdir(parents=True)
    (inbox / "ab123" / "notes.txt").write_text("hi")
    assert bf.backfill(inbox)["files"] == 0


def test_missing_inbox_is_an_error(tmp_db, tmp_path):
    with pytest.raises(FileNotFoundError):
        bf.backfill(tmp_path / "nope")


def test_cli(tmp_db, tmp_path, capsys):
    inbox = tmp_path / "inbox"
    build_3mf(inbox / "ab123" / "ab123_a.gcode.3mf")
    assert bf.main(["--inbox", str(inbox), "--db", str(tmp_db)]) == 0
    assert "Backfill complete" in capsys.readouterr().out
    assert bf.main(["--inbox", str(tmp_path / "missing"), "--db", str(tmp_db)]) == 1
