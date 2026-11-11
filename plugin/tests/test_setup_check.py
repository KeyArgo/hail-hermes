"""Tests for the first-run setup checks and the /setup-check dashboard route."""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

from hail_hermes import setup_check as setup_check_module

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


def test_all_checks_pass_when_everything_is_present(monkeypatch):
    from hail_hermes import hermes_voice
    monkeypatch.setattr(hermes_voice, "hermes_available", lambda: True)
    monkeypatch.setattr(setup_check_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    checks = setup_check_module.run_checks()
    assert [c["id"] for c in checks] == ["ffmpeg", "hermes", "python", "voice", "speech"]
    assert all(c["ok"] is True for c in checks)
    assert all(c["fix"] == "" for c in checks)


def test_missing_tools_report_plain_words(monkeypatch):
    monkeypatch.setattr(setup_check_module.shutil, "which", lambda name: None)
    checks = {c["id"]: c for c in setup_check_module.run_checks()}
    assert checks["ffmpeg"]["ok"] is False
    assert checks["ffmpeg"]["message"] == "Voice replies need a small free tool called ffmpeg."
    assert checks["ffmpeg"]["fix"] == "Install ffmpeg, then press Check again."
    assert checks["hermes"]["ok"] is False
    assert checks["hermes"]["message"] == "Hermes was not found on this computer."
    assert checks["python"]["ok"] is True


def test_raising_check_never_raises_and_reports_could_not_check(monkeypatch):
    def boom(name):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(setup_check_module.shutil, "which", boom)
    checks = {c["id"]: c for c in setup_check_module.run_checks()}
    assert checks["ffmpeg"]["ok"] is False
    assert checks["ffmpeg"]["message"] == "Could not check this."
    assert checks["ffmpeg"]["fix"] == ""
    assert checks["hermes"]["ok"] is False
    assert checks["hermes"]["message"] == "Could not check this."
    assert checks["python"]["ok"] is True


def test_setup_check_route_returns_all_checks(monkeypatch, api_module):
    from hail_hermes import hermes_voice
    monkeypatch.setattr(hermes_voice, "hermes_available", lambda: True)
    monkeypatch.setattr(setup_check_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    result = api_module.setup_check()
    assert result["ok"] is True
    assert [c["id"] for c in result["checks"]] == ["ffmpeg", "hermes", "python", "voice", "speech"]
    assert all(set(c) == {"id", "state", "ok", "message", "fix"} for c in result["checks"])
    assert all(c["state"] == "ok" for c in result["checks"])


def test_unrunnable_check_is_unknown_and_top_level_ok_is_false(monkeypatch, api_module):
    def boom(name):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(setup_check_module.shutil, "which", boom)
    result = api_module.setup_check()
    checks = {c["id"]: c for c in result["checks"]}
    assert checks["ffmpeg"]["state"] == "unknown"
    assert checks["ffmpeg"]["ok"] is False
    assert checks["hermes"]["state"] == "unknown"
    assert checks["hermes"]["ok"] is False
    assert result["ok"] is False


def test_voice_check_reports_both_ways(monkeypatch):
    from hail_hermes import hermes_voice, setup_check

    monkeypatch.setattr(hermes_voice, "hermes_available", lambda: True)
    assert setup_check._check_voice()["ok"] is True

    # When the imports themselves cannot be done in this process, the honest answer is "unknown":
    # claiming "can't speak" told the owner the product was broken while the hub was speaking.
    monkeypatch.setattr(hermes_voice, "hermes_available", lambda: False)
    result = setup_check._check_voice()
    assert result["state"] in ("unknown", "action")
    if result["state"] == "action":
        assert "You can still type" in result["message"]

    # And when the modules import but the callables are missing, it is a real "action".
    monkeypatch.setitem(sys.modules, "tools.tts_tool", types.ModuleType("tools.tts_tool"))
    monkeypatch.setitem(sys.modules, "tools.transcription_tools", types.ModuleType("tools.transcription_tools"))
    result = setup_check._check_voice()
    assert result["state"] == "action"
    assert "You can still type" in result["message"]
