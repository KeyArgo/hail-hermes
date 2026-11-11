"""Tests for Desktop-triggered Pocket hub startup."""

from __future__ import annotations

import concurrent.futures
import threading

import pytest

from hail_hermes import autostart


def test_live_hub_is_not_started_again(tmp_path, monkeypatch):
    monkeypatch.setattr(autostart, "_answers", lambda _path: True)

    def unexpected_start(*_args, **_kwargs):
        raise AssertionError("a live hub must not be started again")

    monkeypatch.setattr(autostart.subprocess, "Popen", unexpected_start)
    assert autostart.ensure_hub(tmp_path) is False


def test_concurrent_callers_start_once(tmp_path, monkeypatch):
    running = threading.Event()
    launches = []

    class FakeProcess:
        pid = 12345

        def poll(self):
            return None

    def start(*args, **kwargs):
        launches.append((args, kwargs))
        running.set()
        return FakeProcess()

    monkeypatch.setattr(autostart, "_answers", lambda _path: running.is_set())
    monkeypatch.setattr(autostart.subprocess, "Popen", start)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: autostart.ensure_hub(tmp_path), range(2)))

    assert sorted(results) == [False, True]
    assert len(launches) == 1
    detach = autostart._popen_detach_kwargs()
    for key, value in detach.items():
        assert launches[0][1][key] == value


def test_hub_argv_prefers_the_hermes_launcher(tmp_path):
    argv = autostart._hub_argv(tmp_path, hermes_cli="/usr/bin/hermes")

    assert argv[0] == "/usr/bin/hermes"
    assert argv[1:3] == ["hail", "--state-dir"]
    assert argv[3:] == [str(tmp_path), "serve", "--host", "127.0.0.1"]


def test_hub_argv_uses_module_when_the_package_name_is_importable(tmp_path):
    argv = autostart._hub_argv(tmp_path, plugin_dir=tmp_path / "hail_hermes", hermes_cli=False)

    assert argv[1:3] == ["-m", "hail_hermes"]
    assert argv[3:] == ["--state-dir", str(tmp_path), "serve", "--host", "127.0.0.1"]


def test_hub_argv_bootstraps_when_hermes_installed_a_hyphenated_plugin_dir(tmp_path):
    plugin_dir = tmp_path / "hail-hermes"

    argv = autostart._hub_argv(tmp_path, plugin_dir=plugin_dir, hermes_cli=False)

    assert argv[1] == "-c"
    assert "spec_from_file_location" in argv[2]
    assert argv[3] == str(plugin_dir)
    assert argv[4] == str(tmp_path)


def test_process_exists_rejects_impossible_pids():
    assert autostart._process_exists(0) is False
    assert autostart._process_exists(-1) is False


def test_start_failure_reports_last_log_line(tmp_path, monkeypatch):
    class FailedProcess:
        pid = 12345

        def poll(self):
            return 1

    def start(*_args, **_kwargs):
        (tmp_path / "hub.log").write_text("first line\nstartup failed\n")
        return FailedProcess()

    monkeypatch.setattr(autostart, "_answers", lambda _path: False)
    monkeypatch.setattr(autostart.subprocess, "Popen", start)

    with pytest.raises(autostart.HubStartError, match="startup failed"):
        autostart.ensure_hub(tmp_path)


def test_unresponsive_previous_process_is_not_replaced(tmp_path, monkeypatch):
    (tmp_path / "autostart.pid").write_text("12345\n")
    monkeypatch.setattr(autostart, "_answers", lambda _path: False)
    monkeypatch.setattr(autostart, "_process_exists", lambda pid: pid == 12345)

    def unexpected_start(*_args, **_kwargs):
        raise AssertionError("an existing process must not be replaced")

    monkeypatch.setattr(autostart.subprocess, "Popen", unexpected_start)

    with pytest.raises(autostart.HubStartError):
        autostart.ensure_hub(tmp_path)
