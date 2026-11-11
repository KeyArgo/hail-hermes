"""Acceptance tests for HermesCliAgent bridge."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from conftest import make_script, pid_alive
from hail_hermes.agent import AgentError, DeviceInfo, HermesCliAgent, build_agent

DEV = DeviceInfo("dev-1", "watch", "wear")
FIXTURES_DIR = Path(__file__).parent / "fixtures"
FAKE_HERMES_SCRIPT = FIXTURES_DIR / "fake_hermes.py"
FAKE_HERMES_SIGTERM_TRAP = FIXTURES_DIR / "fake_hermes_sigterm_trap.py"


async def collect(agen) -> list[str]:
    return [x async for x in agen]


@pytest.fixture
def fake_hermes(tmp_path):
    log = tmp_path / "calls.jsonl"
    pid_file = tmp_path / "child.pid"
    script = make_script(
        tmp_path / "hermes",
        f"import runpy\n"
        f"runpy.run_path({str(FAKE_HERMES_SCRIPT)!r}, run_name='__main__')\n",
    )

    class FakeHermesHelper:
        def __init__(self):
            self.binary = str(script)
            self.log_path = log
            self.pid_path = pid_file

        def env_for(
            self,
            fixture_name: str | None = None,
            mode: str = "replay",
        ) -> dict[str, str]:
            env = {
                "FAKE_RECORD_LOG": str(self.log_path),
                "FAKE_HERMES_PID_FILE": str(self.pid_path),
                "FAKE_HERMES_MODE": mode,
            }
            if fixture_name:
                fixture_path = FIXTURES_DIR / fixture_name
                env["FAKE_HERMES_FIXTURE"] = str(fixture_path)
            return env

        def calls(self) -> list[dict[str, Any]]:
            if not self.log_path.exists():
                return []
            return [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines() if line.strip()]

        def read_pid(self) -> int | None:
            if not self.pid_path.exists():
                return None
            content = self.pid_path.read_text(encoding="utf-8").strip()
            return int(content) if content else None

    return FakeHermesHelper()


async def test_replaying_turn1_and_turn2_resumed(fake_hermes):
    session_map: dict[str, str] = {}
    session_calls: list[tuple[str, str]] = []

    def on_session(device_id: str, sid: str) -> None:
        session_calls.append((device_id, sid))
        session_map[device_id] = sid

    def session_lookup(device_id: str) -> str | None:
        return session_map.get(device_id)

    agent = HermesCliAgent(
        binary=fake_hermes.binary,
        session_lookup=session_lookup,
        on_session=on_session,
        model="fake-model",
        provider="fake-provider",
        env=fake_hermes.env_for("hermes-stream-turn1.jsonl"),
    )

    # 1. First turn: new session
    turn1_deltas = await collect(agent.run(DEV, "Remember sunflower."))
    assert turn1_deltas == ["Sun", "flower", " —", " got", " it", "."]
    assert "".join(turn1_deltas) == "Sunflower — got it."

    expected_sid = "local-668a88d9bfe289e5d7e9780d4efa9c6399a320b5665e89f6c3b9a6882368c2dc"
    assert session_map.get(DEV.device_id) == expected_sid
    assert session_calls == [(DEV.device_id, expected_sid)]
    assert "" not in [sid for _, sid in session_calls]

    calls = fake_hermes.calls()
    assert len(calls) == 1
    call1_argv = calls[0]["argv"]
    assert call1_argv[:5] == ["chat", "--query-file", "-", "--format", "stream-json"]
    assert "--resume" not in call1_argv
    assert "-m" in call1_argv and call1_argv[call1_argv.index("-m") + 1] == "fake-model"
    assert "--provider" in call1_argv and call1_argv[call1_argv.index("--provider") + 1] == "fake-provider"

    # 2. Second turn: resumed session
    agent.env = fake_hermes.env_for("hermes-stream-turn2-resume.jsonl")
    turn2_deltas = await collect(agent.run(DEV, "What flower?"))
    assert turn2_deltas == ["Sun", "flower", "."]
    assert "".join(turn2_deltas) == "Sunflower."

    calls = fake_hermes.calls()
    assert len(calls) == 2
    call2_argv = calls[1]["argv"]
    assert call2_argv[:5] == ["chat", "--query-file", "-", "--format", "stream-json"]
    assert "--resume" in call2_argv
    assert call2_argv[call2_argv.index("--resume") + 1] == expected_sid
    assert "-m" not in call2_argv
    assert "--provider" not in call2_argv


async def test_argv_never_contains_query_text_and_stdin_received(fake_hermes):
    agent = HermesCliAgent(
        binary=fake_hermes.binary,
        env=fake_hermes.env_for("hermes-stream-turn1.jsonl"),
    )
    query = "Say hi in five words! 🌻 漢字 \U0001f680 and quotes: 'hello' \"world\""
    deltas = await collect(agent.run(DEV, query))
    assert len(deltas) > 0

    argv = agent.argv(DEV, query)
    argv_joined = " ".join(argv)
    assert query not in argv_joined
    assert "Say hi" not in argv_joined
    assert "🌻" not in argv_joined
    assert "漢字" not in argv_joined

    calls = fake_hermes.calls()
    assert len(calls) == 1
    assert query not in " ".join(calls[0]["argv"])
    assert calls[0]["stdin"] == query


def test_source_flag():
    agent_default = HermesCliAgent(binary="hermes")
    argv_default = agent_default.argv(DEV, "hi")
    assert "--source" not in argv_default

    agent_cli = HermesCliAgent(binary="hermes", source="cli")
    argv_cli = agent_cli.argv(DEV, "hi")
    assert "--source" in argv_cli
    assert argv_cli[argv_cli.index("--source") + 1] == "cli"


async def test_rejected_source_error(fake_hermes):
    session_calls: list[tuple[str, str]] = []
    agent = HermesCliAgent(
        binary=fake_hermes.binary,
        source="pocket",
        on_session=lambda d, s: session_calls.append((d, s)),
        env=fake_hermes.env_for("hermes-stream-error-source.jsonl"),
    )

    with pytest.raises(AgentError) as exc_info:
        await collect(agent.run(DEV, "hi"))

    exc = exc_info.value
    exc_message = str(exc)
    assert "Gateway" not in exc_message
    assert "pocket" not in exc_message
    assert "Gateway does not support source 'pocket'" in exc.detail
    assert session_calls == []


async def test_fake_crash_without_result(fake_hermes):
    agent = HermesCliAgent(
        binary=fake_hermes.binary,
        env=fake_hermes.env_for(mode="crash"),
    )

    with pytest.raises(AgentError) as exc_info:
        await collect(agent.run(DEV, "crash me"))

    exc = exc_info.value
    assert "hermes run failed" == str(exc)
    assert "exit 3" in exc.detail
    assert "unhandled crash" in exc.detail


async def test_cancelling_consumer_terminates_child_within_5s(fake_hermes):
    agent = HermesCliAgent(
        binary=fake_hermes.binary,
        env=fake_hermes.env_for(mode="hang"),
    )

    got: list[str] = []

    async def consume():
        async for chunk in agent.run(DEV, "hang"):
            got.append(chunk)

    task = asyncio.create_task(consume())

    pid: int | None = None
    for _ in range(50):
        pid = fake_hermes.read_pid()
        if pid is not None:
            break
        await asyncio.sleep(0.05)

    assert pid is not None
    # Process is currently alive
    assert pid_alive(pid)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Assert pid is gone within 5 s
    terminated = False
    for _ in range(50):
        if not pid_alive(pid):
            terminated = True
            break
        await asyncio.sleep(0.1)

    assert terminated, f"PID {pid} was not terminated within 5s"


async def test_cancelling_sigterm_ignored_still_kills_within_3s(tmp_path):
    pid_file = tmp_path / "child.pid"
    script = make_script(
        tmp_path / "hermes",
        "import runpy\n"
        f"runpy.run_path({str(FAKE_HERMES_SIGTERM_TRAP)!r}, run_name='__main__')\n",
    )
    env = {
        "FAKE_HERMES_PID_FILE": str(pid_file),
    }
    agent = HermesCliAgent(binary=str(script), env=env)
    got: list[str] = []

    async def consume():
        async for chunk in agent.run(DEV, "hang"):
            got.append(chunk)

    task = asyncio.create_task(consume())
    pid: int | None = None
    for _ in range(100):
        if pid_file.exists() and got:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
            break
        await asyncio.sleep(0.02)
    assert pid is not None
    assert got == ["start"]

    t0 = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    while time.monotonic() - t0 < 3.0:
        if not pid_alive(pid):
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail(f"PID {pid} still alive {time.monotonic() - t0:.2f}s after cancel")


def test_toolsets_on_every_turn_and_model_only_on_new_session():
    sessions = {"dev-resumed": "sess-prev-456"}
    agent = HermesCliAgent(
        binary="hermes",
        toolsets="file,terminal",
        model="claude-haiku",
        provider="anthropic",
        session_lookup=lambda d: sessions.get(d),
    )

    # 1. New session
    dev_new = DeviceInfo("dev-new")
    argv_new = agent.argv(dev_new, "hello")
    assert "-t" in argv_new
    assert argv_new[argv_new.index("-t") + 1] == "file,terminal"
    assert "-m" in argv_new
    assert argv_new[argv_new.index("-m") + 1] == "claude-haiku"
    assert "--provider" in argv_new
    assert argv_new[argv_new.index("--provider") + 1] == "anthropic"
    assert "--resume" not in argv_new

    # 2. Resumed session
    dev_resumed = DeviceInfo("dev-resumed")
    argv_resumed = agent.argv(dev_resumed, "follow up")
    assert "-t" in argv_resumed
    assert argv_resumed[argv_resumed.index("-t") + 1] == "file,terminal"
    assert "--resume" in argv_resumed
    assert argv_resumed[argv_resumed.index("--resume") + 1] == "sess-prev-456"
    assert "-m" not in argv_resumed
    assert "--provider" not in argv_resumed


async def test_nostream_yields_result_text_once(fake_hermes):
    agent = HermesCliAgent(
        binary=fake_hermes.binary,
        env=fake_hermes.env_for(mode="nostream"),
    )
    result = await collect(agent.run(DEV, "hello"))
    assert result == ["whole reply"]


def test_build_agent_passthrough():
    class FakeStore:
        def agent_session(self, d):
            return "stored-sid"

        def set_agent_session(self, d, s):
            pass

    store = FakeStore()
    cfg = {
        "backend": "hermes",
        "binary": "/bin/true",
        "toolsets": "web,browser",
        "model": "gpt-4o",
        "provider": "openai",
        "source": "cli",
        "extra_args": ["--verbose"],
        "timeout": 120,
    }
    agent = build_agent(cfg, store)
    assert isinstance(agent, HermesCliAgent)
    assert agent.binary == "/bin/true"
    assert agent.toolsets == "web,browser"
    assert agent.model == "gpt-4o"
    assert agent.provider == "openai"
    assert agent.source == "cli"
    assert agent.extra_args == ["--verbose"]
    assert agent.timeout == 120.0

    # Default None passthrough
    agent_default = build_agent({"backend": "hermes"})
    assert isinstance(agent_default, HermesCliAgent)
    assert agent_default.source == "cli"  # build_agent keeps watch sessions visible in Hermes
    assert agent_default.toolsets is None
    assert agent_default.model is None
    assert agent_default.provider is None


async def test_rejected_toolset_retries_once_with_default_tools(tmp_path):
    """Before the plugin is enabled, Hermes doesn't know the "pocket" toolset and fails with invalid_params."""
    log = tmp_path / "argv.log"
    fake = make_script(
        tmp_path / "fake_hermes",
        "import json, sys\n"
        f"open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "sys.stdin.read()\n"
        "if '-t' in sys.argv:\n"
        "    print(json.dumps({'type': 'result', 'session_id': '', 'exit_code': 1, 'text': '', 'error': 'invalid_params'}))\n"
        "    sys.exit(1)\n"
        "print(json.dumps({'type': 'text', 'text': 'Hi!'}))\n"
        "print(json.dumps({'type': 'result', 'session_id': 'local-x', 'exit_code': 0, 'text': 'Hi!'}))\n",
    )
    agent = HermesCliAgent(binary=str(fake), toolsets="clarify,pocket")
    out = [d async for d in agent.run(DeviceInfo(device_id="d1"), "hello")]
    assert "".join(out) == "Hi!"
    runs = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(runs) == 2 and "-t" in runs[0] and "-t" not in runs[1]
    assert agent.toolsets == "clarify,pocket"
