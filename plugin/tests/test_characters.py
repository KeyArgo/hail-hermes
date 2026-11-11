"""Tests for Pocket characters, Kokoro voice blending, per-call TTS voices, and character switching."""

from __future__ import annotations

import asyncio
import io
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from websockets.asyncio.client import connect

from hail_hermes import protocol as P
from hail_hermes.agent import EchoAgent
from hail_hermes.audio import FallbackTTS, KokoroTTS, SilenceTTS
from hail_hermes.hub import HubConfig, PocketHub
from hail_hermes.kokoro_worker import _serve, parse_voice_spec, resolve_voice
from hail_hermes.settings import (
    CHARACTERS,
    DEFAULTS,
    HERMES_VOICE,
    NOUS_VOICE,
    character_info,
    read_settings,
    update_settings,
)
from hail_hermes.store import Store


class FakeKokoro:
    def __init__(self) -> None:
        self.styles = {
            "am_puck": np.array([1.0, 3.0, 5.0], dtype=np.float32),
            "am_adam": np.array([3.0, 5.0, 7.0], dtype=np.float32),
            "af_heart": np.array([10.0, 20.0, 30.0], dtype=np.float32),
        }
        self.style_calls: list[str] = []
        self.create_calls: list[dict[str, Any]] = []

    def get_voice_style(self, name: str) -> np.ndarray:
        self.style_calls.append(name)
        return self.styles[name]

    def create(self, text: str, voice: Any, speed: float = 1.0, lang: str = "en-us"):
        self.create_calls.append(
            {"text": text, "voice": voice, "speed": speed, "lang": lang}
        )
        return np.zeros(240, dtype=np.float32), 24000


def test_parse_and_resolve_voice_blend() -> None:
    kokoro = FakeKokoro()
    parts = parse_voice_spec(HERMES_VOICE)
    assert parts == [("am_puck", 0.5), ("am_adam", 0.5)]

    blended = resolve_voice(kokoro, HERMES_VOICE)
    assert isinstance(blended, np.ndarray)
    assert kokoro.style_calls == ["am_puck", "am_adam"]
    assert np.allclose(blended, np.array([2.0, 4.0, 6.0], dtype=np.float32))

    # Plain voice name passes through without blend decomposition.
    assert resolve_voice(kokoro, "af_heart") == "af_heart"

    # Weighted blend normalizes arbitrary positive weights.
    weighted = resolve_voice(kokoro, "am_puck:1,am_adam:3")
    assert np.allclose(weighted, np.array([2.5, 4.5, 6.5], dtype=np.float32))

    with pytest.raises(ValueError):
        parse_voice_spec("")
    with pytest.raises(ValueError):
        parse_voice_spec("am_puck:0")
    with pytest.raises(ValueError):
        parse_voice_spec("am_puck:-0.5,am_adam:0.5")
    with pytest.raises(ValueError):
        parse_voice_spec("am_puck:bad")


def test_kokoro_worker_serve_blend_and_per_call_voice(monkeypatch: pytest.MonkeyPatch) -> None:
    kokoro = FakeKokoro()
    requests = [
        json.dumps({"id": 1, "text": "Hello from default"}) + "\n",
        json.dumps({"id": 2, "text": "Hello from Nous", "voice": "af_heart"}) + "\n",
        json.dumps({"id": 3, "text": "Hello from blend", "voice": "am_puck:0.5,am_adam:0.5"}) + "\n",
    ]
    fake_stdin = io.TextIOWrapper(io.BytesIO("".join(requests).encode("utf-8")))
    monkeypatch.setattr(sys, "stdin", fake_stdin)
    out = io.BytesIO()

    _serve(out, kokoro, voice=HERMES_VOICE, speed=1.0, lang="en-us")

    assert len(kokoro.create_calls) == 3
    assert isinstance(kokoro.create_calls[0]["voice"], np.ndarray)
    assert np.allclose(kokoro.create_calls[0]["voice"], np.array([2.0, 4.0, 6.0], dtype=np.float32))
    assert kokoro.create_calls[0]["lang"] == "en-us"

    assert kokoro.create_calls[1]["voice"] == "af_heart"
    assert kokoro.create_calls[1]["lang"] == "en-us"

    assert isinstance(kokoro.create_calls[2]["voice"], np.ndarray)
    assert np.allclose(kokoro.create_calls[2]["voice"], np.array([2.0, 4.0, 6.0], dtype=np.float32))


def test_settings_character_and_voice_overrides(tmp_path: Path) -> None:
    cfg = read_settings(tmp_path)
    assert cfg["character"] == DEFAULTS["character"]
    assert cfg["voice_hermes"] == HERMES_VOICE
    assert cfg["voice_nous"] == NOUS_VOICE
    assert CHARACTERS["hermes"]["face"] == "hermes"
    assert CHARACTERS["nous"]["face"] == "nous"

    info_hermes = character_info({"character": "hermes"}, state_dir=tmp_path)
    assert info_hermes == {"id": "hermes", "face": "hermes", "voice": HERMES_VOICE}

    update_settings(
        tmp_path,
        {
            "character": "nous",
            "voice_nous": "am_puck:0.25,am_adam:0.75",
            "voice_hermes": "am_adam",
        },
    )
    info_nous = character_info(state_dir=tmp_path)
    assert info_nous == {"id": "nous", "face": "nous", "voice": "am_puck:0.25,am_adam:0.75"}

    update_settings(tmp_path, {"character": "hermes"})
    assert character_info(state_dir=tmp_path)["voice"] == "am_adam"

    with pytest.raises(ValueError):
        update_settings(tmp_path, {"character": "unknown"})
    with pytest.raises(ValueError):
        update_settings(tmp_path, {"voice_hermes": "am_puck:0"})


@pytest.mark.asyncio
async def test_kokoro_tts_per_call_voice_without_restart(tmp_path: Path) -> None:
    log_file = tmp_path / "worker_requests.jsonl"
    worker_script = (
        "import json, os, sys\n"
        f"log_path = {str(log_file)!r}\n"
        "for raw in sys.stdin.buffer:\n"
        "    req = json.loads(raw)\n"
        "    req['pid'] = os.getpid()\n"
        "    with open(log_path, 'a', encoding='utf-8') as fh:\n"
        "        fh.write(json.dumps(req) + '\\n')\n"
        "    pcm = b'\\x00\\x00' * 240\n"
        "    hdr = json.dumps({'id': req['id'], 'ok': True, 'rate': 24000, 'bytes': len(pcm)}) + '\\n'\n"
        "    sys.stdout.buffer.write(hdr.encode('utf-8') + pcm)\n"
        "    sys.stdout.buffer.flush()\n"
    )
    tts = KokoroTTS(worker_cmd=[sys.executable, "-u", "-c", worker_script])
    try:
        await tts.synthesize("default call")
        await tts.synthesize("hermes call", voice=HERMES_VOICE)
        await tts.synthesize("nous call", voice=NOUS_VOICE)
    finally:
        await tts.close()

    rows = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 3
    assert "voice" not in rows[0]
    assert rows[1]["voice"] == HERMES_VOICE
    assert rows[2]["voice"] == NOUS_VOICE
    assert rows[0]["pid"] == rows[1]["pid"] == rows[2]["pid"]


async def _recv_until(ws, predicate, timeout: float = 5.0) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        assert remaining > 0, "timed out waiting for expected message"
        raw = await asyncio.wait_for(ws.recv(), remaining)
        if isinstance(raw, bytes):
            continue
        msg = json.loads(raw)
        if predicate(msg):
            return msg


@pytest.mark.asyncio
async def test_hub_character_welcome_set_character_and_voice_commands(tmp_path: Path) -> None:
    log_file = tmp_path / "hub_worker.jsonl"
    worker_script = (
        "import json, os, sys\n"
        f"log_path = {str(log_file)!r}\n"
        "for raw in sys.stdin.buffer:\n"
        "    req = json.loads(raw)\n"
        "    req['pid'] = os.getpid()\n"
        "    with open(log_path, 'a', encoding='utf-8') as fh:\n"
        "        fh.write(json.dumps(req) + '\\n')\n"
        "    pcm = b'\\x00\\x00' * 160\n"
        "    hdr = json.dumps({'id': req['id'], 'ok': True, 'rate': 16000, 'bytes': len(pcm)}) + '\\n'\n"
        "    sys.stdout.buffer.write(hdr.encode('utf-8') + pcm)\n"
        "    sys.stdout.buffer.flush()\n"
    )
    store = Store(tmp_path)
    token = store.issue_token("dev-1", "Pocket", "esp32")
    tts = FallbackTTS(
        KokoroTTS(worker_cmd=[sys.executable, "-u", "-c", worker_script]),
        SilenceTTS(),
    )
    cfg = HubConfig(host="127.0.0.1", port=0, audio_pacing=False, stream_speech=False)
    try:
        async with PocketHub(agent=EchoAgent(prefix="Echo: "), tts=tts, store=store, config=cfg) as hub:
            async with connect(hub.url) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "type": "hello",
                            "v": P.PROTOCOL_VERSION,
                            "device_id": "dev-1",
                            "name": "Pocket",
                            "kind": "wear",
                            "caps": {},
                            "token": token,
                            "modes": True,
                        }
                    )
                )
                welcome = json.loads(await ws.recv())
                assert welcome["type"] == "welcome"
                mode_msg = json.loads(await ws.recv())
                assert mode_msg["type"] == "mode"
                slots_msg = json.loads(await ws.recv())
                assert slots_msg["type"] == "slots"
                char_msg = json.loads(await ws.recv())
                assert char_msg["type"] == "character"
                assert char_msg["id"] == DEFAULTS["character"]

                # Voice command switches character to Nous and applies to the next reply without restarting.
                await ws.send(json.dumps({"v": 2, "type": "text", "text": "Switch to Nous girl!"}))
                switched_char = await _recv_until(ws, lambda m: m.get("type") == "character")
                assert switched_char["id"] == "nous"
                assert read_settings(tmp_path)["character"] == "nous"
                await _recv_until(ws, lambda m: m.get("type") == "state" and m.get("screen") == "idle")

                # Next reply uses Nous voice ('af_heart').
                await ws.send(json.dumps({"v": 2, "type": "text", "text": "Hello there"}))
                await _recv_until(ws, lambda m: m.get("type") == "state" and m.get("screen") == "idle")

                # Voice command switches back to Hermes.
                await ws.send(json.dumps({"v": 2, "type": "text", "text": "switch to hermes"}))
                back_char = await _recv_until(ws, lambda m: m.get("type") == "character")
                assert back_char["id"] == "hermes"
                assert read_settings(tmp_path)["character"] == "hermes"
                await _recv_until(ws, lambda m: m.get("type") == "state" and m.get("screen") == "idle")

                # Explicit set_character protocol message also switches character.
                await ws.send(json.dumps({"v": 2, "type": "set_character", "id": "nous"}))
                direct_char = await _recv_until(ws, lambda m: m.get("type") == "character")
                assert direct_char["id"] == "nous"
    finally:
        await tts.close()

    rows = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    assert len(rows) >= 3
    assert rows[0]["voice"] == NOUS_VOICE
    assert rows[1]["voice"] == NOUS_VOICE
    assert rows[2]["voice"] == HERMES_VOICE
    assert len({r["pid"] for r in rows}) == 1
