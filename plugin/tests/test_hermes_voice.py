import array
import json
import os
import sys
import types
import wave

import pytest

from hail_hermes import audio, hermes_voice


@pytest.fixture
def hermes(monkeypatch):
    def module(name, **values):
        mod = types.ModuleType(name)
        mod.__dict__.update(values)
        monkeypatch.setitem(sys.modules, name, mod)
        return mod

    module("tools", __path__=[])
    stt = module("tools.transcription_tools", transcribe_audio=lambda path: {})
    streaming = module("tools.tts_streaming", resolve_streaming_provider=lambda cfg: None)
    tts = module("tools.tts_tool", _load_tts_config=lambda: {"provider": "edge"},
                 text_to_speech_tool=lambda text, output_path, **kwargs: "{}")
    # Force the stdlib decoder independently of an installed Hermes checkout.
    module("hermes_cli", __path__=[])
    module("hermes_cli.web_routers", __path__=[])
    module("hermes_cli.web_routers.audio")
    return stt, streaming, tts


@pytest.mark.asyncio
async def test_stt_wav_and_cleanup(hermes):
    seen = []

    def transcribe(path):
        seen.append(path)
        with wave.open(path, "rb") as wav:
            assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)
            assert wav.readframes(2) == b"\x01\x00\x02\x00"
        return {"success": True, "transcript": " hello "}

    hermes[0].transcribe_audio = transcribe
    assert await hermes_voice.HermesSTT().transcribe(b"\x01\x00\x02\x00") == "hello"
    assert not os.path.exists(seen[0])
    hermes[0].transcribe_audio = lambda path: {"success": False}
    with pytest.raises(audio.STTError):
        await hermes_voice.HermesSTT().transcribe(b"")


@pytest.mark.asyncio
async def test_streaming_resamples(hermes):
    class Streamer:
        sample_rate = 1

        def stream(self, text):
            assert text == "hello"
            self.sample_rate = 24000
            yield b"\x00\x00" * 120
            yield b"\x01\x00" * 120

    hermes[1].resolve_streaming_provider = lambda cfg: Streamer()
    assert len(await hermes_voice.HermesTTS().synthesize("hello")) == 320


@pytest.mark.asyncio
async def test_file_wav_and_cleanup(hermes):
    paths = []

    def synthesize(text, output_path, **kwargs):
        paths.append(output_path)
        with wave.open(output_path, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\x01\x00" * 10)
        return json.dumps({"success": True, "file_path": output_path})

    hermes[2].text_to_speech_tool = synthesize
    assert await hermes_voice.HermesTTS().synthesize("hello") == b"\x01\x00" * 10
    assert not os.path.exists(paths[0])


@pytest.mark.asyncio
async def test_compressed_file_needs_ffmpeg(hermes):
    def synthesize(text, output_path, **kwargs):
        with open(output_path, "wb") as handle:
            handle.write(b"OggS" + b"\x00" * 20)
        return json.dumps({"success": True, "file_path": output_path})

    hermes[2].text_to_speech_tool = synthesize
    with pytest.raises(audio.TTSError, match="ffmpeg"):
        await hermes_voice.HermesTTS().synthesize("hello")


@pytest.mark.asyncio
async def test_voice_rate_bad_value_refused_and_default_applied(hermes):
    from hail_hermes import settings as settings_module

    for bad in ("5%", "-5", "-100%", "slow", 5):
        with pytest.raises(ValueError):
            settings_module.validate({"voice_rate": bad})
    assert settings_module.validate({"voice_rate": "+10%"})["voice_rate"] == "+10%"
    assert settings_module.DEFAULTS["voice_rate"] == "-5%"

    seen: dict = {}

    def synthesize(text, output_path, **kwargs):
        seen.update(kwargs)
        with wave.open(output_path, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\x01\x00" * 10)
        return json.dumps({"success": True, "file_path": output_path})

    hermes[2].text_to_speech_tool = synthesize
    assert await hermes_voice.HermesTTS().synthesize("hello") == b"\x01\x00" * 10
    assert seen["speed"] == 0.95  # the -5% default, as Hermes' speed multiplier


def test_resampling():
    samples = array.array("h", [0, 1000, 2000])
    if sys.byteorder == "big":
        samples.byteswap()
    output = hermes_voice.resample_s16le(samples.tobytes(), 3, 6)
    result = array.array("h")
    result.frombytes(output)
    if sys.byteorder == "big":
        result.byteswap()
    assert list(result) == [0, 500, 1000, 1500, 2000, 2000]
    assert len(hermes_voice.resample_s16le(samples.tobytes(), 3, 2)) == 4


def test_auto_selection(hermes, monkeypatch):
    assert hermes_voice.hermes_available()
    assert isinstance(audio.build_stt("auto"), hermes_voice.HermesSTT)
    assert isinstance(audio.build_tts("auto"), hermes_voice.HermesTTS)
    monkeypatch.setattr(hermes_voice, "hermes_available", lambda: False)
    monkeypatch.setattr(audio, "_parakeet_files_exist", lambda *args: True)
    monkeypatch.setattr(audio, "_kokoro_files_exist", lambda *args: False)
    monkeypatch.setattr(audio, "_piper_files_exist", lambda *args: False)
    monkeypatch.setattr(audio, "_espeak_binary", lambda: "/usr/bin/espeak")
    assert isinstance(audio.build_stt("auto"), audio.ParakeetSTT)
    assert isinstance(audio.build_tts("auto"), audio.EspeakTTS)


def test_split_sentences_pieces():
    assert hermes_voice.split_sentences("It is warm. Clear skies! Stay safe.") == [
        "It is warm.",
        "Clear skies!",
        "Stay safe.",
    ]
    assert hermes_voice.split_sentences("3.5 degrees") == ["3.5 degrees"]
    assert hermes_voice.split_sentences("Just one sentence.") == ["Just one sentence."]
    assert hermes_voice.split_sentences("   ") == []


def test_split_sentences_keeps_abbreviations_joined_and_caps_at_six():
    assert hermes_voice.split_sentences("Dr. Smith is here. He waved.") == [
        "Dr. Smith is here.",
        "He waved.",
    ]
    assert hermes_voice.split_sentences("Bring things, e.g. milk. Then go.") == [
        "Bring things, e.g. milk.",
        "Then go.",
    ]
    long = " ".join(f"Sentence {i}." for i in range(1, 9))
    pieces = hermes_voice.split_sentences(long)
    assert len(pieces) == 6
    assert pieces[-1] == "Sentence 6. Sentence 7. Sentence 8."


@pytest.mark.asyncio
async def test_sentences_spoken_in_order_with_gap(hermes):
    heard: list[str] = []

    class Streamer:
        sample_rate = 16000

        def stream(self, text):
            heard.append(text)
            yield b"\x01\x00" * 1600  # 100 ms of tone

    hermes[1].resolve_streaming_provider = lambda cfg: Streamer()
    pcm = await hermes_voice.HermesTTS().synthesize("One. Two! Three?")
    assert heard == ["One.", "Two!", "Three?"]
    tone = b"\x01\x00" * 1600
    gap = b"\x00\x00" * (16000 * 120 // 1000)
    assert pcm == tone + gap + tone + gap + tone
