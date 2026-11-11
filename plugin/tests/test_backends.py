"""Agent / STT / TTS backends and protocol helpers (no network)."""

import array
import asyncio
import json
import os
import sys
import textwrap

import pytest

from conftest import make_script, pid_alive
from hail_hermes import protocol as P
from hail_hermes.agent import AgentError, DeviceInfo, EchoAgent, HermesCliAgent, build_agent
from hail_hermes.audio import (CommandSTT, CommandTTS, SilenceTTS, StubSTT, build_stt, build_tts,
                                 pcm_to_wav, resample, wav_to_pcm16k)
from hail_hermes.cli import build_hub, main
from hail_hermes.store import Store

DEV = DeviceInfo("dev-1", "watch", "wear")


async def collect(agen):
    return [x async for x in agen]


# -- fake `hermes` binary speaking stream-json ------------------------------------
FAKE_HERMES = textwrap.dedent('''\
    import json, os, sys, time
    if os.environ.get("FAKE_HERMES_PID_FILE"):
        with open(os.environ["FAKE_HERMES_PID_FILE"], "w") as fh:
            fh.write(str(os.getpid()))
    stdin_text = ""
    if "--query-file" in sys.argv:
        qidx = sys.argv.index("--query-file")
        if qidx + 1 < len(sys.argv) and sys.argv[qidx + 1] == "-":
            stdin_text = sys.stdin.read()
    record_log = os.environ.get("FAKE_RECORD_LOG")
    if record_log:
        with open(record_log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"argv": sys.argv[1:], "stdin": stdin_text}) + "\\n")
    argv = sys.argv[1:]
    mode = os.environ.get("FAKE_HERMES_MODE", "ok")
    def emit(o):
        print(json.dumps(o), flush=True)
    sid = argv[argv.index("--resume") + 1] if "--resume" in argv else "sess-new"
    emit({"type": "system", "subtype": "init", "model": "fake", "session_id": ""})
    q = stdin_text
    if mode == "hang":
        emit({"type": "text", "text": "start"})
        time.sleep(60)
    if mode == "fail":
        emit({"type": "result", "session_id": sid, "exit_code": 1, "text": "", "error": "provider down"})
        sys.exit(1)
    if mode == "crash":
        print("boom", file=sys.stderr)
        sys.exit(3)
    if mode == "nostream":
        emit({"type": "result", "session_id": sid, "exit_code": 0, "text": "whole reply"})
        sys.exit(0)
    for w in ("echo: ", q):
        emit({"type": "text", "text": w})
    emit({"type": "tool_use", "name": "device_haptic", "input": {}})
    emit({"type": "result", "session_id": sid, "exit_code": 0, "text": "echo: " + q})
''')


@pytest.fixture
def fake_hermes(tmp_path, monkeypatch):
    script = make_script(tmp_path / "hermes", FAKE_HERMES)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_RECORD_LOG", str(log))

    class FakeHermes:
        def __str__(self):
            return str(script)

        def calls(self):
            return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]

    return FakeHermes()


async def test_hermes_cli_agent_streams_and_resumes_session(fake_hermes, tmp_path):
    store = Store(tmp_path / "s")
    store.issue_token(DEV.device_id, "watch", "wear")
    agent = build_agent({"backend": "hermes", "binary": str(fake_hermes)}, store)
    assert await collect(agent.run(DEV, "hi")) == ["echo: ", "hi"]
    assert store.agent_session(DEV.device_id) == "sess-new"
    assert await collect(agent.run(DEV, "again")) == ["echo: ", "again"]
    first, second = fake_hermes.calls()
    assert first["argv"][:5] == ["chat", "--query-file", "-", "--format", "stream-json"]
    assert first["argv"][first["argv"].index("--source") + 1] == "cli"  # build_agent keeps watch sessions visible in Hermes
    assert first["stdin"] == "hi"
    assert "--resume" not in first["argv"]
    assert second["argv"][second["argv"].index("--resume") + 1] == "sess-new"
    assert second["stdin"] == "again"


async def test_hermes_cli_agent_errors(fake_hermes, monkeypatch):
    agent = HermesCliAgent(binary=str(fake_hermes))
    monkeypatch.setenv("FAKE_HERMES_MODE", "fail")
    with pytest.raises(AgentError) as exc_info:
        await collect(agent.run(DEV, "x"))
    assert str(exc_info.value) == "hermes run failed"
    assert "provider down" in exc_info.value.detail
    monkeypatch.setenv("FAKE_HERMES_MODE", "crash")
    with pytest.raises(AgentError) as exc_info:
        await collect(agent.run(DEV, "x"))
    assert str(exc_info.value) == "hermes run failed"
    assert "exit 3" in exc_info.value.detail
    monkeypatch.setenv("FAKE_HERMES_MODE", "nostream")
    assert await collect(agent.run(DEV, "x")) == ["whole reply"]


async def test_hermes_cli_agent_cancel_kills_process(fake_hermes, monkeypatch, tmp_path):
    pid_file = tmp_path / "fake.pid"
    monkeypatch.setenv("FAKE_HERMES_PID_FILE", str(pid_file))
    monkeypatch.setenv("FAKE_HERMES_MODE", "hang")
    agent = HermesCliAgent(binary=str(fake_hermes))
    got = []

    async def consume():
        async for d in agent.run(DEV, "x"):
            got.append(d)

    task = asyncio.create_task(consume())
    for _ in range(200):
        if got:
            break
        await asyncio.sleep(0.02)
    assert got == ["start"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # No leftover fake hermes process for this test.
    assert not pid_alive(int(pid_file.read_text()))


async def test_echo_agent():
    assert "".join(await collect(EchoAgent().run(DEV, "hi there"))) == "You said: hi there"


# -- audio ----------------------------------------------------------------------
async def test_stub_and_command_stt(tmp_path):
    assert await StubSTT("configured").transcribe(b"\x00" * 640) == "configured"
    script = tmp_path / "stt.py"
    script.write_text("import sys, wave\nw = wave.open(sys.argv[1]); print(f'{w.getframerate()} {w.getnframes()}')\n")
    stt = build_stt({"backend": "command", "command": [sys.executable, str(script), "{wav}"]})
    assert isinstance(stt, CommandSTT)
    assert await stt.transcribe(b"\x00\x00" * 1600) == "16000 1600"


async def test_command_tts_resamples_to_16k(tmp_path):
    script = tmp_path / "tts.py"
    script.write_text(textwrap.dedent("""\
        import io, sys, wave
        buf = io.BytesIO()
        w = wave.open(buf, "wb"); w.setnchannels(2); w.setsampwidth(2); w.setframerate(22050)
        w.writeframes(b"\\x10\\x00\\x10\\x00" * 22050); w.close()
        sys.stdout.buffer.write(buf.getvalue())
    """))
    tts = build_tts({"backend": "command", "command": [sys.executable, str(script), "{text}"]})
    assert isinstance(tts, CommandTTS)
    pcm = await tts.synthesize("hello")
    assert len(pcm) == 2 * 16000  # 1 s of mono s16le @ 16 kHz
    assert set(array.array("h", pcm)) == {16}


def test_wav_roundtrip_and_streaming_header():
    pcm = array.array("h", range(-500, 500)).tobytes()
    assert wav_to_pcm16k(pcm_to_wav(pcm)) == pcm
    wav = bytearray(pcm_to_wav(pcm))
    wav[4:8] = wav[40:44] = b"\xff\xff\xff\xff"  # espeak-ng --stdout style
    assert wav_to_pcm16k(bytes(wav)) == pcm


def test_resample_lengths():
    s = array.array("h", [0, 1000] * 4410)
    assert len(resample(s, 44100, 16000)) == 3200
    assert list(resample(array.array("h", [1, 2, 3]), 16000, 16000)) == [1, 2, 3]


async def test_silence_tts_and_backend_factory():
    pcm = await SilenceTTS(ms_per_char=10).synthesize("x" * 10)
    assert pcm == b"\x00" * 3200
    assert build_tts(None) is None and build_tts({"backend": "none"}) is None
    with pytest.raises(ValueError):
        build_tts({"backend": "nope"})
    with pytest.raises(ValueError):
        build_stt({"backend": "nope"})
    with pytest.raises(ValueError):
        build_agent({"backend": "nope"})


def test_protocol_frames_and_hello_validation():
    assert P.FRAME_BYTES == 640 and P.FRAME_SECONDS == 0.02
    assert [len(f) for f in P.frames(b"\x01" * 1280)] == [640, 640]
    assert P.frames(b"") == []
    assert [len(f) for f in P.frames(b"\x01" * 641)] == [640, 640]  # odd byte dropped, then padded
    P.validate_hello({"v": 2, "device_id": "a", "kind": "wear"})
    for bad in ({"v": 1, "device_id": "a", "kind": "wear"}, {"v": 2, "device_id": "a", "kind": "x"},
                {"device_id": "a", "kind": "wear"}, {"v": 2, "device_id": "a", "kind": "sim", "token": 3}):
        with pytest.raises(P.ProtocolError):
            P.validate_hello(bad)
    assert P.normalize_caps({"push": 1, "zap": True}) == {"push": True, "haptic": False, "led": False,
                                                          "battery": False}
    assert P.normalize_caps(None)["push"] is False


def test_build_hub_from_config(tmp_path):
    hub = build_hub({"state_dir": str(tmp_path), "port": 9001, "idle_timeout": 30, "unknown": 1,
                     "agent": {"backend": "echo"}, "stt": {"text": "yo"}, "tts": {"backend": "silence"}})
    assert hub.config.port == 9001 and hub.config.idle_timeout == 30
    assert isinstance(hub.agent, EchoAgent) and isinstance(hub.tts, SilenceTTS)


def test_cli_devices_and_revoke(tmp_path, capsys):
    store = Store(tmp_path)
    store.issue_token("dev-x", "pixel", "phone")
    assert main(["--state-dir", str(tmp_path), "devices"]) == 0
    assert "dev-x" in capsys.readouterr().out
    assert main(["--state-dir", str(tmp_path), "revoke", "dev-x"]) == 0
    assert main(["--state-dir", str(tmp_path), "revoke", "dev-x"]) == 1
    assert main(["--state-dir", str(tmp_path), "notify", "t", "b"]) == 1  # hub not running
