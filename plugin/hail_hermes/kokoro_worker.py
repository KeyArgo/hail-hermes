"""Persistent Kokoro TTS worker.

Loads the ONNX model once, then serves utterances on stdin so the hub does not
pay a cold start on every turn. Wire format:

  request:  one JSON line ``{"id": n, "text": "..."}``
  response: one JSON line ``{"id": n, "ok": true, "rate": 24000, "bytes": N}``
            followed by exactly N bytes of s16le mono PCM
            or ``{"id": n, "ok": false, "error": "..."}``

Runs under ``~/.hermes/voice-venv/bin/python``. Imports are stdlib, numpy, and
kokoro_onnx only. CPU execution provider only — this process must not touch the GPU.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys

try:
    import numpy as np
except ImportError:  # the hub only imports parse_voice_spec; the worker runs where numpy exists
    np = None


def _protocol_stdout():
    """Point fd 1 at stderr so library noise cannot corrupt PCM frames.

    The original stdout is kept on a duplicated fd and used only for the
    length-prefixed protocol the parent reads.
    """
    sys.stdout.flush()
    saved = os.dup(sys.stdout.fileno())
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    return os.fdopen(saved, "wb", buffering=0)


def _write_all(out, data: bytes) -> None:
    """The protocol stream is unbuffered, so one write can send less than asked (long replies exceed the pipe buffer)."""
    view = memoryview(data)
    while view:
        sent = out.write(view)
        view = view[sent or 0:]


def _float_to_s16le(samples) -> bytes:
    audio = np.asarray(samples, dtype=np.float32)
    audio = np.clip(audio, -1.0, 1.0)
    pcm = np.rint(audio * 32767.0)
    pcm = pcm.astype(np.int16)
    return pcm.tobytes()


def _write_json(out, header: dict) -> None:
    payload = json.dumps(header, ensure_ascii=False) + "\n"
    out.write(payload.encode("utf-8"))


def parse_voice_spec(spec: str) -> list[tuple[str, float]]:
    """Parse a voice name or blend spec like ``'am_puck:0.5,am_adam:0.5'``."""
    if not isinstance(spec, str) or not spec.strip():
        raise ValueError("voice spec must be a non-empty string")
    parts: list[tuple[str, float]] = []
    for raw_item in spec.split(","):
        item = raw_item.strip()
        if not item:
            raise ValueError(f"invalid voice spec: {spec!r}")
        if ":" in item:
            name, raw_weight = item.split(":", 1)
            name = name.strip()
            try:
                weight = float(raw_weight.strip())
            except ValueError as exc:
                raise ValueError(f"invalid voice weight in {spec!r}") from exc
        else:
            name = item
            weight = 1.0
        if not name or not math.isfinite(weight) or weight <= 0:
            raise ValueError(f"invalid voice spec component in {spec!r}")
        parts.append((name, weight))
    return parts


def resolve_voice(kokoro, spec: str | np.ndarray):
    """Resolve a single voice name or weighted blend spec via ``kokoro.get_voice_style``."""
    if isinstance(spec, np.ndarray):
        return spec
    if not isinstance(spec, str) or not spec.strip():
        raise ValueError("voice must be a non-empty string")
    cleaned = spec.strip()
    if "," not in cleaned and ":" not in cleaned:
        return cleaned
    parts = parse_voice_spec(cleaned)
    total = sum(weight for _, weight in parts)
    blended: np.ndarray | None = None
    for name, weight in parts:
        style = np.asarray(kokoro.get_voice_style(name), dtype=np.float32)
        scaled = style * np.float32(weight / total)
        blended = scaled if blended is None else (blended + scaled)
    assert blended is not None
    return blended.astype(np.float32, copy=False)


def _serve(out, kokoro, voice: str, speed: float, lang: str = "en-us") -> None:
    for raw in sys.stdin.buffer:
        line = raw.strip()
        if not line:
            continue
        req_id = None
        try:
            request = json.loads(line)
            if isinstance(request, dict):
                req_id = request.get("id")
                text = request.get("text", "")
                req_voice = request.get("voice")
            else:
                text = ""
                req_voice = None
            if not isinstance(text, str):
                text = ""
            if text.strip() == "":
                _write_json(out, {"id": req_id, "ok": True, "rate": 24000, "bytes": 0})
                continue
            active_voice = voice if req_voice is None else req_voice
            samples, rate = kokoro.create(
                text,
                voice=resolve_voice(kokoro, active_voice),
                speed=speed,
                lang=lang,
            )
            pcm = _float_to_s16le(samples)
            _write_json(out, {
                "id": req_id,
                "ok": True,
                "rate": int(rate),
                "bytes": len(pcm),
            })
            if pcm:
                _write_all(out, pcm)
        except BrokenPipeError:
            return
        except Exception:
            try:
                _write_json(out, {
                    "id": req_id,
                    "ok": False,
                    "error": "synthesis failed",
                })
            except BrokenPipeError:
                return


def main() -> None:
    parser = argparse.ArgumentParser(description="Persistent Kokoro TTS worker")
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--voice", default="af_sarah")
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--lang", default="en-us")
    args = parser.parse_args()

    protocol_out = _protocol_stdout()
    # Use the GPU when this Python has a working CUDA onnxruntime (about 30x faster); otherwise CPU.
    want_gpu = os.environ.get("POCKET_TTS_DEVICE", "auto") != "cpu"

    model_path = os.path.join(args.model_dir, "kokoro-v1.0.onnx")
    voices_path = os.path.join(args.model_dir, "voices-v1.0.bin")
    if not os.path.isfile(model_path) or not os.path.isfile(voices_path):
        print("kokoro model files are missing", file=sys.stderr)
        sys.exit(1)

    from kokoro_onnx import Kokoro

    try:
        import onnxruntime as ort

        sess_options = ort.SessionOptions()
        sess_options.intra_op_num_threads = int(
            os.environ.get("POCKET_TTS_THREADS") or os.cpu_count() or 4
        )
        providers = ["CPUExecutionProvider"]
        if want_gpu and "CUDAExecutionProvider" in ort.get_available_providers():
            providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        session = ort.InferenceSession(model_path, sess_options=sess_options, providers=providers)
        print(f"kokoro providers: {session.get_providers()}", file=sys.stderr)
        kokoro = Kokoro.from_session(session, voices_path)
    except Exception:
        kokoro = Kokoro(model_path, voices_path)
    try:
        _serve(protocol_out, kokoro, args.voice, args.speed, args.lang)
    finally:
        protocol_out.close()


if __name__ == "__main__":
    main()
