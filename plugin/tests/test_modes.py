"""Tests for Quick ask, session switching, sessions table parsing, and hand-offs."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from conftest import Client, EchoTTS, FakeAgent, RecordingSTT, is_type, make_script, running_hub
from hail_hermes.agent import HermesCliAgent, parse_sessions_table
from hail_hermes.cli import build_hub
from hail_hermes.quick import HANDOFF_MARKER, QuickAgent
from hail_hermes.settings import DEFAULTS, read_settings, update_settings

SAMPLE_SESSIONS_TABLE = """\
Title                     Workspace     Last Active   ID
──────────────────────────────────────────────────────────────────────
Refactor auth module      /work/app     5m ago        20260419_100000_a1
Weekly grocery list       /home/alex     2d ago        20260417_093000_b2
Morning standup notes     ~/notes       yesterday     sess_standup_03
… 4 more not shown
"""


class FakeSSEServer:
    """Local OpenAI-compatible ``/v1/chat/completions`` SSE test server."""

    def __init__(self, responses: list[list[str]] | None = None):
        self.responses: list[list[str]] = list(responses or [["Quick ", "reply."]])
        self.requests: list[dict[str, Any]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                outer.requests.append({"path": self.path, "body": body})
                chunks = outer.responses.pop(0) if outer.responses else ["Default."]

                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    for chunk in chunks:
                        payload = json.dumps({"choices": [{"delta": {"content": chunk}}]})
                        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                        self.wfile.flush()
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except OSError:
                    pass

            def log_message(self, format, *args):
                return

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self) -> "FakeSSEServer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


class GatedAgent:
    """Full agent controllable with an asyncio.Event for background hand-off tests."""

    def __init__(self, chunks=("Calendar: ", "dentist at 4 PM."), fail: bool = False):
        self.chunks = list(chunks)
        self.fail = fail
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.calls: list[tuple[str, str, str | None, bool]] = []

    async def run(self, device, text, *, session_id=None, new_session=False):
        self.calls.append((device.device_id, text, session_id, new_session))
        self.started.set()
        try:
            await self.release.wait()
            if self.fail:
                raise RuntimeError("boom")
            for chunk in self.chunks:
                yield chunk
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


def _make_fake_hermes(tmp_path: Path, table: str = SAMPLE_SESSIONS_TABLE) -> str:
    return make_script(
        tmp_path / "fake-hermes",
        f"""import json, sys

argv = sys.argv[1:]
if argv[:2] == ["sessions", "list"]:
    sys.stdout.write({table!r})
    sys.exit(0)

query = sys.stdin.read().strip()
resume = argv[argv.index("--resume") + 1] if "--resume" in argv else None
assert "--yolo" not in argv
print(json.dumps({{"type": "text", "text": f"resumed={{resume}} q={{query}}"}}), flush=True)
print(json.dumps({{"type": "result", "session_id": resume or "default_sid", "exit_code": 0}}), flush=True)
""",
    )


def test_parse_sessions_table():
    items = parse_sessions_table(SAMPLE_SESSIONS_TABLE)
    assert items == [
        {"id": "20260419_100000_a1", "title": "Refactor auth module", "age": "5m ago", "folder": "/work/app"},
        {"id": "20260417_093000_b2", "title": "Weekly grocery list", "age": "2d ago", "folder": "/home/alex"},
        {"id": "sess_standup_03", "title": "Morning standup notes", "age": "yesterday", "folder": "~/notes"},
    ]


async def test_quick_streams_deltas_and_keeps_history(state_dir):
    with FakeSSEServer([["It is ", "3 PM."], ["Still ", "3 PM."]]) as sse:
        quick = QuickAgent(sse.url, "qwen-local")
        full = FakeAgent(chunks=("Full ", "Hermes."))
        async with running_hub(state_dir, agent=full, quick_agent=quick) as hub:
            async with Client.open(hub) as c:
                device_id, _, _ = await c.pair(modes=True)
                mode_msg = await c.expect("mode")
                assert mode_msg["mode"] == "quick"

                await c.send("text", text="What time is it?")
                msgs1 = await c.until(is_type("state", screen="idle"))
                asst1 = [m for m in msgs1 if m.get("type") == "transcript" and m.get("role") == "assistant"]
                assert [m["text"] for m in asst1 if not m["final"]] == ["It is ", "3 PM."]
                assert asst1[-1] == {"type": "transcript", "v": 2, "role": "assistant", "text": "It is 3 PM.", "final": True}
                assert full.calls == []

                await c.send("text", text="Are you sure?")
                msgs2 = await c.until(is_type("state", screen="idle"))
                asst2 = [m for m in msgs2 if m.get("type") == "transcript" and m.get("role") == "assistant" and m["final"]]
                assert asst2[0]["text"] == "Still 3 PM."

                assert len(sse.requests) == 2
                req1, req2 = sse.requests[0]["body"], sse.requests[1]["body"]
                assert sse.requests[0]["path"] == "/v1/chat/completions"
                assert req1["model"] == "qwen-local"
                assert req1["max_tokens"] == 200
                assert req1["temperature"] == 0.3
                assert req1["chat_template_kwargs"] == {"enable_thinking": False}
                assert HANDOFF_MARKER in req1["messages"][0]["content"]
                assert req2["messages"][1:] == [
                    {"role": "user", "content": "What time is it?"},
                    {"role": "assistant", "content": "It is 3 PM."},
                    {"role": "user", "content": "Are you sure?"},
                ]

                quick.clear(device_id)
                assert quick.history(device_id) == []


async def test_set_mode_session_uses_resume_id_and_bad_session(state_dir, tmp_path):
    fake_bin = _make_fake_hermes(tmp_path)
    recorded_sids: list[tuple[str, str]] = []
    cli_agent = HermesCliAgent(
        binary=str(fake_bin),
        session_lookup=lambda _d: "per_device_default",
        on_session=lambda d, s: recorded_sids.append((d, s)),
    )
    with FakeSSEServer([["Quick answer."]]) as sse:
        quick = QuickAgent(sse.url, "qwen-local")
        async with running_hub(state_dir, agent=cli_agent, quick_agent=quick) as hub:
            async with Client.open(hub) as c:
                device_id, token, _ = await c.pair(modes=True)
                assert (await c.expect("mode"))["mode"] == "quick"
                assert (await c.expect("slots"))["type"] == "slots"
                assert (await c.expect("character"))["id"] == DEFAULTS["character"]

                await c.send("list_sessions", limit=8)
                sessions_msg = await c.expect("sessions")
                assert len(sessions_msg["items"]) == 3
                assert sessions_msg["items"][0]["id"] == "20260419_100000_a1"

                # Unknown session id -> bad_session error, stays in quick mode
                await c.send("set_mode", mode="session", session_id="nonexistent_id")
                err = await c.expect("error")
                assert err["code"] == "bad_session"

                # Valid session id -> mode switches and persists across reconnects
                await c.send("set_mode", mode="session", session_id="20260417_093000_b2")
                mode_msg = await c.expect("mode")
                assert mode_msg["mode"] == "session"
                assert mode_msg["session_id"] == "20260417_093000_b2"
                assert mode_msg.get("title") == "Weekly grocery list"

                await c.send("text", text="add milk")
                msgs = await c.until(is_type("state", screen="idle"))
                finals = [m["text"] for m in msgs if m.get("type") == "transcript" and m.get("role") == "assistant" and m.get("final")]
                assert finals == ["resumed=20260417_093000_b2 q=add milk"]
                # Per-device lookup was not disturbed by the session override
                assert recorded_sids == []

            # Reconnect remembers mode and session_id
            async with Client.open(hub) as c2:
                await c2.hello(device_id, token=token, modes=True)
                await c2.expect("welcome")
                rem = await c2.expect("mode")
                await c2.expect("slots")
                await c2.expect("character")
                assert rem["mode"] == "session"
                assert rem["session_id"] == "20260417_093000_b2"


async def test_handoff_split_marker_speaks_on_it_and_delivers_result_later(state_dir):
    # Split [[HERMES]] across 4 deltas; verify zero marker leakage, "On it." spoken, and result spoken later.
    stt = RecordingSTT("check calendar")
    tts = EchoTTS(stt)
    full = GatedAgent(chunks=("Dentist ", "at 4 PM."))
    with FakeSSEServer([["[", "[HER", "MES]", "]"], ["Quick follow-up."]]) as sse:
        quick = QuickAgent(sse.url, "qwen-local")
        async with running_hub(state_dir, agent=full, stt=stt, tts=tts, quick_agent=quick) as hub:
            async with Client.open(hub) as c:
                await c.pair(modes=True)
                await c.expect("mode")

                await c.send("text", text="Draft a haiku about the sea")
                on_it_msgs = await c.until(is_type("state", screen="idle"))
                asst_msgs = [
                    m for m in on_it_msgs
                    if isinstance(m, dict) and m.get("type") == "transcript" and m.get("role") == "assistant"
                ]
                for m in asst_msgs:
                    assert HANDOFF_MARKER not in m["text"]
                    assert "[" not in m["text"]
                assert [m["text"] for m in asst_msgs if m["final"]] == ["On it."]
                assert any(isinstance(m, dict) and m.get("type") == "audio_start" for m in on_it_msgs)

                await asyncio.wait_for(full.started.wait(), 2.0)
                # Interrupt while idle must not cancel the background hand-off
                await c.send("interrupt")
                assert not full.cancelled.is_set()

                # Release the background hand-off while device is idle -> spoken (transcript + audio)
                full.release.set()
                later_msgs = await c.until(is_type("state", screen="idle"))
                later_finals = [
                    m["text"] for m in later_msgs
                    if isinstance(m, dict) and m.get("type") == "transcript" and m.get("role") == "assistant" and m.get("final")
                ]
                assert later_finals == ["Dentist at 4 PM."]
                assert any(isinstance(m, dict) and m.get("type") == "audio_start" for m in later_msgs)
                assert full.calls[0][3] is True  # new_session=True


async def test_handoff_when_busy_sends_notify_and_failure_speaks_fallback(state_dir):
    stt = RecordingSTT("hi")
    tts = EchoTTS(stt)
    full = GatedAgent(chunks=("Background ", "done."))
    slow_quick_gate = asyncio.Event()

    class BusyQuickAgent:
        def __init__(self):
            self.turn = 0

        async def run(self, device, text):
            self.turn += 1
            if self.turn == 1:
                yield "[[HER"
                yield "MES]]"
            else:
                yield "Thinking..."
                await slow_quick_gate.wait()

    async with running_hub(state_dir, agent=full, stt=stt, tts=tts, quick_agent=BusyQuickAgent()) as hub:
        async with Client.open(hub) as c:
            await c.pair(modes=True)
            await c.send("text", text="Run long task")
            await c.until(is_type("state", screen="idle"))
            await asyncio.wait_for(full.started.wait(), 2.0)

            # Start a new turn so the device is busy when hand-off finishes; must not cancel hand-off
            await c.send("text", text="Keep me busy")
            await c.until(is_type("state", screen="thinking"))
            assert not full.cancelled.is_set()

            # New rule: a hand-off answer that finishes while the device is busy is queued and SPOKEN after the
            # current turn (no longer a notify-only fallback).
            full.release.set()
            await asyncio.sleep(0.2)
            slow_quick_gate.set()
            msgs = await c.until(is_type("transcript", role="assistant", text="Background done.", final=True))
            assert not any(is_type("notify")(m) for m in msgs)
            await c.until(is_type("state", screen="idle"))

    # Hand-off failure speaks "That didn't work."
    failing_full = GatedAgent(fail=True)
    failing_full.release.set()
    with FakeSSEServer([["[[HERMES]]"]]) as sse:
        async with running_hub(
            state_dir / "fail", agent=failing_full, stt=stt, tts=tts, quick_agent=QuickAgent(sse.url, "m")
        ) as hub:
            async with Client.open(hub) as c:
                await c.pair(modes=True)
                await c.send("text", text="Break")
                await c.until(is_type("state", screen="idle"))  # On it.
                err_msgs = await c.until(is_type("state", screen="idle"))
                finals = [
                    m["text"] for m in err_msgs
                    if isinstance(m, dict) and m.get("type") == "transcript" and m.get("final")
                ]
                assert finals == ["That didn't work."]


async def test_client_ignoring_modes_still_gets_answers_and_settings(state_dir):
    assert DEFAULTS["quick_url"] == ""
    assert DEFAULTS["quick_model"] == ""
    hub_default = build_hub({"state_dir": str(state_dir), "agent": "echo", "tts": "none"})
    assert isinstance(hub_default.quick_agent, QuickAgent)
    assert read_settings(state_dir)["quick_backend"] == "hermes"

    with FakeSSEServer([["Ignored ", "modes works."]]) as sse:
        update_settings(state_dir, {"quick_url": sse.url, "quick_model": "fast-1"})
        assert read_settings(state_dir)["quick_model"] == "fast-1"
        hub = build_hub({"state_dir": str(state_dir), "agent": "echo", "tts": "none", "port": 0})
        assert isinstance(hub.quick_agent, QuickAgent)
        async with hub:
            async with Client.open(hub) as c:
                await c.pair()
                await c.send("text", text="hello")
                msgs = await c.until(is_type("state", screen="idle"))
                finals = [
                    m["text"] for m in msgs
                    if isinstance(m, dict) and m.get("type") == "transcript" and m.get("role") == "assistant" and m.get("final")
                ]
                assert finals == ["Ignored modes works."]
