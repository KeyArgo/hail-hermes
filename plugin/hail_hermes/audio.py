"""Pluggable STT / TTS backends. All PCM in and out is s16le, 16 kHz, mono."""

from __future__ import annotations

import array
import glob
import asyncio
import io
import json
import logging
import os
import queue
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import wave
from typing import Any, Protocol

from .protocol import SAMPLE_RATE

log = logging.getLogger(__name__)

_WINDOWS = os.name == "nt"


def _spawn_kwargs() -> dict[str, int]:
    """Detach the worker from us: new process group on Windows, new session on POSIX."""
    if _WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def _kill_process(proc) -> None:
    if _WINDOWS:
        # kill() alone would orphan the real worker behind a venv launcher.
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False)
        return
    os.killpg(proc.pid, signal.SIGKILL)


class STT(Protocol):
    async def transcribe(self, pcm: bytes) -> str: ...


class TTS(Protocol):
    async def synthesize(self, text: str) -> bytes: ...


class STTError(Exception):
    """Speech-to-text failed.

    ``code`` is a stable token for clients. The message is fixed so a backend
    traceback or temp path never leaks out of the hub.
    """

    def __init__(self, code: str = "stt_failed"):
        self.code = code
        super().__init__("transcription failed")


class TTSError(Exception):
    """Speech synthesis failed.

    ``code`` is a stable token for clients. The message is fixed so a worker
    error string or filesystem path never leaks out of the hub.
    """

    def __init__(self, code: str = "tts_failed"):
        self.code = code
        super().__init__("ffmpeg is needed to decode TTS audio" if code == "ffmpeg_needed" else "synthesis failed")


class _WorkerCrashed(Exception):
    """The TTS child died or its stdout framing broke. Internal only."""


# -- WAV helpers --------------------------------------------------------------
def pcm_to_wav(pcm: bytes, rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def wav_to_pcm16k(data: bytes) -> bytes:
    """Decode a 16-bit PCM WAV (any rate, mono/stereo) into 16 kHz mono s16le.

    Tolerates the 0xFFFFFFFF size fields espeak-ng writes when streaming to stdout."""
    if len(data) >= 44 and data[:4] == b"RIFF" and data[40:44] == b"\xff\xff\xff\xff":
        data = data[:4] + (len(data) - 8).to_bytes(4, "little") + data[8:40] + (len(data) - 44).to_bytes(4, "little") + data[44:]
    with wave.open(io.BytesIO(data), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError(f"unsupported WAV sample width {w.getsampwidth()} (need 16-bit)")
        channels, rate = w.getnchannels(), w.getframerate()
        raw = w.readframes(w.getnframes())
    samples = array.array("h")
    samples.frombytes(raw[: len(raw) - len(raw) % 2])
    if sys.byteorder == "big":
        samples.byteswap()
    if channels > 1:
        samples = array.array("h", (
            int(sum(samples[i:i + channels]) / channels) for i in range(0, len(samples) - channels + 1, channels)
        ))
    out = resample(samples, rate, SAMPLE_RATE)
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


def resample(samples: array.array, src: int, dst: int) -> array.array:
    """Linear-interpolation resampler (good enough for speech; no numpy dependency)."""
    if src == dst or not samples:
        return array.array("h", samples)
    n_out = int(len(samples) * dst / src)
    step = src / dst
    last = len(samples) - 1
    out = array.array("h", bytes(2 * n_out))
    for i in range(n_out):
        pos = i * step
        j = int(pos)
        frac = pos - j
        a = samples[j]
        b = samples[j + 1] if j < last else a
        out[i] = int(a + (b - a) * frac)
    return out


async def _run(argv: list[str], stdin: bytes | None = None, timeout: float = 60.0) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
    if proc.returncode:
        raise RuntimeError(f"{argv[0]} exited {proc.returncode}: {err.decode(errors='replace')[-300:]}")
    return out


def _argv(template: str | list[str], **subs: str) -> list[str]:
    parts = shlex.split(template) if isinstance(template, str) else list(template)
    return [p.format(**subs) for p in parts]


# -- STT ----------------------------------------------------------------------
class StubSTT:
    """Ignores the audio and returns configured text — lets voice flows run with no ASR installed."""

    def __init__(self, text: str = "hello"):
        self.text = text

    async def transcribe(self, pcm: bytes) -> str:
        return self.text


class CommandSTT:
    """Runs an ASR CLI on a temp WAV; stdout is the transcript.

    ``command`` is an argv template with ``{wav}`` (e.g. ``whisper-cli -nt -f {wav}``) — the same shape
    as Hermes' ``HERMES_LOCAL_STT_COMMAND``. No shell is involved."""

    def __init__(self, command: str | list[str], timeout: float = 120.0):
        self.command = command
        self.timeout = timeout

    async def transcribe(self, pcm: bytes) -> str:
        fd, path = tempfile.mkstemp(suffix=".wav", prefix="pocket-stt-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(pcm_to_wav(pcm))
            out = await _run(_argv(self.command, wav=path), timeout=self.timeout)
            return out.decode(errors="replace").strip()
        finally:
            os.unlink(path)


def _call_transcriber(fn, path: str):
    """Invoke Hermes' transcriber. ``source`` labels this caller for its hook only."""
    return fn(path, model=None, source="pocket")


class _STTJob:
    """One transcription for the HermesSTT daemon thread.

    ``ok`` and ``result`` are published before ``done`` is set, so a waiter that
    observes the event also observes the outcome.
    """

    def __init__(self) -> None:
        self.done = threading.Event()
        self.ok = False
        self.result = None

    def succeed(self, result) -> None:
        self.result = result
        self.ok = True
        self.done.set()

    def fail(self) -> None:
        self.done.set()


class HermesSTT:
    """In-process STT through Hermes' own ``transcribe_audio``.

    Hermes caches ``_local_model`` and unloads it after idle time, so staying in-process
    keeps Whisper warm between turns instead of paying a fresh load on every utterance.
    The PCM is written as a 0600 16 kHz mono WAV and removed even when transcription fails.

    Transcription runs on one daemon thread per instance. ``asyncio.to_thread`` is not
    used: its pool threads are non-daemon, so a hung call would survive the timeout and
    block interpreter shutdown, and each repeat would add another thread. While that
    daemon thread is still inside the transcriber, further calls raise ``STTError``
    immediately.
    """

    def __init__(self, transcribe_fn=None, timeout: float = 60.0):
        self._transcribe_fn = transcribe_fn
        self.timeout = timeout
        self._gate = threading.Lock()
        self._inflight = False
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None

    def _fn(self):
        if self._transcribe_fn is None:
            from tools.transcription_tools import transcribe_audio
            self._transcribe_fn = transcribe_audio
        return self._transcribe_fn

    def _claim(self) -> bool:
        """Reserve the single worker. False when a previous call is still inside it."""
        with self._gate:
            if self._inflight:
                return False
            self._inflight = True
            return True

    def _release(self) -> None:
        with self._gate:
            self._inflight = False

    def _ensure_worker(self) -> None:
        with self._gate:
            if self._thread is not None and self._thread.is_alive():
                return
            # Daemon on purpose: a transcriber that ignores the timeout must not pin
            # process exit the way a non-daemon pool thread does.
            self._thread = threading.Thread(
                target=self._worker_loop,
                name="pocket-hermes-stt",
                daemon=True,
            )
            self._thread.start()

    def _worker_loop(self) -> None:
        while True:
            fn, path, job = self._queue.get()
            try:
                job.succeed(_call_transcriber(fn, path))
            except BaseException:
                # A provider helper may raise SystemExit; the waiting caller must still be released.
                job.fail()
            finally:
                self._release()

    async def _wait_job(self, job: _STTJob) -> None:
        # Poll so the wait stays on the event loop. Waiting in the default executor
        # would pin another non-daemon thread for as long as the transcriber runs.
        while not job.done.is_set():
            await asyncio.sleep(0.01)

    async def transcribe(self, pcm: bytes) -> str:
        if not self._claim():
            raise STTError("stt_failed")
        path: str | None = None
        submitted = False
        job: _STTJob | None = None
        try:
            try:
                # mkstemp raises OSError when the disk is full. That must become STTError.
                fd, path = tempfile.mkstemp(suffix=".wav", prefix="pocket-stt-")
                with os.fdopen(fd, "wb") as handle:
                    os.chmod(path, 0o600)
                    handle.write(pcm_to_wav(pcm))
                fn = self._fn()
                job = _STTJob()
                self._ensure_worker()
                self._queue.put((fn, path, job))
                submitted = True
                await asyncio.wait_for(self._wait_job(job), self.timeout)
            except STTError:
                raise
            except Exception:
                raise STTError("stt_failed") from None
        finally:
            if path is not None:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            if not submitted:
                self._release()
        if job is None or not job.ok:
            raise STTError("stt_failed")
        result = job.result
        if not isinstance(result, dict) or not result.get("success"):
            raise STTError("stt_failed")
        text = result.get("transcript")
        if not isinstance(text, str):
            raise STTError("stt_failed")
        return text.strip()


_PARAKEET_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "parakeet_worker.py")
_PARAKEET_PYTHON = "~/.hermes/voice-venv/bin/python"
_PARAKEET_MODEL_DIR = "~/.hermes/models/parakeet-tdt-0.6b-v3-int8"


class ParakeetSTT:
    """One long-lived Parakeet worker, reused across utterances.

    Request: one JSON line ``{"id": n, "bytes": len(pcm), "rate": 16000}`` followed
    by ``bytes`` of s16le mono PCM.
    Response: one JSON line ``{"id": n, "ok": true, "text": "..."}`` or
    ``{"id": n, "ok": false, "text": ""}``.
    Requests are serialized. A timeout kills the child; the next call starts a new one.
    If the child dies mid-request, it is restarted once and the utterance is retried once.
    ``worker_cmd`` replaces the argv so tests can run a fake worker.
    """

    def __init__(
        self,
        python: str | None = None,
        model_dir: str | None = None,
        timeout: float = 30.0,
        worker_cmd: list[str] | None = None,
    ):
        self.python = os.path.expanduser(python or _PARAKEET_PYTHON)
        self.model_dir = os.path.expanduser(model_dir or _PARAKEET_MODEL_DIR)
        self.timeout = timeout
        self.worker_cmd = list(worker_cmd) if worker_cmd is not None else None
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._next_id = 1

    def _command(self) -> list[str]:
        if self.worker_cmd is not None:
            return list(self.worker_cmd)
        return self._default_command()

    def _default_command(self) -> list[str]:
        return [
            self.python,
            "-u",
            _PARAKEET_WORKER,
            "--model-dir",
            self.model_dir,
        ]

    async def _ensure(self) -> asyncio.subprocess.Process:
        proc = self._proc
        if proc is not None and proc.returncode is None:
            return proc
        if proc is not None:
            await proc.wait()
            self._proc = None
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self._command(),
                env=self._child_env(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                **_spawn_kwargs(),
            )
        except OSError:
            raise STTError("stt_failed") from None
        return self._proc

    def _kill(self) -> asyncio.subprocess.Process | None:
        """SIGKILL the worker process group. Synchronous so cancellation still lands."""
        proc = self._proc
        self._proc = None
        if proc is None:
            return None
        if proc.returncode is None:
            try:
                _kill_process(proc)
            except (ProcessLookupError, PermissionError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        return proc

    async def _stop(self) -> None:
        proc = self._kill()
        if proc is None:
            return
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except (TimeoutError, ProcessLookupError):
            pass

    async def close(self) -> None:
        """Terminate the worker child, if one is running."""
        async with self._lock:
            await self._stop()

    async def _exchange(self, proc: asyncio.subprocess.Process, req_id: int, pcm: bytes) -> str:
        if proc.stdin is None or proc.stdout is None:
            raise _WorkerCrashed
        usable = pcm[: len(pcm) - (len(pcm) % 2)]
        line = json.dumps({"id": req_id, "bytes": len(usable), "rate": SAMPLE_RATE}) + "\n"
        try:
            proc.stdin.write(line.encode("utf-8"))
            if usable:
                proc.stdin.write(usable)
            await proc.stdin.drain()
            resp_line = await proc.stdout.readline()
            if not resp_line:
                raise _WorkerCrashed
            try:
                resp = json.loads(resp_line)
            except json.JSONDecodeError:
                raise _WorkerCrashed from None
            if not isinstance(resp, dict) or resp.get("id") != req_id:
                raise _WorkerCrashed
            if not resp.get("ok"):
                raise STTError("stt_failed")
            text = resp.get("text", "")
            if not isinstance(text, str):
                raise _WorkerCrashed
            return text.strip()
        except STTError:
            raise
        except _WorkerCrashed:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            raise _WorkerCrashed from None

    async def _request_once(self, pcm: bytes) -> str:
        proc = await self._ensure()
        req_id = self._next_id
        self._next_id += 1
        try:
            return await asyncio.wait_for(
                self._exchange(proc, req_id, pcm),
                self.timeout,
            )
        except TimeoutError:
            await self._stop()
            raise STTError("stt_failed") from None
        except asyncio.CancelledError:
            await self._stop()
            raise
        except _WorkerCrashed:
            await self._stop()
            raise

    async def _transcribe_locked(self, pcm: bytes) -> str:
        for attempt in (0, 1):
            try:
                return await self._request_once(pcm)
            except _WorkerCrashed:
                await self._stop()
                if attempt == 1:
                    raise STTError("stt_failed") from None
        raise STTError("stt_failed")

    async def transcribe(self, pcm: bytes) -> str:
        async with self._lock:
            try:
                return await self._transcribe_locked(pcm)
            except asyncio.CancelledError:
                await self._stop()
                raise


# -- TTS ----------------------------------------------------------------------
def _ffmpeg_to_pcm16k(path: str) -> bytes:
    """Decode any audio file ffmpeg understands into 16 kHz mono PCM."""
    binary = shutil.which("ffmpeg")
    if not binary:
        raise TTSError("ffmpeg_needed")
    proc = subprocess.run(
        [binary, "-v", "quiet", "-i", path, "-f", "s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "pipe:1"],
        capture_output=True, check=False,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise TTSError()
    return proc.stdout


class EdgeTTS:
    """Microsoft Edge read-aloud voices, with the voice chosen per character.

    Hermes' own TTS path builds its voice from the app config and drops a
    per-call voice, so every character sounded the same. This backend keeps the
    character's voice: a Kokoro spec is mapped onto the closest edge voice, and
    an edge id is used as-is.
    """

    def __init__(self, voice: str = "en-US-AriaNeural", rate: str = "+0%", timeout: float = 30.0):
        self.voice = voice
        self.rate = rate
        self.timeout = timeout

    async def synthesize(self, text: str, voice: str | None = None) -> bytes:
        from .hermes_voice import _edge_voice

        target = _edge_voice(voice) or _edge_voice(self.voice) or self.voice
        try:
            import edge_tts
        except ImportError as exc:
            raise TTSError() from exc
        with tempfile.TemporaryDirectory(prefix="hail-edge-") as directory:
            path = os.path.join(directory, "out.mp3")
            try:
                communicate = edge_tts.Communicate(text, target, rate=self.rate)
                await asyncio.wait_for(communicate.save(path), timeout=self.timeout)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise TTSError() from exc
            log.info("edge tts: voice=%s chars=%d", target, len(text))
            return _ffmpeg_to_pcm16k(path)


class SilenceTTS:
    """Stub TTS: ``ms_per_char`` of silence per character (bounded) — exercises the audio path."""

    def __init__(self, ms_per_char: float = 10.0, max_ms: int = 5000):
        self.ms_per_char = ms_per_char
        self.max_ms = max_ms

    async def synthesize(self, text: str) -> bytes:
        ms = min(self.max_ms, max(20, int(len(text) * self.ms_per_char)))
        return b"\x00\x00" * (SAMPLE_RATE * ms // 1000)


class CommandTTS:
    """Runs a TTS CLI that writes a 16-bit WAV to stdout; ``{text}`` is substituted into the argv."""

    def __init__(self, command: str | list[str], timeout: float = 60.0):
        self.command = command
        self.timeout = timeout

    async def synthesize(self, text: str) -> bytes:
        return wav_to_pcm16k(await _run(_argv(self.command, text=text), timeout=self.timeout))


class EspeakTTS(CommandTTS):
    def __init__(self, voice: str = "en-us", binary: str | None = None, timeout: float = 60.0):
        binary = binary or shutil.which("espeak-ng") or shutil.which("espeak") or "espeak-ng"
        super().__init__([binary, "-v", voice, "--stdout", "{text}"], timeout=timeout)


_KOKORO_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kokoro_worker.py")
_KOKORO_PYTHON = "~/.hermes/voice-venv/bin/python"
_KOKORO_GPU_PYTHON = "~/.local/share/pocket-kokoro-gpu-venv/bin/python"
_KOKORO_MODEL_DIR = "~/.hermes/profiles/dev/tts/kokoro"
_PIPER_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "piper_worker.py")
_PIPER_PYTHON = "~/.local/share/pocket-piper-venv/bin/python"
_PIPER_MODEL = "~/.local/share/piper-voices/en_US-lessac-medium.onnx"


def _pcm_to_16k(pcm: bytes, rate: int) -> bytes:
    """Resample s16le mono PCM from ``rate`` to 16 kHz."""
    if rate <= 0:
        raise TTSError("tts_failed")
    usable = pcm[:len(pcm) - (len(pcm) % 2)]
    samples = array.array("h")
    samples.frombytes(usable)
    if sys.byteorder == "big":
        samples.byteswap()
    out = resample(samples, rate, SAMPLE_RATE)
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


class _WorkerTTS:
    """One long-lived Kokoro worker, reused across utterances.

    Request: one JSON line ``{"id": n, "text": "..."}``.
    Response: one JSON header ``{"id", "ok": true, "rate", "bytes"}`` followed by
    exactly ``bytes`` of s16le mono PCM, or ``{"id", "ok": false, "error"}``.
    Audio comes back at the worker's rate (24 kHz) and is resampled to 16 kHz.
    Requests are serialized. A timeout kills the child; the next call starts a new one.
    If the child dies mid-request, it is restarted once and the utterance is retried once.
    ``worker_cmd`` replaces the argv so tests can run a fake worker.
    """

    def __init__(self, timeout: float = 30.0, worker_cmd: list[str] | None = None):
        self.timeout = timeout
        self.worker_cmd = list(worker_cmd) if worker_cmd is not None else None
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._next_id = 1

    def _command(self) -> list[str]:
        if self.worker_cmd is not None:
            return list(self.worker_cmd)
        return self._default_command()

    def _child_env(self) -> dict[str, str]:  # also bound onto ParakeetSTT below
        """Environment for the worker. A GPU venv ships its CUDA libraries inside site-packages/nvidia."""
        env = dict(os.environ)
        venv = os.path.dirname(os.path.dirname(os.path.expanduser(getattr(self, "python", "") or "")))
        libs = sorted(glob.glob(os.path.join(venv, "lib*", "python3*", "site-packages", "nvidia", "*", "lib")))
        if libs:
            env["LD_LIBRARY_PATH"] = ":".join(libs + [env.get("LD_LIBRARY_PATH", "")]).rstrip(":")
        return env

    def _default_command(self) -> list[str]:
        raise NotImplementedError

    async def _ensure(self) -> asyncio.subprocess.Process:
        proc = self._proc
        if proc is not None and proc.returncode is None:
            return proc
        if proc is not None:
            await proc.wait()
            self._proc = None
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self._command(),
                env=self._child_env(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                **_spawn_kwargs(),
            )
        except OSError:
            raise TTSError("tts_failed") from None
        return self._proc

    def _kill(self) -> asyncio.subprocess.Process | None:
        """SIGKILL the worker process group. Synchronous so cancellation still lands."""
        proc = self._proc
        self._proc = None
        if proc is None:
            return None
        if proc.returncode is None:
            try:
                _kill_process(proc)
            except (ProcessLookupError, PermissionError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        return proc

    async def _stop(self) -> None:
        proc = self._kill()
        if proc is None:
            return
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except (TimeoutError, ProcessLookupError):
            pass

    async def close(self) -> None:
        """Terminate the worker child, if one is running."""
        async with self._lock:
            await self._stop()

    async def _exchange(
        self,
        proc: asyncio.subprocess.Process,
        req_id: int,
        text: str,
        voice: str | None = None,
    ) -> tuple[bytes, int]:
        if proc.stdin is None or proc.stdout is None:
            raise _WorkerCrashed
        payload: dict[str, Any] = {"id": req_id, "text": text}
        if voice:
            payload["voice"] = voice
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        try:
            proc.stdin.write(line.encode("utf-8"))
            await proc.stdin.drain()
            header_line = await proc.stdout.readline()
            if not header_line:
                raise _WorkerCrashed
            try:
                header = json.loads(header_line)
            except json.JSONDecodeError:
                raise _WorkerCrashed from None
            if not isinstance(header, dict) or header.get("id") != req_id:
                raise _WorkerCrashed
            if not header.get("ok"):
                raise TTSError("tts_failed")
            nbytes = header.get("bytes", 0)
            rate = header.get("rate", SAMPLE_RATE)
            if isinstance(nbytes, bool) or not isinstance(nbytes, int) or nbytes < 0:
                raise _WorkerCrashed
            if isinstance(rate, bool) or not isinstance(rate, int) or rate <= 0:
                raise _WorkerCrashed
            if nbytes == 0:
                return b"", rate
            try:
                pcm = await proc.stdout.readexactly(nbytes)
            except asyncio.IncompleteReadError:
                raise _WorkerCrashed from None
            return pcm, rate
        except TTSError:
            raise
        except _WorkerCrashed:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            raise _WorkerCrashed from None

    async def _request_once(self, text: str, voice: str | None = None) -> bytes:
        proc = await self._ensure()
        req_id = self._next_id
        self._next_id += 1
        try:
            pcm, rate = await asyncio.wait_for(
                self._exchange(proc, req_id, text, voice=voice),
                self.timeout,
            )
        except TimeoutError:
            await self._stop()
            raise TTSError("tts_failed") from None
        except asyncio.CancelledError:
            await self._stop()
            raise
        except _WorkerCrashed:
            await self._stop()
            raise
        return _pcm_to_16k(pcm, rate)

    async def _synthesize_locked(self, text: str, voice: str | None = None) -> bytes:
        for attempt in (0, 1):
            try:
                return await self._request_once(text, voice=voice)
            except _WorkerCrashed:
                await self._stop()
                if attempt == 1:
                    raise TTSError("tts_failed") from None
        raise TTSError("tts_failed")

    async def synthesize(self, text: str, voice: str | None = None) -> bytes:
        async with self._lock:
            try:
                return await self._synthesize_locked(text, voice=voice)
            except asyncio.CancelledError:
                await self._stop()
                raise


class KokoroTTS(_WorkerTTS):
    """One long-lived Kokoro worker, reused across utterances."""

    def __init__(
        self,
        python: str | None = None,
        model_dir: str | None = None,
        voice: str = "af_sarah",
        speed: float = 1.0,
        timeout: float = 30.0,
        worker_cmd: list[str] | None = None,
    ):
        super().__init__(timeout=timeout, worker_cmd=worker_cmd)
        self.python = os.path.expanduser(python or _KOKORO_PYTHON)
        self.model_dir = os.path.expanduser(model_dir or _KOKORO_MODEL_DIR)
        self.voice = voice
        self._default_voice = voice
        self.speed = speed

    def _default_command(self) -> list[str]:
        return [
            self.python,
            "-u",
            _KOKORO_WORKER,
            "--model-dir",
            self.model_dir,
            "--voice",
            self.voice,
            "--speed",
            str(self.speed),
        ]

    async def synthesize(self, text: str, voice: str | None = None) -> bytes:
        effective_voice = voice if voice is not None else (
            self.voice if self.voice != self._default_voice else None
        )
        return await super().synthesize(text, voice=effective_voice)


class PiperTTS(_WorkerTTS):
    """One long-lived Piper worker, reused across utterances."""

    def __init__(
        self,
        python: str | None = None,
        model: str | None = None,
        timeout: float = 30.0,
        worker_cmd: list[str] | None = None,
    ):
        super().__init__(timeout=timeout, worker_cmd=worker_cmd)
        self.python = os.path.expanduser(python or _PIPER_PYTHON)
        self.model = os.path.expanduser(model or _PIPER_MODEL)

    def _default_command(self) -> list[str]:
        return [
            self.python,
            "-u",
            _PIPER_WORKER,
            "--model",
            self.model,
        ]


class FallbackTTS:
    """Try ``primary``, and on ``TTSError`` synthesize with ``secondary`` instead.

    The switch is logged once so a down primary does not spam every utterance.
    """

    def __init__(self, primary, secondary):
        self.primary = primary
        self.secondary = secondary
        self._logged_fallback = False

    async def synthesize(self, text: str, voice: str | None = None) -> bytes:
        try:
            if voice is not None and isinstance(self.primary, (KokoroTTS, FallbackTTS)):
                return await self.primary.synthesize(text, voice=voice)
            return await self.primary.synthesize(text)
        except TTSError:
            if not self._logged_fallback:
                log.warning("primary TTS failed; using fallback")
                self._logged_fallback = True
            if voice is not None and isinstance(self.secondary, (KokoroTTS, FallbackTTS)):
                return await self.secondary.synthesize(text, voice=voice)
            return await self.secondary.synthesize(text)

    async def close(self) -> None:
        primary_close = getattr(self.primary, "close", None)
        if primary_close is not None:
            await primary_close()
        secondary_close = getattr(self.secondary, "close", None)
        if secondary_close is not None:
            await secondary_close()


def _espeak_binary() -> str | None:
    """Prefer espeak-ng, then espeak. None when neither executable is on PATH."""
    return shutil.which("espeak-ng") or shutil.which("espeak")


def _parakeet_files_exist(python: str | None = None, model_dir: str | None = None) -> bool:
    py = os.path.expanduser(python or _PARAKEET_PYTHON)
    mdir = os.path.expanduser(model_dir or _PARAKEET_MODEL_DIR)
    return os.path.isfile(py) and os.path.isdir(mdir)


def build_stt(cfg: dict[str, Any] | str | None) -> STT:
    if isinstance(cfg, str):
        cfg = {"backend": cfg}
    else:
        cfg = dict(cfg or {})
    backend = cfg.pop("backend", "stub")
    auto_chosen = False
    if backend == "auto":
        from .hermes_voice import hermes_available
        backend = ("hermes" if hermes_available() else
                   "parakeet" if _parakeet_files_exist(cfg.get("python"), cfg.get("model_dir"))
                   else "stub")
        auto_chosen = True
    if backend == "stub":
        # A stub STT returns the same text for every utterance ("hello" by default). Running the
        # product on it looks like a working microphone while nothing is being transcribed, which is
        # the definition of a fake feature. Only accept it when it was asked for explicitly.
        if auto_chosen:
            log.error(
                "speech-to-text resolved to the STUB backend: no local model and no provider key, so "
                "every utterance will transcribe as %r. Speech input is NOT working. Set "
                "audio.stt.backend explicitly, or use --stt-text only for tests.",
                cfg.get("text", "hello"),
            )
        return StubSTT(cfg.get("text", "hello"))
    if backend == "command":
        return CommandSTT(cfg["command"], timeout=float(cfg.get("timeout", 120)))
    if backend == "hermes":
        from .hermes_voice import HermesSTT as ProviderSTT
        return ProviderSTT(timeout=float(cfg.get("timeout", 60)))
    if backend == "parakeet":
        return ParakeetSTT(
            python=cfg.get("python"),
            model_dir=cfg.get("model_dir"),
            timeout=float(cfg.get("timeout", 30)),
        )
    raise ValueError(f"unknown stt backend {backend!r} (auto|stub|command|hermes|parakeet)")


def _piper_files_exist(python: str | None = None, model: str | None = None) -> bool:
    py = os.path.expanduser(python or _PIPER_PYTHON)
    mdl = os.path.expanduser(model or _PIPER_MODEL)
    return os.path.isfile(py) and os.path.isfile(mdl)


def _resolve_kokoro_model_dir(model_dir: str | None = None) -> str:
    mdir = os.path.expanduser(model_dir or _KOKORO_MODEL_DIR)
    if (
        model_dir is None
        and _KOKORO_MODEL_DIR == "~/.hermes/profiles/dev/tts/kokoro"
        and os.environ.get("HERMES_HOME")
        and not os.path.isfile(os.path.join(mdir, "kokoro-v1.0.onnx"))
    ):
        alt = os.path.join(os.path.expanduser(os.environ["HERMES_HOME"]), "tts", "kokoro")
        if os.path.isfile(os.path.join(alt, "kokoro-v1.0.onnx")):
            return alt
    return mdir


def _kokoro_files_exist(python: str | None = None, model_dir: str | None = None) -> bool:
    py = os.path.expanduser(python or _KOKORO_PYTHON)
    mdir = _resolve_kokoro_model_dir(model_dir)
    return (
        os.path.isfile(py)
        and os.path.isfile(os.path.join(mdir, "kokoro-v1.0.onnx"))
        and os.path.isfile(os.path.join(mdir, "voices-v1.0.bin"))
    )


def build_tts(cfg: dict[str, Any] | str | None) -> TTS | None:
    if isinstance(cfg, str):
        cfg = {"backend": cfg}
    else:
        cfg = dict(cfg or {})
    backend = cfg.pop("backend", "none")
    if backend in ("none", None, ""):
        return None
    if backend == "silence":
        return SilenceTTS(float(cfg.get("ms_per_char", 10)), int(cfg.get("max_ms", 5000)))
    if backend == "espeak":
        return EspeakTTS(cfg.get("voice", "en-us"), cfg.get("binary"))
    if backend == "command":
        return CommandTTS(cfg["command"], timeout=float(cfg.get("timeout", 60)))
    if backend == "edge":
        return EdgeTTS(voice=cfg.get("voice", "en-US-AriaNeural"), timeout=float(cfg.get("timeout", 30)))
    if backend == "hermes":
        from .hermes_voice import HermesTTS
        return HermesTTS()
    if backend == "auto":
        from .hermes_voice import HermesTTS, hermes_available
        if hermes_available():
            return HermesTTS()
        gpu_py = cfg.get("kokoro_gpu_python") or _KOKORO_GPU_PYTHON
        if cfg.get("prefer", "kokoro") == "kokoro" and _kokoro_files_exist(gpu_py, cfg.get("model_dir")):
            # Default: warm, expressive Kokoro "Heart" on the GPU, with Piper as the fast fallback.
            primary = KokoroTTS(
                python=gpu_py,
                model_dir=_resolve_kokoro_model_dir(cfg.get("model_dir")),
                voice=cfg.get("voice", "af_heart"),
                speed=float(cfg.get("speed", 1.05)),
                timeout=float(cfg.get("timeout", 30)),
            )
            if _piper_files_exist(None, None):
                return FallbackTTS(primary, PiperTTS(timeout=float(cfg.get("timeout", 30))))
            return primary
        if _piper_files_exist(cfg.get("python"), cfg.get("model")):
            backend = "piper"
        elif _kokoro_files_exist(cfg.get("kokoro_python") or cfg.get("python"), cfg.get("model_dir")):
            backend = "kokoro"
        else:
            binary = _espeak_binary()
            if binary:
                return EspeakTTS(cfg.get("voice", "en-us"), binary)
            return None
    if backend == "piper":
        primary = PiperTTS(
            python=cfg.get("python"),
            model=cfg.get("model"),
            timeout=float(cfg.get("timeout", 30)),
        )
        kokoro_py = cfg.get("kokoro_python")
        kokoro_dir = cfg.get("kokoro_model_dir") or cfg.get("model_dir")
        if _kokoro_files_exist(kokoro_py, kokoro_dir):
            secondary = KokoroTTS(
                python=kokoro_py,
                model_dir=_resolve_kokoro_model_dir(kokoro_dir),
                voice=cfg.get("voice", "af_sarah"),
                speed=float(cfg.get("speed", 1.0)),
                timeout=float(cfg.get("timeout", 30)),
            )
            return FallbackTTS(primary, secondary)
        binary = _espeak_binary()
        if binary:
            return FallbackTTS(primary, EspeakTTS(binary=binary))
        return primary
    if backend == "kokoro":
        primary = KokoroTTS(
            python=cfg.get("python"),
            model_dir=_resolve_kokoro_model_dir(cfg.get("model_dir")),
            voice=cfg.get("voice", "af_sarah"),
            speed=float(cfg.get("speed", 1.0)),
            timeout=float(cfg.get("timeout", 30)),
        )
        binary = _espeak_binary()
        if binary:
            return FallbackTTS(primary, EspeakTTS(binary=binary))
        return primary
    raise ValueError(f"unknown tts backend {backend!r} (auto|none|silence|espeak|command|hermes|kokoro|piper)")


ParakeetSTT._child_env = _WorkerTTS._child_env
