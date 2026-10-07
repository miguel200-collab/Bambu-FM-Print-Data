import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import database as db
from station import families as fam
from station import gcode_features as gf
from tests.helpers import build_3mf

T0 = datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc)


# =========================================================================== pure logic

def fp(names=("benchy",), area=454.4, dims=(16.2, 39.0), max_z=48.0):
    return {"names": sorted(names), "area": area, "dims": sorted(dims) if dims else None, "max_z": max_z}


def test_fingerprint_normalizes_names_and_sorts_dims():
    feats = {"object_names": ["3D Benchy!", "  "], "curated": {
        "footprint_area_mm2": 100.0, "bbox_x_mm": 40.0, "bbox_y_mm": 10.0, "max_z_mm": 5.0}}
    out = fam.fingerprint(feats)
    assert out == {"names": ["3dbenchy"], "area": 100.0, "dims": [10.0, 40.0], "max_z": 5.0}
    assert fam.fingerprint({"curated": {}})["dims"] is None


def test_geometry_distance():
    assert fam.geometry_distance(fp(), fp()) == 0.0
    assert fam.geometry_distance(fp(area=454.4), fp(area=440.0)) < 0.05
    assert fam.geometry_distance(fp(area=454.4), fp(area=300.0)) > 0.3
    # part rotated 90 degrees on the plate: x/y swapped -> sorted dims still match
    assert fam.geometry_distance(fp(dims=(16.2, 39.0)), fp(dims=(39.0, 16.2))) == 0.0
    assert fam.geometry_distance({"names": []}, {"names": []}) is None


def test_names_match_uses_jaccard():
    assert fam.names_match(fp(names=("a", "b")), fp(names=("a", "b")))
    assert fam.names_match(fp(names=("a", "b")), fp(names=("a", "b", "c")))       # 2/3
    assert not fam.names_match(fp(names=("a", "b", "c")), fp(names=("a", "x", "y")))  # 1/5
    assert not fam.names_match(fp(names=()), fp(names=("a",)))


def test_score_prefers_name_match_over_geometry_only():
    same_name_different_size = fam.score(fp(), fp(area=454.4 * 1.2))   # 20% apart: name saves it
    geometry_only = fam.score(fp(names=("x",)), fp(names=("y",), area=454.4 * 0.99))
    assert same_name_different_size is not None and geometry_only is not None
    assert same_name_different_size < geometry_only


def test_score_geometry_tolerance_boundary():
    base = fp(names=("x",))
    assert fam.score(base, fp(names=("y",), area=454.4 * 0.96)) is not None     # ~4% apart
    assert fam.score(base, fp(names=("y",), area=454.4 * 0.90)) is None         # ~10% apart


def test_generic_name_cannot_bridge_unrelated_designs():
    """Two different parts both called 'Cube' must not merge."""
    small = fp(names=("cube",), area=100.0, dims=(10, 10), max_z=10.0)
    huge = fp(names=("cube",), area=10000.0, dims=(100, 100), max_z=100.0)
    assert fam.score(small, huge) is None


def test_choose_family_window_and_tiebreak():
    new = fp()
    t = T0
    cands = [
        ("old", fp(), T0 - timedelta(days=20)),                 # outside the 14-day window
        ("older-match", fp(), T0 - timedelta(days=5)),
        ("recent-match", fp(), T0 - timedelta(days=1)),
        ("unrelated", fp(names=("z",), area=5.0, dims=(1, 1), max_z=1.0), T0 - timedelta(hours=1)),
    ]
    assert fam.choose_family(new, t, cands) == "recent-match"
    assert fam.choose_family(new, t, cands[:1]) is None
    assert fam.choose_family(new, t, []) is None
    assert fam.choose_family(new, t, cands[3:]) is None


def test_choose_family_picks_the_closest_geometry():
    new = fp(names=("n",), area=1000.0)
    cands = [
        ("far", fp(names=("a",), area=1040.0), T0),
        ("near", fp(names=("b",), area=1005.0), T0),
    ]
    assert fam.choose_family(new, T0 + timedelta(hours=1), cands) == "near"


def test_config_diff():
    prev = {"infill": "15%", "walls": "2", "gone": "x"}
    cur = {"infill": "25%", "walls": "2", "new": "y"}
    assert fam.config_diff(prev, cur) == {
        "gone": ["x", None], "infill": ["15%", "25%"], "new": [None, "y"]}
    assert fam.config_diff(prev, dict(prev)) == {}


def test_config_diff_truncates_and_caps():
    long = "x" * 1000
    assert fam.config_diff({"k": "a"}, {"k": long})["k"][1] == "x" * 200
    many_prev = {f"k{i}": "1" for i in range(500)}
    many_cur = {f"k{i}": "2" for i in range(500)}
    assert len(fam.config_diff(many_prev, many_cur)) == 200


# =========================================================================== through the database

def add_upload(upload_id, student, renamed, received):
    db.create_upload(upload_id, f"incoming/{upload_id}", student, renamed, renamed, "any", received.isoformat())
    db.mark_uploaded(upload_id, None, received.isoformat(), status="done")


def submit(tmp_path, upload_id, student="ab123", *, received=T0, name=None, **build):
    """Create an upload row + real synthetic file, extract, and record it."""
    renamed = name or f"{student}_{upload_id}.gcode.3mf"
    add_upload(upload_id, student, renamed, received)
    path = build_3mf(tmp_path / f"{upload_id}.gcode.3mf", **build)
    feats = gf.extract_features(path)
    return db.record_upload_features(upload_id, gf.sha256_file(path), feats)


def row(upload_id):
    return next(r for r in db.list_recent_uploads(100) if r["upload_id"] == upload_id)


def test_first_upload_starts_a_family(tmp_db, tmp_path):
    out = submit(tmp_path, "u1")
    assert out["attempt_index"] == 1 and out["config_diff"] is None
    assert row("u1")["design_family_id"] == out["design_family_id"]


def test_tweaked_reupload_joins_the_family_with_a_config_diff(tmp_db, tmp_path):
    a = submit(tmp_path, "u1", names=["Bracket"])
    b = submit(tmp_path, "u2", received=T0 + timedelta(hours=3), names=["Bracket"],
               config_overrides={"sparse_infill_density": "25%"})
    assert b["design_family_id"] == a["design_family_id"]
    assert b["attempt_index"] == 2
    assert b["config_diff"] == {"sparse_infill_density": ["15%", "25%"]}
    stored = sqlite3.connect(tmp_db).execute(
        "SELECT config_diff FROM uploads WHERE upload_id='u2'").fetchone()[0]
    assert json.loads(stored) == b["config_diff"]


def test_renamed_object_but_same_geometry_still_matches(tmp_db, tmp_path):
    a = submit(tmp_path, "u1", names=["Bracket"])
    b = submit(tmp_path, "u2", received=T0 + timedelta(days=1), names=["Bracket v2 FINAL"],
               area=454.4 * 1.02)                      # 2% bigger, new name
    assert b["design_family_id"] == a["design_family_id"] and b["attempt_index"] == 2


def test_diff_is_against_the_previous_attempt_not_the_first(tmp_db, tmp_path):
    submit(tmp_path, "u1")
    submit(tmp_path, "u2", received=T0 + timedelta(hours=1), config_overrides={"sparse_infill_density": "25%"})
    c = submit(tmp_path, "u3", received=T0 + timedelta(hours=2),
               config_overrides={"sparse_infill_density": "25%", "wall_loops": "4"})
    assert c["attempt_index"] == 3
    assert c["config_diff"] == {"wall_loops": ["2", "4"]}      # NOT infill: that changed in attempt 2


def test_a_drifting_design_is_followed_through_the_family(tmp_db, tmp_path):
    """Each attempt is ~4% bigger: 1->2 and 2->3 are close, though 1->3 is ~8%."""
    ids = [submit(tmp_path, f"u{i}", received=T0 + timedelta(days=i), names=[f"part{i}"],
                  area=454.4 * (1.04 ** i), dims=(39.0 * 1.04 ** i, 16.2), max_z="48.00")["design_family_id"]
           for i in range(3)]
    assert len(set(ids)) == 1


def test_a_different_design_starts_a_new_family(tmp_db, tmp_path):
    a = submit(tmp_path, "u1", names=["Bracket"])
    b = submit(tmp_path, "u2", received=T0 + timedelta(hours=1), names=["Lego brick"],
               area=2000.0, dims=(80.0, 40.0), max_z="12.00")
    assert b["design_family_id"] != a["design_family_id"]
    assert b["attempt_index"] == 1 and b["config_diff"] is None


def test_upload_outside_the_14_day_window_starts_a_new_family(tmp_db, tmp_path):
    a = submit(tmp_path, "u1")
    b = submit(tmp_path, "u2", received=T0 + timedelta(days=15))
    assert b["design_family_id"] != a["design_family_id"] and b["attempt_index"] == 1


def test_window_is_measured_from_the_latest_attempt(tmp_db, tmp_path):
    a = submit(tmp_path, "u1")
    submit(tmp_path, "u2", received=T0 + timedelta(days=10))
    c = submit(tmp_path, "u3", received=T0 + timedelta(days=20))   # 10 days after u2
    assert c["design_family_id"] == a["design_family_id"] and c["attempt_index"] == 3


def test_families_never_span_students(tmp_db, tmp_path):
    a = submit(tmp_path, "u1", student="ab123")
    b = submit(tmp_path, "u2", student="cd456", received=T0 + timedelta(hours=1))   # identical file
    assert b["design_family_id"] != a["design_family_id"]


def test_student_name_case_does_not_split_a_family(tmp_db, tmp_path):
    a = submit(tmp_path, "u1", student="ab123")
    b = submit(tmp_path, "u2", student="AB123", received=T0 + timedelta(hours=1))
    assert b["design_family_id"] == a["design_family_id"]


def test_identical_file_is_stored_once_but_counts_as_another_attempt(tmp_db, tmp_path):
    p = build_3mf(tmp_path / "same.gcode.3mf")
    sha = gf.sha256_file(p)
    feats = gf.extract_features(p)
    add_upload("u1", "ab123", "ab123_same.gcode.3mf", T0)
    add_upload("u2", "ab123", "ab123_same2.gcode.3mf", T0 + timedelta(hours=1))
    db.record_upload_features("u1", sha, feats)
    out = db.record_upload_features("u2", sha, db.get_file_features(sha))
    assert out["attempt_index"] == 2 and out["config_diff"] == {}
    assert row("u1")["file_sha256"] == row("u2")["file_sha256"] == sha
    n = sqlite3.connect(tmp_db).execute("SELECT COUNT(*) FROM file_features").fetchone()[0]
    assert n == 1


def test_same_toolpath_under_a_new_name_shares_gcode_md5(tmp_db, tmp_path):
    submit(tmp_path, "u1", names=["Bracket"], md5="ab" * 16)
    submit(tmp_path, "u2", received=T0 + timedelta(hours=1), names=["Bracket"], md5="ab" * 16,
           config_overrides={"printer_model": "renamed-profile"})     # different file bytes
    r1, r2 = row("u1"), row("u2")
    assert r1["file_sha256"] != r2["file_sha256"]       # not byte-identical...
    assert r1["gcode_md5"] == r2["gcode_md5"] == "ab" * 16   # ...but the same sliced toolpath


def test_recording_twice_is_idempotent(tmp_db, tmp_path):
    out1 = submit(tmp_path, "u1")
    path = tmp_path / "u1.gcode.3mf"
    out2 = db.record_upload_features("u1", gf.sha256_file(path), gf.extract_features(path))
    assert out2["design_family_id"] == out1["design_family_id"]
    count = sqlite3.connect(tmp_db).execute("SELECT upload_count FROM design_families").fetchone()[0]
    assert count == 1


def test_plain_3mf_gets_features_row_but_no_family(tmp_db, tmp_path):
    add_upload("u1", "ab123", "ab123_model.gcode.3mf", T0)
    p = build_3mf(tmp_path / "model.3mf", sliced=False)
    out = db.record_upload_features("u1", gf.sha256_file(p), gf.extract_features(p))
    assert out == {"design_family_id": None, "attempt_index": None, "config_diff": None}
    conn = sqlite3.connect(tmp_db)
    assert conn.execute("SELECT is_sliced FROM file_features").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM design_families").fetchone()[0] == 0


def test_unreadable_file_is_recorded_not_retried_forever(tmp_db):
    add_upload("u1", "ab123", "ab123_x.gcode.3mf", T0)
    db.record_upload_features("u1", "f" * 64, gf.failed_result("not a valid 3MF/ZIP archive"))
    assert row("u1")["design_family_id"] is None
    err = sqlite3.connect(tmp_db).execute("SELECT error FROM file_features").fetchone()[0]
    assert "not a valid" in err


def test_unknown_upload_raises(tmp_db):
    with pytest.raises(KeyError):
        db.record_upload_features("nope", "a" * 64, gf.failed_result("x"))


def test_get_file_features_cache_and_versioning(tmp_db, tmp_path):
    assert db.get_file_features("0" * 64) is None
    out = submit(tmp_path, "u1")
    sha = row("u1")["file_sha256"]
    cached = db.get_file_features(sha)
    assert cached["curated"]["layer_count"] == 2 and cached["config"]["wall_loops"] == "2"
    # a row written by an OLDER extractor must be treated as missing so it is redone
    conn = sqlite3.connect(tmp_db)
    conn.execute("UPDATE file_features SET extractor_version = 0")
    conn.commit()
    conn.close()
    assert db.get_file_features(sha) is None
    assert out["design_family_id"]


def test_file_features_table_has_every_curated_column(tmp_db):
    cols = {r[1] for r in sqlite3.connect(tmp_db).execute("PRAGMA table_info(file_features)")}
    assert set(gf.CURATED_COLUMNS) <= cols
    assert {"file_sha256", "gcode_md5", "raw_json", "extractor_version", "is_sliced"} <= cols


def test_migration_adds_identity_columns_to_an_existing_uploads_table(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE uploads (upload_id TEXT PRIMARY KEY, blob_key TEXT NOT NULL, student_name TEXT,"
                 " original_filename TEXT, renamed_filename TEXT, target_printer TEXT, printer_serial TEXT,"
                 " upload_status TEXT NOT NULL, error_message TEXT, received_at TEXT, uploaded_at TEXT)")
    conn.execute("INSERT INTO uploads (upload_id, blob_key, upload_status) VALUES ('u','k','done')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setattr(db, "_current_era", 1)
    db.init_db()
    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(uploads)")}
    assert {"file_sha256", "gcode_md5", "design_family_id", "attempt_index", "config_diff", "era"} <= cols
    assert [r["upload_id"] for r in db.list_uploads_for_backfill()] == ["u"]
