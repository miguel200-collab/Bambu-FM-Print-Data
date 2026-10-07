"""Shared pytest fixtures.

Every test that touches SQLite gets its own throw-away database: ``database.DB_PATH``
is redirected to a temp file, so tests can never touch the real print_dataset.db.
"""

from __future__ import annotations

import pytest

import database as db


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """A freshly initialised, isolated database. Yields its path."""
    path = tmp_path / "test_dataset.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    return path


def make_job(
    job_id: str,
    subtask: str = "part",
    *,
    serial: str = "SERIAL_A",
    start: str = "2026-10-01T10:00:00+00:00",
    end: str | None = "2026-10-01T11:00:00+00:00",
    state: str | None = "FINISH",
    label: int | None = None,
    label_time: str | None = None,
    print_error: int | None = None,
    filament: str | None = "PLA",
) -> None:
    """Insert a (possibly ended, possibly labeled) job through the public db API."""
    db.create_job(
        job_id=job_id,
        printer_serial=serial,
        printer_ip="10.0.0.1",
        start_time=start,
        subtask_name=subtask,
        filament_type=filament,
    )
    if end and state:
        db.update_job_end(
            job_id=job_id, end_time=end, final_state=state, print_error=print_error,
        )
    if label is not None:
        db.write_label(job_id, label, label_time or "2026-10-01T12:00:00+00:00")
