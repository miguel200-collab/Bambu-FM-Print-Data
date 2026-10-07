import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import database as db
from tests.conftest import make_job

T0 = datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat()


def add_upload(upload_id, student, renamed, received=T0, status="done"):
    db.create_upload(upload_id, f"incoming/{upload_id}", student, renamed, renamed, "any", _iso(received))
    db.mark_uploaded(upload_id, None, _iso(received), status=status)


# --------------------------------------------------------------------------- schema

def test_init_db_is_idempotent(tmp_db):
    db.init_db()
    db.init_db()
    conn = sqlite3.connect(tmp_db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(print_jobs)")}
    conn.close()
    assert {"label_source", "era", "print_error", "user_label"} <= cols


def _make_old_db(path):
    """The Jun-2026 production schema: no era, no label_source, no uploads.era."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE print_jobs (job_id TEXT PRIMARY KEY, printer_serial TEXT NOT NULL,"
        " printer_ip TEXT NOT NULL, subtask_name TEXT, gcode_file TEXT, start_time TEXT,"
        " end_time TEXT, final_state TEXT, print_error INTEGER, nozzle_temper REAL,"
        " bed_temper REAL, filament_type TEXT, user_label INTEGER, label_time TEXT,"
        " layer_height REAL, infill_density REAL, wall_loops INTEGER)"
    )
    conn.execute(
        "CREATE TABLE uploads (upload_id TEXT PRIMARY KEY, blob_key TEXT NOT NULL, student_name TEXT,"
        " original_filename TEXT, renamed_filename TEXT, target_printer TEXT, printer_serial TEXT,"
        " upload_status TEXT NOT NULL, error_message TEXT, received_at TEXT, uploaded_at TEXT)"
    )
    conn.execute("INSERT INTO print_jobs (job_id, printer_serial, printer_ip, final_state, user_label)"
                 " VALUES ('old-labeled','S','ip','FINISH',0)")
    conn.execute("INSERT INTO print_jobs (job_id, printer_serial, printer_ip) VALUES ('old-open','S','ip')")
    conn.execute("INSERT INTO uploads (upload_id, blob_key, upload_status) VALUES ('u-old','k','done')")
    conn.commit()
    conn.close()


def test_migration_backfills_old_database_to_era_1(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    _make_old_db(path)
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setattr(db, "_current_era", 1)

    db.init_db()

    labeled = db.get_job("old-labeled")
    assert labeled["era"] == 1
    assert labeled["label_source"] == "popup"      # old labels came from the popup
    assert labeled["user_label"] == 0              # data untouched
    open_job = db.get_job("old-open")
    assert open_job["era"] == 1
    assert open_job["label_source"] is None        # nothing to attribute
    assert db.list_recent_uploads()[0]["era"] == 1


def test_backfill_never_reruns_after_era_2_starts(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    _make_old_db(path)
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setattr(db, "_current_era", 1)
    db.init_db()

    db.set_current_era(2)
    make_job("new-1", "new")
    db.init_db()   # e.g. the daemon restarts — must not relabel anything era 1

    assert db.get_job("new-1")["era"] == 2
    assert db.get_job("old-labeled")["era"] == 1


# --------------------------------------------------------------------------- era / jobs

def test_new_rows_default_to_era_1_and_follow_set_current_era(tmp_db):
    make_job("a", "a")
    assert db.get_job("a")["era"] == 1
    db.set_current_era(2)
    make_job("b", "b")
    add_upload("u1", "ab123", "ab123_x.gcode.3mf")
    assert db.get_job("b")["era"] == 2
    assert db.list_recent_uploads()[0]["era"] == 2
    assert db.get_current_era() == 2


def test_job_lifecycle_and_print_error(tmp_db):
    make_job("a", "part", state="FAILED", print_error=7)
    row = db.get_job("a")
    assert row["final_state"] == "FAILED"
    assert row["print_error"] == 7
    assert row["subtask_name"] == "part"
    assert row["user_label"] is None


def test_get_job_missing_returns_none(tmp_db):
    assert db.get_job("nope") is None


def test_write_label_records_source_and_reports_missing(tmp_db):
    make_job("a", "a")
    assert db.write_label("a", 1, "2026-10-01T12:00:00+00:00", "web") is True
    row = db.get_job("a")
    assert (row["user_label"], row["label_source"], row["label_time"]) == (
        1, "web", "2026-10-01T12:00:00+00:00")
    # a student may correct their label; the latest wins
    assert db.write_label("a", 0, "2026-10-01T13:00:00+00:00", "web") is True
    assert db.get_job("a")["user_label"] == 0
    assert db.write_label("missing", 1, "t", "web") is False


def test_list_recent_uploads_includes_blob_key(tmp_db):
    """Regression (Jul 29): the SELECT once omitted blob_key -> IndexError."""
    add_upload("u1", "ab123", "ab123_x.gcode.3mf")
    row = db.list_recent_uploads()[0]
    assert row["blob_key"] == "incoming/u1"


# --------------------------------------------------------------------------- join

def test_find_upload_exact_match(tmp_db):
    add_upload("u1", "ab123", "ab123_benchy.gcode.3mf")
    m = db.find_upload_for_job("ab123_benchy", _iso(T0 + timedelta(hours=2)))
    assert (m.confidence, m.upload_id, m.student_name) == ("exact", "u1", "ab123")


def test_find_upload_normalized_match_and_48h_window(tmp_db):
    add_upload("u1", "ab123", "ab123_benchy.gcode.3mf")
    assert db.find_upload_for_job("AB123 Benchy", _iso(T0 + timedelta(hours=47))).confidence == "normalized"
    # outside the (tighter) normalized window -> no match...
    assert db.find_upload_for_job("AB123 Benchy", _iso(T0 + timedelta(hours=49))).confidence == "none"
    # ...but an EXACT name is still fine days later
    assert db.find_upload_for_job("ab123_benchy", _iso(T0 + timedelta(days=10))).confidence == "exact"
    # and not after the 30-day exact window
    assert db.find_upload_for_job("ab123_benchy", _iso(T0 + timedelta(days=31))).confidence == "none"


def test_find_upload_ignores_job_that_started_before_the_upload(tmp_db):
    add_upload("u1", "ab123", "ab123_benchy.gcode.3mf")
    assert db.find_upload_for_job("ab123_benchy", _iso(T0 - timedelta(hours=3))).confidence == "none"
    # but clock skew of under an hour is tolerated
    assert db.find_upload_for_job("ab123_benchy", _iso(T0 - timedelta(minutes=30))).confidence == "exact"


def test_find_upload_only_considers_archived_uploads(tmp_db):
    add_upload("u1", "ab123", "ab123_benchy.gcode.3mf", status="failed")
    assert db.find_upload_for_job("ab123_benchy", _iso(T0 + timedelta(hours=1))).confidence == "none"


def test_find_upload_latest_reupload_wins_and_is_not_ambiguous(tmp_db):
    add_upload("u-old", "ab123", "ab123_benchy.gcode.3mf", received=T0)
    add_upload("u-new", "AB123", "ab123_benchy.gcode.3mf", received=T0 + timedelta(days=1))
    m = db.find_upload_for_job("ab123_benchy", _iso(T0 + timedelta(days=1, hours=2)))
    assert (m.confidence, m.upload_id) == ("exact", "u-new")


def test_find_upload_different_students_same_name_is_ambiguous(tmp_db):
    add_upload("u1", "ab123", "shared.gcode.3mf")
    add_upload("u2", "cd456", "shared.gcode.3mf", received=T0 + timedelta(hours=1))
    m = db.find_upload_for_job("shared", _iso(T0 + timedelta(hours=3)))
    assert m.confidence == "ambiguous"
    assert m.upload_id == "u2"   # most recent is still returned


def test_find_upload_calibration_and_blank_never_match(tmp_db):
    add_upload("u1", "ab123", "auto_hms_check_for_user.gcode.3mf")
    assert db.find_upload_for_job("auto_hms_check_for_user.gcode", _iso(T0 + timedelta(hours=1))).confidence == "none"
    assert db.find_upload_for_job(None, _iso(T0)).confidence == "none"
    assert db.find_upload_for_job("", None).confidence == "none"


# --------------------------------------------------------------------------- my prints

NOW = T0 + timedelta(days=2)


def _seed_two_students():
    add_upload("u1", "ab123", "ab123_benchy.gcode.3mf")
    add_upload("u2", "cd456", "cd456_lego.gcode.3mf")
    make_job("j1", "ab123_benchy", start=_iso(T0 + timedelta(hours=1)), end=_iso(T0 + timedelta(hours=2)))
    make_job("j2", "cd456_lego", start=_iso(T0 + timedelta(hours=1)), end=_iso(T0 + timedelta(hours=3)))
    make_job("j3", "stray_print", start=_iso(T0 + timedelta(hours=1)), end=_iso(T0 + timedelta(hours=3)))


def test_list_jobs_for_netid_filters_by_student(tmp_db):
    _seed_two_students()
    mine = db.list_jobs_for_netid("ab123", now=NOW)
    assert [j["job_id"] for j in mine] == ["j1"]
    assert mine[0]["filename"] == "ab123_benchy.gcode.3mf"
    assert mine[0]["match_confidence"] == "exact"
    assert [j["job_id"] for j in db.list_jobs_for_netid("cd456", now=NOW)] == ["j2"]


def test_list_jobs_for_netid_is_case_and_punctuation_insensitive(tmp_db):
    _seed_two_students()
    assert [j["job_id"] for j in db.list_jobs_for_netid("  AB123 ", now=NOW)] == ["j1"]
    assert db.list_jobs_for_netid("", now=NOW) == []
    assert db.list_jobs_for_netid("nobody", now=NOW) == []


def test_list_jobs_for_netid_only_recent_ended_non_calibration(tmp_db):
    add_upload("u1", "ab123", "ab123_benchy.gcode.3mf")
    add_upload("u2", "ab123", "ab123_old.gcode.3mf")
    add_upload("u3", "ab123", "ab123_running.gcode.3mf")
    make_job("recent", "ab123_benchy", start=_iso(T0 + timedelta(hours=1)), end=_iso(T0 + timedelta(hours=2)))
    make_job("old", "ab123_old", start=_iso(T0 + timedelta(hours=1)), end=_iso(T0 + timedelta(hours=2)))
    make_job("running", "ab123_running", start=_iso(T0 + timedelta(hours=1)), end=None, state=None)
    make_job("cal", "auto_hms_check_for_user.gcode", start=_iso(T0 + timedelta(hours=1)),
             end=_iso(T0 + timedelta(hours=2)))

    ids = lambda now: sorted(j["job_id"] for j in db.list_jobs_for_netid("ab123", now=now))
    assert ids(NOW) == ["old", "recent"]
    # 20 days later the 14-day window has closed for both
    assert ids(T0 + timedelta(days=20)) == []


def test_list_jobs_for_netid_newest_first_and_shows_label(tmp_db):
    add_upload("u1", "ab123", "ab123_a.gcode.3mf")
    add_upload("u2", "ab123", "ab123_b.gcode.3mf")
    make_job("first", "ab123_a", start=_iso(T0 + timedelta(hours=1)), end=_iso(T0 + timedelta(hours=2)),
             label=1, label_source="web")
    make_job("second", "ab123_b", start=_iso(T0 + timedelta(hours=3)), end=_iso(T0 + timedelta(hours=4)))
    jobs = db.list_jobs_for_netid("ab123", now=NOW)
    assert [j["job_id"] for j in jobs] == ["second", "first"]
    assert jobs[1]["user_label"] == 1 and jobs[1]["label_source"] == "web"
    assert jobs[0]["user_label"] is None


def test_legacy_done_uploads_are_backfilled_as_blob_deleted(tmp_path, monkeypatch):
    """Rows archived before blob_deleted existed must not look like undeleted (duplicate) blobs."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE uploads (upload_id TEXT PRIMARY KEY, blob_key TEXT NOT NULL, student_name TEXT,"
                 " original_filename TEXT, renamed_filename TEXT, target_printer TEXT, printer_serial TEXT,"
                 " upload_status TEXT NOT NULL, error_message TEXT, received_at TEXT, uploaded_at TEXT)")
    conn.execute("INSERT INTO uploads (upload_id, blob_key, upload_status) VALUES ('done1','incoming/a','done')")
    conn.execute("INSERT INTO uploads (upload_id, blob_key, upload_status) VALUES ('fail1','incoming/b','failed')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setattr(db, "_current_era", 1)
    db.init_db()
    assert db.latest_upload_for_blob("incoming/a")["blob_deleted"] == 1
    assert db.latest_upload_for_blob("incoming/b") is None          # failed uploads are retryable


def test_latest_upload_for_blob_picks_the_newest_and_ignores_failures(tmp_db):
    db.create_upload("u1", "incoming/x", "ab123", "x", "ab123_x.gcode.3mf", "any", "2026-10-01T10:00:00+00:00")
    db.mark_uploaded("u1", None, "2026-10-01T10:00:05+00:00", status="done")
    db.create_upload("u2", "incoming/x", "ab123", "x", "ab123_x.gcode.3mf", "any", "2026-10-02T10:00:00+00:00")
    db.mark_uploaded("u2", None, "2026-10-02T10:00:05+00:00", status="failed", error_message="e")
    assert db.latest_upload_for_blob("incoming/x")["upload_id"] == "u1"
    assert db.latest_upload_for_blob("incoming/none") is None
    db.set_blob_deleted("u1", True)
    assert db.latest_upload_for_blob("incoming/x")["blob_deleted"] == 1
    db.set_blob_deleted("u1", False)
    assert db.latest_upload_for_blob("incoming/x")["blob_deleted"] == 0
