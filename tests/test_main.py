import queue
import subprocess
import sys
import threading
from pathlib import Path

import main
from mqtt_listener import PrintEndEvent

REPO = Path(__file__).resolve().parent.parent


def evt(job_id="j1", state="FINISH"):
    return PrintEndEvent(job_id=job_id, printer_name="The Allee", subtask_name="part", final_state=state)


def test_importing_main_does_not_load_tkinter():
    """The daemon is headless: no GUI toolkit may be pulled in by main."""
    code = "import sys, main; sys.exit(1 if 'tkinter' in sys.modules else 0)"
    r = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_gui_module_is_gone():
    assert not (REPO / "labeler_gui.py").exists()


class Recorder:
    def __init__(self, stop, fail_on=None, stop_after=None):
        self.events = []
        self.stop = stop
        self.fail_on = fail_on
        self.stop_after = stop_after

    def notify_print_end(self, event):
        self.events.append(event)
        if self.stop_after is not None and len(self.events) >= self.stop_after:
            self.stop.set()
        if event.job_id == self.fail_on:
            raise RuntimeError("boom")


def test_event_loop_delivers_events_in_order_then_stops():
    q, stop = queue.Queue(), threading.Event()
    q.put(evt("a"))
    q.put(evt("b", "FAILED"))
    rec = Recorder(stop, stop_after=2)
    main._run_event_loop(q, rec, stop, poll_s=0.01)
    assert [e.job_id for e in rec.events] == ["a", "b"]


def test_event_loop_survives_a_failing_notifier():
    q, stop = queue.Queue(), threading.Event()
    for jid in ("a", "bad", "c"):
        q.put(evt(jid))
    rec = Recorder(stop, fail_on="bad", stop_after=3)
    main._run_event_loop(q, rec, stop, poll_s=0.01)
    assert [e.job_id for e in rec.events] == ["a", "bad", "c"]   # "c" still delivered


def test_event_loop_works_without_a_notifier_and_exits_on_stop():
    q, stop = queue.Queue(), threading.Event()
    q.put(evt("a"))
    t = threading.Thread(target=main._run_event_loop, args=(q, None, stop), kwargs={"poll_s": 0.01})
    t.start()
    stop.set()
    t.join(timeout=3)
    assert not t.is_alive()


def test_event_loop_heartbeat_fires_on_schedule_and_survives_errors():
    q, stop = queue.Queue(), threading.Event()
    beats = []
    ticks = iter(range(0, 1000))

    def heartbeat():
        beats.append(1)
        if len(beats) == 1:
            raise RuntimeError("heartbeat failure must not kill the loop")
        if len(beats) >= 3:
            stop.set()

    # Fake clock advances 10s per call; interval 25s -> a beat every ~3 iterations.
    main._run_event_loop(q, None, stop, heartbeat=heartbeat, heartbeat_interval_s=25,
                         poll_s=0.001, clock=lambda: next(ticks) * 10.0)
    assert len(beats) == 3


def test_make_heartbeat_reports_connected_printers(caplog):
    class M:
        def __init__(self, ok):
            self.ok = ok

        def is_connected(self):
            return self.ok

    with caplog.at_level("INFO", logger="main"):
        main._make_heartbeat([M(True), M(False)])()
        main._make_heartbeat([])()
    text = caplog.text
    assert "1/2 printer(s) connected" in text
    assert "no live monitors" in text


def test_mock_run_writes_job_and_event_flows_through_the_loop(tmp_db, monkeypatch):
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    q, stop = queue.Queue(), threading.Event()

    main._run_mock(q)

    event = q.get_nowait()
    q.put(event)
    rec = Recorder(stop, stop_after=1)
    main._run_event_loop(q, rec, stop, poll_s=0.01)

    import database as db
    assert rec.events[0].final_state == "FINISH"
    job = db.get_job(rec.events[0].job_id)
    assert job["printer_serial"] == "MOCK_SERIAL_001"
    assert job["era"] == 1


def test_station_services_are_skipped_without_a_web_section():
    # No web config -> nothing started (and in particular no open HTTP port).
    assert main._start_station_services([], {}) == []
    assert main._start_station_services([], {"printers": []}) == []
