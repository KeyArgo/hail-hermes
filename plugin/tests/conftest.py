"""Shared fixtures: async test runner, in-process hub, fake agent, protocol test client."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import stat
import sys
import uuid
from typing import Any

import pytest
from websockets.asyncio.client import connect

from hail_hermes.control import ControlClient
from hail_hermes.hub import HubConfig, PocketHub
from hail_hermes.store import Store

TIMEOUT = 15.0  # generous for shared CI runners; a passing wait returns as soon as the message lands


def pid_alive(pid: int) -> bool:
    """True if ``pid`` names a running process.

    On Windows ``os.kill(pid, 0)`` is CTRL_C_EVENT, not a liveness probe, so ask the
    kernel directly.  STILL_ACTIVE (259) is what GetExitCodeProcess reports while the
    process runs; once it has exited the handle still opens until closed, so the code
    must be checked rather than the OpenProcess result alone.
    """
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def make_script(path, body: str) -> str:
    """Write an executable script; return the path to spawn.

    POSIX: ``path`` is the script itself (shebang ``#!sys.executable`` + chmod) — same
    as the tests did inline before. Windows: shebang scripts are not executable
    (WinError 193), so write ``<path>.py`` plus a ``<path>.cmd`` shim running
    ``"<sys.executable>" "<path>.py" %*`` and return the ``.cmd`` path.
    """
    if os.name != "nt":
        path = str(path)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"#!{sys.executable}\n{body}")
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        return path
    py_path = f"{path}.py"
    with open(py_path, "w", encoding="utf-8") as fh:
        fh.write(body)
    cmd_path = f"{path}.cmd"
    escaped = str(py_path).replace('"', '""')
    with open(cmd_path, "w", encoding="utf-8") as fh:
        fh.write(f'@"{sys.executable}" "{escaped}" %*\r\n')
    return cmd_path


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    """Run ``async def`` tests with asyncio.run (no pytest-asyncio dependency)."""
    if inspect.iscoroutinefunction(pyfuncitem.obj):
        kwargs = {n: pyfuncitem.funcargs[n] for n in pyfuncitem._fixtureinfo.argnames}
        asyncio.run(asyncio.wait_for(pyfuncitem.obj(**kwargs), 30))
        return True
    return None


class FakeAgent:
    """Scripted agent: yields ``chunks``; ``block`` makes it wait (for interrupt tests)."""

    def __init__(self, chunks=("Hello ", "from ", "Hermes."), block: bool = False, fail: str | None = None):
        self.chunks = list(chunks)
        self.block = block
        self.fail = fail
        self.calls: list[tuple[str, str]] = []
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def run(self, device, text):
        self.calls.append((device.device_id, text))
        self.started.set()
        try:
            if self.fail:
                raise RuntimeError(self.fail)
            for chunk in self.chunks:
                yield chunk
            if self.block:
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class RecordingSTT:
    def __init__(self, text: str | None = None):
        self.text = text
        self.received: list[bytes] = []

    async def transcribe(self, pcm: bytes) -> str:
        self.received.append(pcm)
        return self.text if self.text is not None else f"heard {len(pcm)} bytes"


class EchoTTS:
    """Plays back the last microphone utterance — the 'audio echo' path."""

    def __init__(self, stt: RecordingSTT):
        self.stt = stt

    async def synthesize(self, text: str) -> bytes:
        return self.stt.received[-1] if self.stt.received else b"\x01\x00" * 800


@contextlib.asynccontextmanager
async def running_hub(state_dir, agent=None, stt=None, tts=None, quick_agent=None, **cfg):
    config = HubConfig(**{"host": "127.0.0.1", "port": 0, "audio_pacing": False, **cfg})
    hub = PocketHub(agent=agent or FakeAgent(), stt=stt or RecordingSTT("hello"), tts=tts,
                    store=Store(state_dir), config=config, quick_agent=quick_agent)
    async with hub:
        yield hub


class Client:
    def __init__(self, ws, hub: PocketHub):
        self.ws = ws
        self.hub = hub

    @classmethod
    @contextlib.asynccontextmanager
    async def open(cls, hub: PocketHub, path: str | None = None):
        url = hub.url if path is None else f"ws://127.0.0.1:{hub.port}{path}"
        async with connect(url) as ws:
            yield cls(ws, hub)

    async def send(self, type_: str, **fields: Any) -> None:
        await self.ws.send(json.dumps({"type": type_, "v": 2, **fields}))

    async def send_raw(self, data) -> None:
        await self.ws.send(data)

    async def recv(self, timeout: float = TIMEOUT):
        raw = await asyncio.wait_for(self.ws.recv(), timeout)
        return raw if isinstance(raw, bytes) else json.loads(raw)

    async def expect(self, type_: str, timeout: float = TIMEOUT) -> dict:
        msg = await self.recv(timeout)
        assert isinstance(msg, dict) and msg["type"] == type_, f"expected {type_}, got {msg!r}"
        assert msg["v"] == 2
        return msg

    async def until(self, pred, timeout: float = TIMEOUT) -> list:
        """Collect messages up to and including the first one matching ``pred``."""
        out = []
        async with asyncio.timeout(timeout):
            while True:
                msg = await self.recv(timeout)
                out.append(msg)
                if pred(msg):
                    return out

    async def silent(self, timeout: float = 0.2) -> bool:
        try:
            msg = await self.recv(timeout)
        except asyncio.TimeoutError:
            return True
        raise AssertionError(f"unexpected message {msg!r}")

    async def hello(self, device_id: str, token: str | None = None, kind: str = "sim",
                    caps: dict | None = None, name: str = "test", modes: bool = False) -> None:
        fields = {"device_id": device_id, "name": name, "kind": kind,
                  "caps": caps if caps is not None else {"push": True, "haptic": True, "led": False, "battery": True}}
        if token:
            fields["token"] = token
        if modes:
            fields["modes"] = True
        await self.send("hello", **fields)

    async def pair(self, device_id: str | None = None, **hello_kw) -> tuple[str, str, dict]:
        """hello → pending → owner approval → paired → welcome. Returns (device_id, token, welcome)."""
        device_id = device_id or str(uuid.uuid4())
        await self.hello(device_id, **hello_kw)
        pending = await self.expect("pair_pending")
        client = ControlClient(self.hub.store.dir)
        result = await asyncio.to_thread(client.call, "pair_approve", request_id=pending["request_id"])
        assert result["device_id"] == device_id
        token = (await self.expect("paired"))["token"]
        welcome = await self.expect("welcome")
        return device_id, token, welcome


def is_type(t: str, **match):
    return lambda m: isinstance(m, dict) and m["type"] == t and all(m.get(k) == v for k, v in match.items())


def screens(msgs) -> list[str]:
    return [m["screen"] for m in msgs if isinstance(m, dict) and m["type"] == "state"]


@pytest.fixture
def state_dir(tmp_path):
    return tmp_path / "pocket-state"


@pytest.fixture(autouse=True)
def _reset_rejected_toolsets():
    """The hub remembers rejected toolsets for the process lifetime; tests must not leak that to each other."""
    import hail_hermes.agent as agent_module
    agent_module._REJECTED_TOOLSETS.clear()
    yield
    agent_module._REJECTED_TOOLSETS.clear()


@pytest.fixture(autouse=True)
def _isolated_default_state(tmp_path, monkeypatch):
    """Anything that falls back to the default state dir (e.g. QuickAgent history) writes to a temp dir, never ~/.hermes."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home" / ".hermes"))
    monkeypatch.setenv("HERMES_POCKET_STATE", str(tmp_path / "default-state"))
