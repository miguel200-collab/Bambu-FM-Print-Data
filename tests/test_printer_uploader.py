import httpx
import pytest

from station import printer_uploader as pu

REAL_CLIENT = httpx.Client


def install(monkeypatch, handler):
    def factory(**kw):
        kw.pop("transport", None)
        return REAL_CLIENT(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(pu.httpx, "Client", factory)


def test_timeout_constant_exists():
    """Regression (Jul 30): the code referenced `_Timeout`, so the first real call raised NameError."""
    assert isinstance(pu._TIMEOUT, httpx.Timeout)
    assert pu._TIMEOUT.read >= 60                     # large files over the LAN take a while
    assert not hasattr(pu, "_Timeout")


def test_upload_error_is_a_runtime_error():
    assert issubclass(pu.UploadError, RuntimeError)


@pytest.fixture
def gcode(tmp_path):
    f = tmp_path / "part.gcode.3mf"
    f.write_bytes(b"PK\x03\x04" + b"x" * 1000)
    return f


def printer(handler_overrides=None):
    """A fake printer implementing the 3-step flow; overrides let a test break one step."""
    log = []
    overrides = handler_overrides or {}

    def handler(req):
        log.append(req)
        if req.method == "GET" and req.url.host == "10.0.0.5":
            return overrides.get("creds", httpx.Response(200, json={
                "oss_url": "https://oss.example/up", "object_name": "obj1", "upload_id": "uid-9"}))
        if req.method == "PUT":
            return overrides.get("put", httpx.Response(200))
        if req.method == "POST":
            return overrides.get("land", httpx.Response(200, json={"print": {"success": True}}))
        return httpx.Response(404)

    handler.log = log
    return handler


def test_full_upload_flow(monkeypatch, gcode):
    h = printer()
    install(monkeypatch, h)
    pu.upload_file("10.0.0.5", "12345678", gcode, "ab123_part.gcode.3mf")

    get, put, post = h.log
    assert str(get.url) == "https://10.0.0.5/api/file?"
    assert get.headers["authorization"].startswith("Basic ")            # bblp:<access code>
    assert str(put.url) == "https://oss.example/up/obj1"
    assert put.headers["authorization"] == "uid-9" and len(put.content) == 1004
    import json
    body = json.loads(post.content)["print"]["param"]
    assert body["filename"] == "ab123_part.gcode.3mf" and body["url"] == "obj1"
    assert body["action"] == "save"                                      # lands in Files; never auto-starts


def test_auto_start_is_opt_in(monkeypatch, gcode):
    h = printer()
    install(monkeypatch, h)
    pu.upload_file("10.0.0.5", "x", gcode, "f.gcode.3mf", auto_start=True)
    import json
    assert json.loads(h.log[-1].content)["print"]["param"]["action"] == "start"


@pytest.mark.parametrize("override,message", [
    ({"creds": httpx.Response(500, text="nope")}, "credential request failed"),
    ({"creds": httpx.Response(200, text="<html>")}, "not JSON"),
    ({"creds": httpx.Response(200, json={"object_name": "x"})}, "oss_url"),
    ({"put": httpx.Response(500, text="oss down")}, "OSS upload failed"),
    ({"land": httpx.Response(500, text="bad")}, "land-file request failed"),
    ({"land": httpx.Response(200, json={"print": {"success": False}})}, "rejected"),
])
def test_each_step_failure_raises_upload_error_not_a_crash(monkeypatch, gcode, override, message):
    install(monkeypatch, printer(override))
    with pytest.raises(pu.UploadError, match=message):
        pu.upload_file("10.0.0.5", "x", gcode, "f.gcode.3mf")


def test_missing_local_file(tmp_path):
    with pytest.raises(pu.UploadError, match="not found"):
        pu.upload_file("10.0.0.5", "x", tmp_path / "nope.gcode.3mf", "f.gcode.3mf")


def test_network_failure_surfaces_as_an_httpx_error(monkeypatch, gcode):
    def boom(req):
        raise httpx.ConnectError("unreachable")

    install(monkeypatch, boom)
    with pytest.raises(httpx.HTTPError):
        pu.upload_file("10.0.0.5", "x", gcode, "f.gcode.3mf")      # file_watcher catches this (non-fatal)
