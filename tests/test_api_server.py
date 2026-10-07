from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import database as db
from station import api_server
from tests.conftest import make_job

SECRET = "s3cret-key"
KEY = {"x-station-key": SECRET}
T0 = datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc)
NOW = T0 + timedelta(days=2)


class FakeMonitor:
    def __init__(self, serial="SERIAL_A", name="The Allee"):
        self.serial = serial
        self.name = name
        self.hostname = "10.0.0.5"
        self._access_code = "12345678"

    def snapshot(self):
        return {"name": self.name, "serial": self.serial, "connected": True, "state": "IDLE"}

    def camera_url(self):
        return None


class FakeWatcher:
    def __init__(self):
        self.blobs = []

    def enqueue_blob(self, blob):
        self.blobs.append(blob)


def make_client(secret=SECRET, watcher=None, email=False, monitors=None):
    monitors = [FakeMonitor()] if monitors is None else monitors
    app = api_server.create_app(
        lambda: monitors,
        watcher=watcher,
        webhook_secret=secret,
        email_labels_enabled=email,
        now_fn=lambda: NOW,
    )
    return TestClient(app)


def iso(dt):
    return dt.isoformat()


@pytest.fixture
def seeded(tmp_db):
    """ab123 uploaded + printed 'ab123_benchy' (ended 1 day before NOW)."""
    db.create_upload("u1", "incoming/u1", "ab123", "benchy.gcode.3mf",
                     "ab123_benchy.gcode.3mf", "any", iso(T0))
    db.mark_uploaded("u1", None, iso(T0), status="done")
    make_job("job-1", "ab123_benchy", start=iso(T0 + timedelta(hours=1)), end=iso(T0 + timedelta(hours=2)))
    return tmp_db


# --------------------------------------------------------------------------- status / camera

def test_health_and_printers():
    c = make_client()
    assert c.get("/api/health").json() == {"status": "ok", "printers": 1}
    assert c.get("/api/printers").json()[0]["serial"] == "SERIAL_A"


def test_camera_404_502_200(monkeypatch):
    c = make_client()
    assert c.get("/api/printer/NOPE/camera").status_code == 404

    monkeypatch.setattr(api_server, "fetch_snapshot", lambda **kw: None)
    assert c.get("/api/printer/SERIAL_A/camera").status_code == 502

    monkeypatch.setattr(api_server, "fetch_snapshot", lambda **kw: b"\xff\xd8jpeg")
    r = c.get("/api/printer/SERIAL_A/camera")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content == b"\xff\xd8jpeg"


# --------------------------------------------------------------------------- blob webhook

def test_blob_webhook_requires_key_and_enqueues():
    watcher = FakeWatcher()
    c = make_client(watcher=watcher)
    body = {"pathname": "incoming/a__any__f.gcode.3mf", "url": "https://x/y", "downloadUrl": "https://x/z"}

    assert c.post("/api/blob-webhook", json=body).status_code == 401
    assert c.post("/api/blob-webhook", json=body, headers={"x-station-key": "wrong"}).status_code == 401
    assert watcher.blobs == []

    r = c.post("/api/blob-webhook", json=body, headers=KEY)
    assert r.status_code == 200
    assert watcher.blobs == [{"pathname": body["pathname"], "url": "https://x/y", "downloadUrl": "https://x/z"}]


def test_blob_webhook_503_without_watcher():
    c = make_client(watcher=None)
    r = c.post("/api/blob-webhook", json={"pathname": "p", "url": "u"}, headers=KEY)
    assert r.status_code == 503


def test_non_ascii_key_is_401_not_500():
    c = make_client(watcher=FakeWatcher())
    r = c.post("/api/blob-webhook", json={"pathname": "p", "url": "u"},
               headers={"x-station-key": "caf\u00e9".encode("utf-8")})
    assert r.status_code == 401


# --------------------------------------------------------------------------- my-prints

def test_my_prints_fails_closed_without_configured_secret(seeded):
    c = make_client(secret=None)
    assert c.get("/api/my-prints?netid=ab123").status_code == 503
    assert c.get("/api/my-prints?netid=ab123", headers=KEY).status_code == 503


def test_my_prints_requires_key(seeded):
    c = make_client()
    assert c.get("/api/my-prints?netid=ab123").status_code == 401
    assert c.get("/api/my-prints?netid=ab123", headers={"x-station-key": "no"}).status_code == 401


def test_my_prints_requires_netid(seeded):
    c = make_client()
    assert c.get("/api/my-prints", headers=KEY).status_code == 400
    assert c.get("/api/my-prints?netid=%20%20", headers=KEY).status_code == 400


def test_my_prints_returns_only_the_students_prints_with_printer_name(seeded):
    c = make_client()
    r = c.get("/api/my-prints?netid=AB123", headers=KEY)
    assert r.status_code == 200
    body = r.json()
    assert body["netid"] == "ab123"
    assert body["window_days"] == 14
    assert [p["job_id"] for p in body["prints"]] == ["job-1"]
    assert body["prints"][0]["printer_name"] == "The Allee"
    assert c.get("/api/my-prints?netid=someone-else", headers=KEY).json()["prints"] == []


# --------------------------------------------------------------------------- label webhook

def label(c, **over):
    body = {"job_id": "job-1", "outcome": "failed", "source": "web", "netid": "ab123"}
    body.update(over)
    return c.post("/api/label-webhook", json=body, headers=KEY)


def test_label_fails_closed_and_requires_key(seeded):
    assert make_client(secret=None).post("/api/label-webhook", json={
        "job_id": "job-1", "outcome": "failed", "netid": "ab123"}).status_code == 503
    c = make_client()
    assert c.post("/api/label-webhook", json={"job_id": "job-1", "outcome": "failed",
                                              "netid": "ab123"}).status_code == 401
    assert db.get_job("job-1")["user_label"] is None


def test_label_success_records_label_source_and_time(seeded):
    c = make_client()
    r = label(c, outcome="failed")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "job_id": "job-1", "label": 1, "source": "web"}
    row = db.get_job("job-1")
    assert (row["user_label"], row["label_source"], row["label_time"]) == (1, "web", iso(NOW))

    # the student may correct it
    assert label(c, outcome="succeeded").json()["label"] == 0
    assert db.get_job("job-1")["user_label"] == 0


def test_label_netid_matching_is_case_insensitive(seeded):
    assert label(make_client(), netid=" AB123 ").status_code == 200


def test_label_rejects_someone_elses_print(seeded):
    c = make_client()
    assert label(c, netid="cd456").status_code == 403
    assert db.get_job("job-1")["user_label"] is None


def test_label_web_requires_netid(seeded):
    assert label(make_client(), netid=None).status_code == 400


def test_label_unknown_job_404(seeded):
    assert label(make_client(), job_id="nope").status_code == 404


def test_label_unfinished_job_409(seeded):
    make_job("running", "ab123_benchy", end=None, state=None)
    assert label(make_client(), job_id="running").status_code == 409


def test_label_window_closed_410(seeded):
    # NOW is 1 day after the job ended; move "now" 20 days on.
    app = api_server.create_app(lambda: [], webhook_secret=SECRET, now_fn=lambda: NOW + timedelta(days=20))
    r = TestClient(app).post("/api/label-webhook", headers=KEY, json={
        "job_id": "job-1", "outcome": "failed", "source": "web", "netid": "ab123"})
    assert r.status_code == 410
    assert db.get_job("job-1")["user_label"] is None


def test_label_invalid_outcome_or_source_is_422(seeded):
    c = make_client()
    assert label(c, outcome="maybe").status_code == 422
    assert label(c, source="popup").status_code == 422


def test_label_job_without_matching_upload_is_refused(seeded):
    make_job("stray", "unrelated_print", start=iso(T0 + timedelta(hours=1)), end=iso(T0 + timedelta(hours=2)))
    assert label(make_client(), job_id="stray").status_code == 403


def test_email_labels_disabled_by_default(seeded):
    c = make_client(email=False)
    r = label(c, source="email", netid=None)
    assert r.status_code == 503
    assert db.get_job("job-1")["user_label"] is None


def test_email_labels_when_enabled(seeded):
    c = make_client(email=True)
    r = label(c, source="email", netid=None, outcome="succeeded")
    assert r.status_code == 200
    row = db.get_job("job-1")
    assert (row["user_label"], row["label_source"]) == (0, "email")
