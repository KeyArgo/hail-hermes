"""A child process must not inherit the owner's credentials.

Review item 2. The TTS/STT worker, ffmpeg, the `command` backends and `adb` were all spawned with an
environment derived from `dict(os.environ)`, so every provider key and token in the parent Hermes
process was handed to programs this plugin does not control.

These tests set real-looking secrets in the parent environment, drive each spawn site, and assert that
the child's environment does not contain them — the shape that fails on the reviewed tree and passes
once every site goes through `minimal_env()`.
"""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from hail_hermes import audio
from hail_hermes.childenv import minimal_env

SECRETS = {
    "OPENAI_API_KEY": "sk-test-should-not-leak",
    "ANTHROPIC_API_KEY": "sk-ant-should-not-leak",
    "GITHUB_TOKEN": "ghp_should_not_leak",
    "HERMES_AUTH_TOKEN": "tok-should-not-leak",
    "AWS_SECRET_ACCESS_KEY": "aws-should-not-leak",
}


@pytest.fixture()
def parent_env(monkeypatch):
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/someone")
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("POCKET_TTS_VOICE", "warm")
    monkeypatch.setenv("SYSTEMROOT", "C:\\Windows")
    return SECRETS


def test_minimal_env_drops_every_secret(parent_env):
    env = minimal_env()
    for name in SECRETS:
        assert name not in env, f"{name} leaked into a child environment"


def test_minimal_env_keeps_what_a_child_needs(parent_env):
    env = minimal_env()
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/home/someone"
    assert env["LANG"] == "C.UTF-8"
    assert env["SYSTEMROOT"] == "C:\\Windows", "Windows children cannot open sockets without this"
    assert env["POCKET_TTS_VOICE"] == "warm", "the plugin's own knobs must survive"


def test_extra_values_are_applied_last():
    env = minimal_env({"VIRTUAL_ENV": "/opt/tts-venv"}, source={"PATH": "/bin"})
    assert env == {"PATH": "/bin", "VIRTUAL_ENV": "/opt/tts-venv"}


async def test_the_command_backend_is_spawned_with_the_minimal_env(parent_env, monkeypatch):
    captured: dict[str, object] = {}

    async def fake_exec(*argv, **kwargs):
        captured["env"] = kwargs.get("env")

        class P:
            returncode = 0

            async def communicate(self, stdin=None):
                return b"out", b""

        return P()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await audio._run(["/bin/true"])
    assert captured["env"] is not None, "_run passed no env, so the child inherited ours"
    for name in SECRETS:
        assert name not in captured["env"], f"{name} reached a command backend"


def test_ffmpeg_is_spawned_with_the_minimal_env(parent_env, monkeypatch):
    captured: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(argv, 0, b"pcm", b"")

    monkeypatch.setattr(audio.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(audio.subprocess, "run", fake_run)
    audio._ffmpeg_to_pcm16k("/tmp/x.oga")
    assert captured["env"] is not None, "ffmpeg was spawned with the inherited environment"
    for name in SECRETS:
        assert name not in captured["env"], f"{name} reached ffmpeg"


def test_the_worker_env_is_minimal_and_still_carries_the_venv(parent_env):
    class Worker(audio._WorkerTTS):
        def _default_command(self):
            return ["python", "-c", "pass"]

    worker = Worker()
    # Subclasses (Kokoro, Piper, Parakeet) carry the interpreter they run under; `_child_env` reads it
    # to find the venv's CUDA libraries and to name VIRTUAL_ENV.
    worker.python = "/opt/tts-venv/bin/python"
    env = worker._child_env()
    for name in SECRETS:
        assert name not in env, f"{name} reached the TTS/STT worker"
    assert "PATH" in env
    assert env.get("VIRTUAL_ENV") == "/opt/tts-venv", "a worker still needs its own interpreter's venv"
