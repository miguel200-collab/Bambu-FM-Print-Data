import sqlite3

import pytest

import database as db
from analysis import audit
from tests.conftest import make_job


def test_cohen_kappa_known_values():
    # a=2 both failed, b=1, c=1, d=2  -> po = 4/6, pe = 0.5, kappa = 1/3
    assert audit.cohen_kappa(2, 1, 1, 2) == pytest.approx(1 / 3)
    # perfect agreement
    assert audit.cohen_kappa(5, 0, 0, 5) == pytest.approx(1.0)
    # empty table and degenerate (pe == 1) are undefined, not a crash
    assert audit.cohen_kappa(0, 0, 0, 0) is None
    assert audit.cohen_kappa(0, 0, 0, 10) is None


def test_machine_verdict():
    assert audit.machine_verdict("FAILED", None) == 1
    assert audit.machine_verdict("FINISH", None) == 0
    assert audit.machine_verdict("FINISH", 0) == 0
    assert audit.machine_verdict("FINISH", 7) == 1   # an error code overrides FINISH
    assert audit.machine_verdict(None, None) is None


def test_find_label_bursts():
    # 4 labels within 1s each, then a lone label much later, then a pair.
    times = [
        "2026-10-01T10:00:00+00:00",
        "2026-10-01T10:00:01+00:00",
        "2026-10-01T10:00:02+00:00",
        "2026-10-01T10:00:03+00:00",
        "2026-10-01T12:00:00+00:00",
        "2026-10-01T14:00:00+00:00",
        "2026-10-01T14:00:02+00:00",
    ]
    assert audit.find_label_bursts(times) == {"bursts": 1, "labels_in_bursts": 4}
    assert audit.find_label_bursts([]) == {"bursts": 0, "labels_in_bursts": 0}
    assert audit.find_label_bursts(["not-a-date"]) == {"bursts": 0, "labels_in_bursts": 0}


def _seed_labels():
    # a (both failed) = C, F ; b (machine failed, human ok) = D ;
    # c (machine ok, human failed) = E ; d (both ok) = A, B
    make_job("A", "a", state="FINISH", label=0, label_time="2026-10-01T12:00:00+00:00")
    make_job("B", "b", state="FINISH", label=0, label_time="2026-10-01T13:00:00+00:00")
    make_job("C", "c", state="FAILED", label=1, label_time="2026-10-01T14:00:00+00:00")
    make_job("D", "d", state="FAILED", label=0, label_time="2026-10-01T15:00:00+00:00")
    make_job("E", "e", state="FINISH", label=1, label_time="2026-10-01T16:00:00+00:00")
    make_job("F", "f", state="FAILED", label=1, label_time="2026-10-01T17:00:00+00:00")


def test_run_audit_label_math(tmp_db):
    _seed_labels()
    result = audit.run_audit(tmp_db)
    lab = result["labels"]
    assert (lab["both_failed"], lab["machine_failed_human_ok"],
            lab["machine_ok_human_failed"], lab["both_ok"]) == (2, 1, 1, 2)
    assert lab["n"] == 6
    assert lab["percent_agreement"] == pytest.approx(4 / 6)
    assert lab["kappa"] == pytest.approx(1 / 3)
    assert lab["bursts"] == 0


def test_volume_excludes_calibration_and_counts_unended(tmp_db):
    make_job("A", "real", state="FINISH", label=0)
    make_job("CAL", "auto_hms_check_for_user.gcode", state="FAILED", label=1)
    make_job("OPEN", "never_ended", end=None, state=None)
    vol = audit.run_audit(tmp_db)["volume"]
    assert vol["total_rows"] == 3
    assert vol["calibration_jobs_excluded"] == 1
    assert vol["jobs"] == 2
    assert vol["never_ended"] == 1
    assert vol["labeled"] == 1
    assert vol["labeled_failed"] == 0
    assert vol["per_filament"] == {"PLA": 2}


def test_match_rate(tmp_db):
    # Upload 1 is printed (exact), upload 2 never printed, job "stray" has no upload.
    db.create_upload("u1", "incoming/x", "ab123", "benchy.gcode.3mf",
                     "ab123_benchy.gcode.3mf", "any", "2026-10-01T09:00:00+00:00")
    db.mark_uploaded("u1", None, "2026-10-01T09:01:00+00:00", status="done")
    db.create_upload("u2", "incoming/y", "ab123", "other.gcode.3mf",
                     "ab123_other.gcode.3mf", "any", "2026-10-01T09:05:00+00:00")
    db.mark_uploaded("u2", None, "2026-10-01T09:06:00+00:00", status="done")
    make_job("J1", "ab123_benchy", start="2026-10-01T10:00:00+00:00")
    make_job("J2", "stray", start="2026-10-01T10:30:00+00:00")

    m = audit.run_audit(tmp_db)["matching"]
    assert m["uploads"] == 2
    assert m["uploads_with_a_job"] == 1
    assert m["upload_match_rate"] == pytest.approx(0.5)
    assert m["jobs"] == 2
    assert m["jobs_with_an_upload"] == 1
    assert m["jobs_by_confidence"] == {"exact": 1, "none": 1}


def test_match_ignores_jobs_before_the_upload(tmp_db):
    db.create_upload("u1", "incoming/x", "ab123", "benchy.gcode.3mf",
                     "ab123_benchy.gcode.3mf", "any", "2026-10-05T09:00:00+00:00")
    db.mark_uploaded("u1", None, "2026-10-05T09:01:00+00:00", status="done")
    # Same name, but the job started days BEFORE the file was uploaded: not a match.
    make_job("J1", "ab123_benchy", start="2026-10-01T10:00:00+00:00")
    m = audit.run_audit(tmp_db)["matching"]
    assert m["uploads_with_a_job"] == 0


def test_audit_is_read_only(tmp_db):
    _seed_labels()
    before = tmp_db.read_bytes()
    audit.run_audit(tmp_db)
    # Opening read-only must not alter the file...
    assert tmp_db.read_bytes() == before
    # ...and the connection it uses must refuse writes.
    conn = audit.connect_readonly(tmp_db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM print_jobs")
    conn.close()


def test_old_schema_without_uploads_table(tmp_path):
    """The Jun-2026 snapshot has no uploads/era/label_source — audit must cope."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE print_jobs (job_id TEXT PRIMARY KEY, printer_serial TEXT, printer_ip TEXT,"
        " subtask_name TEXT, gcode_file TEXT, start_time TEXT, end_time TEXT, final_state TEXT,"
        " print_error INTEGER, nozzle_temper REAL, bed_temper REAL, filament_type TEXT,"
        " user_label INTEGER, label_time TEXT)"
    )
    conn.execute(
        "INSERT INTO print_jobs (job_id, printer_serial, printer_ip, subtask_name, start_time,"
        " final_state, user_label, label_time) VALUES ('1','S','ip','x','2026-06-23T10:00:00+00:00',"
        "'FINISH',0,'2026-06-23T11:00:00+00:00')"
    )
    conn.commit()
    conn.close()
    result = audit.run_audit(path)
    assert result["matching"] is None
    assert "per_era" not in result["volume"]
    assert "no uploads table" in audit.format_report(result)


def test_not_a_makerlab_db(tmp_path):
    path = tmp_path / "other.db"
    sqlite3.connect(path).execute("CREATE TABLE t (x)").connection.close()
    with pytest.raises(ValueError):
        audit.run_audit(path)


def test_cli_missing_file_returns_error(tmp_path, capsys):
    assert audit.main([str(tmp_path / "nope.db")]) == 1
    assert "not found" in capsys.readouterr().err


def test_cli_report_and_json(tmp_db, capsys):
    _seed_labels()
    assert audit.main([str(tmp_db)]) == 0
    out = capsys.readouterr().out
    assert "Cohen's kappa" in out and "0.33" in out
    assert audit.main([str(tmp_db), "--json"]) == 0
    assert '"kappa"' in capsys.readouterr().out
