"""Tests for the Desktop Wrist pane backend."""

from __future__ import annotations

import base64
import concurrent.futures
import importlib
import json
import sys
import threading
import time
import types
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "hail_hermes"


def _stub_fastapi():
    try:
        import fastapi  # noqa: F401
        return
    except ImportError:
        pass

    class FileResponse:
        def __init__(self, path, media_type=None, filename=None):
            self.path = Path(path)
            self.media_type = media_type
            self.filename = filename

    class APIRouter:
        def _deco(self, *args, **kwargs):
            return lambda fn: fn

        get = post = _deco

    mod = types.ModuleType("fastapi")
    mod.APIRouter = APIRouter
    responses = types.ModuleType("fastapi.responses")
    responses.FileResponse = FileResponse
    sys.modules["fastapi"] = mod
    sys.modules["fastapi.responses"] = responses


class FakeClient:
    def __init__(self, live=None, pending=None, errors=None):
        self.snapshot = live
        self.pending_rows = list(pending or [])
        self.errors = errors or {}
        self.approved = []

    def live(self):
        return self.snapshot

    def connected(self):
        return list((self.snapshot or {}).get("devices", []))

    def call(self, op, **args):
        if op in self.errors:
            raise self.errors[op]
        if op == "devices":
            return self.connected()
        if op == "pair_list":
            return list(self.pending_rows)
        if op == "pair_approve":
            self.approved.append(args.get("request_id", args.get("match_code")))
            return {"device_id": "dev-1"}
        if op == "pair_deny":
            return {}
        raise AssertionError(f"unexpected operation: {op}")


@pytest.fixture()
def api_module(monkeypatch, tmp_path):
    _stub_fastapi()
    monkeypatch.syspath_prepend(str(PLUGIN_ROOT.parent))
    name = "hail_hermes.dashboard.plugin_api"
    sys.modules.pop(name, None)
    mod = importlib.import_module(name)
    monkeypatch.setattr(mod, "_resolve_state_dir", lambda: str(tmp_path))
    yield mod
    sys.modules.pop(name, None)


def test_pane_identity_is_persistent_and_safe_under_concurrency(api_module, tmp_path):
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        ids = list(executor.map(lambda _: api_module._pane_device_id(tmp_path), range(16)))
    assert len(set(ids)) == 1
    assert json.loads((tmp_path / "pane.json").read_text()) == {"device_id": ids[0]}


def test_status_starts_hub_and_returns_identity(monkeypatch, api_module, tmp_path):
    client = FakeClient(live={
        "port": 8767,
        "devices": [{"device_id": "watch", "kind": "wear"}],
    })
    calls = []
    monkeypatch.setattr(api_module, "ensure_hub", lambda state: calls.append(state) or True)
    monkeypatch.setattr(api_module, "_client", lambda: client)

    result = api_module.status()

    assert calls == [str(tmp_path)]
    assert result["running"] is True
    assert result["started"] is True
    assert result["device_id"] == api_module._pane_device_id(tmp_path)
    assert result["ws_url"] == "ws://127.0.0.1:8767/pocket"
    assert result["devices"] == client.connected()


def test_status_reports_existing_hub(monkeypatch, api_module):
    monkeypatch.setattr(api_module, "ensure_hub", lambda state: False)
    monkeypatch.setattr(api_module, "_client", lambda: FakeClient(live={"port": 1234}))
    result = api_module.status()
    assert result["running"] is True
    assert result["started"] is False
    assert result["port"] == 1234


def test_status_reports_startup_log_tail(monkeypatch, api_module):
    def fail(_state):
        raise api_module.HubStartError("address already in use")

    monkeypatch.setattr(api_module, "ensure_hub", fail)
    result = api_module.status()
    assert result == {"running": False, "hint": "address already in use"}


def test_status_handles_unresponsive_control(monkeypatch, api_module):
    monkeypatch.setattr(api_module, "ensure_hub", lambda state: False)
    client = FakeClient(live={"port": 8767}, errors={"devices": ConnectionError("closed")})
    monkeypatch.setattr(api_module, "_client", lambda: client)
    assert api_module.status()["running"] is False


def test_status_preserves_control_operation_error(monkeypatch, api_module):
    monkeypatch.setattr(api_module, "ensure_hub", lambda state: False)
    client = FakeClient(live={"port": 8767}, errors={"devices": RuntimeError("rejected")})
    monkeypatch.setattr(api_module, "_client", lambda: client)
    result = api_module.status()
    assert result["running"] is True
    assert result["error"] == "rejected"


def _request(pane_device_id, **overrides):
    row = {
        "request_id": "request-1",
        "device_id": pane_device_id,
        "kind": "desktop",
        "remote": "127.0.0.1",
    }
    row.update(overrides)
    return row


def test_approve_self_accepts_own_loopback_desktop(monkeypatch, api_module, tmp_path):
    device_id = api_module._pane_device_id(tmp_path)
    client = FakeClient(live={"port": 8767}, pending=[_request(device_id)])
    monkeypatch.setattr(api_module, "_client", lambda: client)

    assert api_module.approve_self({"request_id": "request-1"}) == {"ok": True}
    assert client.approved == ["request-1"]


@pytest.mark.parametrize("overrides", [
    {"device_id": "different"},
    {"kind": "wear"},
    {"remote": "192.0.2.1"},
    {"remote": "localhost"},
    {"request_id": "other"},
])
def test_approve_self_rejects_other_requests(monkeypatch, api_module, tmp_path, overrides):
    device_id = api_module._pane_device_id(tmp_path)
    client = FakeClient(live={"port": 8767}, pending=[_request(device_id, **overrides)])
    monkeypatch.setattr(api_module, "_client", lambda: client)

    assert api_module.approve_self({"request_id": "request-1"}) == {"ok": False}
    assert client.approved == []


@pytest.mark.parametrize("remote", ["::1", "::ffff:127.0.0.1"])
def test_approve_self_accepts_ipv6_loopback(monkeypatch, api_module, tmp_path, remote):
    device_id = api_module._pane_device_id(tmp_path)
    client = FakeClient(live={"port": 8767}, pending=[_request(device_id, remote=remote)])
    monkeypatch.setattr(api_module, "_client", lambda: client)
    assert api_module.approve_self({"request_id": "request-1"}) == {"ok": True}


def test_approve_self_rejects_expired_at_decision(monkeypatch, api_module, tmp_path):
    device_id = api_module._pane_device_id(tmp_path)
    client = FakeClient(
        live={"port": 8767},
        pending=[_request(device_id)],
        errors={"pair_approve": RuntimeError("pairing request not found")},
    )
    monkeypatch.setattr(api_module, "_client", lambda: client)
    assert api_module.approve_self({"request_id": "request-1"}) == {"ok": False}


def test_approve_self_rejects_missing_id_and_hub_down(monkeypatch, api_module):
    monkeypatch.setattr(api_module, "_client", lambda: FakeClient())
    assert api_module.approve_self({}) == {"ok": False}
    assert api_module.approve_self({"request_id": "request-1"}) == {"ok": False}


def test_pending_and_owner_decisions(monkeypatch, api_module):
    row = _request("watch", kind="wear")
    client = FakeClient(live={"port": 8767}, pending=[row])
    monkeypatch.setattr(api_module, "_client", lambda: client)
    assert api_module.pending() == {"running": True, "pending": [row]}
    assert api_module.approve({"request_id": "request-1"})["ok"] is True
    assert api_module.deny({"request_id": "request-1"}) == {"ok": True}
    assert api_module.approve({"match_code": "4821"})["ok"] is True
    assert client.approved[-1] == "4821"
    assert api_module.deny({"match_code": "4821"}) == {"ok": True}
    assert api_module.approve({}) == {"error": "request_id required"}


def test_pending_hub_down(monkeypatch, api_module):
    monkeypatch.setattr(api_module, "_client", lambda: FakeClient())
    assert api_module.pending()["pending"] == []
    assert api_module.pending()["running"] is False


def test_sample_json_and_wav(api_module):
    assert base64.b64decode(api_module.sample()["base64"])[:4] == b"RIFF"
    assert api_module.sample_wav().path.is_file()


def test_soul_templates_returns_all_fields(api_module):
    res = api_module.get_soul_templates()
    assert res["ok"] is True
    assert len(res["templates"]) == 6
    for tpl in res["templates"]:
        assert set(tpl) == {"id", "name", "description", "soul"}
        assert all(isinstance(tpl[key], str) and tpl[key] for key in tpl)


def test_soul_templates_bad_file_is_graceful(api_module, monkeypatch):
    real_read_text = Path.read_text
    # Take the path from the module that is actually imported. A computed repo root breaks
    # whenever the test runs from a different checkout than the one on sys.path (an editable
    # install, a second clone), and the failure looks like a real bug.
    route_json = Path(api_module.__file__).resolve().parents[1] / "content" / "souls.json"

    def fake_read_text(self, *args, **kwargs):
        if self.resolve() == route_json:
            return "{not json"
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    res = api_module.get_soul_templates()
    assert res["ok"] is False
    assert res["templates"] == []
    assert isinstance(res["error"], str) and res["error"]


def test_every_route_returns_fast_when_hub_is_down(monkeypatch, api_module):
    monkeypatch.setattr(api_module, "ensure_hub", lambda state_dir: False)
    monkeypatch.setattr(api_module, "list_quick_models", lambda endpoints: [])

    calls = [
        lambda: api_module.status(),
        lambda: api_module.start_hub(),
        lambda: api_module.pending(),
        lambda: api_module.approve({"request_id": "req-1"}),
        lambda: api_module.deny({"request_id": "req-1"}),
        lambda: api_module.approve_self({"request_id": "req-1"}),
        lambda: api_module.get_models(),
        lambda: api_module.get_settings(),
        lambda: api_module.post_settings({"tts_voice": "default"}),
        lambda: api_module.post_slots(["session-1"]),
        lambda: api_module.restart_hub(),
    ]
    for fn in calls:
        t0 = time.monotonic()
        res = fn()
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0
        assert res is not None

    status_res = api_module.status()
    assert status_res["running"] is False
    assert "hint" in status_res


def test_every_route_returns_fast_when_control_client_hangs(monkeypatch, api_module):
    def _slow_ensure(state_dir):
        time.sleep(5.0)
        return True

    class _HangingControlClient:
        def __init__(self, state_dir=None, timeout=1.0):
            self.timeout = timeout

        def live(self):
            return {"port": 8767, "pid": 999999, "control_url": "ws://127.0.0.1:8767/control", "devices": []}

        def connected(self):
            time.sleep(5.0)
            return []

        def call(self, op, **kwargs):
            time.sleep(5.0)
            return None

    monkeypatch.setattr(api_module, "ensure_hub", _slow_ensure)
    monkeypatch.setattr(api_module, "ControlClient", _HangingControlClient)

    route_checks = [
        ("status", lambda: api_module.status()),
        ("start", lambda: api_module.start_hub()),
        ("pending", lambda: api_module.pending()),
        ("approve", lambda: api_module.approve({"request_id": "req-1"})),
        ("deny", lambda: api_module.deny({"request_id": "req-1"})),
        ("approve_self", lambda: api_module.approve_self({"request_id": "req-1"})),
        ("settings", lambda: api_module.post_settings({"tts_voice": "default"})),
        ("slots", lambda: api_module.post_slots(["session-1"])),
        ("restart_hub", lambda: api_module.restart_hub()),
    ]
    for name, fn in route_checks:
        t0 = time.monotonic()
        res = fn()
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0, f"{name} took {elapsed:.2f}s"
        if name in ("pending", "approve", "deny", "approve_self"):
            assert res["running"] is False
            assert "hint" in res


def test_only_one_ensure_hub_thread_runs_at_a_time(monkeypatch, api_module):
    active = 0
    max_active = 0
    calls = 0
    gate = threading.Event()
    lock = threading.Lock()

    def _blocking_ensure(state_dir):
        nonlocal active, max_active, calls
        with lock:
            calls += 1
            active += 1
            max_active = max(max_active, active)
        gate.wait(timeout=2.0)
        with lock:
            active -= 1
        return True

    monkeypatch.setattr(api_module, "ensure_hub", _blocking_ensure)
    for _ in range(5):
        api_module.status()
        api_module.start_hub()
    assert calls == 1
    assert max_active == 1
    gate.set()


def _png_bytes() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"fakepngdata" * 8


def _jpeg_bytes() -> bytes:
    return b"\xff\xd8\xff\xe0" + b"fakejpegdata" * 8


def test_face_post_and_get_roundtrip_png(api_module):
    raw = _png_bytes()
    res = api_module.post_face({"data": base64.b64encode(raw).decode("ascii"), "type": "image/png"})
    assert res == {"ok": True}
    face_path = api_module._face_path("image/png")
    assert face_path.is_file()
    assert base64.b64decode(base64.b64decode((api_module.get_face())["data"])) == raw
    assert api_module.get_face()["type"] == "image/png"


def test_face_post_jpeg_replaces_png(api_module):
    api_module.post_face({"data": base64.b64encode(_png_bytes()).decode("ascii"), "type": "image/png"})
    raw = _jpeg_bytes()
    res = api_module.post_face({"data": base64.b64encode(raw).decode("ascii"), "type": "image/jpeg"})
    assert res == {"ok": True}
    data = api_module.get_face()
    assert data["type"] == "image/jpeg"
    assert base64.b64decode(base64.b64decode(data["data"])) == raw
    assert not api_module._face_path("image/png").exists()


def test_face_get_without_face_returns_null_data(api_module):
    res = api_module.get_face()
    assert res == {"ok": True, "data": None}


def test_face_post_rejects_wrong_type(api_module):
    res = api_module.post_face({"data": base64.b64encode(_png_bytes()).decode("ascii"), "type": "../x"})
    assert res["ok"] is False
    assert res["error"]


def test_face_post_rejects_bad_base64(api_module):
    res = api_module.post_face({"data": "!!!not base64!!!", "type": "image/png"})
    assert res["ok"] is False
    assert res["error"]


def test_face_post_rejects_wrong_magic_bytes(api_module):
    res = api_module.post_face({"data": base64.b64encode(b"not-a-png").decode("ascii"), "type": "image/png"})
    assert res["ok"] is False
    assert res["error"]


def test_face_post_rejects_oversize(api_module):
    big = _png_bytes() + b"\x00" * (2 * 1024 * 1024)
    res = api_module.post_face({"data": base64.b64encode(big).decode("ascii"), "type": "image/png"})
    assert res["ok"] is False
    assert "2 MB" in res["error"]


def test_face_reset_removes_saved_face(api_module):
    api_module.post_face({"data": base64.b64encode(_png_bytes()).decode("ascii"), "type": "image/png"})
    assert api_module.get_face()["data"] is not None
    res = api_module.post_face_reset()
    assert res["ok"] is True
    assert api_module.get_face()["data"] is None
    assert not api_module._face_path("image/png").exists()


def test_two_endpoint_adds_at_once_keep_both(monkeypatch, api_module):
    import threading

    real_read = api_module.read_settings

    def slow_read(state_dir):
        values = real_read(state_dir)
        time.sleep(0.2)
        return values

    monkeypatch.setattr(api_module, "read_settings", slow_read)
    monkeypatch.setattr(api_module, "_notify_hub_settings_changed", lambda state_dir: None)
    urls = ["http://192.0.2.1:8000/v1", "http://192.0.2.2:8000/v1"]
    threads = [threading.Thread(target=api_module.post_settings, args=({"add_endpoint": u},)) for u in urls]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(real_read(api_module._resolve_state_dir())["quick_endpoints"]) == urls
