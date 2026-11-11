"""Persistent Piper TTS worker.

Loads the Piper ONNX voice once, then serves utterances on stdin so the hub
does not pay a cold start on every turn. Wire format:

  request:  one JSON line ``{"id": n, "text": "..."}``
  response: one JSON line ``{"id": n, "ok": true, "rate": 22050, "bytes": N}``
            followed by exactly N bytes of s16le mono PCM
            or ``{"id": n, "ok": false, "error": "..."}``

Runs under ``~/.local/share/pocket-piper-venv/bin/python``.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import wave


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


def _write_json(out, header: dict) -> None:
    payload = json.dumps(header, ensure_ascii=False) + "\n"
    out.write(payload.encode("utf-8"))


def _synthesize_pcm(voice, text: str, default_rate: int = 22050) -> tuple[bytes, int]:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wave_writer:
        wave_writer.setnchannels(1)
        wave_writer.setsampwidth(2)
        wave_writer.setframerate(default_rate)
        voice.synthesize_wav(text, wave_writer)
    with wave.open(io.BytesIO(buf.getvalue()), "rb") as wave_reader:
        rate = int(wave_reader.getframerate())
        pcm = wave_reader.readframes(wave_reader.getnframes())
    return pcm, rate


def _serve(out, voice) -> None:
    default_rate = int(getattr(getattr(voice, "config", None), "sample_rate", 22050) or 22050)
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
            else:
                text = ""
            if not isinstance(text, str):
                text = ""
            if text.strip() == "":
                _write_json(out, {"id": req_id, "ok": True, "rate": default_rate, "bytes": 0})
                continue
            pcm, rate = _synthesize_pcm(voice, text, default_rate)
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
    parser = argparse.ArgumentParser(description="Persistent Piper TTS worker")
    parser.add_argument("--model", required=True)
    args = parser.parse_args()

    protocol_out = _protocol_stdout()
    os.environ["ONNX_PROVIDER"] = "CPUExecutionProvider"

    model_path = os.path.expanduser(args.model)
    if not os.path.isfile(model_path):
        print("piper model file is missing", file=sys.stderr)
        sys.exit(1)

    from piper import PiperVoice

    voice = PiperVoice.load(model_path)
    try:
        _serve(protocol_out, voice)
    finally:
        protocol_out.close()


if __name__ == "__main__":
    main()
