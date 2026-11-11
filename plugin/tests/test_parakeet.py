"""Warm Parakeet speech-to-text worker (fake worker, no model needed)."""

from __future__ import annotations

import sys
import pytest
from hail_hermes.audio import ParakeetSTT, StubSTT, build_stt
from hail_hermes import hermes_voice


_FAKE_PARAKEET_WORKER = """
import json, os, sys

mode = sys.argv[1]
pid = os.getpid()
while True:
    line = sys.stdin.buffer.readline()
    if not line:
        break
    req = json.loads(line)
    pcm = sys.stdin.buffer.read(req["bytes"])
    if mode == "crash_first":
        flag = sys.argv[2]
        if not os.path.exists(flag):
            open(flag, "w").close()
            sys.exit(1)
    resp = {"id": req["id"], "ok": True, "text": f"pid={pid} bytes={len(pcm)}"}
    sys.stdout.write(json.dumps(resp) + "\\n")
    sys.stdout.flush()
"""


async def test_parakeet_reuses_one_child_across_two_calls():
    stt = ParakeetSTT(worker_cmd=[sys.executable, "-u", "-c", _FAKE_PARAKEET_WORKER, "ok"])
    try:
        first = await stt.transcribe(b"\x00\x00" * 160)
        second = await stt.transcribe(b"\x00\x00" * 320)
        assert first.split()[0] == second.split()[0]
        assert first.endswith("bytes=320")
        assert second.endswith("bytes=640")
    finally:
        await stt.close()


async def test_parakeet_restarts_after_crash(tmp_path):
    flag = str(tmp_path / "crashed")
    stt = ParakeetSTT(worker_cmd=[sys.executable, "-u", "-c", _FAKE_PARAKEET_WORKER, "crash_first", flag])
    try:
        out = await stt.transcribe(b"\x00\x00" * 100)
        assert out.endswith("bytes=200")
    finally:
        await stt.close()


def test_build_stt_auto_selection(tmp_path, monkeypatch):
    py = tmp_path / "python"
    model_dir = tmp_path / "parakeet-tdt-0.6b-v3-int8"

    py.write_text("#!/bin/sh\n")
    model_dir.mkdir()
    monkeypatch.setattr(hermes_voice, "hermes_available", lambda: True)
    assert isinstance(build_stt({"backend": "auto", "python": str(py), "model_dir": str(model_dir)}),
                      hermes_voice.HermesSTT)
    monkeypatch.setattr(hermes_voice, "hermes_available", lambda: False)
    stt_parakeet = build_stt({"backend": "auto", "python": str(py), "model_dir": str(model_dir)})
    assert isinstance(stt_parakeet, ParakeetSTT)
    py.unlink()
    assert isinstance(build_stt({"backend": "auto", "python": str(py), "model_dir": str(model_dir)}), StubSTT)
