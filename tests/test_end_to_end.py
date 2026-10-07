"""
One student's whole journey through the REAL code (only the network is faked):

  upload webhook -> worker thread -> download -> archive -> blob delete ->
  feature extraction + design family -> printer events over MQTT ->
  /api/my-prints -> /api/label-webhook -> audit -> backup
"""

import json
import queue
import sqlite3
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import database as db
from analysis import audit, backup_db
from mqtt_listener import PrinterMonitor
from station import api_server
from station import file_watcher as fw
from tests.helpers import build_3mf

KEY = {"x-station-key": "e2e-secret"}


class Msg:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()


def wait_for(predicate, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def system(tmp_db, tmp_path, monkeypatch):
    """A running station: watcher thread + MQTT monitor + HTTP API, all sharing one DB."""
    db.set_current_era(2)
    inbox = tmp_path / "inbox"
    payloads: dict[str, bytes] = {}
    deleted: list[str] = []

    watcher = fw.FileWatcher(blob_token="tok", get_monitors=lambda: [], base_dir=tmp_path, inbox_dir=inbox)
    monkeypatch.setattr(watcher, "_list_pending_blobs", lambda: [])
    monkeypatch.setattr(watcher, "_download_blob",
                        lambda blob, dest: dest.write_bytes(payloads[blob["pathname"]]))
    monkeypatch.setattr(watcher, "_delete_blob", lambda blob: deleted.append(blob["pathname"]) or True)

    printer = PrinterMonitor(hostname="10.0.0.5", serial="SER1", access_code="x", name="The Allee",
                             event_queue=queue.Queue())
    app = api_server.create_app(lambda: [printer], watcher=watcher, webhook_secret="e2e-secret")
    client = TestClient(app)

    watcher.start()
    yield type("System", (), dict(
        client=client, watcher=watcher, printer=printer, inbox=inbox, payloads=payloads,
        deleted=deleted, tmp_path=tmp_path))
    watcher.stop()


def upload(system, netid, filename, **build):
    """Simulate the Vercel site's webhook for one student upload and wait for the archive."""
    pathname = f"incoming/{netid}__any__{filename}"
    path = build_3mf(system.tmp_path / f"src_{filename}", **build)
    system.payloads[pathname] = path.read_bytes()
    r = system.client.post("/api/blob-webhook", headers=KEY,
                           json={"pathname": pathname, "url": f"https://blob/{pathname}"})
    assert r.status_code == 200
    assert wait_for(lambda: fully_processed(pathname)), "worker did not finish the upload"
    return pathname


def fully_processed(pathname):
    """
    The worker marks an upload 'done' BEFORE it deletes the blob and extracts
    features (archive first, so nothing can be lost). A test that reads the row
    in between would race, so wait for the LAST step: features recorded.
    """
    return any(
        u["blob_key"] == pathname and u["upload_status"] == "done"
        and u["blob_deleted"] is not None and u["file_sha256"]
        for u in db.list_recent_uploads(100)
    )


def run_print(system, subtask, final="FINISH"):
    def feed(**f):
        system.printer._on_message(None, None, Msg({"print": f}))

    feed(gcode_state="IDLE")
    feed(gcode_state="RUNNING", subtask_name=subtask, nozzle_temper=220.0, vt_tray={"tray_type": "PLA"})
    feed(gcode_state=final)


def test_a_students_whole_journey(system):
    # --- 1. student uploads a sliced file -----------------------------------------------------
    pathname = upload(system, "ab123", "bracket.gcode.3mf", names=["Bracket"])
    archived = system.inbox / "ab123" / "ab123_bracket.gcode.3mf"
    assert archived.exists()
    assert system.deleted == [pathname]
    up = db.list_recent_uploads(10)[0]
    assert up["upload_status"] == "done" and up["era"] == 2 and up["blob_deleted"] == 1
    assert up["file_sha256"] and up["design_family_id"] and up["attempt_index"] == 1

    # --- 2. they print it from Bambu Farm Manager (the printer reports the file name) ----------
    run_print(system, "ab123_bracket")
    job = [j for j in sqlite3.connect(db.DB_PATH).execute("SELECT job_id, era, final_state FROM print_jobs")][0]
    assert job[1] == 2 and job[2] == "FINISH"

    # --- 3. they open the label page: it lists exactly that print -----------------------------
    r = system.client.get("/api/my-prints?netid=AB123", headers=KEY)
    prints = r.json()["prints"]
    assert [p["job_id"] for p in prints] == [job[0]]
    assert prints[0]["printer_name"] == "The Allee" and prints[0]["match_confidence"] == "exact"
    assert prints[0]["user_label"] is None
    assert system.client.get("/api/my-prints?netid=cd456", headers=KEY).json()["prints"] == []

    # --- 4. another student cannot label it; the owner can ------------------------------------
    body = {"job_id": job[0], "outcome": "failed", "source": "web"}
    assert system.client.post("/api/label-webhook", headers=KEY, json={**body, "netid": "cd456"}).status_code == 403
    ok = system.client.post("/api/label-webhook", headers=KEY, json={**body, "netid": "ab123"})
    assert ok.status_code == 200 and ok.json()["label"] == 1

    # --- 5. the dataset reflects it, and the audit + backup can read it -----------------------
    row = db.get_job(job[0])
    assert (row["user_label"], row["label_source"], row["era"]) == (1, "web", 2)
    result = audit.run_audit(db.DB_PATH)
    assert result["volume"]["labeled"] == 1 and result["volume"]["labeled_failed"] == 1
    assert result["volume"]["per_era"] == {2: 1}
    assert result["matching"]["upload_match_rate"] == 1.0 and result["matching"]["job_coverage"] == 1.0

    snap = backup_db.backup(db.DB_PATH, system.tmp_path / "backups", system.inbox)
    assert snap["row_counts"]["print_jobs"] == 1 and snap["row_counts"]["file_features"] == 1
    assert snap["archive"]["copied"] == 1
    restored = sqlite3.connect(snap["snapshot"])
    assert restored.execute("SELECT user_label FROM print_jobs").fetchone()[0] == 1


def test_iterating_on_a_design_builds_a_family_and_each_print_is_matched(system):
    upload(system, "ab123", "bracket_v1.gcode.3mf", names=["Bracket"])
    run_print(system, "ab123_bracket_v1", final="FAILED")
    upload(system, "ab123", "bracket_v2.gcode.3mf", names=["Bracket"],
           config_overrides={"sparse_infill_density": "40%", "wall_loops": "4"})
    run_print(system, "ab123_bracket_v2")

    ups = {u["renamed_filename"]: u for u in db.list_recent_uploads(10)}
    v1, v2 = ups["ab123_bracket_v1.gcode.3mf"], ups["ab123_bracket_v2.gcode.3mf"]
    assert v1["design_family_id"] == v2["design_family_id"]
    assert (v1["attempt_index"], v2["attempt_index"]) == (1, 2)

    conn = sqlite3.connect(db.DB_PATH)
    diff = json.loads(conn.execute("SELECT config_diff FROM uploads WHERE attempt_index = 2").fetchone()[0])
    assert diff == {"sparse_infill_density": ["15%", "40%"], "wall_loops": ["2", "4"]}

    mine = system.client.get("/api/my-prints?netid=ab123", headers=KEY).json()["prints"]
    assert sorted(p["filename"] for p in mine) == ["ab123_bracket_v1.gcode.3mf", "ab123_bracket_v2.gcode.3mf"]
    assert {p["match_confidence"] for p in mine} == {"exact"}
    # joined view an analyst would build: job outcome <- upload <- family attempt
    states = {j[0]: j[1] for j in conn.execute("SELECT subtask_name, final_state FROM print_jobs")}
    assert states == {"ab123_bracket_v1": "FAILED", "ab123_bracket_v2": "FINISH"}


def test_a_bad_upload_never_takes_the_pipeline_down(system):
    """A garbage file is archived + flagged; the next student's upload still works."""
    system.payloads["incoming/zz9__any__junk.gcode.3mf"] = b"not a zip at all"
    system.client.post("/api/blob-webhook", headers=KEY,
                       json={"pathname": "incoming/zz9__any__junk.gcode.3mf", "url": "https://blob/j"})
    assert wait_for(lambda: fully_processed("incoming/zz9__any__junk.gcode.3mf"))
    upload(system, "ab123", "good.gcode.3mf")
    assert (system.inbox / "ab123" / "ab123_good.gcode.3mf").exists()
    assert (system.inbox / "zz9" / "zz9_junk.gcode.3mf").exists()
    junk = [u for u in db.list_recent_uploads(10) if u["student_name"] == "zz9"][0]
    assert junk["design_family_id"] is None
