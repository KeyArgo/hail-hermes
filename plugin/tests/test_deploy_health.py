"""The deploy check asks the hub, and a failed restart stops the deploy.

Quality review: the deploy script decided health by counting lines from `hermes hail devices`. That command
lists devices, so two paired watches failed the check while a crashed hub with no devices passed it — the
one thing the check existed for.

These drive the real helper against a real HTTP server and, on POSIX, run the real script with a stand-in
`hermes` on PATH, so both halves of the claim are behaviour rather than text.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HEALTH = ROOT / "tools" / "hub-health.py"

sys.path.insert(0, str(ROOT / "tools"))
from importlib import import_module

health = import_module("hub-health") if (ROOT / "tools" / "hub-health.py").exists() else None


class _Status(BaseHTTPRequestHandler):
    body: dict = {"running": True, "version": "1.0.0", "mode": "session", "devices": []}
    status = 200
    seen = 0

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's name
        type(self).seen += 1
        payload = json.dumps(type(self).body).encode()
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture()
def server():
    httpd = HTTPServer(("127.0.0.1", 0), _Status)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd, f"http://127.0.0.1:{httpd.server_port}/status"
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


def test_a_running_hub_is_reported_with_what_it_said(server):
    _httpd, url = server
    _Status.body = {"running": True, "version": "1.0.0", "mode": "session",
                    "devices": [{"name": "Galaxy Watch"}]}
    ok, message = health.wait_for_hub(url, timeout=5, poll=0.05)
    assert ok, message
    assert "v1.0.0" in message and "devices=1" in message


def test_a_hub_that_says_it_is_not_running_is_not_healthy(server):
    _httpd, url = server
    _Status.body = {"running": False}
    ok, message = health.wait_for_hub(url, timeout=0.6, poll=0.05)
    assert not ok
    assert "running=False" in message


def test_no_answer_is_a_failure(server):
    _httpd, url = server
    _Status.status = 500
    try:
        ok, message = health.wait_for_hub(url, timeout=0.6, poll=0.05)
    finally:
        _Status.status = 200
    assert not ok and "did not answer" in message


def test_the_cli_exits_non_zero_when_the_hub_is_down():
    url = "http://127.0.0.1:1/status"
    result = subprocess.run([sys.executable, str(HEALTH), url, "--timeout", "0.5"],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "FAILED" in result.stderr


def test_the_cli_exits_zero_for_a_running_hub(server):
    _httpd, url = server
    _Status.body = {"running": True, "version": "1.0.0", "mode": "quick", "devices": []}
    result = subprocess.run([sys.executable, str(HEALTH), url, "--timeout", "5"],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "hub is up" in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="drives a POSIX shell script with a stub on PATH")
def test_a_failed_restart_stops_the_deploy(tmp_path):
    """The old script echoed a warning and carried on to report success."""
    repo, fake_bin = _fake_deploy_env(tmp_path, restart_exit=1)
    result = _run_deploy(repo, fake_bin)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "could not restart the hub" in result.stderr


@pytest.mark.skipif(os.name == "nt", reason="drives a POSIX shell script with a stub on PATH")
def test_a_restart_that_leaves_no_hub_fails(tmp_path):
    """`hermes hail restart` succeeding is not evidence the hub came up."""
    repo, fake_bin = _fake_deploy_env(tmp_path, restart_exit=0)
    result = _run_deploy(repo, fake_bin, extra_env={"HAIL_HUB_URL": "http://127.0.0.1:1/status",
                                                   "HAIL_HEALTH_TIMEOUT": "1"})
    assert result.returncode == 1, result.stdout + result.stderr
    assert "not answering" in result.stderr


def _fake_deploy_env(tmp_path, restart_exit: int):
    repo = tmp_path / "repo"
    modules = repo / "plugin" / "hail_hermes"
    (modules / "dashboard").mkdir(parents=True)
    (modules / "desktop").mkdir(parents=True)
    for name in ("settings.py", "hub.py", "cli.py", "agent.py", "store.py", "control.py"):
        (modules / name).write_text("# stub\n", encoding="utf-8")
    (modules / "dashboard" / "plugin_api.py").write_text("# stub\n", encoding="utf-8")
    (modules / "desktop" / "plugin.js").write_text("// stub\n", encoding="utf-8")
    (repo / "tools").mkdir()
    (repo / "tools" / "hub-health.py").write_text(HEALTH.read_text(encoding="utf-8"), encoding="utf-8")
    (repo / "tools" / "deploy-plugin.sh").write_text(
        (ROOT / "tools" / "deploy-plugin.sh").read_text(encoding="utf-8"), encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    hermes = fake_bin / "hermes"
    hermes.write_text(f"#!/usr/bin/env bash\nexit {restart_exit}\n", encoding="utf-8")
    hermes.chmod(0o755)
    return repo, fake_bin


def _run_deploy(repo, fake_bin, extra_env=None):
    home = repo.parent / "hermes-home"
    dest = home / "plugins" / "hail-hermes"
    desktop = home / "desktop-plugins" / "hail-hermes"
    for path in (dest / "dashboard", dest / "desktop", desktop):
        path.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({
        "PATH": f"{fake_bin}{os.pathsep}{env.get('PATH', '')}",
        "HERMES_HOME": str(home),
        "DEST": str(dest),
        "DESKTOP": str(desktop),
        "PYTHONPATH": str(repo / "plugin"),
    })
    env.update(extra_env or {})
    return subprocess.run(["/usr/bin/env", "bash", str(repo / "tools" / "deploy-plugin.sh"), str(repo)],
                          capture_output=True, text=True, timeout=120, env=env)
