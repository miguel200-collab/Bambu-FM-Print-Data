from datetime import datetime, timedelta, timezone

import pytest

import database as db
from mqtt_listener import PrintEndEvent
from station import label_notifier as ln
from tests.conftest import make_job

T0 = datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc)
URL = "https://site.example/api/notify-print-end"


class PostRecorder:
    def __init__(self, status=200, error=None):
        self.calls = []
        self.status = status
        self.error = error

    def __call__(self, url, *, json, headers, timeout):
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        if self.error:
            raise self.error
        return self.status


def notifier(post=None, **kw):
    kw.setdefault("enabled", True)
    kw.setdefault("notify_url", URL)
    kw.setdefault("secret", "shh")
    return ln.LabelNotifier(post=post or PostRecorder(), **kw)


def consenting(n):
    """Pretend consent exists (it can't yet: that is the point of the gate)."""
    n._has_consent = lambda upload: True
    return n


def add_upload(upload_id, student, renamed, received=T0):
    db.create_upload(upload_id, f"incoming/{upload_id}", student, renamed, renamed, "any", received.isoformat())
    db.mark_uploaded(upload_id, None, received.isoformat(), status="done")


def event_for(job_id="job-1", subtask="ab123_bracket"):
    make_job(job_id, subtask, start=(T0 + timedelta(hours=1)).isoformat(), end=(T0 + timedelta(hours=2)).isoformat())
    return PrintEndEvent(job_id=job_id, printer_name="The Allee", subtask_name=subtask, final_state="FINISH")


# --------------------------------------------------------------------------- disabled (the default)

def test_default_is_disabled():
    assert ln.LabelNotifier().enabled is False
    assert ln.LabelNotifier.from_config({}).enabled is False
    assert ln.LabelNotifier.from_config({"labels": {"era": 2}}).enabled is False


def test_disabled_only_logs_and_never_touches_the_network(tmp_db, caplog):
    add_upload("u1", "ab123", "ab123_bracket.gcode.3mf")
    post = PostRecorder()
    n = ln.LabelNotifier(enabled=False, post=post)
    with caplog.at_level("INFO", logger="station.label_notifier"):
        assert n.notify_print_end(event_for()) == ln.DISABLED
    assert post.calls == []
    assert "disabled" in caplog.text and "ab123" in caplog.text    # useful trace: who WOULD be emailed


def test_disabled_does_not_even_need_a_database_row(tmp_db):
    ev = PrintEndEvent(job_id="not-in-db", printer_name="p", subtask_name="x", final_state="FINISH")
    assert ln.LabelNotifier().notify_print_end(ev) == ln.DISABLED


# --------------------------------------------------------------------------- from_config

def test_from_config_reads_labels_and_web_sections():
    n = ln.LabelNotifier.from_config({
        "labels": {"email_enabled": True, "notify_url": URL, "email_domain": "@cornell.edu"},
        "web": {"webhook_secret": "shh"},
    })
    assert (n.enabled, n.notify_url, n.secret, n.email_domain) == (True, URL, "shh", "cornell.edu")


# --------------------------------------------------------------------------- enabled

def test_enabled_without_consent_sends_nothing(tmp_db):
    """The safety gate: flipping the flag alone must never email anyone."""
    add_upload("u1", "ab123", "ab123_bracket.gcode.3mf")
    post = PostRecorder()
    assert notifier(post).notify_print_end(event_for()) == ln.NO_CONSENT
    assert post.calls == []


def test_enabled_with_consent_posts_the_expected_payload_with_the_station_key(tmp_db):
    add_upload("u1", "AB123", "AB123_bracket.gcode.3mf")
    post = PostRecorder()
    result = consenting(notifier(post)).notify_print_end(event_for(subtask="AB123_bracket"))
    assert result == ln.SENT
    (call,) = post.calls
    assert call["url"] == URL
    assert call["headers"] == {"x-station-key": "shh"}
    assert call["json"] == {
        "job_id": "job-1", "netid": "ab123",
        "filename": "AB123_bracket.gcode.3mf", "printer_name": "The Allee",
    }
    assert "email" not in call["json"]      # the website derives the address; the laptop never sends one


@pytest.mark.parametrize("status", [400, 401, 501, 502])
def test_website_errors_are_reported_not_raised(tmp_db, status):
    add_upload("u1", "ab123", "ab123_bracket.gcode.3mf")
    assert consenting(notifier(PostRecorder(status=status))).notify_print_end(event_for()) == ln.FAILED


def test_network_errors_never_propagate(tmp_db):
    add_upload("u1", "ab123", "ab123_bracket.gcode.3mf")
    n = consenting(notifier(PostRecorder(error=ConnectionError("tunnel down"))))
    assert n.notify_print_end(event_for()) == ln.FAILED      # swallowed + logged


@pytest.mark.parametrize("kw", [{"notify_url": ""}, {"secret": ""}])
def test_enabled_but_misconfigured(tmp_db, kw):
    add_upload("u1", "ab123", "ab123_bracket.gcode.3mf")
    post = PostRecorder()
    assert consenting(notifier(post, **kw)).notify_print_end(event_for()) == ln.MISCONFIGURED
    assert post.calls == []


def test_print_without_a_matching_submission_is_skipped(tmp_db):
    post = PostRecorder()
    n = consenting(notifier(post))
    assert n.notify_print_end(event_for(subtask="someone_elses_file")) == ln.NO_SUBMISSION
    assert post.calls == []


def test_ambiguous_match_is_skipped(tmp_db):
    add_upload("u1", "ab123", "shared.gcode.3mf")
    add_upload("u2", "cd456", "shared.gcode.3mf", received=T0 + timedelta(minutes=5))
    post = PostRecorder()
    assert consenting(notifier(post)).notify_print_end(event_for(subtask="shared")) == ln.AMBIGUOUS
    assert post.calls == []


@pytest.mark.parametrize("student", ["Miguel", "miguel_ramirez", "123456", "ab", "a1b2", "abcde1"])
def test_only_netid_shaped_names_can_become_an_email_address(tmp_db, student):
    add_upload("u1", student, f"{student}_part.gcode.3mf")
    post = PostRecorder()
    assert consenting(notifier(post)).notify_print_end(event_for(subtask=f"{student}_part")) == ln.INVALID_NETID
    assert post.calls == []


def test_unknown_job_is_handled(tmp_db):
    ev = PrintEndEvent(job_id="ghost", printer_name="p", subtask_name="x", final_state="FINISH")
    assert consenting(notifier()).notify_print_end(ev) == ln.NO_JOB


def test_database_errors_never_propagate(tmp_db, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(db, "get_job", boom)
    ev = PrintEndEvent(job_id="x", printer_name="p", subtask_name="x", final_state="FINISH")
    assert notifier().notify_print_end(ev) == ln.FAILED


def test_consent_gate_is_closed_until_implemented(tmp_db):
    """If someone implements consent, this test must be replaced deliberately."""
    add_upload("u1", "ab123", "ab123_bracket.gcode.3mf")
    match = db.find_upload_for_job("ab123_bracket", (T0 + timedelta(hours=1)).isoformat())
    assert ln.LabelNotifier()._has_consent(match.upload) is False


# --------------------------------------------------------------------------- wiring in main

def test_main_event_loop_runs_the_real_disabled_notifier(tmp_db, caplog):
    import queue
    import threading
    import main

    add_upload("u1", "ab123", "ab123_bracket.gcode.3mf")
    ev = event_for()
    q, stop = queue.Queue(), threading.Event()
    q.put(ev)

    class StopAfter(ln.LabelNotifier):
        def notify_print_end(self, event):
            result = super().notify_print_end(event)
            stop.set()
            return result

    with caplog.at_level("INFO"):
        main._run_event_loop(q, StopAfter.from_config({}), stop, poll_s=0.01)
    assert "Email labeling disabled" in caplog.text
