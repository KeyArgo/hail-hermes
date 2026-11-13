"""Warm Hermes STT and Kokoro TTS. Unit tests use fakes — no Whisper, no GPU."""

from __future__ import annotations

import array
import asyncio
import logging
import os
import sys
import tempfile
import textwrap
import threading
import time
import wave

import pytest

from conftest import pid_alive


@pytest.fixture(autouse=True)
def _no_real_gpu_kokoro(monkeypatch, tmp_path):
    """The owner's machine has a real GPU Kokoro venv; tests must not depend on it."""
    monkeypatch.setattr("hail_hermes.audio._KOKORO_GPU_PYTHON", str(tmp_path / "no-gpu-kokoro" / "python"))

from hail_hermes.audio import (
    CommandSTT,
    CommandTTS,
    EspeakTTS,
    FallbackTTS,
    HermesSTT,
    KokoroTTS,
    PiperTTS,
    STTError,
    SilenceTTS,
    StubSTT,
    TTSError,
    build_stt,
    build_tts,
)
from hail_hermes.agent import DeviceInfo, HermesCliAgent
from hail_hermes.cli import build_hub, main

# Fake Kokoro child. Speaks the same pipe protocol and never loads a model.
FAKE_WORKER = textwrap.dedent("""\
    import array
    import json
    import math
    import os
    import struct
    import sys
    import time

    # argv: <pidfile> <mode> <rate> <seconds> [timeout] - passed by the helpers below, because the
    # worker is spawned with a minimal environment now (review item 2).
    #
    # ASCII only, on purpose: this file is written by the parent and parsed by a child interpreter, and a
    # non-ASCII byte written with a platform default encoding (cp1252 on Windows) is not valid UTF-8, so
    # the child dies with a SyntaxError before it reads a single request.
    argv = sys.argv[1:] + ["", "sine", "24000", "0.25", "5"]
    mode = argv[1] or "sine"
    pidfile = argv[0]


    def current_mode():
        # Read per request: a test flips the mode mid-run, and the child no longer inherits our
        # environment (review item 2), so a small file carries the change.
        if not pidfile:
            return mode
        try:
            with open(pidfile + ".mode", encoding="utf-8") as handle:
                return handle.read().strip() or mode
        except OSError:
            return mode
    if pidfile:
        with open(pidfile, "a", encoding="utf-8") as handle:
            handle.write(str(os.getpid()) + "\\n")

    def sine_pcm():
        rate = int(argv[2] or "24000")
        seconds = float(argv[3] or "0.25")
        count = int(seconds * rate)
        samples = array.array("h", (
            int(16000 * math.sin(2 * math.pi * 440 * i / rate))
            for i in range(count)
        ))
        if sys.byteorder != "little":
            samples.byteswap()
        return samples.tobytes(), rate

    def tone_pcm(text):
        rate = 24000
        value = int(text.split(":", 1)[1])
        count = 2400
        pcm = struct.pack("<h", value) * count
        return pcm, rate

    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    while True:
        raw = stdin.readline()
        if not raw:
            break
        request = json.loads(raw)
        mode = current_mode()
        if mode == "hang":
            time.sleep(3600)
        text = request.get("text") or ""
        if text.strip() == "":
            pcm = b""
            rate = 24000
        elif text.startswith("tone:"):
            pcm, rate = tone_pcm(text)
        else:
            pcm, rate = sine_pcm()
        header = {"id": request.get("id"), "ok": True, "rate": rate, "bytes": len(pcm)}
        stdout.write((json.dumps(header) + "\\n").encode("utf-8"))
        stdout.flush()
        if mode == "pause":
            time.sleep(0.05)
        if pcm:
            stdout.write(pcm)
        stdout.flush()
        if mode == "once":
            break
""")


def _write_worker(tmp_path):
    script = tmp_path / "fake_kokoro.py"
    script.write_text(FAKE_WORKER, encoding="utf-8", newline="\n")
    return script


def _pids(path) -> list[int]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [int(line) for line in lines if line.strip()]


def _alive(pid: int) -> bool:
    if not pid_alive(pid):
        return False
    if os.name == "nt":
        return True
    stat_path = f"/proc/{pid}/stat"
    try:
        with open(stat_path, encoding="utf-8") as handle:
            stat = handle.read()
    except OSError:
        return False
    end = stat.rfind(")")
    if end < 0 or end + 2 >= len(stat):
        return True
    return stat[end + 2] != "Z"


def _idents() -> set[int | None]:
    return {t.ident for t in threading.enumerate()}


def _spawned(before: set[int | None]) -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.ident not in before]


def _block_on(release: threading.Event, started: threading.Event, calls: dict):
    """Fake transcriber: record the WAV path, then block until ``release`` is set."""

    def fake(path, model=None, source=None):
        calls["n"] += 1
        calls["path"] = path
        started.set()
        release.wait(timeout=30)
        return {"success": True, "transcript": "  back  "}

    return fake


async def _transcribe_when_free(stt: HermesSTT, pcm: bytes) -> str:
    deadline = time.monotonic() + 15.0
    while True:
        try:
            return await stt.transcribe(pcm)
        except STTError:
            if time.monotonic() >= deadline:
                raise AssertionError("transcriber stayed busy")
            await asyncio.sleep(0.01)


def _worker_argv(script, pidfile, env):
    """Knobs travel as arguments: the child's environment is an allowlist (review item 2)."""
    return [
        sys._base_executable, "-u", str(script),
        str(pidfile),
        str(env.get("FAKE_KOKORO_MODE", "sine")),
        str(env.get("FAKE_KOKORO_RATE", "24000")),
        str(env.get("FAKE_KOKORO_SECONDS", "0.25")),
        str(env.get("TIMEOUT", "5")),
    ]


def _set_mode(pidfile, mode: str) -> None:
    """Flip the fake worker's behaviour mid-test (the child no longer reads our environment)."""
    pidfile.write_text  # noqa: B018 - keeps the type obvious to a reader
    with open(str(pidfile) + ".mode", "w", encoding="utf-8") as handle:
        handle.write(mode)


def _kokoro(tmp_path, monkeypatch, **env):
    script = _write_worker(tmp_path)
    pidfile = tmp_path / "pids"
    tts = KokoroTTS(
        timeout=float(env.get("TIMEOUT", "5")),
        worker_cmd=_worker_argv(script, pidfile, env),
    )
    return tts, pidfile


def _piper(tmp_path, monkeypatch, **env):
    script = tmp_path / "fake_piper.py"
    script.write_text(FAKE_WORKER, encoding="utf-8", newline="\n")
    pidfile = tmp_path / "piper_pids"
    env = {"FAKE_KOKORO_RATE": "22050", **env}
    tts = PiperTTS(
        timeout=float(env.get("TIMEOUT", "5")),
        worker_cmd=_worker_argv(script, pidfile, env),
    )
    return tts, pidfile


# -- Hermes STT -----------------------------------------------------------------
async def test_hermes_stt_wav_roundtrip_and_cleanup():
    pcm = array.array("h", [-32768, -1, 0, 1, 32767, 42]).tobytes()
    seen = {}

    def fake(path, model=None, source=None):
        seen["path"] = path
        seen["model"] = model
        seen["source"] = source
        info = os.stat(path)
        seen["mode"] = info.st_mode & 0o777
        with wave.open(path, "rb") as handle:
            seen["channels"] = handle.getnchannels()
            seen["width"] = handle.getsampwidth()
            seen["rate"] = handle.getframerate()
            seen["frames"] = handle.readframes(handle.getnframes())
        return {"success": True, "transcript": "  hello pocket  ", "provider": "local"}

    stt = HermesSTT(transcribe_fn=fake, timeout=5)
    assert await stt.transcribe(pcm) == "hello pocket"
    assert seen["source"] == "pocket"
    assert seen["model"] is None
    if os.name != "nt":
        # Windows has no POSIX owner-only permission bits: chmod only toggles the
        # read-only flag, so a 0o600 request is reported back as 0o666.
        assert seen["mode"] == 0o600
    assert seen["channels"] == 1
    assert seen["width"] == 2
    assert seen["rate"] == 16000
    assert seen["frames"] == pcm
    assert not os.path.exists(seen["path"])


async def test_hermes_stt_cleanup_when_transcriber_raises():
    seen = {}

    def fake(path, model=None, source=None):
        seen["path"] = path
        raise RuntimeError(f"NEEDLE_PATH_LEAK {path} cuda exploded")

    stt = HermesSTT(transcribe_fn=fake)
    with pytest.raises(STTError) as caught:
        await stt.transcribe(b"\x00\x00" * 160)
    err = caught.value
    assert err.code == "stt_failed"
    message = str(err)
    assert "NEEDLE_PATH_LEAK" not in message
    assert seen["path"] not in message
    assert "cuda" not in message
    assert not os.path.exists(seen["path"])


async def test_hermes_stt_failure_dict_hides_backend_text():
    seen = {}

    def fake(path, model=None, source=None):
        seen["path"] = path
        return {
            "success": False,
            "transcript": "",
            "error": f"Local transcription failed at {path}: cuda exploded NEEDLE",
        }

    stt = HermesSTT(transcribe_fn=fake)
    with pytest.raises(STTError) as caught:
        await stt.transcribe(b"\x00\x00" * 80)
    err = caught.value
    assert err.code == "stt_failed"
    message = str(err)
    assert seen["path"] not in message
    assert "NEEDLE" not in message
    assert "cuda" not in message
    assert ".wav" not in message
    assert not os.path.exists(seen["path"])


async def test_hermes_stt_timeout_keeps_one_daemon_worker():
    release = threading.Event()
    started = threading.Event()
    calls = {"n": 0, "path": ""}
    pcm = b"\x00\x00" * 8
    stt = HermesSTT(transcribe_fn=_block_on(release, started, calls), timeout=0.6)
    before = _idents()
    task = asyncio.create_task(stt.transcribe(pcm))
    try:
        # A shared CI runner can take seconds to schedule the worker thread; the old two-second window
        # reported that as "transcription is broken". Poll (not `to_thread`: this test counts threads, and
        # the executor would add one of its own).
        for _ in range(1500):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set(), "the transcriber thread never started"
        with pytest.raises(STTError) as caught:
            await task
        err = caught.value
        assert err.code == "stt_failed"
        assert str(err) == "transcription failed"
        assert calls["path"] not in str(err)
        assert calls["n"] == 1
        assert calls["path"] and not os.path.exists(calls["path"])
        spawned = _spawned(before)
        assert len(spawned) == 1
        assert spawned[0].daemon is True
        worker_ident = spawned[0].ident

        marked = time.monotonic()
        with pytest.raises(STTError) as caught:
            await stt.transcribe(pcm)
        # It must refuse instead of queueing behind the busy worker (which is blocked for 30 s); a loaded
        # runner spends a few hundred ms on scheduling alone, so the bound is generous but still decisive.
        assert time.monotonic() - marked < 2.0
        assert caught.value.code == "stt_failed"
        assert str(caught.value) == "transcription failed"
        assert calls["n"] == 1
        assert [t.ident for t in _spawned(before)] == [worker_ident]

        release.set()
        assert await _transcribe_when_free(stt, pcm) == "back"
        assert calls["n"] == 2
        assert [t.ident for t in _spawned(before)] == [worker_ident]
    finally:
        release.set()
        if not task.done():
            task.cancel()
        try:
            await task
        except (STTError, asyncio.CancelledError):
            pass


async def test_hermes_stt_cancel_keeps_one_daemon_worker():
    release = threading.Event()
    started = threading.Event()
    calls = {"n": 0, "path": ""}
    pcm = b"\x00\x00" * 4
    stt = HermesSTT(transcribe_fn=_block_on(release, started, calls), timeout=30)
    before = _idents()
    task = asyncio.create_task(stt.transcribe(pcm))
    try:
        # A shared CI runner can take seconds to schedule the worker thread; the old two-second window
        # reported that as "transcription is broken". Poll (not `to_thread`: this test counts threads, and
        # the executor would add one of its own).
        for _ in range(1500):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set(), "the transcriber thread never started"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls["path"] and not os.path.exists(calls["path"])
        spawned = _spawned(before)
        assert len(spawned) == 1
        assert spawned[0].daemon is True
        worker_ident = spawned[0].ident

        marked = time.monotonic()
        with pytest.raises(STTError) as caught:
            await stt.transcribe(pcm)
        # It must refuse instead of queueing behind the busy worker (which is blocked for 30 s); a loaded
        # runner spends a few hundred ms on scheduling alone, so the bound is generous but still decisive.
        assert time.monotonic() - marked < 2.0
        assert caught.value.code == "stt_failed"
        assert str(caught.value) == "transcription failed"
        assert calls["n"] == 1
        assert [t.ident for t in _spawned(before)] == [worker_ident]

        release.set()
        assert await _transcribe_when_free(stt, pcm) == "back"
        assert calls["n"] == 2
        assert [t.ident for t in _spawned(before)] == [worker_ident]
    finally:
        release.set()


async def test_hermes_stt_mkstemp_oserror_is_stt_error(monkeypatch):
    real_mkstemp = tempfile.mkstemp
    state = {"fail": True}
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        if state["fail"]:
            raise OSError(28, "No space left on device NEEDLE /tmp/pocket-stt-secret.wav")
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr("hail_hermes.audio.tempfile.mkstemp", flaky)

    def fake(path, model=None, source=None):
        calls["n"] += 1
        return {"success": True, "transcript": " recovered "}

    stt = HermesSTT(transcribe_fn=fake, timeout=5)
    with pytest.raises(STTError) as caught:
        await stt.transcribe(b"\x00\x00" * 8)
    err = caught.value
    assert err.code == "stt_failed"
    assert str(err) == "transcription failed"
    assert err.__cause__ is None
    assert err.__suppress_context__
    assert "NEEDLE" not in str(err)
    assert "No space" not in str(err)
    assert ".wav" not in str(err)
    assert calls["n"] == 0

    state["fail"] = False
    assert await stt.transcribe(b"\x00\x00" * 8) == "recovered"
    assert calls["n"] == 1


# -- Kokoro TTS -----------------------------------------------------------------
async def test_kokoro_reuses_one_child(tmp_path, monkeypatch):
    tts, pidfile = _kokoro(tmp_path, monkeypatch, FAKE_KOKORO_SECONDS="1.0", FAKE_KOKORO_MODE="sine")
    pid = None
    try:
        first = await tts.synthesize("hello")
        pid = tts._proc.pid
        second = await tts.synthesize("hello again")
        empty = await tts.synthesize("   ")
        assert tts._proc.pid == pid
        assert _pids(pidfile) == [pid]
        expect = 16000 * 2
        assert len(first) % 2 == 0
        assert len(second) % 2 == 0
        assert abs(len(first) - expect) <= 4
        assert abs(len(second) - expect) <= 4
        assert empty == b""
    finally:
        await tts.close()
    assert pid is not None
    assert not _alive(pid)


async def test_kokoro_timeout_kills_and_next_call_restarts(tmp_path, monkeypatch):
    tts, pidfile = _kokoro(
        tmp_path,
        monkeypatch,
        FAKE_KOKORO_MODE="hang",
        FAKE_KOKORO_SECONDS="0.25",
        TIMEOUT="0.4",
    )
    try:
        started = time.monotonic()
        with pytest.raises(TTSError) as caught:
            await tts.synthesize("hello")
        elapsed = time.monotonic() - started
        assert caught.value.code == "tts_failed"
        assert "hello" not in str(caught.value)
        assert elapsed < 3.0
        assert elapsed >= 0.3
        hung = _pids(pidfile)
        assert len(hung) == 1
        assert not _alive(hung[0])
        _set_mode(pidfile, "sine")
        pcm = await tts.synthesize("hello")
        pids = _pids(pidfile)
        assert len(pids) == 2
        assert pids[1] != hung[0]
        assert tts._proc.pid == pids[1]
        assert len(pcm) % 2 == 0
        assert abs(len(pcm) - int(0.25 * 16000) * 2) <= 4
    finally:
        await tts.close()


async def test_kokoro_restarts_after_child_exit(tmp_path, monkeypatch):
    tts, pidfile = _kokoro(
        tmp_path,
        monkeypatch,
        FAKE_KOKORO_MODE="once",
        FAKE_KOKORO_SECONDS="0.25",
    )
    try:
        first = await tts.synthesize("hello")
        second = await tts.synthesize("hello")
        pids = _pids(pidfile)
        assert len(pids) == 2
        assert pids[0] != pids[1]
        assert tts._proc.pid == pids[1]
        expect = int(0.25 * 16000) * 2
        assert abs(len(first) - expect) <= 4
        assert abs(len(second) - expect) <= 4
    finally:
        await tts.close()


async def test_kokoro_serializes_concurrent_calls(tmp_path, monkeypatch):
    tts, pidfile = _kokoro(tmp_path, monkeypatch, FAKE_KOKORO_MODE="pause")
    try:
        texts = ("tone:100", "tone:200", "tone:300")
        results = await asyncio.gather(*(tts.synthesize(text) for text in texts))
        assert _pids(pidfile) == [tts._proc.pid]
        for text, pcm in zip(texts, results):
            value = int(text.split(":", 1)[1])
            samples = array.array("h")
            samples.frombytes(pcm)
            assert len(pcm) % 2 == 0
            assert len(samples) == 1600
            assert set(samples) == {value}
    finally:
        await tts.close()


# -- fallback and factories -----------------------------------------------------
async def test_fallback_uses_secondary_on_tts_error(caplog):
    class Boom:
        def __init__(self):
            self.calls = 0

        async def synthesize(self, text: str) -> bytes:
            self.calls += 1
            raise TTSError("tts_failed")

    class Ok:
        def __init__(self):
            self.calls = []

        async def synthesize(self, text: str) -> bytes:
            self.calls.append(text)
            return b"\x02\x00"

    primary = Boom()
    secondary = Ok()
    tts = FallbackTTS(primary, secondary)
    with caplog.at_level(logging.WARNING, logger="hail_hermes.audio"):
        assert await tts.synthesize("hi") == b"\x02\x00"
        assert await tts.synthesize("again") == b"\x02\x00"
    assert primary.calls == 2
    assert secondary.calls == ["hi", "again"]
    warnings = [record for record in caplog.records if "fallback" in record.message]
    assert len(warnings) == 1


async def test_fallback_keeps_primary_when_it_works():
    class Ok:
        def __init__(self):
            self.calls = []

        async def synthesize(self, text: str) -> bytes:
            self.calls.append(text)
            return b"\x03\x00"

    class Unused:
        async def synthesize(self, text: str) -> bytes:
            raise AssertionError("secondary should not run")

    primary = Ok()
    tts = FallbackTTS(primary, Unused())
    assert await tts.synthesize("hi") == b"\x03\x00"
    assert primary.calls == ["hi"]


def test_build_stt_and_tts_backends(monkeypatch):
    stub = build_stt(None)
    assert isinstance(stub, StubSTT)
    hermes = build_stt({"backend": "hermes", "timeout": "15"})
    assert isinstance(hermes, HermesSTT)
    assert hermes.timeout == 15.0
    command = build_stt({"backend": "command", "command": ["true", "{wav}"]})
    assert isinstance(command, CommandSTT)

    assert build_tts(None) is None
    assert build_tts({"backend": "none"}) is None
    assert isinstance(build_tts({"backend": "silence"}), SilenceTTS)
    assert isinstance(build_tts({"backend": "command", "command": ["true"]}), CommandTTS)

    monkeypatch.setattr("hail_hermes.audio.shutil.which", lambda name: None)
    bare = build_tts({
        "backend": "kokoro",
        "voice": "af_bella",
        "speed": "1.2",
        "timeout": "9",
        "python": "~/voice-python",
        "model_dir": "~/models/kokoro",
    })
    assert isinstance(bare, KokoroTTS)
    assert bare.voice == "af_bella"
    assert bare.speed == 1.2
    assert bare.timeout == 9.0
    assert bare.python == os.path.expanduser("~/voice-python")
    assert bare.model_dir == os.path.expanduser("~/models/kokoro")
    assert bare._proc is None

    def which(name):
        if name == "espeak-ng":
            return "/usr/bin/espeak-ng"
        return None

    monkeypatch.setattr("hail_hermes.audio.shutil.which", which)
    wrapped = build_tts({"backend": "kokoro", "voice": "af_sarah"})
    assert isinstance(wrapped, FallbackTTS)
    assert isinstance(wrapped.primary, KokoroTTS)
    assert isinstance(wrapped.secondary, EspeakTTS)
    assert wrapped.primary.voice == "af_sarah"
    assert wrapped.secondary.command[0] == "/usr/bin/espeak-ng"

    def which_espeak(name):
        if name == "espeak":
            return "/usr/bin/espeak"
        return None

    monkeypatch.setattr("hail_hermes.audio.shutil.which", which_espeak)
    espeak_only = build_tts({"backend": "kokoro", "voice": "af_sarah"})
    assert isinstance(espeak_only, FallbackTTS)
    assert isinstance(espeak_only.secondary, EspeakTTS)
    assert espeak_only.secondary.command == [
        "/usr/bin/espeak", "-v", "en-us", "--stdout", "{text}",
    ]
    plain = build_tts({"backend": "espeak"})
    assert isinstance(plain, EspeakTTS)
    assert plain.command[0] == "/usr/bin/espeak"

    with pytest.raises(ValueError):
        build_stt({"backend": "nope"})
    with pytest.raises(ValueError):
        build_tts({"backend": "nope"})


# -- Piper TTS ------------------------------------------------------------------
async def test_piper_reuses_one_child(tmp_path, monkeypatch):
    default_piper = PiperTTS()
    assert default_piper.python == os.path.expanduser("~/.local/share/pocket-piper-venv/bin/python")
    assert default_piper.model == os.path.expanduser("~/.local/share/piper-voices/en_US-lessac-medium.onnx")
    assert default_piper._command()[-2:] == ["--model", default_piper.model]

    tts, pidfile = _piper(tmp_path, monkeypatch, FAKE_KOKORO_SECONDS="1.0", FAKE_KOKORO_MODE="sine")
    pid = None
    try:
        first = await tts.synthesize("hello")
        pid = tts._proc.pid
        second = await tts.synthesize("hello again")
        empty = await tts.synthesize("   ")
        assert tts._proc.pid == pid
        assert _pids(pidfile) == [pid]
        expect = 16000 * 2
        assert len(first) % 2 == 0
        assert len(second) % 2 == 0
        assert abs(len(first) - expect) <= 4
        assert abs(len(second) - expect) <= 4
        assert empty == b""
    finally:
        await tts.close()
    assert pid is not None
    assert not _alive(pid)


def test_build_tts_piper_and_auto(tmp_path, monkeypatch):
    piper_py = tmp_path / "piper-venv" / "bin" / "python"
    piper_model = tmp_path / "piper-voices" / "en_US-lessac-medium.onnx"
    kokoro_py = tmp_path / "voice-venv" / "bin" / "python"
    kokoro_dir = tmp_path / "kokoro"

    monkeypatch.setattr("hail_hermes.audio._PIPER_PYTHON", str(piper_py))
    monkeypatch.setattr("hail_hermes.audio._PIPER_MODEL", str(piper_model))
    monkeypatch.setattr("hail_hermes.audio._KOKORO_PYTHON", str(kokoro_py))
    monkeypatch.setattr("hail_hermes.audio._KOKORO_MODEL_DIR", str(kokoro_dir))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr("hail_hermes.audio.shutil.which", lambda name: None)

    # Nothing installed: auto returns None, piper returns bare PiperTTS.
    assert build_tts("auto") is None
    assert build_tts({"backend": "auto"}) is None
    bare_piper = build_tts({"backend": "piper"})
    assert isinstance(bare_piper, PiperTTS)

    # Only espeak on PATH: auto selects EspeakTTS; piper wraps PiperTTS -> EspeakTTS.
    monkeypatch.setattr(
        "hail_hermes.audio.shutil.which",
        lambda name: "/usr/bin/espeak-ng" if name == "espeak-ng" else None,
    )
    auto_espeak = build_tts("auto")
    assert isinstance(auto_espeak, EspeakTTS)
    piper_espeak = build_tts({"backend": "piper"})
    assert isinstance(piper_espeak, FallbackTTS)
    assert isinstance(piper_espeak.primary, PiperTTS)
    assert isinstance(piper_espeak.secondary, EspeakTTS)

    # Kokoro files exist (no Piper yet): auto selects KokoroTTS; piper falls back to KokoroTTS.
    kokoro_py.parent.mkdir(parents=True, exist_ok=True)
    kokoro_py.write_text("", encoding="utf-8")
    kokoro_dir.mkdir(parents=True, exist_ok=True)
    (kokoro_dir / "kokoro-v1.0.onnx").write_bytes(b"onnx")
    (kokoro_dir / "voices-v1.0.bin").write_bytes(b"voices")

    auto_kokoro = build_tts("auto")
    assert isinstance(auto_kokoro, FallbackTTS)
    assert isinstance(auto_kokoro.primary, KokoroTTS)
    assert isinstance(auto_kokoro.secondary, EspeakTTS)

    piper_with_kokoro = build_tts({"backend": "piper"})
    assert isinstance(piper_with_kokoro, FallbackTTS)
    assert isinstance(piper_with_kokoro.primary, PiperTTS)
    assert isinstance(piper_with_kokoro.secondary, KokoroTTS)

    # Piper files also exist: auto prefers PiperTTS (with Kokoro fallback).
    piper_py.parent.mkdir(parents=True, exist_ok=True)
    piper_py.write_text("", encoding="utf-8")
    piper_model.parent.mkdir(parents=True, exist_ok=True)
    piper_model.write_bytes(b"piper-onnx")

    auto_piper = build_tts("auto")
    assert isinstance(auto_piper, FallbackTTS)
    assert isinstance(auto_piper.primary, PiperTTS)
    assert isinstance(auto_piper.secondary, KokoroTTS)

    # Piper only (no Kokoro, no espeak): auto returns bare PiperTTS.
    monkeypatch.setattr("hail_hermes.audio.shutil.which", lambda name: None)
    (kokoro_dir / "voices-v1.0.bin").unlink()
    auto_piper_bare = build_tts("auto")
    assert isinstance(auto_piper_bare, PiperTTS)


def test_cli_serve_default_agent_toolsets_and_voice_settings(tmp_path, monkeypatch):
    piper_py = tmp_path / "piper" / "python"
    piper_model = tmp_path / "piper" / "model.onnx"
    piper_py.parent.mkdir(parents=True, exist_ok=True)
    piper_py.write_text("", encoding="utf-8")
    piper_model.write_bytes(b"onnx")
    monkeypatch.setattr("hail_hermes.audio._PIPER_PYTHON", str(piper_py))
    monkeypatch.setattr("hail_hermes.audio._PIPER_MODEL", str(piper_model))
    monkeypatch.setattr("hail_hermes.audio._KOKORO_PYTHON", str(tmp_path / "no-kokoro"))
    monkeypatch.setattr("hail_hermes.audio.shutil.which", lambda name: None)

    hub = build_hub({"state_dir": str(tmp_path / "state")})
    assert isinstance(hub.agent, HermesCliAgent)
    assert hub.agent.toolsets == "clarify,pocket"
    assert "pocket" in hub.agent.toolsets.split(",")
    assert hub.agent.model is None
    assert hub.agent.provider is None
    assert isinstance(hub.tts, PiperTTS)
    argv = hub.agent.argv(DeviceInfo(device_id="dev-1"))
    assert "-t" in argv
    assert "pocket" in argv[argv.index("-t") + 1].split(",")
    assert "-m" not in argv
    assert "--provider" not in argv

    configured = build_hub({
        "state_dir": str(tmp_path / "state2"),
        "agent": {
            "model": "anthropic/claude-sonnet-4",
            "provider": "openrouter",
            "toolsets": "web",
        },
    })
    assert isinstance(configured.agent, HermesCliAgent)
    assert configured.agent.model == "anthropic/claude-sonnet-4"
    assert configured.agent.provider == "openrouter"
    assert configured.agent.toolsets == "web"

    captured = {}
    class _Hub:
        async def serve_forever(self):
            return None

    def _fake_build(cfg):
        captured["cfg"] = cfg
        return _Hub()

    monkeypatch.setattr("hail_hermes.cli.build_hub", _fake_build)
    monkeypatch.setattr("hail_hermes.cli.asyncio.run", lambda coro: coro.close())
    assert main(["--state-dir", str(tmp_path / "state3"), "serve"]) == 0
    assert captured["cfg"]["tts"]["backend"] == "auto"
    assert "pocket" in captured["cfg"]["agent"]["toolsets"].split(",")


@pytest.mark.skipif(os.environ.get("POCKET_REAL_VOICE") != "1", reason="set POCKET_REAL_VOICE=1 to run real Kokoro")
async def test_real_kokoro_smoke():
    tts = KokoroTTS()
    try:
        started = time.perf_counter()
        first = await tts.synthesize("Hello from Hermes.")
        first_s = time.perf_counter() - started
        assert first_s < 10.0
        assert len(first) > int(0.5 * 16000 * 2)
        started = time.perf_counter()
        second = await tts.synthesize("Hello from Hermes.")
        second_s = time.perf_counter() - started
        assert second_s < 10.0
        assert len(second) > int(0.5 * 16000 * 2)
        print(
            f"KOKORO_TIMING first={first_s:.3f}s second={second_s:.3f}s "
            f"bytes={len(first)},{len(second)}",
            file=sys.stderr,
        )
    finally:
        await tts.close()


def test_the_fake_worker_is_ascii_so_no_platform_encoding_can_break_it(tmp_path):
    """The child parses this file; the parent's default encoding must not be able to corrupt it.

    Windows CI failed every Kokoro and Piper test with "synthesis failed" because this source contained a
    non-ASCII character and was written with the platform default (cp1252): the child interpreter then read
    0x97 where UTF-8 required a sequence and died before answering. ASCII source plus an explicit encoding
    makes the two halves agree on every platform.
    """
    script = _write_worker(tmp_path)
    raw = script.read_bytes()
    raw.decode("utf-8")  # what the child interpreter requires
    assert raw == FAKE_WORKER.encode("ascii"), (
        "the fake worker must be ASCII: any non-ASCII byte is written with the platform default encoding"
    )
    assert "\r\n" not in FAKE_WORKER, "keep the line endings plain; the child reads it as source"


def test_the_fake_worker_runs_when_spawned_like_windows_would(tmp_path, monkeypatch):
    """A child started from a written file still speaks the protocol — the path, not the mock."""
    import json
    import subprocess

    script = _write_worker(tmp_path)
    pidfile = tmp_path / "pids"
    proc = subprocess.run(
        [sys._base_executable, "-u", str(script), str(pidfile), "sine", "24000", "0.05", "5"],
        input=(json.dumps({"id": 1, "text": "hello"}) + "\n").encode("utf-8"),
        capture_output=True, timeout=60, check=False,
    )
    header = json.loads(proc.stdout.split(b"\n", 1)[0])
    assert header["ok"] is True, proc.stderr.decode("utf-8", "replace")[-400:]
    assert header["rate"] == 24000 and header["bytes"] > 0
