import sqlite3
import threading
import time
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
    monkeypatch.setattr(watcher, "_delete_blob", lambda blob: h.deleted.append(blob) or True)
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


# --------------------------------------------------------------------------- ZIP / sliced checks (M4)

def test_zip_signature_check(tmp_path):
    ok = tmp_path / "ok"
    ok.write_bytes(b"PK\x03\x04rest")
    bad = tmp_path / "bad"
    bad.write_bytes(b"G28\nG1 X1")
    short = tmp_path / "short"
    short.write_bytes(b"PK")
    f = fw.FileWatcher._has_zip_signature
    assert f(ok) and not f(bad) and not f(short)
    assert not f(tmp_path / "missing")


def test_non_zip_upload_logs_a_warning_but_is_still_archived(harness, caplog):
    harness.payload = b"; just gcode text\nG1 X1\n"
    with caplog.at_level("WARNING", logger="station.file_watcher"):
        harness.watcher._process_blob(blob(filename="fake.gcode.3mf"))
    assert "not a ZIP/3MF package" in caplog.text
    assert (harness.inbox / "ab123" / "ab123_fake.gcode.3mf").exists()   # warning, not failure
    assert harness.deleted == [blob(filename="fake.gcode.3mf")]
    assert upload_row()["upload_status"] == "done"


def test_valid_zip_has_no_signature_warning(harness, caplog):
    harness.payload = good_3mf_bytes(harness.tmp_path)
    with caplog.at_level("WARNING", logger="station.file_watcher"):
        harness.watcher._process_blob(blob())
    assert "not a ZIP/3MF package" not in caplog.text
    assert "contains no G-code" not in caplog.text


def test_plain_3mf_model_warns_that_it_has_no_gcode(harness, caplog):
    """The exact cause of the Jul-30 BFM 'file error': a model renamed to .gcode.3mf."""
    harness.payload = good_3mf_bytes(harness.tmp_path, sliced=False)
    with caplog.at_level("WARNING", logger="station.file_watcher"):
        harness.watcher._process_blob(blob(filename="model.gcode.3mf"))
    assert "contains no G-code" in caplog.text
    assert upload_row()["upload_status"] == "done"
    assert upload_row()["design_family_id"] is None
    assert sqlite3.connect(db.DB_PATH).execute("SELECT is_sliced FROM file_features").fetchone()[0] == 0


# =========================================================================== real HTTP behaviour (MockTransport)

REAL_CLIENT = httpx.Client


@pytest.fixture
def mock_http(monkeypatch):
    """Route every httpx.Client through a MockTransport driven by `handler`."""
    def install(handler):
        def factory(**kw):
            kw.pop("transport", None)
            return REAL_CLIENT(transport=httpx.MockTransport(handler), **kw)
        monkeypatch.setattr(httpx, "Client", factory)
    return install


@pytest.fixture
def plain(tmp_db, tmp_path):
    """A FileWatcher whose real _download_blob/_delete_blob/_list_pending_blobs run."""
    return fw.FileWatcher(blob_token="tok", get_monitors=lambda: [], base_dir=tmp_path,
                          inbox_dir=tmp_path / "inbox")


def test_download_sends_bearer_and_prefers_the_signed_url(plain, mock_http, tmp_path):
    seen = []
    mock_http(lambda req: seen.append(req) or httpx.Response(200, content=b"PK\x03\x04payload"))
    dest = tmp_path / "out.bin"
    plain._download_blob({"url": "https://private.blob/x", "downloadUrl": "https://signed.blob/x?sig=1"}, dest)
    assert str(seen[0].url) == "https://signed.blob/x?sig=1"
    assert seen[0].headers["authorization"] == "Bearer tok"
    assert dest.read_bytes() == b"PK\x03\x04payload"


def test_download_falls_back_to_the_plain_url_with_bearer_auth(plain, mock_http, tmp_path):
    """Regression (Jul 30): private blobs 403 without the Bearer token on the plain url."""
    def handler(req):
        if req.headers.get("authorization") != "Bearer tok":
            return httpx.Response(403)
        return httpx.Response(200, content=b"data")

    mock_http(handler)
    dest = tmp_path / "out.bin"
    plain._download_blob({"url": "https://private.blob/x"}, dest)
    assert dest.read_bytes() == b"data"


def test_download_streams_files_larger_than_one_chunk(plain, mock_http, tmp_path):
    big = b"x" * (fw._CHUNK * 2 + 123)
    mock_http(lambda req: httpx.Response(200, content=big))
    dest = tmp_path / "big.bin"
    plain._download_blob({"url": "https://b/x"}, dest)
    assert dest.stat().st_size == len(big)


def test_download_errors(plain, mock_http, tmp_path):
    mock_http(lambda req: httpx.Response(403))
    with pytest.raises(httpx.HTTPStatusError):
        plain._download_blob({"url": "https://b/x"}, tmp_path / "o")
    with pytest.raises(fw.printer_uploader.UploadError):
        plain._download_blob({}, tmp_path / "o")


def test_process_blob_with_a_403_download_fails_cleanly(plain, mock_http, tmp_path):
    deleted = []
    plain._delete_blob = lambda b: deleted.append(b)
    mock_http(lambda req: httpx.Response(403))
    plain._process_blob(blob())
    row = upload_row()
    assert row["upload_status"] == "failed" and "403" in row["error_message"]
    assert deleted == []                                   # blob kept for a retry
    assert not (tmp_path / "inbox" / "ab123").exists()
    assert list((tmp_path / "incoming").iterdir()) == []   # no temp file left behind


def test_list_pending_blobs_paginates_with_bearer_and_prefix(plain, mock_http):
    seen = []

    def handler(req):
        seen.append(req)
        if "cursor" not in req.url.params:
            return httpx.Response(200, json={"blobs": [{"pathname": "incoming/a"}], "hasMore": True, "cursor": "c1"})
        return httpx.Response(200, json={"blobs": [{"pathname": "incoming/b"}], "hasMore": False})

    mock_http(handler)
    assert [b["pathname"] for b in plain._list_pending_blobs()] == ["incoming/a", "incoming/b"]
    assert all(r.headers["authorization"] == "Bearer tok" for r in seen)
    assert seen[0].url.params["prefix"] == "incoming/"
    assert seen[1].url.params["cursor"] == "c1"


def test_list_pending_blobs_returns_what_it_has_on_an_error_page(plain, mock_http):
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(200, json={"blobs": [{"pathname": "incoming/a"}], "hasMore": True, "cursor": "c"})
        return httpx.Response(500, text="boom")

    mock_http(handler)
    assert [b["pathname"] for b in plain._list_pending_blobs()] == ["incoming/a"]


def test_list_pending_blobs_caps_pages(plain, mock_http):
    """A broken API that always says hasMore must not loop forever / burn Blob operations."""
    calls = []
    mock_http(lambda req: calls.append(1) or httpx.Response(
        200, json={"blobs": [], "hasMore": True, "cursor": "again"}))
    plain._list_pending_blobs()
    assert len(calls) == 10


def test_delete_blob_sends_authenticated_delete(plain, mock_http):
    seen = []
    mock_http(lambda req: seen.append(req) or httpx.Response(200))
    plain._delete_blob({"url": "https://b/x", "pathname": "incoming/x"})
    (req,) = seen
    assert req.method == "DELETE" and req.headers["authorization"] == "Bearer tok"
    import json as _json
    assert _json.loads(req.content) == {"urls": ["https://b/x"]}


def test_delete_blob_without_url_does_nothing_and_failure_only_warns(plain, mock_http, caplog):
    seen = []
    mock_http(lambda req: seen.append(req) or httpx.Response(500))
    plain._delete_blob({"pathname": "incoming/x"})
    assert seen == []
    with caplog.at_level("WARNING", logger="station.file_watcher"):
        plain._delete_blob({"url": "https://b/x"})              # does not raise
    assert "Blob delete returned HTTP 500" in caplog.text


# =========================================================================== printer push is non-fatal

class FakeMonitor:
    def __init__(self, serial="SER1", state="IDLE"):
        self.serial = serial
        self.hostname = "10.0.0.5"
        self.name = f"printer-{serial}"
        self._access_code = "12345678"
        self._state = state

    def snapshot(self):
        return {"state": self._state}


def with_monitors(h, monitors):
    h.watcher._get_monitors = lambda: monitors


def test_printer_push_failure_is_non_fatal(harness, monkeypatch):
    """Regression (Jul 30): a failed push must not block archiving or blob deletion."""
    with_monitors(harness, [FakeMonitor()])
    harness.payload = good_3mf_bytes(harness.tmp_path)

    def boom(**kw):
        raise fw.printer_uploader.UploadError("printer unreachable")

    monkeypatch.setattr(fw.printer_uploader, "upload_file", boom)
    harness.watcher._process_blob(blob())
    assert (harness.inbox / "ab123" / "ab123_bracket.gcode.3mf").exists()
    assert harness.deleted == [blob()]
    assert upload_row()["upload_status"] == "done"


def test_printer_push_is_called_with_the_renamed_file_and_never_auto_starts(harness, monkeypatch):
    with_monitors(harness, [FakeMonitor()])
    harness.payload = good_3mf_bytes(harness.tmp_path)
    calls = []
    monkeypatch.setattr(fw.printer_uploader, "upload_file", lambda **kw: calls.append(kw))
    harness.watcher._process_blob(blob(target="SER1"))
    (kw,) = calls
    assert kw["printer_ip"] == "10.0.0.5" and kw["access_code"] == "12345678"
    assert kw["remote_filename"] == "ab123_bracket.gcode.3mf"
    assert kw["auto_start"] is False
    assert upload_row()["printer_serial"] == "SER1"


def test_no_printers_at_all_is_non_fatal(harness):
    harness.payload = good_3mf_bytes(harness.tmp_path)
    harness.watcher._process_blob(blob())
    assert (harness.inbox / "ab123" / "ab123_bracket.gcode.3mf").exists()
    assert upload_row()["printer_serial"] is None


def test_resolve_monitor_rules(harness):
    busy, idle, other = FakeMonitor("A", "RUNNING"), FakeMonitor("B", "IDLE"), FakeMonitor("C", "FINISH")
    w = harness.watcher
    w._get_monitors = lambda: [busy, idle, other]
    assert w._resolve_monitor("C") is other                     # explicit serial wins
    assert w._resolve_monitor("any") is idle                    # prefers a free printer
    assert w._resolve_monitor("GONE") is idle                   # vanished target -> auto-pick
    w._get_monitors = lambda: [busy]
    assert w._resolve_monitor("any") is busy                    # nothing free -> first
    w._get_monitors = lambda: []
    assert w._resolve_monitor("any") is None


# =========================================================================== idempotence + retries

def test_a_failed_blob_is_retried_later(harness):
    harness.download_error = httpx.ConnectError("down")
    harness.watcher._process_blob(blob())
    harness.download_error = None
    harness.payload = good_3mf_bytes(harness.tmp_path)
    harness.watcher._process_blob(blob())
    statuses = sorted(r["upload_status"] for r in db.list_recent_uploads(10))
    assert statuses == ["done", "failed"]
    assert (harness.inbox / "ab123" / "ab123_bracket.gcode.3mf").exists()


def test_reupload_of_the_same_name_replaces_the_archived_copy(harness, monkeypatch):
    """
    Regression: the blob is deleted after processing, so re-exporting the SAME file
    name later is a NEW upload at the same pathname. It used to be dropped forever.
    """
    monkeypatch.setattr(fw, "_DUPLICATE_WINDOW_S", 0.0)       # "some time later"
    harness.payload = good_3mf_bytes(harness.tmp_path, names=["A"])
    harness.watcher._process_blob(blob())
    first_bytes = harness.payload
    harness.payload = good_3mf_bytes(harness.tmp_path, names=["A"], config_overrides={"wall_loops": "6"})
    harness.watcher._process_blob(blob())

    archived = harness.inbox / "ab123" / "ab123_bracket.gcode.3mf"
    assert archived.read_bytes() == harness.payload != first_bytes
    rows = db.list_recent_uploads(10)
    assert [r["upload_status"] for r in rows] == ["done", "done"]
    assert harness.deleted == [blob(), blob()]                 # both blobs were cleaned up


def test_duplicate_notification_right_after_completion_is_ignored(harness):
    harness.payload = good_3mf_bytes(harness.tmp_path)
    harness.watcher._process_blob(blob())
    harness.download_error = RuntimeError("must not be downloaded again")
    harness.watcher._process_blob(blob())                      # queue duplicate, moments later
    assert len(db.list_recent_uploads(10)) == 1
    assert harness.deleted == [blob()]


def test_blob_that_could_not_be_deleted_is_never_reprocessed(harness, monkeypatch):
    """Same object still listed (delete failed): re-archiving it would create duplicates."""
    monkeypatch.setattr(fw, "_DUPLICATE_WINDOW_S", 0.0)
    harness.watcher._delete_blob = lambda b: False
    harness.payload = good_3mf_bytes(harness.tmp_path)
    harness.watcher._process_blob(blob())
    assert upload_row()["blob_deleted"] == 0
    harness.download_error = RuntimeError("must not be downloaded again")
    harness.watcher._process_blob(blob())
    assert len(db.list_recent_uploads(10)) == 1


def test_delete_exception_is_non_fatal_and_recorded(harness):
    """A network blip while deleting must not undo a successfully archived submission."""
    def boom(b):
        raise httpx.ConnectError("flaky wifi")

    harness.watcher._delete_blob = boom
    harness.payload = good_3mf_bytes(harness.tmp_path)
    harness.watcher._process_blob(blob())
    assert (harness.inbox / "ab123" / "ab123_bracket.gcode.3mf").exists()
    row = upload_row()
    assert row["upload_status"] == "done" and row["blob_deleted"] == 0
    assert row["file_sha256"]                                   # features still recorded


def test_successful_delete_is_recorded(harness):
    harness.payload = good_3mf_bytes(harness.tmp_path)
    harness.watcher._process_blob(blob())
    assert upload_row()["blob_deleted"] == 1


def test_archive_failure_keeps_the_blob_so_nothing_is_lost(harness, monkeypatch):
    """The blob must only be deleted AFTER the file is safely archived."""
    harness.payload = good_3mf_bytes(harness.tmp_path)

    def boom(self, target):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", boom)
    harness.watcher._process_blob(blob())
    row = upload_row()
    assert row["upload_status"] == "failed" and "disk full" in row["error_message"]
    assert harness.deleted == []                                # blob still in Vercel for a retry
    assert not (harness.inbox / "ab123" / "ab123_bracket.gcode.3mf").exists()
    assert list((harness.tmp_path / "incoming").iterdir()) == []


# =========================================================================== hostile pathnames

HOSTILE = [
    "incoming/..__any__f.gcode.3mf",
    "incoming/.__any__f.gcode.3mf",
    "incoming/../../evil__any__f.gcode.3mf",
    "incoming/a/b__any__c/d.gcode.3mf",
    "incoming/..\\..\\evil__any__f.gcode.3mf",
    "incoming/C:\\Windows__any__f.gcode.3mf",
    "incoming/ab123__../../..__f.gcode.3mf",
    "incoming/ab123__any__..\\..\\x.gcode.3mf",
    "incoming/ab123__any__../../x.gcode.3mf",
    "incoming/ab123__any__...",
]


@pytest.mark.parametrize("pathname", HOSTILE)
def test_hostile_pathnames_can_never_write_outside_the_inbox(harness, pathname):
    harness.payload = good_3mf_bytes(harness.tmp_path)
    harness.watcher._process_blob({"pathname": pathname, "url": "https://b/x"})

    allowed = {"inbox", "incoming", "payload.gcode.3mf"}
    stray = {p.name for p in harness.tmp_path.iterdir() if p.name not in allowed and not p.name.startswith("test_dataset")}
    assert stray == set(), f"files escaped the inbox: {stray}"
    archived = [p for p in harness.inbox.rglob("*") if p.is_file()]
    assert len(archived) == 1
    assert harness.inbox.resolve() in archived[0].resolve().parents


def test_parse_pathname_sanitizes_every_part():
    p = fw.FileWatcher._parse_pathname
    assert p("incoming/..__any__f.gcode.3mf") == ("anon", "any", "f.gcode.3mf")
    assert p("incoming/a b__x/y__my file.gcode.3mf") == ("a_b", "x_y", "my_file.gcode.3mf")
    assert p("incoming/ab123__any__..") == ("ab123", "any", "anon")


# =========================================================================== lifecycle

def wait_for(predicate, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_startup_catch_up_enqueues_only_unprocessed_blobs(plain, monkeypatch):
    done = blob(filename="old.gcode.3mf")
    db.create_upload("u-old", done["pathname"], "ab123", "old", "ab123_old.gcode.3mf", "any",
                     "2026-10-01T00:00:00+00:00")
    db.mark_uploaded("u-old", None, "2026-10-01T00:00:00+00:00", status="done")
    new = blob(filename="new.gcode.3mf")
    processed, got_new = [], threading.Event()
    monkeypatch.setattr(plain, "_list_pending_blobs", lambda: [done, new])
    monkeypatch.setattr(plain, "_process_blob", lambda b: (processed.append(b["pathname"]), got_new.set()))
    plain.start()
    try:
        assert got_new.wait(3.0)
    finally:
        plain.stop()
    assert processed == [new["pathname"]]


def test_webhook_enqueue_is_processed_by_the_worker_thread(plain, monkeypatch):
    seen = threading.Event()
    got = []
    monkeypatch.setattr(plain, "_list_pending_blobs", lambda: [])
    monkeypatch.setattr(plain, "_process_blob", lambda b: (got.append(b), seen.set()))
    plain.start()
    try:
        plain.enqueue_blob({"pathname": "incoming/x__any__f.gcode.3mf", "url": "u"})
        assert seen.wait(3.0)
    finally:
        plain.stop()
    assert got[0]["pathname"].endswith("f.gcode.3mf")


def test_a_crashing_blob_never_kills_the_worker(plain, monkeypatch):
    calls, second = [], threading.Event()

    def process(b):
        calls.append(b["pathname"])
        if len(calls) == 1:
            raise RuntimeError("boom")
        second.set()

    monkeypatch.setattr(plain, "_list_pending_blobs", lambda: [])
    monkeypatch.setattr(plain, "_process_blob", process)
    plain.start()
    try:
        plain.enqueue_blob({"pathname": "one"})
        plain.enqueue_blob({"pathname": "two"})
        assert second.wait(3.0)
    finally:
        plain.stop()
    assert calls == ["one", "two"]


def test_fallback_poll_enqueues_pending_blobs_that_are_not_done(plain, monkeypatch):
    done = blob(filename="old.gcode.3mf")
    db.create_upload("u-old", done["pathname"], "ab123", "old", "ab123_old.gcode.3mf", "any", "2026-10-01T00:00:00+00:00")
    db.mark_uploaded("u-old", None, "2026-10-01T00:00:00+00:00", status="done")
    new = blob(filename="new.gcode.3mf")
    monkeypatch.setattr(plain, "_list_pending_blobs", lambda: [done, new])
    plain._fallback_poll()
    assert plain._queue.get_nowait() == new
    assert plain._queue.empty()


def test_startup_catch_up_failure_does_not_prevent_startup(plain, monkeypatch):
    def broken():
        raise httpx.ConnectError("no internet")

    monkeypatch.setattr(plain, "_list_pending_blobs", broken)
    plain.start()
    try:
        assert plain._thread.is_alive()
    finally:
        plain.stop()
