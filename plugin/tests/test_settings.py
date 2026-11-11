from __future__ import annotations

import json
import os
import signal
from pathlib import Path

import pytest

from hail_hermes.agent import DeviceInfo, HermesCliAgent, build_agent
from hail_hermes.cli import DEFAULT_AGENT_TOOLSETS, _apply_serve_defaults, main
from test_dashboard_api import _stub_fastapi

_stub_fastapi()

from hail_hermes.dashboard import plugin_api  # noqa: E402
from hail_hermes.settings import CHOICES, DEFAULTS, read_settings, update_settings, validate


def test_settings_merge_order(tmp_path: Path) -> None:
    # 1. Built-in defaults when settings.json is absent.
    cfg = _apply_serve_defaults({"state_dir": str(tmp_path)})
    assert cfg["stt"] == {"backend": "auto"}
    assert cfg["tts"] == {"backend": "auto"}
    assert cfg["agent"]["toolsets"] == DEFAULT_AGENT_TOOLSETS
    assert "model" not in cfg["agent"]
    assert "provider" not in cfg["agent"]

    # 2. settings.json overrides built-in defaults.
    update_settings(
        tmp_path,
        {
            "model": "anthropic/claude-sonnet-4",
            "provider": "openrouter",
            "toolsets": "pocket",
            "stt": "parakeet",
            "tts": "kokoro",
        },
    )
    cfg = _apply_serve_defaults({"state_dir": str(tmp_path)})
    assert cfg["stt"] == {"backend": "parakeet"}
    assert cfg["tts"] == {"backend": "kokoro"}
    assert cfg["agent"]["model"] == "anthropic/claude-sonnet-4"
    assert cfg["agent"]["provider"] == "openrouter"
    assert cfg["agent"]["toolsets"] == "pocket"

    # 3. Explicit --config / flags override settings.json.
    explicit = _apply_serve_defaults(
        {
            "state_dir": str(tmp_path),
            "stt": "hermes",
            "tts": {"backend": "piper"},
            "agent": {
                "model": "gpt-4o",
                "provider": "openai",
                "toolsets": "clarify",
            },
        }
    )
    assert explicit["stt"] == {"backend": "hermes"}
    assert explicit["tts"] == {"backend": "piper"}
    assert explicit["agent"]["model"] == "gpt-4o"
    assert explicit["agent"]["provider"] == "openai"
    assert explicit["agent"]["toolsets"] == "clarify"


def test_settings_rejects_unknown_key_and_value(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(ValueError, match="unknown settings"):
        validate({"unknown_key": "value"})

    with pytest.raises(ValueError, match="invalid stt"):
        update_settings(tmp_path, {"stt": "whisper"})

    with pytest.raises(ValueError, match="invalid tts"):
        update_settings(tmp_path, {"tts": "espeak"})

    assert main(["--state-dir", str(tmp_path), "settings"]) == 0
    out = capsys.readouterr().out
    assert "stt=auto" in out
    assert "tts=auto" in out

    assert main(["--state-dir", str(tmp_path), "settings", "stt", "parakeet"]) == 0
    assert read_settings(tmp_path)["stt"] == "parakeet"

    assert main(["--state-dir", str(tmp_path), "settings", "bogus", "1"]) == 1
    assert "unknown settings" in capsys.readouterr().err

    assert main(["--state-dir", str(tmp_path), "settings", "stt", "bogus"]) == 1
    assert "invalid stt" in capsys.readouterr().err


def test_settings_cli_boolean_values(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for text in ("true", "True", "TRUE", "yes", "YES", "on", "On"):
        assert main(["--state-dir", str(tmp_path), "settings", "lan_enabled", text]) == 0
        assert read_settings(tmp_path)["lan_enabled"] is True
    for text in ("false", "False", "FALSE", "no", "No", "off", "OFF"):
        assert main(["--state-dir", str(tmp_path), "settings", "lan_enabled", text]) == 0
        assert read_settings(tmp_path)["lan_enabled"] is False

    assert main(["--state-dir", str(tmp_path), "settings", "lan_enabled", "maybe"]) == 1
    assert "must be a boolean" in capsys.readouterr().err

    assert main(["--state-dir", str(tmp_path), "settings", "model", "True"]) == 0
    assert read_settings(tmp_path)["model"] == "True"


def test_plugin_api_get_and_post_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plugin_api, "_resolve_state_dir", lambda: str(tmp_path))

    initial = plugin_api.get_settings()
    for key, val in DEFAULTS.items():
        if key == "soul_path":
            assert initial[key] == str(tmp_path / "soul.md")  # follows the configured state dir
            continue
        assert initial[key] == val
    assert {k: v for k, v in initial["choices"].items() if k in ("stt", "tts")} == {
        "stt": ["auto", "parakeet", "hermes"],
        "tts": ["auto", "piper", "kokoro", "edge", "none"],
    }
    assert initial["choices"] == CHOICES

    saved = plugin_api.post_settings(
        {"model": "claude-3-7-sonnet", "provider": "anthropic", "stt": "parakeet", "tts": "none"}
    )
    assert saved["ok"] is True
    assert saved["saved"] is True
    assert saved["restart_needed"] is True
    assert saved["settings"]["tts"] == "none"

    assert plugin_api.post_settings({"character": "nous"})["restart_needed"] is False

    after = plugin_api.get_settings()
    assert after["model"] == "claude-3-7-sonnet"
    assert after["provider"] == "anthropic"
    assert after["stt"] == "parakeet"
    assert after["tts"] == "none"

    bad_key = plugin_api.post_settings({"nope": "1"})
    assert bad_key["ok"] is False
    assert "unknown settings" in bad_key["error"]

    bad_val = plugin_api.post_settings({"tts": "invalid"})
    assert bad_val["ok"] is False
    assert "invalid tts" in bad_val["error"]


def test_restart_hub_refuses_pid_without_hail_hermes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc_root = tmp_path / "proc"
    monkeypatch.setattr(plugin_api, "_resolve_state_dir", lambda: str(tmp_path))
    monkeypatch.setattr(plugin_api, "_PROC_ROOT", proc_root)

    pid = 42424
    (tmp_path / "live.json").write_text(json.dumps({"pid": pid, "port": 8767}), encoding="utf-8")
    proc_dir = proc_root / str(pid)
    proc_dir.mkdir(parents=True)
    (proc_dir / "cmdline").write_bytes(b"/usr/bin/python3\x00-m\x00http.server\x00")

    kills: list[tuple[int, int]] = []
    ensured: list[str | Path | None] = []
    monkeypatch.setattr(os, "kill", lambda target, sig: kills.append((target, sig)))
    monkeypatch.setattr(plugin_api, "ensure_hub", lambda state_dir: ensured.append(state_dir) or True)

    refused = plugin_api.restart_hub()
    assert refused["ok"] is False
    assert kills == []
    assert ensured == []

    (proc_dir / "cmdline").write_bytes(b"/usr/bin/python3\x00-m\x00hail_hermes\x00serve\x00")
    restarted = plugin_api.restart_hub()
    assert restarted == {"ok": True, "started": True}
    assert kills == [(pid, signal.SIGTERM)]
    assert ensured == [str(tmp_path)]


def test_build_agent_default_source_is_cli() -> None:
    direct = HermesCliAgent(binary="hermes")
    assert direct.source is None
    assert "--source" not in direct.argv(DeviceInfo(device_id="dev-1"))

    hub_agent = build_agent({"binary": "hermes"})
    assert isinstance(hub_agent, HermesCliAgent)
    assert hub_agent.source == "cli"
    argv = hub_agent.argv(DeviceInfo(device_id="watch-1"))
    idx = argv.index("--source")
    assert argv[idx + 1] == "cli"
