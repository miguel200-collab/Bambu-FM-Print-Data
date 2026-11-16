"""Config loading without printers, the network, or the real dataset."""

import json
from pathlib import Path

import pytest

import main


def _point_at(monkeypatch, path: Path) -> None:
    monkeypatch.setattr(main, "_CONFIG_PATH", path)


def test_missing_config_file(tmp_path, monkeypatch):
    _point_at(monkeypatch, tmp_path / "config.json")
    with pytest.raises(FileNotFoundError):
        main._load_config()
    assert main._load_full_config() == {}


def test_empty_printer_list_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"printers": [], "web": {"api_port": 8080}}), encoding="utf-8")
    _point_at(monkeypatch, path)
    with pytest.raises(ValueError):
        main._load_config()


def test_invalid_json_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text("{", encoding="utf-8")
    _point_at(monkeypatch, path)
    with pytest.raises(json.JSONDecodeError):
        main._load_config()


def test_load_config_returns_printers_and_the_rest_of_the_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "printers": [
                    {
                        "name": "Printer 1",
                        "ip": "10.0.0.8",
                        "serial": "SERIAL_A",
                        "access_code": "secret",
                    }
                ],
                "web": {"api_host": "127.0.0.1", "api_port": 8080, "poll_interval_s": 0},
                "labels": {"era": 1, "email_enabled": False},
            }
        ),
        encoding="utf-8",
    )
    _point_at(monkeypatch, path)
    repo_db = Path(__file__).resolve().parents[1] / "print_dataset.db"
    existed = repo_db.exists()

    printers = main._load_config()
    assert printers == [
        {
            "name": "Printer 1",
            "ip": "10.0.0.8",
            "serial": "SERIAL_A",
            "access_code": "secret",
        }
    ]
    full = main._load_full_config()
    assert full["web"]["api_host"] == "127.0.0.1"
    assert full["web"]["api_port"] == 8080
    assert full["labels"]["era"] == 1
    assert repo_db.exists() is existed
    assert not (tmp_path / "print_dataset.db").exists()
