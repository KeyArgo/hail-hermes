"""Persistent Parakeet STT worker.

Each request is a JSON line containing id, bytes and rate, followed by exactly
that many bytes of s16le mono PCM. Each response is one JSON line containing
id, ok and text.
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def _protocol_stdout():
    """Keep library output off the protocol stream."""
    sys.stdout.flush()
    saved = os.dup(sys.stdout.fileno())
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    return os.fdopen(saved, "wb", buffering=0)


def _write_json(out, response: dict) -> None:
    data = (json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8")
    view = memoryview(data)
    while view:
        written = out.write(view)
        if not written:
            raise BrokenPipeError
        view = view[written:]


def _read_exact(stream, count: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < count:
        chunk = stream.read(count - len(chunks))
        if not chunk:
            raise EOFError
        chunks.extend(chunk)
    return bytes(chunks)


def _serve(out, recognizer) -> None:
    import numpy as np

    source = sys.stdin.buffer
    while True:
        line = source.readline()
        if not line:
            return
        request_id = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                return
            request_id = request.get("id")
            count = request.get("bytes")
            rate = request.get("rate")
            # A malformed length leaves the stream impossible to resynchronize.
            if (
                isinstance(count, bool)
                or not isinstance(count, int)
                or count < 0
                or count % 2
                or isinstance(rate, bool)
                or not isinstance(rate, int)
                or rate <= 0
            ):
                return
            pcm = _read_exact(source, count)
            samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
            stream = recognizer.create_stream()
            stream.accept_waveform(rate, samples)
            recognizer.decode_stream(stream)
            _write_json(out, {"id": request_id, "ok": True, "text": stream.result.text})
        except (BrokenPipeError, EOFError):
            return
        except Exception:
            try:
                _write_json(out, {"id": request_id, "ok": False, "text": ""})
            except BrokenPipeError:
                return


def _default_threads(state_dir: str | None = None) -> int:
    try:
        from .settings import read_settings

        val = read_settings(state_dir).get("stt_threads")
        if isinstance(val, int) and not isinstance(val, bool) and val >= 1:
            return val
    except Exception:
        pass
    return os.cpu_count() or 4


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Persistent Parakeet STT worker")
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--threads", type=int, default=_default_threads())
    args = parser.parse_args(argv)

    protocol_out = _protocol_stdout()
    import sherpa_onnx

    d = os.path.expanduser(args.model_dir)
    recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=f"{d}/encoder.int8.onnx",
        decoder=f"{d}/decoder.int8.onnx",
        joiner=f"{d}/joiner.int8.onnx",
        tokens=f"{d}/tokens.txt",
        num_threads=max(1, int(args.threads)),
        provider="cpu",
        decoding_method="greedy_search",
        model_type="nemo_transducer",
    )
    try:
        _serve(protocol_out, recognizer)
    finally:
        protocol_out.close()


if __name__ == "__main__":
    main()
