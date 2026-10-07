"""
`python main.py --mock` as a real subprocess, in a throw-away copy of the code so it
cannot touch the real database or log. Proves the daemon starts headless, processes
an end-of-print event through the event loop, and shuts down cleanly on Ctrl+C.
"""

import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX SIGINT")


def _ignore_sigint():
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def _default_sigint():
    signal.signal(signal.SIGINT, signal.SIG_DFL)


@pytest.mark.parametrize("sigint_at_start", [_default_sigint, _ignore_sigint],
                         ids=["normal-start", "started-as-background-job (SIGINT inherited as ignored)"])
def test_main_mock_runs_headless_and_exits_cleanly(tmp_path, sigint_at_start):
    sandbox = tmp_path / "app"
    sandbox.mkdir()
    for name in ("main.py", "database.py", "mqtt_listener.py"):
        shutil.copy(REPO / name, sandbox / name)
    shutil.copytree(REPO / "station", sandbox / "station", ignore=shutil.ignore_patterns("__pycache__"))

    proc = subprocess.Popen(
        [sys.executable, "-B", "main.py", "--mock"],
        cwd=sandbox, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        preexec_fn=sigint_at_start,
    )
    lines: list[str] = []
    reader = threading.Thread(target=lambda: lines.extend(iter(proc.stdout.readline, "")), daemon=True)
    reader.start()

    try:
        deadline = time.time() + 20
        while time.time() < deadline and not any("Print ended" in l for l in lines):
            assert proc.poll() is None, "daemon exited early:\n" + "".join(lines)
            time.sleep(0.1)
        assert any("Print ended" in l for l in lines), "no end-of-print event:\n" + "".join(lines)
    finally:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            pytest.fail("daemon did not stop on SIGINT:\n" + "".join(lines))
    reader.join(timeout=2)
    out = "".join(lines)

    assert proc.returncode == 0, out
    assert "MOCK MODE" in out and "Daemon running (headless)" in out
    assert "state=FINISH" in out and "Email labeling disabled" in out
    assert "Stop requested" in out and "stopped" in out
    assert "tkinter" not in out.lower() and "Traceback" not in out
    # Mock data went to the mock database, never the real dataset.
    assert (sandbox / "print_dataset_mock.db").exists()
    assert not (sandbox / "print_dataset.db").exists()
    # No web section => no HTTP server was opened.
    assert "Station API listening" not in out
