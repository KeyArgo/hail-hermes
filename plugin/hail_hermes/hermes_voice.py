"""Hermes voice providers, loaded only when selected."""

from __future__ import annotations

import asyncio
import array
import json
import os
import re
import sys
import tempfile
from typing import Any
import wave

from .audio import HermesSTT as _LegacyHermesSTT, STTError, TTSError, pcm_to_wav, wav_to_pcm16k
from .protocol import SAMPLE_RATE


def hermes_available() -> bool:
    try:
        from tools.transcription_tools import transcribe_audio
        from tools.tts_streaming import resolve_streaming_provider
        from tools.tts_tool import text_to_speech_tool, _load_tts_config
        return all(callable(fn) for fn in (
            transcribe_audio, resolve_streaming_provider, text_to_speech_tool, _load_tts_config
        ))
    except ImportError:
        return False


def resample_s16le(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    if src_rate <= 0 or dst_rate <= 0 or len(pcm) % 2:
        raise ValueError("invalid PCM or sample rate")
    if src_rate == dst_rate:
        return pcm
    samples = array.array("h")
    samples.frombytes(pcm)
    if sys.byteorder == "big":
        samples.byteswap()
    count = len(samples) * dst_rate // src_rate
    out = array.array("h")
    for i in range(count):
        pos = i * src_rate / dst_rate
        left = min(int(pos), len(samples) - 1)
        right = min(left + 1, len(samples) - 1)
        out.append(int(samples[left] + (samples[right] - samples[left]) * (pos - left)))
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


class HermesSTT(_LegacyHermesSTT):
    async def transcribe(self, pcm: bytes) -> str:
        def run() -> str:
            from tools.transcription_tools import transcribe_audio
            with tempfile.TemporaryDirectory(prefix="pocket-stt-") as directory:
                path = os.path.join(directory, "input.wav")
                with open(path, "wb") as handle:
                    handle.write(pcm_to_wav(pcm))
                result = transcribe_audio(path)
            if not isinstance(result, dict) or not result.get("success"):
                raise STTError()
            text = result.get("transcript")
            if text is None:
                return ""
            if not isinstance(text, str):
                raise STTError()
            return text.strip()

        try:
            return await asyncio.to_thread(run)
        except Exception:
            raise STTError() from None


def _decode(path: str) -> tuple[bytes, int]:
    try:
        from hermes_cli.web_routers.audio import _audio_file_to_pcm
        pcm, rate = _audio_file_to_pcm(path)
        if pcm and rate > 0:
            return pcm, rate
    except Exception:
        pass
    try:
        with open(path, "rb") as handle:
            return wav_to_pcm16k(handle.read()), SAMPLE_RATE
    except (OSError, EOFError, ValueError, wave.Error):
        with open(path, "rb") as handle:
            head = handle.read(12)
        if head.startswith((b"ID3", b"OggS")) or (
            len(head) >= 2 and head[0] == 0xff and head[1] & 0xe0 == 0xe0
        ):
            raise TTSError("ffmpeg_needed") from None
        raise


_EDGE_BY_KOKORO = {
    # Hermes' own speech backend (edge) cannot use Kokoro voice names, so map the
    # character's Kokoro voice onto an equivalent edge voice. Without this the
    # character's voice is ignored and everyone gets the system default.
    "am_puck": "en-US-GuyNeural",
    "am_adam": "en-US-GuyNeural",
    "am_michael": "en-US-GuyNeural",
    "af_heart": "en-US-AriaNeural",
    "af_sarah": "en-US-AriaNeural",
    "af_bella": "en-US-AriaNeural",
    "af_nicole": "en-US-AriaNeural",
    "af_sky": "en-US-AriaNeural",
}


def _edge_voice(spec: str | None) -> str | None:
    """Pick an edge voice id out of a character's voice spec.

    Accepts either an edge id already ("en-US-GuyNeural") or a Kokoro spec
    ("am_puck:0.5,am_adam:0.5"), which maps onto the closest edge voice.
    """
    for part in (spec or "").split(","):
        name = part.split(":")[0].strip()
        if not name:
            continue
        mapped = _EDGE_BY_KOKORO.get(name.lower())
        if mapped:
            return mapped
        if name.lower().endswith("neural"):
            return name
    return None


def _edge_speed(rate: str) -> float:
    """Turn edge-tts' rate ("-5%") into the speed multiplier Hermes' TTS tool takes (0.95)."""
    return 1.0 + int(rate.rstrip("%")) / 100.0


def speakable(text: str) -> str:
    """Strip markdown and spell out abbreviations for the speech engine."""
    if not isinstance(text, str):
        text = str(text)
    for mark in ("**", "__", "`"):
        text = text.replace(mark, "")
    lines = []
    for line in text.splitlines():
        line = line.lstrip()
        if line.startswith("#"):
            line = line.lstrip("#").lstrip()
        elif line.startswith(">"):
            line = line.lstrip(">").lstrip()
        lines.append(line.replace("|", " "))
    text = "\n".join(lines)
    for short, spoken in (
        ("e.g.", "for example"),
        ("i.e.", "that is"),
        ("etc.", "and so on"),
        ("vs.", "versus"),
        ("&", "and"),
    ):
        text = re.sub(re.escape(short), spoken, text, flags=re.IGNORECASE)
    return " ".join(text.split())


_ABBREVIATIONS = {
    "dr", "mr", "mrs", "ms", "prof", "sr", "jr", "st", "vs", "etc", "approx",
    "dept", "est", "fig", "inc", "ltd", "no", "gov", "hon", "rev", "gen",
    "sgt", "capt", "col", "maj", "mt", "ave", "blvd", "rd",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct",
    "nov", "dec", "mon", "tue", "tues", "wed", "thu", "thur", "thurs", "fri",
    "sat", "sun",
}


def _ends_with_abbreviation(piece: str) -> bool:
    """True when a piece ends mid-abbreviation ("Dr.", "e.g.", "U.S."), so it stays joined."""
    words = piece.split()
    if not words:
        return False
    word = words[-1]
    if not word.endswith("."):
        return False
    stem = word.rstrip(".").lower()
    if not stem:
        return False
    if stem in _ABBREVIATIONS:
        return True
    # Dotted initials like "e.g", "i.e", "U.S" or "J" — every segment a single letter.
    return all(len(part) == 1 and part.isalpha() for part in stem.split("."))


def split_sentences(text: str) -> list[str]:
    """Break a reply into sentences so long answers are spoken piece by piece.

    Splits on ``.``, ``!``, ``?`` followed by a space or the end, keeps common
    abbreviations and numbers inside their sentence ("Dr.", "e.g.", "3.5"),
    drops empty pieces, and never returns more than 6 pieces (the remainder
    is joined into the last one).
    """
    if not isinstance(text, str):
        text = str(text)
    pieces: list[str] = []
    for raw in re.split(r"(?<=[.!?])\s+", text.strip()):
        piece = raw.strip()
        if not piece:
            continue
        if pieces and _ends_with_abbreviation(pieces[-1]):
            pieces[-1] = f"{pieces[-1]} {piece}"
        else:
            pieces.append(piece)
    if len(pieces) > 6:
        pieces = pieces[:5] + [" ".join(pieces[5:])]
    return pieces


# Silent gap between spoken sentences (~120 ms of s16le mono at SAMPLE_RATE).
_SENTENCE_GAP_BYTES = b"\x00" * (2 * SAMPLE_RATE * 120 // 1000)


class HermesTTS:
    async def synthesize(self, text: str, voice: str | None = None) -> bytes:
        pieces = split_sentences(speakable(text))
        voice_id = _edge_voice(voice)

        def run() -> bytes:
            from .settings import read_settings
            from tools.tts_streaming import resolve_streaming_provider
            from tools.tts_tool import _load_tts_config, text_to_speech_tool

            config = _load_tts_config()
            streamer = resolve_streaming_provider(config)
            # Read once per utterance so an owner change takes effect on the next reply.
            rate = str(read_settings().get("voice_rate") or "-5%")
            provider = str((config or {}).get("provider") or "edge").strip().lower()

            def render(piece: str) -> bytes:
                if streamer is not None:
                    pcm = b"".join(streamer.stream(piece))
                    return resample_s16le(pcm, streamer.sample_rate, SAMPLE_RATE)

                with tempfile.TemporaryDirectory(prefix="pocket-tts-") as directory:
                    path = os.path.join(directory, "output.wav")
                    kwargs: dict[str, Any] = {"output_path": path}
                    if voice_id:
                        kwargs["voice_id"] = voice_id
                    if provider == "edge":
                        kwargs["speed"] = _edge_speed(rate)
                    result = json.loads(text_to_speech_tool(piece, **kwargs))
                    if not isinstance(result, dict) or not result.get("success"):
                        raise TTSError()
                    paths = result.get("file_paths") or [result.get("file_path")]
                    if not isinstance(paths, list) or not paths or not all(
                        isinstance(p, str) and p for p in paths
                    ):
                        raise TTSError()
                    return b"".join(
                        resample_s16le(pcm, rate, SAMPLE_RATE)
                        for pcm, rate in (_decode(p) for p in paths)
                    )

            # Sentences are spoken in order with a short silent gap between them.
            out: list[bytes] = []
            for i, piece in enumerate(pieces or [""]):
                if i:
                    out.append(_SENTENCE_GAP_BYTES)
                out.append(render(piece))
            return b"".join(out)

        try:
            return await asyncio.to_thread(run)
        except TTSError:
            raise
        except Exception:
            raise TTSError() from None
