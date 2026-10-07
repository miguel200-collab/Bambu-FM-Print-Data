import json
import queue
import sqlite3

import pytest

import database as db
from mqtt_listener import PrinterMonitor, PrintEndEvent


class Msg:
    def __init__(self, payload):
        self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()


@pytest.fixture
def mon(tmp_db):
    q: queue.Queue = queue.Queue()
    m = PrinterMonitor(hostname="10.0.0.9", serial="SER1", access_code="12345678", name="The Allee", event_queue=q)
    m.q = q
    return m


def feed(m, **print_fields):
    m._on_message(None, None, Msg({"print": print_fields}))


def events(m):
    out = []
    while not m.q.empty():
        out.append(m.q.get_nowait())
    return out


def jobs():
    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM print_jobs ORDER BY start_time").fetchall()
    finally:
        conn.close()


# --------------------------------------------------------------------------- baseline handling

@pytest.mark.parametrize("state", ["FINISH", "FAILED", "IDLE"])
def test_leftover_terminal_or_idle_state_at_startup_is_ignored(mon, state):
    """A printer still showing last night's FINISH must not look like a new completion."""
    feed(mon, gcode_state=state)
    assert events(mon) == [] and jobs() == []


def test_print_already_running_at_startup_is_tracked(mon):
    feed(mon, gcode_state="RUNNING", subtask_name="resumed_part")
    rows = jobs()
    assert len(rows) == 1 and rows[0]["subtask_name"] == "resumed_part" and rows[0]["end_time"] is None
    feed(mon, gcode_state="FINISH")
    assert [e.final_state for e in events(mon)] == ["FINISH"]


# --------------------------------------------------------------------------- normal lifecycle

def test_start_then_finish_creates_one_job_and_one_event(mon):
    feed(mon, gcode_state="IDLE")
    feed(mon, gcode_state="RUNNING", subtask_name="ab123_bracket", gcode_file="/data/Metadata/plate_1.gcode",
         nozzle_temper=220.5, bed_temper=55.0, vt_tray={"tray_type": "PLA"})
    feed(mon, gcode_state="FINISH", nozzle_temper=30.0)

    (job,) = jobs()
    assert job["printer_serial"] == "SER1" and job["printer_ip"] == "10.0.0.9"
    assert job["subtask_name"] == "ab123_bracket"
    assert job["final_state"] == "FINISH" and job["end_time"] and job["start_time"]
    assert job["filament_type"] == "PLA"
    assert job["nozzle_temper"] == 30.0                       # end-of-print telemetry overwrites
    assert job["user_label"] is None and job["era"] == 1
    (evt,) = events(mon)
    assert isinstance(evt, PrintEndEvent)
    assert (evt.job_id, evt.printer_name, evt.subtask_name, evt.final_state) == (
        job["job_id"], "The Allee", "ab123_bracket", "FINISH")


def test_failed_print_records_state_and_print_error(mon):
    feed(mon, gcode_state="IDLE")
    feed(mon, gcode_state="RUNNING", subtask_name="x")
    feed(mon, gcode_state="RUNNING", print_error=50348044)    # error arrives mid-print
    feed(mon, gcode_state="FAILED")
    (job,) = jobs()
    assert job["final_state"] == "FAILED" and job["print_error"] == 50348044
    assert [e.final_state for e in events(mon)] == ["FAILED"]


def test_zero_print_error_is_not_recorded(mon):
    feed(mon, gcode_state="IDLE")
    feed(mon, gcode_state="RUNNING", print_error=0)
    feed(mon, gcode_state="FINISH")
    assert jobs()[0]["print_error"] is None


def test_print_error_does_not_leak_into_the_next_job(mon):
    feed(mon, gcode_state="IDLE")
    feed(mon, gcode_state="RUNNING")
    feed(mon, gcode_state="RUNNING", print_error=7)           # error arrives mid-print
    feed(mon, gcode_state="FAILED")
    feed(mon, gcode_state="RUNNING")                          # next print starts
    feed(mon, gcode_state="FINISH")
    first, second = jobs()
    assert first["print_error"] == 7 and second["print_error"] is None


def test_error_code_on_the_message_that_starts_a_print_is_treated_as_stale(mon):
    """The printer may still report the PREVIOUS print's error when a new one starts."""
    feed(mon, gcode_state="IDLE")
    feed(mon, gcode_state="RUNNING", print_error=7)
    feed(mon, gcode_state="FINISH")
    assert jobs()[0]["print_error"] is None


def test_prepare_then_running_is_one_job(mon):
    feed(mon, gcode_state="IDLE")
    feed(mon, gcode_state="PREPARE")
    feed(mon, gcode_state="RUNNING")
    feed(mon, gcode_state="FINISH")
    assert len(jobs()) == 1 and len(events(mon)) == 1


def test_repeated_identical_states_do_not_duplicate(mon):
    feed(mon, gcode_state="IDLE")
    for _ in range(5):
        feed(mon, gcode_state="RUNNING")
    for _ in range(5):
        feed(mon, gcode_state="FINISH")
    assert len(jobs()) == 1 and len(events(mon)) == 1


def test_back_to_back_prints_make_two_jobs(mon):
    feed(mon, gcode_state="IDLE")
    for name in ("first", "second"):
        feed(mon, gcode_state="RUNNING", subtask_name=name)
        feed(mon, gcode_state="FINISH")
    assert [j["subtask_name"] for j in jobs()] == ["first", "second"]
    assert [e.subtask_name for e in events(mon)] == ["first", "second"]


def test_terminal_state_without_a_tracked_job_is_ignored(mon):
    feed(mon, gcode_state="IDLE")
    feed(mon, gcode_state="FINISH")                            # we never saw this print start
    assert events(mon) == [] and jobs() == []


def test_jobs_are_stamped_with_the_current_era(mon):
    db.set_current_era(2)
    feed(mon, gcode_state="IDLE")
    feed(mon, gcode_state="RUNNING")
    feed(mon, gcode_state="FINISH")
    assert jobs()[0]["era"] == 2


# --------------------------------------------------------------------------- pause / resume

def test_pause_and_resume_stays_one_job(mon):
    """
    A student pausing a print must not create a second job row (and orphan the
    first, which would never get an end state / label).
    """
    feed(mon, gcode_state="IDLE")
    feed(mon, gcode_state="RUNNING", subtask_name="long_print")
    feed(mon, gcode_state="PAUSE")
    feed(mon, gcode_state="RUNNING")
    feed(mon, gcode_state="FINISH")

    rows = jobs()
    assert len(rows) == 1, "pause/resume created a duplicate job"
    assert rows[0]["final_state"] == "FINISH"
    assert len(events(mon)) == 1


def test_paused_print_that_is_then_cancelled_ends_the_original_job(mon):
    feed(mon, gcode_state="IDLE")
    feed(mon, gcode_state="RUNNING")
    feed(mon, gcode_state="PAUSE")
    feed(mon, gcode_state="FAILED")
    (job,) = jobs()
    assert job["final_state"] == "FAILED"
    assert len(events(mon)) == 1


def test_a_new_print_after_an_abandoned_one_is_a_new_job(mon):
    """RUNNING -> IDLE (no terminal state seen) then RUNNING again is a NEW print, not a resume."""
    feed(mon, gcode_state="IDLE")
    feed(mon, gcode_state="RUNNING", subtask_name="abandoned")
    feed(mon, gcode_state="IDLE")
    feed(mon, gcode_state="RUNNING", subtask_name="next")
    feed(mon, gcode_state="FINISH")
    names = [j["subtask_name"] for j in jobs()]
    assert names == ["abandoned", "next"]


# --------------------------------------------------------------------------- telemetry

def test_empty_values_never_overwrite_known_telemetry(mon):
    feed(mon, gcode_state="IDLE", subtask_name="keep_me", gcode_file="plate_1.gcode")
    feed(mon, subtask_name="", gcode_file=None)
    snap = mon.snapshot()
    assert snap["subtask_name"] == "keep_me" and snap["gcode_file"] == "plate_1.gcode"


def test_filament_from_vt_tray_then_ams(mon):
    assert PrinterMonitor._extract_filament({"vt_tray": {"tray_type": "PETG"}}) == "PETG"
    ams = {"ams": {"ams": [{"tray": [{"tray_type": ""}, {"tray_type": "ABS"}]}]}}
    assert PrinterMonitor._extract_filament(ams) == "ABS"
    assert PrinterMonitor._extract_filament({"vt_tray": {"tray_type": "PLA"}, **ams}) == "PLA"   # vt_tray wins
    assert PrinterMonitor._extract_filament({}) is None
    assert PrinterMonitor._extract_filament({"ams": "garbage"}) is None


def test_snapshot_shape_and_ipcam_url(mon):
    feed(mon, gcode_state="IDLE", nozzle_temper=21.0, bed_temper=22.0)
    mon._on_message(None, None, Msg({"ipcam": {"ipcam_url": "http://10.0.0.9/stream.jpg"}}))
    snap = mon.snapshot()
    assert set(snap) == {"name", "serial", "ip", "connected", "state", "subtask_name", "gcode_file",
                         "nozzle_temper", "bed_temper", "filament_type", "camera_url"}
    assert snap["state"] == "IDLE" and snap["nozzle_temper"] == 21.0 and snap["bed_temper"] == 22.0
    assert snap["camera_url"] == "http://10.0.0.9/stream.jpg" == mon.camera_url()
    assert snap["connected"] is False                          # never connected in the test
    json.dumps(snap)                                           # API-serialisable


# --------------------------------------------------------------------------- hostile / broken input

@pytest.mark.parametrize("payload", [
    b"not json", b"\xff\xfe\x00bad utf8", b"", b"[]", b"null", b'"string"', b"123",
    json.dumps({"print": "not a dict"}).encode(),
    json.dumps({"print": {}}).encode(),                         # no gcode_state
    json.dumps({"info": {"command": "x"}}).encode(),
])
def test_garbage_messages_are_ignored_without_raising(mon, payload):
    mon._on_message(None, None, Msg(payload))
    assert events(mon) == [] and jobs() == []


def test_non_numeric_print_error_is_ignored(mon):
    feed(mon, gcode_state="IDLE")
    feed(mon, gcode_state="RUNNING", print_error="not-a-number")
    feed(mon, gcode_state="FINISH")
    assert jobs()[0]["print_error"] is None


def test_monitor_construction_does_not_touch_the_network(tmp_db):
    m = PrinterMonitor(hostname="203.0.113.1", serial="S", access_code="x", name="n", event_queue=queue.Queue())
    assert m.is_connected() is False and m.serial == "S" and m.hostname == "203.0.113.1" and m.name == "n"
