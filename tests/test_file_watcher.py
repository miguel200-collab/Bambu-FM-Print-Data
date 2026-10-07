import sqlite3
from pathlib import Path

import httpx
import pytest

import database as db
from station import file_watcher as fw
from station import gcode_features as gf
from tests.helpers import build_3mf


# --------------------------------------------------------------------------- harness

class Harness:
    """A FileWatcher with the network faked out. `payload` is what 'downloads'."""

    def __init__(self, tmp_path, watcher):
        self.tmp_path = tmp_path
        self.watcher = watcher
        self.deleted: list[dict] = []
        self.payload: bytes = b""
        self.download_error: Exception | None = None

    @property
    def inbox(self) -> Path:
        return self.tmp_path / "inbox"


@pytest.fixture
def harness(tmp_db, tmp_path, monkeypatch):
    watcher = fw.FileWatcher(
        blob_token="tok", get_monitors=lambda: [], base_dir=tmp_path, inbox_dir=tmp_path / "inbox",
    )
    h = Harness(tmp_path, watcher)

    def fake_download(blob, dest):
        if h.download_error:
            raise h.download_error
        dest.write_bytes(h.payload)

    monkeypatch.setattr(watcher, "_download_blob", fake_download)
    monkeypatch.setattr(watcher, "_delete_blob", lambda blob: h.deleted.append(blob))
    return h


def good_3mf_bytes(tmp_path, **kw) -> bytes:
    return build_3mf(tmp_path / "payload.gcode.3mf", **kw).read_bytes()


def blob(name="ab123", target="any", filename="bracket.gcode.3mf"):
    return {"pathname": f"incoming/{name}__{target}__{filename}", "url": "https://blob/x"}


def upload_row():
    return db.list_recent_uploads(10)[0]


# --------------------------------------------------------------------------- pure helpers

def test_parse_pathname():
    p = fw.FileWatcher._parse_pathname
    assert p("incoming/ab123__SERIAL__bracket.gcode.3mf") == ("ab123", "SERIAL", "bracket.gcode.3mf")
    assert p("incoming/ab123__bracket.gcode.3mf") == ("ab123", "any", "bracket.gcode.3mf")
    assert p("incoming/") == ("anon", "any", "submission.gcode.3mf")
    # the filename may itself contain "__"
    assert p("incoming/a__any__x__y.gcode.3mf") == ("a", "any", "x__y.gcode.3mf")


@pytest.mark.parametrize("name,expected", [
    ("part.gcode.3mf", "part.gcode.3mf"),
    ("part.3mf", "part.gcode.3mf"),
    ("part.gcode", "part.gcode.3mf"),
    ("PART.GCODE.3MF", "PART.gcode.3mf"),
    ("part", "part.gcode.3mf"),
])
def test_normalize_gcode_name(name, expected):
    assert fw._normalize_gcode_name(name) == expected


def test_sanitize_is_shared_with_the_backfill():
    from station.matching import sanitize_name
    assert fw._sanitize is sanitize_name
    assert fw._sanitize("Miguel Ramirez/../x") == "Miguel_Ramirez_.._x"
    assert fw._sanitize("   ") == "anon"


# --------------------------------------------------------------------------- feature hook

def test_happy_path_archives_deletes_blob_and_records_features(harness):
    harness.payload = good_3mf_bytes(harness.tmp_path, names=["Bracket"])
    harness.watcher._process_blob(blob())

    archived = harness.inbox / "ab123" / "ab123_bracket.gcode.3mf"
    assert archived.read_bytes() == harness.payload
    assert harness.deleted == [blob()]
    row = upload_row()
    assert row["upload_status"] == "done"
    assert row["file_sha256"] == gf.sha256_file(archived)
    assert row["design_family_id"] and row["attempt_index"] == 1
    assert row["gcode_md5"]
    conn = sqlite3.connect(db.DB_PATH)
    assert conn.execute("SELECT layer_count FROM file_features").fetchone()[0] == 2


def test_second_tweaked_upload_joins_the_family(harness):
    harness.payload = good_3mf_bytes(harness.tmp_path, names=["Bracket"])
    harness.watcher._process_blob(blob(filename="bracket.gcode.3mf"))
    harness.payload = good_3mf_bytes(harness.tmp_path, names=["Bracket"],
                                     config_overrides={"sparse_infill_density": "30%"})
    harness.watcher._process_blob(blob(filename="bracket_v2.gcode.3mf"))
    rows = {r["renamed_filename"]: r for r in db.list_recent_uploads(10)}
    a, b = rows["ab123_bracket.gcode.3mf"], rows["ab123_bracket_v2.gcode.3mf"]
    assert a["design_family_id"] == b["design_family_id"] and b["attempt_index"] == 2


def test_feature_extraction_crash_never_blocks_archive_or_blob_deletion(harness, monkeypatch):
    harness.payload = good_3mf_bytes(harness.tmp_path)
    monkeypatch.setattr(gf, "extract_features", lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
    harness.watcher._process_blob(blob())

    assert (harness.inbox / "ab123" / "ab123_bracket.gcode.3mf").exists()
    assert harness.deleted == [blob()]
    row = upload_row()
    assert row["upload_status"] == "done"          # still a successful submission
    assert row["file_sha256"] is None              # but no features recorded


def test_database_failure_while_recording_features_is_non_fatal(harness, monkeypatch):
    harness.payload = good_3mf_bytes(harness.tmp_path)

    def broken(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "record_upload_features", broken)
    harness.watcher._process_blob(blob())
    assert (harness.inbox / "ab123" / "ab123_bracket.gcode.3mf").exists()
    assert upload_row()["upload_status"] == "done"


def test_non_zip_file_is_archived_and_recorded_as_unreadable(harness):
    harness.payload = b"definitely not a 3mf"
    harness.watcher._process_blob(blob())
    assert (harness.inbox / "ab123" / "ab123_bracket.gcode.3mf").exists()
    assert harness.deleted == [blob()]
    row = upload_row()
    assert row["file_sha256"] and row["design_family_id"] is None
    err = sqlite3.connect(db.DB_PATH).execute("SELECT error FROM file_features").fetchone()[0]
    assert "not a valid" in err


def test_identical_resubmission_reuses_the_stored_extraction(harness, monkeypatch):
    harness.payload = good_3mf_bytes(harness.tmp_path)
    harness.watcher._process_blob(blob(filename="a.gcode.3mf"))

    calls = []
    real = gf.extract_features
    monkeypatch.setattr(gf, "extract_features", lambda p: calls.append(p) or real(p))
    harness.watcher._process_blob(blob(filename="a_again.gcode.3mf"))   # same bytes
    assert calls == []                                                   # cache hit
    assert sqlite3.connect(db.DB_PATH).execute("SELECT COUNT(*) FROM file_features").fetchone()[0] == 1


def test_download_failure_marks_failed_keeps_blob_and_records_no_features(harness):
    harness.download_error = httpx.ConnectError("down")
    harness.watcher._process_blob(blob())
    row = upload_row()
    assert row["upload_status"] == "failed"
    assert harness.deleted == []
    assert not (harness.inbox / "ab123").exists()
    assert row["file_sha256"] is None
