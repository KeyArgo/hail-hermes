"""Wire constants and message builders for Hail Hermes Protocol v2."""

from __future__ import annotations

import json
from typing import Any

PROTOCOL_VERSION = 2
DEFAULT_PORT = 8767
DEFAULT_PATH = "/pocket"
CONTROL_PATH = "/control"

SAMPLE_RATE = 16000
CODEC = "pcm16le"
FRAME_BYTES = 640  # 20 ms of s16le mono @ 16 kHz
FRAME_SECONDS = FRAME_BYTES / (SAMPLE_RATE * 2)

SCREENS = ("idle", "listening", "thinking", "speaking")
KINDS = ("phone", "wear", "desktop", "sim")
CAPS = ("push", "haptic", "led", "battery")

# Close codes (application range 4000-4999).
CLOSE_VERSION = 4000
CLOSE_REPLACED = 4001
CLOSE_IDLE = 4002
CLOSE_PAIRING = 4003
CLOSE_BAD_HELLO = 4004
CLOSE_REVOKED = 4005

ERROR_MESSAGES = {
    "version": "Protocol version mismatch. Expected v2.",
    "bad_frame": "Audio frame length must be even.",
    "too_long": "Utterance exceeds maximum length limit.",
    "too_short": "Utterance is too short.",
    "not_paired": "Device is not paired or pairing is pending.",
    "pair_timeout": "Pairing request expired.",
    "busy": "Maximum pending pairing requests reached.",
    "denied": "Pairing request was denied by the owner.",
    "revoked": "Device token has been revoked.",
    "bad_session": "That session can't be opened.",
    "stt_failed": "Speech-to-text processing failed.",
    "agent_failed": "Agent execution encountered an error.",
    "tts_failed": "Text-to-speech synthesis failed.",
    "internal": "Internal server error.",
}


class ProtocolError(Exception):
    """A client message violated the protocol."""

    def __init__(self, code: str = "internal"):
        self.code = code
        super().__init__(code)


def msg(type_: str, **fields: Any) -> dict[str, Any]:
    return {"type": type_, "v": PROTOCOL_VERSION, **fields}


def encode(message: dict[str, Any]) -> str:
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False)


def decode(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        raise ProtocolError("internal") from None
    if not isinstance(data, dict):
        raise ProtocolError("internal")
    if type(data.get("v")) is not int or data["v"] != PROTOCOL_VERSION:
        raise ProtocolError("version")
    if not isinstance(data.get("type"), str):
        raise ProtocolError("internal")
    return data


def error(code: str) -> dict[str, Any]:
    if code not in ERROR_MESSAGES:
        code = "internal"
    return msg("error", code=code, message=ERROR_MESSAGES[code])


def state(screen: str, busy: bool = False) -> dict[str, Any]:
    assert screen in SCREENS, screen
    return msg("state", screen=screen, **({"busy": True} if busy else {}))


def transcript(role: str, text: str, final: bool) -> dict[str, Any]:
    return msg("transcript", role=role, text=text, final=final)


def audio_start() -> dict[str, Any]:
    return msg("audio_start", codec=CODEC, rate=SAMPLE_RATE)


def audio_end() -> dict[str, Any]:
    return msg("audio_end")


def frames(pcm: bytes, size: int = FRAME_BYTES) -> list[bytes]:
    """Split PCM into protocol-sized frames; the last frame is zero-padded to a whole frame."""
    out = [pcm[i:i + size] for i in range(0, len(pcm), size)]
    if out and len(out[-1]) < size:
        last = out[-1][:len(out[-1]) // 2 * 2]
        out[-1] = last + b"\x00" * (size - len(last))
    return out


def normalize_caps(raw: Any) -> dict[str, bool]:
    """``hello.caps`` → ``{cap: bool}`` for the known caps (unknown keys dropped)."""
    caps = raw if isinstance(raw, dict) else {}
    return {cap: bool(caps.get(cap)) for cap in CAPS}


def validate_hello(data: dict[str, Any]) -> None:
    if type(data.get("v")) is not int or data["v"] != PROTOCOL_VERSION:
        raise ProtocolError("version")
    device_id = data.get("device_id")
    if not isinstance(device_id, str) or not device_id.strip() or len(device_id) > 128:
        raise ProtocolError("internal")
    kind = data.get("kind")
    if kind not in KINDS:
        raise ProtocolError("internal")
    if "name" in data and (
        not isinstance(data["name"], str) or len(data["name"]) > 64
    ):
        raise ProtocolError("internal")
    token = data.get("token")
    if token is not None and (not isinstance(token, str) or len(token) > 512):
        raise ProtocolError("internal")
