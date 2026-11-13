"""The Pocket hub: WebSocket server, device registry, pairing, and the agent bridge.

One ``PocketHub`` owns every connected device. Device connections speak protocol v2 on
``/pocket``; the Hermes plugin's device tools reach the hub over the loopback-only, token-guarded
``/control`` path (see ``control.py``), which works whether or not Hermes runs in this process.
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
from collections import deque
import hmac
import itertools
import hashlib
import socket
import struct
import inspect
import ipaddress
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Coroutine
from urllib.parse import urlsplit

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request
from websockets.protocol import State

from . import protocol as P
from . import __version__
from .agent import AgentBackend, AgentError, DeviceInfo, EchoAgent, list_hermes_sessions
from .audio import STT, TTS, StubSTT
from .facts import append_fact
from .quick import HANDOFF_MARKER, ensure_soul_file
from .settings import (
    add_settings_listener,
    character_info,
    character_label,
    read_settings,
    remove_settings_listener,
    update_settings,
)
from .store import Store

log = logging.getLogger("hail_hermes.hub")

# 300 ms of s16le @ 16 kHz. Shorter captures are not an utterance (no STT, no agent).
# Length is the PCM byte count, not wall-clock time spent between audio_start and audio_end.
MIN_UTTERANCE_BYTES = 300 * P.SAMPLE_RATE // 1000 * 2
MIN_SPEECH_CHUNK_CHARS = 20
# How long a reply may be read aloud before it switches to a short version.
MAX_SPOKEN_CHARS = 420
# Sentence pieces are packed up to roughly this many characters each.
SPEECH_CHUNK_CHARS = 220
SORRY_REPLY = "Sorry, I didn't catch that."
GOODBYE_REPLY = "Bye! Talk to you soon."
# Prefix for the first turn after the owner stops an answer. The agent must know it was
# interrupted, otherwise it answers as if the previous reply never mattered.
INTERRUPTED_NOTE = (
    "[The user interrupted your previous answer before it finished. "
    "Ask whether to resume it, or take the new request. Do not re-answer silently.]\n"
)
# While a turn runs, the busy state is re-sent this often so a client that
# reconnects (or dropped a frame) never sits on a stale screen.
HEARTBEAT_SECONDS = 3
# A turn exceeding this is cancelled through the normal cancel path and the
# client gets the plain error, then idle. The next queued turn still runs.
HANDOFF_AGENT_TIMEOUT_SECONDS = 300.0
TURN_CEILING_SECONDS = 120

_GOODBYE_PHRASES = frozenset(
    {
        "goodbye",
        "good bye",
        "bye",
        "bye bye",
        "that's all",
        "thats all",
        "that is all",
        "stop listening",
        "thanks that's it",
        "thanks thats it",
        "thank you that's it",
        "thank you thats it",
        "thanks that's all",
        "thanks thats all",
    }
)

_ABBREV_RE = re.compile(
    r"(?:^|[\s(])(?:[a-zA-Z]\.[a-zA-Z]|e\.g|i\.e|etc|vs|mr|mrs|ms|dr|prof|sr|jr|st)\.$",
    re.IGNORECASE,
)


def _lan_ipv4_from_getaddrinfo() -> set[str]:
    addresses = set()
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return addresses
    for info in infos:
        address = info[4][0]
        if not ipaddress.ip_address(address).is_loopback:
            addresses.add(address)
    return addresses


def _lan_ipv4_addresses() -> set[str]:
    if os.name != "posix":
        return _lan_ipv4_from_getaddrinfo()
    addresses = set()
    import fcntl

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for _, name in socket.if_nameindex():
            try:
                data = fcntl.ioctl(sock.fileno(), 0x8915, struct.pack("256s", name.encode()[:15]))
                address = socket.inet_ntoa(data[20:24])
                if not ipaddress.ip_address(address).is_loopback:
                    addresses.add(address)
            except OSError:
                continue
    return addresses


def _send_beacon(payload: bytes) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(payload, ("255.255.255.255", 8768))


def is_goodbye(text: str) -> bool:
    cleaned = text.strip().lower().replace("\u2019", "'")
    cleaned = re.sub(r"[^\w\s']", " ", cleaned)
    cleaned = " ".join(cleaned.split())
    if cleaned in _GOODBYE_PHRASES:
        return True
    return cleaned.replace("'", "") in _GOODBYE_PHRASES


def _clean_phrase(text: str) -> str:
    cleaned = text.strip().lower().replace("\u2019", "'").replace("\u2018", "'")
    cleaned = re.sub(r"[^\w\s']", " ", cleaned)
    return " ".join(cleaned.split())


def _slot_label(slot: dict[str, Any]) -> str:
    return str(slot.get("name") or slot.get("title") or "").strip()


def match_slot(query: str, slots: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Best-matching slot by name: exact, then startswith, then difflib ratio >= 0.75."""
    q = _clean_phrase(query)
    if not q:
        return None
    candidates: list[tuple[dict[str, Any], str]] = []
    for item in (slots or [])[:3]:
        if not isinstance(item, dict):
            continue
        norm = _clean_phrase(_slot_label(item))
        if norm:
            candidates.append((item, norm))
    if not candidates:
        return None

    def _search(target: str) -> dict[str, Any] | None:
        for slot, norm in candidates:
            if norm == target:
                return slot
        for slot, norm in candidates:
            if norm.startswith(target):
                return slot
        best_slot: dict[str, Any] | None = None
        best_ratio = 0.0
        for slot, norm in candidates:
            ratio = difflib.SequenceMatcher(None, target, norm).ratio()
            if ratio >= 0.75 and ratio > best_ratio:
                best_ratio = ratio
                best_slot = slot
        return best_slot

    found = _search(q)
    if found is None and q.endswith(" session"):
        trimmed = q[: -len(" session")].strip()
        if trimmed:
            found = _search(trimmed)
    return found


def _voice_session_title(text: str) -> str:
    words = text.strip().split()
    first_words = " ".join(words[:6])
    return f"Voice: {first_words}" if first_words else "Voice"


def _split_sentences(buf: str, *, final: bool = False) -> tuple[list[str], str]:
    """Split complete sentences (>=20 chars, merging short ones) from ``buf``."""
    chunks: list[str] = []
    chunk_start = 0
    i = 0
    n = len(buf)
    while i < n:
        ch = buf[i]
        is_newline = ch == "\n"
        is_punct_space = ch in ".!?" and (i + 1 < n and buf[i + 1] in " \t\r\n")
        if is_punct_space and ch == "." and _ABBREV_RE.search(buf[chunk_start : i + 1]):
            i += 1
            continue
        if is_newline or is_punct_space:
            end = i + 1
            while end < n and buf[end] in " \t\r\n":
                end += 1
            candidate = buf[chunk_start:end].strip()
            if len(candidate) >= MIN_SPEECH_CHUNK_CHARS:
                chunks.append(candidate)
                chunk_start = end
            i = end
        else:
            i += 1
    remainder = buf[chunk_start:]
    if final:
        rem = remainder.strip()
        if rem:
            chunks.append(rem)
        return chunks, ""
    return chunks, remainder


@dataclass
class HubConfig:
    host: str = "127.0.0.1"
    port: int = P.DEFAULT_PORT
    path: str = P.DEFAULT_PATH
    idle_timeout: float = 60.0      # protocol: silent >60 s = gone
    hello_timeout: float = 15.0
    pair_timeout: float = 300.0
    audio_pacing: bool = True       # send TTS frames at ~real time (20 ms per frame)
    stream_speech: bool = True      # synthesize and stream sentences as deltas arrive
    max_audio_seconds: float = 60.0
    mascot_hash: str = "default"
    warm_voice: bool = False        # serve turns this on: load TTS at start so the first reply isn't slower


@dataclass
class _TurnTiming:
    """Latency spans the hub measures for one turn.

    `start` is taken when the turn begins on the hub (after the device frame has been
    received and the turn launched), so stt/first_text/first_audio/total exclude the
    client's send and any queueing before the turn starts. The audit client starts its
    clock before sending, so its first_audio includes that hop; the two are expected to
    agree within a fraction of a second on a local link. `total` is time to finish
    speaking, not latency.
    """
    gen: int
    start: float = field(default_factory=time.monotonic)
    stt: float | None = None
    first_text: float | None = None
    first_audio: float | None = None

    def format(self, now: float | None = None) -> str:
        total = (time.monotonic() if now is None else now) - self.start
        parts: list[str] = []
        if self.stt is not None:
            parts.append(f"stt={self.stt:.1f}s")
        if self.first_text is not None:
            parts.append(f"first_text={self.first_text:.1f}s")
        if self.first_audio is not None:
            parts.append(f"first_audio={self.first_audio:.1f}s")
        parts.append(f"total={total:.1f}s")
        return " ".join(parts)


@dataclass
class Session:
    ws: ServerConnection
    device: DeviceInfo
    screen: str = "idle"
    audio: bytearray | None = None
    turn: asyncio.Task | None = None
    battery: dict[str, Any] | None = None
    # Bumped on interrupt, disconnect, revoke, and replace. A turn
    # snapshots this when it starts and must not emit once it no longer matches.
    generation: int = 0
    # device_say texts the running turn will speak before it publishes idle.
    pending_say: list[str] = field(default_factory=list)
    # Set when the owner stops a turn early; the next turn tells the agent so it can
    # offer to resume instead of pretending nothing happened.
    interrupted_pending: bool = False
    pending_turns: deque[Callable[[int], Coroutine[Any, Any, None]]] = field(
        default_factory=deque)
    connected_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    timing: _TurnTiming | None = None
    modes: bool = False
    voice_session_started: bool = False
    # Set while a finished background answer is being delivered. An interrupt that lands in the moment
    # between the delivery turn being launched and its first frame must stop the *speech*, not delete an
    # answer the owner has not seen yet (see `_interrupt`).
    delivery_pending: bool = False
    # Set by an interrupt during a pending delivery: publish the text, skip the audio.
    stop_speech: bool = False
    # Set by the turn-ceiling timer; read and cleared by the turn guard.
    ceiling_hit: bool = False
    ceiling_handle: Any = None


@dataclass
class PendingPair:
    ws: ServerConnection
    device: DeviceInfo
    request_id: str
    match_code: str
    remote: str
    created: float
    deadline: float
    decision: asyncio.Future[str]
    # Arrival order. ``created`` is a monotonic clock that ticks every ~15 ms on Windows, so two
    # quick attempts can share a timestamp; the sequence still says which one is newer.
    seq: int = field(default_factory=itertools.count().__next__)


# What to tell the owner when the pairing request they answered is no longer pending.
# Pairing is the one place a person types a code by hand, so every dead end must be a
# sentence they can act on — never "not found" and never an HTTP status.
PAIR_GONE_REASONS = {
    "expired": "That code has expired.",
    "approved": "That code was already approved. The watch is connected.",
    "denied": "That code was already declined.",
    "replaced": "That code is from an earlier try.",
    "watch_left": "The watch stopped waiting for that code.",
    "gone": "That code is not waiting any more.",
}
# How long a gone pairing stays in memory so it can explain itself. Match codes are only
# unique among *live* requests, so an older explanation must not outlive a reused code.
PAIR_GONE_MEMORY_SECONDS = 1800.0
PAIR_GONE_MEMORY_SIZE = 64


class _TurnFailure(Exception):
    """An internal backend failure with a safe public error category."""

    def __init__(self, code: str, detail: str = ""):
        self.detail = detail
        self.code = code
        super().__init__(code)


class PocketHub:
    def __init__(
        self,
        agent: AgentBackend | None = None,
        stt: STT | None = None,
        tts: TTS | None = None,
        store: Store | None = None,
        config: HubConfig | None = None,
        quick_agent: AgentBackend | None = None,
    ):
        self.agent = agent or EchoAgent()
        self.quick_agent = quick_agent
        self.stt = stt or StubSTT()
        self.tts = tts
        self.store = store or Store()
        self.config = config or HubConfig()
        self.sessions: dict[str, Session] = {}
        self.pending: dict[str, PendingPair] = {}
        # Pairing requests that are no longer pending, newest last, with why they went
        # away. Lets an approve of a stale code answer with a sentence instead of a
        # bare "not found" (see PAIR_GONE_REASONS).
        # (lat, lon) -> (monotonic_at, sentence); see _answer_weather.
        self._weather_cache: dict[tuple[float, float], tuple[float, str]] = {}
        self._pair_gone: deque[dict[str, Any]] = deque(maxlen=PAIR_GONE_MEMORY_SIZE)
        # In-flight turn-log writes, kept referenced so the loop does not drop them.
        self._log_tasks: set[asyncio.Task] = set()
        self._connections_by_ip: dict[str, int] = {}
        self._handoff_tasks: set[asyncio.Task] = set()
        self._handoff_counts: dict[str, int] = {}
        self._offline_handoffs: dict[str, deque[tuple[float, str]]] = {}
        # Typed questions that were still waiting in line when the watch dropped; asked again on reconnect.
        self._offline_questions: dict[str, deque[tuple[float, str]]] = {}
        self._server = None
        self._lan_servers = []
        self._beacon_task = None
        self._lan_lock = asyncio.Lock()
        self._stopping = False
        self._mirror: dict[str, Any] | None = None
        self._mirror_user = ""
        self._mirror_reply = ""
        self._control_token = self.store.control_token()
        self.port: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._suppress_slot_broadcast = False
        ensure_soul_file(state_dir=self.store.dir)

    # -- lifecycle ----------------------------------------------------------------
    async def start(self) -> "PocketHub":
        self._loop = asyncio.get_running_loop()
        self._stopping = False
        ensure_soul_file(state_dir=self.store.dir)
        settings = read_settings(self.store.dir)
        self._last_character = character_info(settings)["id"]
        self._last_slots = list(settings.get("slots") or [])
        add_settings_listener(self._on_settings_updated)
        self._server = await serve(
            self._handle, self.config.host, self.config.port,
            process_request=self._process_request, max_size=2**20,
        )
        self.port = self._server.sockets[0].getsockname()[1]
        await self._sync_lan()
        self._write_live()
        log.info("pocket hub listening on ws://%s:%s%s", self.config.host, self.port, self.config.path)
        # Load the voice in the background so the first answer isn't also waiting for the model to load.
        if self.config.warm_voice:
            self._warm_task = asyncio.create_task(self._warm_voice())
            self._brief_warm_task = asyncio.create_task(self._warm_brief_audio())
        return self

    async def _warm_brief_audio(self) -> None:
        """Render the brief's fixed sentences once, in the background, right after start-up.

        Otherwise the very first question of a demo pays the full synthesis cost (~700 ms) while a
        judge watches. These sentences change rarely, so warming them is cheap and makes the first
        answer as fast as the tenth.
        """
        if self.tts is None:
            # No speech backend (replies turned off, or it failed to build): there is nothing to warm.
            # Trying anyway logged a traceback per sentence at every start, which reads like a fault to
            # anyone who runs with speech off.
            return
        from . import brief as brief_api

        try:
            data = brief_api.load(self.store.dir)
        except Exception as exc:  # noqa: BLE001 - a missing brief must never break the hub
            log.debug("brief unavailable, skipping pre-warm: %s", exc)
            return
        sentences = []
        for key in ("schedule", "emails", "trip"):
            questions = {
                "schedule": "What is on my schedule today?",
                "emails": "Any important emails?",
                "trip": "Give me the itinerary for the trip.",
            }[key]
            try:
                sentence = brief_api.answer(questions, self.store.dir)
            except Exception as exc:  # noqa: BLE001 - one unanswerable question must not stop the rest
                log.debug("brief pre-warm skipped %s: %s", key, exc)
                continue
            if sentence:
                sentences.append(sentence)
        work = str(data.get("work") or "").strip()
        if work:
            sentences.append("You were on " + work + ".")
        for sentence in sentences:
            try:
                await self._synthesize_remembered(sentence, self._character_voice())
            except Exception as exc:  # noqa: BLE001 - a warm-up must never break the hub
                # The sentence is still answered on demand; say what happened without a stack, because a
                # stack here looks like a crash in a log a user is asked to paste.
                log.info("brief warm-up skipped one sentence: %s", exc)

    async def _warm_voice(self) -> None:
        stt = self.stt
        if stt is not None and hasattr(stt, "transcribe"):
            try:
                await stt.transcribe(b"\x00" * MIN_UTTERANCE_BYTES)
                log.info("stt warmed up")
            except Exception:
                log.info("stt warm-up failed; the first utterance will load it", exc_info=True)
        tts = self.tts
        while tts is not None and hasattr(tts, "primary"):
            tts = tts.primary
        if tts is None or not hasattr(tts, "synthesize"):
            return
        try:
            await tts.synthesize("Ready.")
            log.info("voice warmed up")
        except Exception:
            log.info("voice warm-up failed; the first reply will load it", exc_info=True)

    async def stop(self) -> None:
        remove_settings_listener(self._on_settings_updated)
        self._stopping = True
        # Clear the live marker FIRST: between closing the listening socket and clearing it,
        # a /status read or the pane's poll could still see a hub that is already dying and
        # try to connect to a closing port.
        self.store.clear_live()
        await self._stop_lan()
        for sess in list(self.sessions.values()):
            self._cancel_turn(sess)
        for task in list(self._handoff_tasks):
            task.cancel()
        if self._handoff_tasks:
            await asyncio.gather(*self._handoff_tasks, return_exceptions=True)
            self._handoff_tasks.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self.store.clear_live()

    async def serve_forever(self) -> None:
        await self.start()
        try:
            await asyncio.Future()
        finally:
            await self.stop()

    async def __aenter__(self) -> "PocketHub":
        return await self.start()

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self.config.host in ("0.0.0.0", "::", "") else self.config.host
        return f"ws://{host}:{self.port}{self.config.path}"

    @property
    def control_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}{P.CONTROL_PATH}"

    def _write_live(self) -> None:
        self.store.write_live({
            "pid": os.getpid(), "port": self.port, "control_url": self.control_url, "updated": time.time(),
            "mirror": self._mirror,
            "devices": [
                {"device_id": s.device.device_id, "name": s.device.name, "kind": s.device.kind,
                 "caps": s.device.caps, "screen": s.screen}
                for s in self.sessions.values()
            ],
        })

    # -- HTTP routing -------------------------------------------------------------
    async def _process_request(self, conn: ServerConnection, request: Request):
        path = request.path.split("?", 1)[0]
        if path == "/status":
            if not _is_loopback(conn.remote_address):
                return conn.respond(403, "status is loopback-only\n")
            paired = await asyncio.to_thread(self.store.paired)
            sessions = self.sessions.copy()
            devices = []
            for device_id in paired.keys() | sessions.keys():
                saved = paired.get(device_id, {})
                session = sessions.get(device_id)
                row = {"name": session.device.name if session else saved.get("name", "?"),
                       "kind": session.device.kind if session else saved.get("kind", "?"),
                       "connected": session is not None,
                       "last_seen": session.last_seen if session else saved.get("last_seen")}
                if session and session.battery is not None:
                    row["battery"] = session.battery
                devices.append(row)
            now = time.monotonic()
            pending = [{"name": p.device.name, "match_code": p.match_code,
                        "expires_in": max(0, int(p.deadline - now))}
                       for p in self.pending.values()
                       if p.deadline > now and p.ws.state is State.OPEN]
            body = json.dumps({"running": True, "version": __version__,
                               "mode": "quick" if self.quick_agent is not None else "session",
                               "character": getattr(self, "_last_character", "hermes"),
                               "devices": devices, "pending": pending})
            response = conn.respond(200, body)
            response.headers["Content-Type"] = "application/json"
            # No `Access-Control-Allow-Origin: *` here (review item 4). The desktop pane reads this
            # route through `ctx.rest('/status')`, i.e. over the plugin's own API, so a wildcard only
            # widened what any web page could read from a loopback service.
            response.headers["Cache-Control"] = "no-store"
            return response
        if path == self.config.path:
            origins = request.headers.get_all("Origin")
            if len(origins) > 1 or (
                origins and not _allowed_device_origin(origins[0])
            ):
                return conn.respond(403, "origin not allowed\n")
            return None
        if path == P.CONTROL_PATH:
            if not _is_loopback(conn.remote_address):
                return conn.respond(403, "control channel is loopback-only\n")
            auth = request.headers.get("Authorization", "")
            if not hmac.compare_digest(auth, f"Bearer {self._control_token}"):
                return conn.respond(401, "bad control token\n")
            return None
        return conn.respond(404, f"Hail Hermes hub: connect to {self.config.path}\n")

    async def _handle(self, ws: ServerConnection) -> None:
        if ws.request.path.split("?", 1)[0] == P.CONTROL_PATH:
            await self._handle_control(ws)
            return
        remote = _remote_ip(ws.remote_address)
        count = self._connections_by_ip.get(remote, 0)
        if count >= 8:
            await ws.close(P.CLOSE_PAIRING, "too many connections")
            return
        self._connections_by_ip[remote] = count + 1
        sess: Session | None = None
        try:
            sess = await self._handshake(ws)
            if sess is not None:
                await self._session_loop(sess)
        except ConnectionClosed:
            pass
        except Exception:
            log.exception("device connection crashed")
            with contextlib.suppress(Exception):
                await self._send(ws, P.error("internal"))
                await ws.close(1011, "internal error")
        finally:
            remaining = self._connections_by_ip[remote] - 1
            if remaining:
                self._connections_by_ip[remote] = remaining
            else:
                del self._connections_by_ip[remote]
            if sess is not None:
                self._detach_turn(sess)
                if self.sessions.get(sess.device.device_id) is sess:
                    del self.sessions[sess.device.device_id]
                    self._write_live()
                    log.info("device %s (%s) disconnected", sess.device.name, sess.device.device_id)

    # -- hello / pairing ----------------------------------------------------------
    async def _send(self, ws: ServerConnection, message: dict[str, Any]) -> None:
        await ws.send(P.encode(message))

    async def _protocol_error(self, ws: ServerConnection, exc: P.ProtocolError) -> bool:
        await self._send(ws, P.error(exc.code))
        if exc.code == "version":
            await ws.close(P.CLOSE_VERSION, "protocol version mismatch")
            return True
        return False

    async def _recv_json(self, ws: ServerConnection, deadline: float) -> dict[str, Any] | None:
        """Next control message, or None at the deadline."""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                raw = await asyncio.wait_for(ws.recv(), remaining)
            except asyncio.TimeoutError:
                return None
            if isinstance(raw, bytes):
                await self._send(ws, P.error("not_paired"))
                continue
            try:
                return P.decode(raw)
            except P.ProtocolError as exc:
                if await self._protocol_error(ws, exc):
                    return None

    async def _handshake(self, ws: ServerConnection) -> Session | None:
        cfg = self.config
        deadline = time.monotonic() + cfg.hello_timeout
        while True:
            hello = await self._recv_json(ws, deadline)
            if hello is None:
                if ws.state is State.OPEN:
                    await ws.close(P.CLOSE_IDLE, "no hello")
                return None
            if hello["type"] == "ping":
                await self._send(ws, P.msg("pong", t=hello.get("t")))
                continue
            if hello["type"] == "hello":
                hello_at = time.monotonic()
                break
            await self._send(ws, P.error("not_paired"))
        try:
            P.validate_hello(hello)
        except P.ProtocolError as exc:
            await self._send(ws, P.error(exc.code))
            code = P.CLOSE_VERSION if exc.code == "version" else P.CLOSE_BAD_HELLO
            await ws.close(code, "invalid hello")
            return None

        device = DeviceInfo(
            device_id=hello["device_id"], name=hello.get("name") or hello["device_id"][:8],
            kind=hello["kind"], caps=P.normalize_caps(hello.get("caps")),
        )
        if not self.store.check_token(device.device_id, hello.get("token")):
            if not await self._pair(ws, device, hello_at):
                return None
        sess = Session(ws=ws, device=device, modes=hello.get("modes") is True)
        if isinstance(hello.get("caps"), dict) and isinstance(hello["caps"].get("battery"), dict):
            sess.battery = _battery(hello["caps"]["battery"])
        old = self.sessions.get(device.device_id)
        self.sessions[device.device_id] = sess
        if old is not None:
            self._detach_turn(old)
            asyncio.create_task(old.ws.close(P.CLOSE_REPLACED, "replaced by a newer connection"))
        await self._send(ws, P.msg("welcome", device_id=device.device_id, state=sess.screen,
                                   mascot_hash=self.config.mascot_hash))
        if sess.modes:
            # Only clients that asked get the new message; older clients keep working untouched.
            await self._send(ws, self._mode_msg(device.device_id))
            await self._send(ws, self._slots_msg())
            await self._send(ws, self._character_msg())
        count = self._handoff_counts.get(device.device_id, 0)
        if sess.modes and count:
            await self._send(ws, P.msg("working", count=count))
        pending = self._offline_handoffs.pop(device.device_id, deque())
        for created, reply in pending:
            if time.monotonic() - created <= 900:
                if self._turn_busy(sess) or sess.audio is not None:
                    await self._send(ws, P.msg("notify", title="Hermes", body=reply))
                await self._start_turn(
                    sess, lambda g, reply=reply: self._say_turn(sess, g, reply))
        for created, question in self._offline_questions.pop(device.device_id, deque()):
            if time.monotonic() - created <= 900:
                await self._start_turn(
                    sess, lambda g, question=question: self._text_turn(sess, g, question))
        self._write_live()
        log.info("device %s (%s, %s) connected caps=%s", device.name, device.kind, device.device_id,
                 [c for c, on in device.caps.items() if on])
        return sess

    def _remember_pair_gone(self, pending: PendingPair, reason: str) -> None:
        self._pair_gone.append({
            "request_id": pending.request_id,
            "match_code": pending.match_code,
            "device_id": pending.device.device_id,
            "reason": reason,
            "at": time.monotonic(),
        })

    def _pair_gone_reason(self, pending: PendingPair) -> str:
        """Why a pending request that is leaving now went away."""
        if pending.deadline <= time.monotonic():
            return "expired"
        newer = [p for p in self.pending.values()
                 if p.device.device_id == pending.device.device_id and p.seq > pending.seq]
        return "replaced" if newer else "watch_left"

    def _pair_gone_message(self, request_id: Any, match_code: Any) -> str:
        """A sentence for an approval whose pairing request is no longer pending.

        Distinguishes expired, already approved, already declined, replaced by a newer
        attempt, and a request this hub never knew (for example after a restart), and
        points at the code the watch is showing when there is one.
        """
        now = time.monotonic()
        record: dict[str, Any] | None = None
        for entry in reversed(self._pair_gone):
            if now - entry["at"] > PAIR_GONE_MEMORY_SECONDS:
                continue
            if request_id and entry["request_id"] == request_id:
                record = entry
                break
            if match_code and entry["match_code"] == match_code:
                record = entry
                break
        reason = record["reason"] if record else "gone"
        base = PAIR_GONE_REASONS.get(reason, PAIR_GONE_REASONS["gone"])
        if reason == "approved":
            return base
        live = [p for p in self.pending.values() if p.ws.state is State.OPEN]
        current: str | None = None
        if record is not None:
            same_device = [p for p in live if p.device.device_id == record["device_id"]]
            if len(same_device) == 1:
                current = same_device[0].match_code
        if current is None and len(live) == 1:
            current = live[0].match_code
        if current:
            return f"{base} The watch is showing {current} now."
        if reason == "watch_left":
            return f"{base} Ask it to show a new one."
        return f"{base} Ask the watch to show a new one."

    def _drop_pending(self, request_id: str, pending: PendingPair) -> None:
        if self.pending.get(request_id) is pending:
            del self.pending[request_id]
            self._remember_pair_gone(pending, self._pair_gone_reason(pending))

    async def _pair(self, ws: ServerConnection, device: DeviceInfo, created: float) -> bool:
        remote = _remote_ip(ws.remote_address)
        self._prune_pending()
        if len(self.pending) >= 8 or sum(p.remote == remote for p in self.pending.values()) >= 3:
            await self._send(ws, P.error("busy"))
            await ws.close(P.CLOSE_PAIRING, "pairing busy")
            return False

        used_codes = {p.match_code for p in self.pending.values()}
        while True:
            match_code = f"{secrets.randbelow(10000):04d}"
            if match_code not in used_codes:
                break
        request_id = secrets.token_urlsafe(16)
        while request_id in self.pending:
            request_id = secrets.token_urlsafe(16)
        pending = PendingPair(
            ws=ws, device=device, request_id=request_id, match_code=match_code,
            remote=remote, created=created, deadline=created + self.config.pair_timeout,
            decision=asyncio.get_running_loop().create_future(),
        )
        self.pending[request_id] = pending
        recv_task: asyncio.Task | None = None
        try:
            remaining = pending.deadline - time.monotonic()
            if remaining <= 0:
                await self._send(ws, P.error("pair_timeout"))
                await ws.close(P.CLOSE_PAIRING, "pairing timed out")
                return False
            await self._send(ws, P.msg(
                "pair_pending", request_id=request_id, match_code=match_code, expires_in=max(1, int(remaining)),
            ))
            recv_task = asyncio.create_task(ws.recv())
            while True:
                remaining = pending.deadline - time.monotonic()
                if remaining <= 0:
                    await self._send(ws, P.error("pair_timeout"))
                    await ws.close(P.CLOSE_PAIRING, "pairing timed out")
                    return False
                done, _ = await asyncio.wait(
                    (recv_task, pending.decision), timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    await self._send(ws, P.error("pair_timeout"))
                    await ws.close(P.CLOSE_PAIRING, "pairing timed out")
                    return False

                # A disconnect or invalid frame takes precedence over an approval
                # observed in the same event-loop iteration.
                if recv_task in done:
                    finished = recv_task
                    recv_task = None
                    try:
                        raw = finished.result()
                    except ConnectionClosed:
                        self._drop_pending(request_id, pending)
                        return False
                    if isinstance(raw, bytes):
                        await self._send(ws, P.error("not_paired"))
                    else:
                        try:
                            message = P.decode(raw)
                        except P.ProtocolError as exc:
                            self._drop_pending(request_id, pending)
                            if await self._protocol_error(ws, exc):
                                return False
                            return False
                        if message["type"] == "ping":
                            await self._send(ws, P.msg("pong", t=message.get("t")))
                        else:
                            await self._send(ws, P.error("not_paired"))
                    if not pending.decision.done():
                        recv_task = asyncio.create_task(ws.recv())

                if pending.decision in done:
                    if pending.decision.result() == "deny":
                        await self._send(ws, P.error("denied"))
                        await ws.close(P.CLOSE_PAIRING, "pairing denied")
                        return False
                    if ws.state is not State.OPEN or time.monotonic() >= pending.deadline:
                        return False
                    if recv_task is not None:
                        recv_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError, ConnectionClosed):
                            await recv_task
                        recv_task = None
                    token = self.store.issue_token(device.device_id, device.name, device.kind)
                    await self._send(ws, P.msg("paired", token=token))
                    log.info("paired %s (%s)", device.name, device.device_id)
                    return True
        finally:
            self._drop_pending(request_id, pending)
            if recv_task is not None:
                recv_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, ConnectionClosed):
                    await recv_task

    def _prune_pending(self) -> None:
        now = time.monotonic()
        for request_id, pending in list(self.pending.items()):
            if pending.deadline <= now:
                del self.pending[request_id]
                self._remember_pair_gone(pending, "expired")
            elif pending.ws.state is not State.OPEN:
                del self.pending[request_id]
                self._remember_pair_gone(pending, self._pair_gone_reason(pending))

    def _pair_list(self) -> list[dict[str, Any]]:
        self._prune_pending()
        now = time.monotonic()
        return [
            {
                "request_id": p.request_id,
                "match_code": p.match_code,
                "device_id": p.device.device_id,
                "name": p.device.name,
                "kind": p.device.kind,
                "remote": p.remote,
                "age_s": max(0.0, now - p.created),
                "expires_in": max(0, int(p.deadline - now)),
            }
            for p in self.pending.values()
        ]

    def _pair_decide(self, args: dict[str, Any], decision: str) -> dict[str, Any]:
        self._prune_pending()
        request_id = args.get("request_id")
        match_code = args.get("match_code")
        if request_id:
            matches = [p for p in self.pending.values() if p.request_id == request_id]
            if match_code is not None:
                matches = [p for p in matches if p.match_code == match_code]
        elif match_code:
            matches = [p for p in self.pending.values() if p.match_code == match_code]
        else:
            raise ValueError("request_id or match_code is required")
        if len(matches) != 1:
            raise LookupError(self._pair_gone_message(request_id, match_code))
        pending = matches[0]
        if pending.decision.done() or pending.ws.state is not State.OPEN:
            raise LookupError(self._pair_gone_message(pending.request_id, pending.match_code))
        del self.pending[pending.request_id]
        pending.decision.set_result(decision)
        self._remember_pair_gone(pending, "approved" if decision == "approve" else "denied")
        return {"device_id": pending.device.device_id}

    async def _revoke(self, device_id: Any) -> dict[str, Any]:
        if not isinstance(device_id, str) or not device_id:
            raise ValueError("device_id is required")
        revoked = self.store.revoke(device_id)
        if self.quick_agent is not None and hasattr(self.quick_agent, "clear"):
            self.quick_agent.clear(device_id)
        sess = self.sessions.get(device_id)
        if sess is not None:
            self._cancel_turn(sess)
            await sess.ws.close(P.CLOSE_REVOKED, "device revoked")
        return {"device_id": device_id, "revoked": revoked}

    # -- session --------------------------------------------------------------
    def _live(self, sess: Session, gen: int) -> bool:
        """True while this turn is still the one the client should hear."""
        return gen == sess.generation and self.sessions.get(sess.device.device_id) is sess

    async def _finish_offline(self, sess: Session, gen: int, deltas, parts: list[str]) -> None:
        """The watch dropped mid-reply. Keep reading the reply, then hand it to the device's
        current connection, or keep it for the next one (owner, 2026-11-07: never lose the question).
        A turn the user interrupted (same connection, newer generation) is not kept."""
        if self.sessions.get(sess.device.device_id) is sess and gen != sess.generation:
            return
        with contextlib.suppress(Exception):
            async for delta in deltas:
                if delta:
                    parts.append(delta)
        reply = "".join(parts).strip()
        if not reply:
            return
        current = self.sessions.get(sess.device.device_id)
        if current is not None and current is not sess:
            await self._start_turn(current, lambda g: self._say_turn(current, g, reply))
            return
        pending = self._offline_handoffs.setdefault(sess.device.device_id, deque(maxlen=3))
        now = time.monotonic()
        while pending and now - pending[0][0] > 900:
            pending.popleft()
        pending.append((now, reply))

    async def _emit(self, sess: Session, gen: int, message: dict[str, Any]) -> bool:
        if not self._live(sess, gen):
            return False
        await self._send(sess.ws, message)
        if message.get("type") == "transcript" and message.get("final"):
            role = message.get("role")
            if role == "user":
                self._mirror_user = str(message.get("text") or "")[-300:]
            elif role == "assistant":
                self._mirror_reply = str(message.get("text") or "")[-300:]
            self._refresh_mirror(sess)
        timing = sess.timing
        if timing is not None and timing.gen == gen:
            mtype = message.get("type")
            if mtype == "transcript" and message.get("role") == "assistant" and timing.first_text is None:
                timing.first_text = time.monotonic() - timing.start
            elif mtype == "audio_start" and timing.first_audio is None:
                timing.first_audio = time.monotonic() - timing.start
        return self._live(sess, gen)

    def _refresh_mirror(self, sess: Session) -> None:
        if sess.device.kind == "desktop":
            return
        self._mirror = {
            "device": sess.device.name,
            "screen": sess.screen,
            "user": self._mirror_user,
            "reply": self._mirror_reply,
            "at": time.time(),
        }
        self._write_live()

    async def set_screen(
        self,
        sess: Session,
        screen: str,
        gen: int | None = None,
        *,
        conversation_end: bool = False,
    ) -> None:
        if gen is not None and not self._live(sess, gen):
            return
        if sess.screen == screen and not conversation_end:
            return
        sess.screen = screen
        self._refresh_mirror(sess)
        msg = P.state(screen)
        if conversation_end:
            msg["conversation_end"] = True
        with contextlib.suppress(ConnectionClosed):
            await self._send(sess.ws, msg)

    async def _settle(self, sess: Session) -> None:
        """Say which screen the session is actually in, once nothing is running.

        Only the task that owns the turn slot emits ``idle`` from its own ``finally``, and an
        interrupt, a queued turn and a background hand-off result can land in any order. When two of
        them interleave, the last frame written can be ``thinking`` with no task left owning the slot
        to overwrite it: the watch sits on a spinner until the user touches it. So every terminal
        path asks the session where it is and says so. ``set_screen`` already drops a duplicate
        frame, which keeps this free when the ordinary path has just emitted ``idle``.
        """
        if not self._live(sess, sess.generation):
            return
        if self._turn_busy(sess) or sess.pending_turns:
            return
        await self.set_screen(sess, "listening" if sess.audio is not None else "idle")

    async def _session_loop(self, sess: Session) -> None:
        ws = sess.ws
        max_audio = int(self.config.max_audio_seconds * P.SAMPLE_RATE * 2)
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), self.config.idle_timeout)
            except asyncio.TimeoutError:
                log.info("device %s silent >%ss; dropping", sess.device.device_id, self.config.idle_timeout)
                await ws.close(P.CLOSE_IDLE, "idle timeout")
                return
            sess.last_seen = time.time()
            if isinstance(raw, bytes):
                if len(raw) % 2:
                    await self._send(ws, P.error("bad_frame"))
                elif sess.audio is None:
                    await self._send(ws, P.error("bad_frame"))
                elif len(sess.audio) + len(raw) > max_audio:
                    sess.audio = None
                    await self._send(ws, P.error("too_long"))
                    if not self._turn_busy(sess):
                        await self.set_screen(sess, "idle")
                else:
                    sess.audio.extend(raw)
                continue
            try:
                message = P.decode(raw)
            except P.ProtocolError as exc:
                if await self._protocol_error(ws, exc):
                    return
                continue
            await self._dispatch(sess, message)

    async def _dispatch(self, sess: Session, message: dict[str, Any]) -> None:
        kind = message["type"]
        if kind == "ping":
            await self._send(sess.ws, P.msg("pong", t=message.get("t")))
        elif kind == "text":
            text = message.get("text")
            if not isinstance(text, str) or not text.strip():
                await self._send(sess.ws, P.error("internal"))
                return
            if len(text) > 4000:
                await self._send(sess.ws, P.error("too_long"))
                return
            sess.audio = None
            make = lambda gen: self._text_turn(sess, gen, text)  # noqa: E731
            make.question = text
            await self._start_turn(sess, make)
        elif kind == "audio_start":
            if message.get("codec", P.CODEC) != P.CODEC or message.get("rate", P.SAMPLE_RATE) != P.SAMPLE_RATE:
                await self._send(sess.ws, P.error("bad_frame"))
                return
            sess.audio = bytearray()
            if not self._turn_busy(sess):
                await self.set_screen(sess, "listening")
        elif kind == "audio_end":
            # No open capture is not an error: a bare audio_end, or a second one
            # immediately after the first, does not start a turn.
            if sess.audio is None:
                return
            pcm = bytes(sess.audio)
            sess.audio = None
            if len(pcm) < MIN_UTTERANCE_BYTES:
                log.info("audio_end rejected as too_short: %d bytes", len(pcm))
                await self._send(sess.ws, P.error("too_short"))
                if not self._turn_busy(sess):
                    await self.set_screen(sess, "idle")
                return
            log.info("audio_end: %d bytes of speech received", len(pcm))
            await self._start_turn(sess, lambda gen: self._audio_turn(sess, gen, pcm))
        elif kind == "interrupt":
            sess.audio = None
            await self._interrupt(sess)
            await self.set_screen(sess, "idle")
        elif kind == "set_mode":
            await self._handle_set_mode(sess, message)
        elif kind in ("set_character", "character"):
            await self._handle_set_character(sess, message)
        elif kind == "list_sessions":
            await self._handle_list_sessions(sess, message)
        elif kind == "get_settings":
            await self._handle_get_settings(sess, message)
        elif kind == "set_setting":
            await self._handle_set_setting(sess, message)
        elif kind == "list_models":
            await self._handle_list_models(sess)
        elif kind == "recap":
            await self._handle_recap(sess, message)
        elif kind == "battery":
            # Proposed extension; unknown types are otherwise ignored.
            sess.battery = _battery(message)
        elif kind == "hello":
            await self._send(sess.ws, P.error("internal"))
        # Unknown types: ignore (protocol versioning rule).

    # -- watch-driven settings ------------------------------------------------
    WATCH_SETTING_KEYS = {
        "character": "character",
        "voice": "voice",
        "model": "model",
        "session": "session",
        "speak": "speak",
    }

    def _watch_presets(self) -> list[dict[str, str]]:
        from . import models as models_api

        saved = read_settings(self.store.dir)
        available = models_api.available_models()
        return models_api.preset_options(saved.get("watch_model_presets"), available)

    def _watch_settings_payload(self) -> dict[str, object]:
        saved = read_settings(self.store.dir)
        can_change = saved.get("watch_can_change")
        can_change = can_change if isinstance(can_change, dict) else {}
        return {
            "can_change": {k: bool(can_change.get(k, True)) for k in ("character", "voice", "model", "session", "speak")},
            "character": saved.get("character", ""),
            "voice_hermes": saved.get("voice_hermes", ""),
            "voice_nous": saved.get("voice_nous", ""),
            "model": saved.get("model", ""),
            "provider": saved.get("provider", ""),
            "voice_session": saved.get("voice_session", "new"),
            "pinned_session_id": saved.get("pinned_session_id", ""),
            "presets": self._watch_presets(),
            "rev": int(time.time()),
        }

    async def _handle_get_settings(self, sess: Session, message: dict[str, Any]) -> None:
        await self._send(sess.ws, P.msg("settings", req=message.get("req"), **self._watch_settings_payload()))

    async def _handle_list_models(self, sess: Session) -> None:
        await self._send(sess.ws, P.msg("models", items=self._watch_presets()))

    async def _handle_recap(self, sess: Session, message: dict[str, Any]) -> None:
        """Tell the watch what that session was about, from the store, without a model call."""
        from .recap import read_session_recap

        session_id = str(message.get("session_id") or "").strip()
        info = await asyncio.to_thread(read_session_recap, session_id)
        if info is None:
            await self._send(sess.ws, P.msg("recap", session_id=session_id, missing=True,
                                            line="That session is gone from this computer."))
            return
        await self._send(sess.ws, P.msg("recap", session_id=session_id, **info))
        # Say it, do not just write it: the whole point of the watch is that you hear it.
        # This uses the same path as `hail say` because speaking needs a real turn
        # generation — handing _drain_speech a made-up generation silently does nothing.
        try:
            if not self._turn_busy(sess):
                await self._start_turn(sess, lambda gen: self._say_turn(sess, gen, info["line"]))
        except Exception:
            log.warning("recap speech failed", exc_info=True)

    async def _handle_set_setting(self, sess: Session, message: dict[str, Any]) -> None:
        req = message.get("req")
        key = str(message.get("key") or "").strip()
        value = message.get("value")
        canonical = self.WATCH_SETTING_KEYS.get(key)
        if canonical is None:
            await self._send(sess.ws, P.msg("setting_error", req=req, code="bad_setting"))
            return
        saved = read_settings(self.store.dir)
        can_change = saved.get("watch_can_change")
        can_change = can_change if isinstance(can_change, dict) else {}
        if not bool(can_change.get(canonical, True)):
            await self._send(sess.ws, P.msg("setting_error", req=req, code="not_allowed"))
            return

        patch: dict[str, Any] = {}
        if canonical == "character":
            from .settings import CHARACTERS

            char_id = str(value or "").strip().lower()
            if char_id not in CHARACTERS:
                await self._send(sess.ws, P.msg("setting_error", req=req, code="bad_character"))
                return
            patch["character"] = char_id
        elif canonical == "voice":
            from .settings import CHARACTERS as _CHARS

            char = str(message.get("character") or "nous").strip().lower()
            if char != "hermes" and char not in _CHARS:
                await self._send(sess.ws, P.msg("setting_error", req=req, code="bad_value"))
                return
            # Whitelist the key: building it from the raw value let a client send
            # character="session" and write voice_session, slipping past the session gate.
            patch["voice_hermes" if char == "hermes" else "voice_nous"] = str(value or "")
        elif canonical == "model":
            presets = {f"{m['source']}::{m['id']}": m for m in self._watch_presets()}
            chosen = presets.get(str(value or ""))
            if chosen is None:
                await self._send(sess.ws, P.msg("setting_error", req=req, code="bad_value"))
                return
            if not chosen["id"]:
                # A provider without a concrete model is a broken pairing, not a shortcut.
                await self._send(sess.ws, P.msg("setting_error", req=req, code="bad_value"))
                return
            patch["provider"] = chosen["source"]
            patch["model"] = chosen["id"]
        elif canonical == "session":
            patch["voice_session"] = "pinned" if str(value or "") else "new"
            patch["pinned_session_id"] = str(value or "")
        elif canonical == "speak":
            patch["speak_replies"] = bool(value)

        if patch:
            try:
                update_settings(self.store.dir, patch)
            except ValueError:
                # A value the settings layer rejects must come back as a named error, or the
                # watch waits forever for a reply that never comes.
                await self._send(sess.ws, P.msg("setting_error", req=req, code="bad_value"))
                return
        payload = self._watch_settings_payload()
        await self._send(sess.ws, P.msg("settings_changed", req=req, key=canonical, value=value, **payload))

    def _effective_mode(self, device_id: str) -> tuple[str, str | None, str | None]:
        mode, session_id = self.store.device_mode(device_id)
        title = self.store.device_mode_title(device_id)
        if mode == "session":
            return "session", session_id, title
        if mode == "quick":
            return ("quick" if self.quick_agent is not None else "session"), None, None
        if self.quick_agent is not None:
            return "quick", None, None
        return "session", session_id, title

    def _mode_msg(self, device_id: str) -> dict[str, Any]:
        mode, session_id, title = self._effective_mode(device_id)
        fields: dict[str, Any] = {"mode": mode}
        if mode == "session":
            if session_id:
                fields["session_id"] = session_id
            if title:
                fields["title"] = title
        return P.msg("mode", **fields)

    def _slots_msg(self) -> dict[str, Any]:
        saved = read_settings(self.store.dir)
        raw_slots = saved.get("slots") or []
        items: list[dict[str, Any]] = []
        if isinstance(raw_slots, list):
            for idx, entry in enumerate(raw_slots[:3]):
                if not isinstance(entry, dict):
                    continue
                sid = str(entry.get("id") or "").strip()
                name = str(entry.get("name") or "").strip()
                title = str(entry.get("title") or "").strip()
                if not sid and not name:
                    continue
                slot_num = entry.get("slot") if isinstance(entry.get("slot"), int) else (idx + 1)
                items.append(
                    {
                        "slot": slot_num,
                        "id": sid,
                        "name": name or title,
                        "title": title or name,
                    }
                )
        return P.msg("slots", items=items)

    def _character_msg(self) -> dict[str, Any]:
        info = character_info(state_dir=self.store.dir)
        return P.msg("character", id=info["face"], face=info["face"])

    def _character_voice(self) -> str:
        return character_info(state_dir=self.store.dir)["voice"]

    async def _synthesize(self, text: str) -> bytes:
        assert self.tts is not None
        voice = self._character_voice()
        try:
            params = inspect.signature(self.tts.synthesize).parameters
        except (TypeError, ValueError):
            params = {}
        if "voice" in params or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        ):
            return await self.tts.synthesize(text, voice=voice)
        return await self.tts.synthesize(text)

    async def _apply_character(self, char_id: str, *, source_sess: Session | None = None) -> None:
        self._last_character = char_id
        self._suppress_slot_broadcast = True
        try:
            update_settings(self.store.dir, {"character": char_id})
        finally:
            self._suppress_slot_broadcast = False
        msg = self._character_msg()
        if source_sess is not None and source_sess.ws.state is State.OPEN:
            with contextlib.suppress(ConnectionClosed):
                await self._send(source_sess.ws, msg)
        for sess in list(self.sessions.values()):
            if sess is not source_sess and sess.modes and sess.ws.state is State.OPEN:
                with contextlib.suppress(ConnectionClosed):
                    await self._send(sess.ws, msg)

    async def _handle_set_character(self, sess: Session, message: dict[str, Any]) -> None:
        raw = message.get("id") or message.get("character") or message.get("name")
        cleaned = _clean_phrase(raw) if isinstance(raw, str) else ""
        char_id = "nous" if cleaned in ("nous", "nous girl") else ("hermes" if cleaned == "hermes" else None)
        if char_id is None:
            await self._send(sess.ws, P.error("bad_character"))
            return
        await self._apply_character(char_id, source_sess=sess)

    def _on_settings_updated(self, directory: Path, _settings: dict[str, Any]) -> None:
        if self._suppress_slot_broadcast or directory != self.store.dir.resolve():
            return
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(lambda: asyncio.create_task(self._sync_lan()))
            new_char = character_info(_settings)["id"]
            char_changed = new_char != getattr(self, "_last_character", None)
            self._last_character = new_char
            new_slots = _settings.get("slots") or []
            slots_changed = new_slots != getattr(self, "_last_slots", None)
            self._last_slots = list(new_slots)
            if char_changed:
                loop.call_soon_threadsafe(lambda: asyncio.create_task(self._broadcast_character()))
            if slots_changed or not char_changed:
                loop.call_soon_threadsafe(lambda: asyncio.create_task(self._broadcast_slots()))

    async def _reload_settings(self) -> dict[str, Any]:
        await self._sync_lan()
        return await self._broadcast_slots()

    async def _stop_lan(self) -> None:
        async with self._lan_lock:
            await self._close_lan()

    async def _close_lan(self) -> None:
        if self._beacon_task:
            self._beacon_task.cancel()
            await asyncio.gather(self._beacon_task, return_exceptions=True)
            self._beacon_task = None
        for server in self._lan_servers:
            server.close()
        for server in self._lan_servers:
            await server.wait_closed()
        self._lan_servers = []

    async def _sync_lan(self) -> None:
        async with self._lan_lock:
            enabled = read_settings(self.store.dir)["lan_enabled"]
            if not enabled or self._stopping:
                await self._close_lan()
                return
            if self._beacon_task or self._server is None:
                return
            if self.config.host not in ("0.0.0.0", "::"):
                for address in _lan_ipv4_addresses():
                    if address == self.config.host:
                        continue
                    try:
                        server = await serve(self._handle, address, self.port,
                            process_request=self._process_request, max_size=2**20)
                        self._lan_servers.append(server)
                    except OSError:
                        log.warning("Could not listen on LAN address %s", address, exc_info=True)
            self._beacon_task = asyncio.create_task(self._beacon_loop())

    async def _beacon_loop(self, sender=None, sleep=asyncio.sleep) -> None:
        if sender is None:
            sender = _send_beacon
        payload = json.dumps({"svc": "hail-hermes", "v": 1, "name": socket.gethostname(),
                              "port": self.port, "fp": hashlib.sha256(self.store.hub_id().encode()).hexdigest()[:12]}).encode()
        while True:
            try:
                sender(payload)
            except OSError:
                log.warning("Could not send discovery beacon", exc_info=True)
            await sleep(2)

    async def _broadcast_character(self) -> dict[str, Any]:
        msg = self._character_msg()
        for sess in list(self.sessions.values()):
            if sess.modes and sess.ws.state is State.OPEN:
                with contextlib.suppress(ConnectionClosed):
                    await self._send(sess.ws, msg)
        return {"ok": True, "id": msg["id"]}

    async def _broadcast_slots(self) -> dict[str, Any]:
        msg = self._slots_msg()
        for sess in list(self.sessions.values()):
            if sess.modes and sess.ws.state is State.OPEN:
                with contextlib.suppress(ConnectionClosed):
                    await self._send(sess.ws, msg)
        return {"ok": True, "items": msg["items"]}

    async def _fetch_sessions(self, limit: int = 30) -> list[dict[str, str]]:
        if hasattr(self.agent, "sessions"):
            raw = getattr(self.agent, "sessions")
            items = raw() if callable(raw) else raw
            return list(items)[:limit]
        binary = getattr(self.agent, "binary", None)
        env = getattr(self.agent, "env", None)
        return await list_hermes_sessions(limit=limit, binary=binary, env=env)

    async def _handle_list_sessions(self, sess: Session, message: dict[str, Any]) -> None:
        raw_limit = message.get("limit", 8)
        if isinstance(raw_limit, int) and not isinstance(raw_limit, bool):
            limit = max(1, min(30, raw_limit))
        else:
            limit = 8
        items = await self._fetch_sessions(limit=limit)
        with contextlib.suppress(ConnectionClosed):
            await self._send(sess.ws, P.msg("sessions", items=items))

    async def _handle_set_mode(self, sess: Session, message: dict[str, Any]) -> None:
        mode = message.get("mode")
        device_id = sess.device.device_id
        if mode == "quick":
            self.store.set_device_mode(device_id, "quick")
            await self._send(sess.ws, P.msg("mode", mode="quick"))
            return
        if mode == "session":
            session_id = message.get("session_id")
            slot_name = message.get("name")
            raw_slots = read_settings(self.store.dir).get("slots") or []
            if (not isinstance(session_id, str) or not session_id.strip()) and isinstance(slot_name, str) and slot_name.strip():
                matched = match_slot(slot_name, raw_slots if isinstance(raw_slots, list) else [])
                if matched is not None:
                    sess.voice_session_started = False
                    self.store.set_device_mode(device_id, "session", str(matched.get("id") or "").strip() or None, title=_slot_label(matched))
                    await self._send(sess.ws, self._mode_msg(device_id))
                    return
            if not isinstance(session_id, str) or not session_id.strip() or session_id.startswith("-"):
                await self._send(sess.ws, P.error("bad_session"))
                return
            session_id = session_id.strip()
            items = await self._fetch_sessions(limit=30)
            match = next((it for it in items if it.get("id") == session_id), None)
            slot_match = next((s for s in raw_slots if isinstance(s, dict) and s.get("id") == session_id), None) if isinstance(raw_slots, list) else None
            if match is None and slot_match is None and session_id != self.store.agent_session(device_id):
                await self._send(sess.ws, P.error("bad_session"))
                return
            title = ( _slot_label(slot_match) if slot_match else None) or (match.get("title") if match else None)
            self.store.set_device_mode(device_id, "session", session_id, title=title)
            await self._send(sess.ws, self._mode_msg(device_id))
            return
        await self._send(sess.ws, P.error("bad_session"))

    def _detach_turn(self, sess: Session) -> None:
        """A connection went away. Never lose the question: a running turn keeps going and its
        reply is queued for the device's next connection (see _emit); only queued work is dropped."""
        if self._turn_busy(sess):
            questions = [q for q in (getattr(m, "question", None) for m in sess.pending_turns) if q]
            if questions:
                kept = self._offline_questions.setdefault(sess.device.device_id, deque(maxlen=3))
                now = time.monotonic()
                kept.extend((now, q) for q in questions)
            sess.pending_turns.clear()
            sess.pending_say.clear()
        else:
            self._cancel_turn(sess)

    def _cancel_turn(self, sess: Session) -> None:
        """Drop queued work and invalidate anything this turn still wants to send."""
        sess.generation += 1
        sess.pending_say.clear()
        sess.pending_turns.clear()
        task = sess.turn
        if task is not None and not task.done():
            task.cancel()

    def _turn_busy(self, sess: Session) -> bool:
        return sess.turn is not None and not sess.turn.done()

    async def _interrupt(self, sess: Session) -> None:
        """Invalidate and cancel the current turn without waiting for its backend.

        Only a turn that is actually running is cancelled. An interrupt that arrives while the device is
        idle used to bump the generation and clear the queued turns anyway, which could silently swallow a
        background hand-off result that was launching in that same instant: the result's turn started
        with a stale generation, saw itself as dead, and said nothing at all — no speech, no text, and no
        ``idle`` frame for the watch to leave "thinking" on. Interrupting when nothing is running now
        leaves the session alone.
        """
        task = sess.turn
        running = task is not None and not task.done()
        if running and sess.delivery_pending:
            # A finished answer is being handed to a turn that has not reached the screen yet. Cancelling
            # it here loses the answer outright — no text, no audio, and no `idle` to leave "thinking" on.
            # Stop the speaking instead and let the text land.
            sess.stop_speech = True
            await self._log_turn(sess, "interrupted")
            await self._settle(sess)
            return
        if running:
            self._cancel_turn(sess)
            if sess.turn is task:
                sess.turn = None
            sess.interrupted_pending = True
            await self._log_turn(sess, "interrupted")
        # The dispatch path also sets idle, but a replacement turn may have started between the two;
        # settling here is what guarantees at least one idle after a cancelled turn.
        await self._settle(sess)

    async def _start_turn(self, sess: Session, make) -> None:
        if self._turn_busy(sess):
            if len(sess.pending_turns) >= 3:
                sess.pending_turns.popleft()
            sess.pending_turns.append(make)
            return
        self._launch_turn(sess, make)

    def _launch_turn(self, sess: Session, make) -> None:
        gen = sess.generation
        sess.turn = asyncio.create_task(self._guard(sess, gen, self._watchdog(sess, gen, make(gen))))

    async def _watchdog(self, sess: Session, gen: int, coro) -> None:
        """Run ``coro`` with the busy heartbeat and the turn ceiling.

        Re-sends the current screen with ``busy: true`` every ``HEARTBEAT_SECONDS``
        while the turn runs, and cancels the turn (the existing cancel path) if it
        exceeds ``TURN_CEILING_SECONDS``. Both stop the moment the turn ends.
        """
        loop = asyncio.get_running_loop()
        heartbeat: asyncio.Task | None = None
        ceiling: asyncio.TimerHandle | None = None
        turn_task = asyncio.current_task()
        assert turn_task is not None

        def hit_ceiling() -> None:
            if not turn_task.done():
                sess.ceiling_hit = True
                turn_task.cancel()

        try:
            heartbeat = asyncio.create_task(self._heartbeat_loop(sess, gen, turn_task))
            ceiling = loop.call_later(TURN_CEILING_SECONDS, hit_ceiling)
            sess.ceiling_handle = ceiling
            await coro
        finally:
            # No await here: an extra loop tick would reorder the end of the turn
            # against a hand-off finishing at the same moment.
            if ceiling is not None:
                ceiling.cancel()
            sess.ceiling_handle = None
            if heartbeat is not None:
                heartbeat.cancel()

    async def _heartbeat_loop(self, sess: Session, gen: int, turn_task: asyncio.Task) -> None:
        # The first heartbeat waits: the turn already announced itself, and the
        # message order of the opening screen must stay untouched.
        while not turn_task.done():
            await asyncio.sleep(HEARTBEAT_SECONDS)
            if turn_task.done():
                return
            await self._send_busy_state(sess, gen)

    async def _send_busy_state(self, sess: Session, gen: int) -> None:
        if not self._live(sess, gen):
            return
        with contextlib.suppress(ConnectionClosed):
            await self._send(sess.ws, P.state(sess.screen, busy=True))

    def _ceiling_hit(self, sess: Session, gen: int) -> bool:
        """True when the CancelledError came from the ceiling timer, not an interrupt."""
        return sess.ceiling_hit and self._live(sess, gen)

    async def _clear_ceiling(self, sess: Session) -> None:
        sess.ceiling_hit = False

    async def _guard(self, sess: Session, gen: int, coro) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            if self._ceiling_hit(sess, gen):
                # Turn ceiling: stop the backend through the cancel path and tell
                # the client with the plain error, then idle. The queue advances
                # in the finally below, so the next queued turn still runs.
                sess.ceiling_hit = False
                sess.pending_say.clear()
                with contextlib.suppress(ConnectionClosed):
                    await self._send(sess.ws, P.error("internal"))
                await self.set_screen(sess, "idle", gen)
            raise
        except ConnectionClosed:
            pass
        except Exception as exc:
            # Keep the child's words in the log but never in what the watch shows: the
            # protocol carries a code, and tests require the raw text stays out of it.
            log.error("turn failed for %s: %s", sess.device.device_id, getattr(exc, "detail", "") or exc)
            code = getattr(exc, "code", None)
            if isinstance(exc, AgentError):
                code = "agent_failed"
            if code not in ("agent_failed", "stt_failed", "tts_failed"):
                code = "internal"
            if self._live(sess, gen):
                sess.pending_say.clear()
                with contextlib.suppress(ConnectionClosed):
                    await self._send(sess.ws, P.error(code))
                await self._log_turn(sess, "error", gen=gen, code=code, detail=str(exc)[:400])
        finally:
            # Only the task occupying the slot may advance the queue. An
            # interrupted task can finish late after a replacement has started.
            if sess.turn is asyncio.current_task():
                sess.turn = None
                if self._live(sess, gen):
                    if sess.pending_turns:
                        self._launch_turn(sess, sess.pending_turns.popleft())
                    else:
                        # A capture may have started while this turn was running.
                        screen = "listening" if sess.audio is not None else "idle"
                        with contextlib.suppress(Exception):
                            await self.set_screen(sess, screen, gen)
            # A task that lost the slot (cancelled by an interrupt, or replaced) used to leave the
            # last frame as whatever it had reached. Ask the session where it is instead.
            with contextlib.suppress(Exception):
                await self._settle(sess)

    async def _audio_turn(self, sess: Session, gen: int, pcm: bytes) -> None:
        timing = _TurnTiming(gen=gen)
        sess.timing = timing
        try:
            await self.set_screen(sess, "thinking", gen)
            try:
                text = (await self.stt.transcribe(pcm)).strip()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                code = getattr(exc, "code", None)
                raise _TurnFailure(
                    code if code in ("stt_failed", "agent_failed", "tts_failed") else "stt_failed",
                    detail=getattr(exc, "detail", "") or str(exc),
                ) from exc
            timing.stt = time.monotonic() - timing.start
            log.info("stt result: %r (%.1fs)", text, timing.stt)
            if not self._live(sess, gen):
                return
            # Empty STT: never silent. Speak any queued device_say lines, then the sorry line.
            if not text:
                sess.pending_say.append(SORRY_REPLY)
                await self._drain_speech(sess, gen, "")
                log.info("turn timing: %s", timing.format())
                return
            # A single stray word is a mis-hear, not a question. Sending "you" to the agent
            # produced answers like "type it on the PC" — confusing and expensive.
            if _looks_like_fragment(text):
                log.info("ignoring fragment: %r", text)
                sess.pending_say.append(DIDNT_CATCH_REPLY)
                await self._drain_speech(sess, gen, "")
                log.info("turn timing: %s", timing.format())
                return
            await self._text_turn(sess, gen, text)
            log.info("turn timing: %s", timing.format())
        finally:
            if sess.timing is timing:
                sess.timing = None

    def _run_full_agent(
        self,
        device: DeviceInfo,
        text: str,
        *,
        session_id: str | None = None,
        new_session: bool = False,
        title: str | None = None,
    ) -> AsyncIterator[str]:
        run_fn = self.agent.run
        try:
            params = inspect.signature(run_fn).parameters
        except (TypeError, ValueError):
            params = {}
        kwargs: dict[str, Any] = {}
        if "session_id" in params:
            kwargs["session_id"] = session_id
        if "new_session" in params:
            kwargs["new_session"] = new_session
        if title:
            if "title" in params:
                kwargs["title"] = title
            elif "session_title" in params:
                kwargs["session_title"] = title
        return run_fn(device, text, **kwargs)

    def _resolve_voice_session(
        self, sess: Session, text: str, explicit_session_id: str | None = None
    ) -> tuple[str | None, bool, str | None]:
        if explicit_session_id:
            return explicit_session_id, False, None
        mode, _, mode_title = self._effective_mode(sess.device.device_id)
        saved = read_settings(self.store.dir)
        if mode == "session" and mode_title:
            raw_slots = saved.get("slots") or []
            slot = match_slot(mode_title, raw_slots) if isinstance(raw_slots, list) else None
            if slot is not None:
                slot_sid = str(slot.get("id") or "").strip()
                if slot_sid:
                    return slot_sid, False, None
                slot_name = _slot_label(slot) or mode_title
                if not sess.voice_session_started:
                    sess.voice_session_started = True
                    return None, True, f"Hail Hermes · {slot_name}"
                return None, False, None
            if not sess.voice_session_started:
                sess.voice_session_started = True
                return None, True, f"Hail Hermes · {mode_title}"
            return None, False, None
        voice_mode = saved.get("voice_session", "new")
        pinned = str(saved.get("pinned_session_id") or "").strip()
        if voice_mode == "pinned" and pinned:
            return pinned, False, None
        if not sess.voice_session_started:
            sess.voice_session_started = True
            name = character_label(character_info(saved)["id"])
            return None, True, f"Hail Hermes · {name}"
        return None, False, None

    def _bind_slot_session(self, sess: Session, slot_title: str | None, new_sid: str | None) -> None:
        if not slot_title or not new_sid:
            return
        raw_slots = read_settings(self.store.dir).get("slots") or []
        if not isinstance(raw_slots, list):
            return
        updated = False
        slots: list[dict[str, Any]] = []
        target_norm = _clean_phrase(slot_title)
        for s in raw_slots[:3]:
            if not isinstance(s, dict):
                continue
            entry = dict(s)
            if not entry.get("id") and _clean_phrase(_slot_label(entry)) == target_norm:
                entry["id"] = new_sid
                updated = True
            slots.append(entry)
        if updated:
            self._suppress_slot_broadcast = True
            try:
                update_settings(self.store.dir, {"slots": slots})
            finally:
                self._suppress_slot_broadcast = False
            self.store.set_device_mode(sess.device.device_id, "session", new_sid, title=slot_title)

    async def _send_mode_and_slots(self, sess: Session, *, mode_override: str | None = None) -> None:
        if mode_override == "quick":
            await self._send(sess.ws, P.msg("mode", mode="quick"))
        else:
            await self._send(sess.ws, self._mode_msg(sess.device.device_id))
        slots_msg = self._slots_msg()
        await self._send(sess.ws, slots_msg)
        for other in list(self.sessions.values()):
            if other is not sess and other.modes and other.ws.state is State.OPEN:
                with contextlib.suppress(ConnectionClosed):
                    await self._send(other.ws, slots_msg)

    async def _try_voice_command(self, sess: Session, text: str) -> str | None:
        cleaned = _clean_phrase(text)
        if not cleaned:
            return None

        if cleaned in (
            "back to quick ask",
            "quick ask",
            "switch to quick ask",
            "go to quick ask",
            "back to quick mode",
            "quick ask mode",
        ):
            sess.voice_session_started = False
            self.store.set_device_mode(sess.device.device_id, "quick")
            await self._send_mode_and_slots(sess, mode_override="quick")
            return "Switched to Quick ask."

        if cleaned in (
            "which session am i in",
            "what session am i in",
            "where am i",
            "which mode am i in",
            "what mode am i in",
        ):
            stored_mode, stored_sid = self.store.device_mode(sess.device.device_id)
            eff_mode, eff_sid, eff_title = self._effective_mode(sess.device.device_id)
            if stored_mode == "quick" or (stored_mode is None and eff_mode == "quick"):
                return "You are in Quick ask mode."
            name = eff_title
            if not name and (eff_sid or stored_sid):
                sid_lookup = eff_sid or stored_sid
                raw_slots = read_settings(self.store.dir).get("slots") or []
                if isinstance(raw_slots, list):
                    for s in raw_slots:
                        if isinstance(s, dict) and s.get("id") == sid_lookup:
                            name = _slot_label(s)
                            break
            label = name or eff_sid or stored_sid or "default"
            return f"You are in session mode: {label}."

        m_new = re.match(
            r"^[\W_]*(?:(?:create|start)\s+(?:a\s+)?)?new\s+session\s+(?:called|named)\s+(.+)$",
            text.strip(),
            re.IGNORECASE | re.DOTALL,
        )
        if m_new:
            raw_name = m_new.group(1).strip(" \t\r\n.!?,;:\"'()-")
            name = " ".join(raw_name.split())
            if name:
                raw_slots = read_settings(self.store.dir).get("slots") or []
                slots: list[dict[str, Any]] = []
                if isinstance(raw_slots, list):
                    for s in raw_slots[:3]:
                        if isinstance(s, dict) and (s.get("id") or s.get("name") or s.get("title")):
                            s_id = str(s.get("id") or "").strip()
                            s_name = str(s.get("name") or s.get("title") or "").strip()
                            s_title = str(s.get("title") or s_name).strip()
                            slots.append({"id": s_id, "name": s_name, "title": s_title})
                if len(slots) >= 3:
                    slots = slots[-2:]
                slots.append({"id": "", "name": name, "title": name})
                self._suppress_slot_broadcast = True
                try:
                    update_settings(self.store.dir, {"slots": slots})
                finally:
                    self._suppress_slot_broadcast = False
                sess.voice_session_started = False
                self.store.set_device_mode(sess.device.device_id, "session", None, title=name)
                await self._send_mode_and_slots(sess)
                return f"Switched to {name}."

        target_name: str | None = None
        m_open = re.match(r"^open\s+(.+?)\s+session$", cleaned)
        if m_open:
            target_name = m_open.group(1).strip()
        else:
            m_switch = re.match(r"^(?:switch\s+to|go\s+to)\s+(.+)$", cleaned)
            if m_switch:
                target_name = m_switch.group(1).strip()
        if target_name:
            if not m_open and target_name in ("nous", "nous girl", "hermes"):
                char_id = "nous" if target_name.startswith("nous") else "hermes"
                await self._apply_character(char_id, source_sess=sess)
                return f"Switched to {'Nous' if char_id == 'nous' else 'Hermes'}."
            raw_slots = read_settings(self.store.dir).get("slots") or []
            slot = match_slot(target_name, raw_slots if isinstance(raw_slots, list) else [])
            if slot is not None:
                slot_name = _slot_label(slot)
                slot_sid = str(slot.get("id") or "").strip() or None
                sess.voice_session_started = False
                self.store.set_device_mode(sess.device.device_id, "session", slot_sid, title=slot_name)
                await self._send_mode_and_slots(sess)
                return f"Switched to {slot_name}."
            names = [
                _slot_label(s)
                for s in (raw_slots if isinstance(raw_slots, list) else [])[:3]
                if isinstance(s, dict) and _slot_label(s)
            ]
            if names:
                return f"Unknown session. Available sessions: {', '.join(names)}."
            return "Unknown session. No named sessions available."

        m_rem = re.match(r"^[\W_]*remember\b[\s,:;-]*(.*)$", text.strip(), re.IGNORECASE | re.DOTALL)
        if m_rem:
            # Quality review, blocker: a session has the agent's own memory toolset, with an entry
            # format, a size budget and a safety scan. Intercepting "remember …" here swallowed the
            # request — the agent never saw it — and stored it where only quick mode looks. In session
            # mode the sentence goes to the agent untouched; the plugin's facts file is for quick mode,
            # which has no agent to hand it to.
            mode, _sid, _title = self._effective_mode(sess.device.device_id)
            if mode != "quick":
                return None
            rest = m_rem.group(1).strip()
            m_that = re.match(r"^that\b[\s,:;-]*(.*)$", rest, re.IGNORECASE | re.DOTALL)
            if m_that:
                rest = m_that.group(1).strip()
            fact = rest.strip(" \t\r\n.!?;,\"'")
            fact = " ".join(fact.split())[:300].strip()
            if fact:
                # The plugin's own file, never core's MEMORY.md (review item 3).
                append_fact(fact, self.store.dir)
                return "Got it, I'll remember that."

        return None

    async def _send_working(self, device_id: str, count: int) -> None:
        sess = self.sessions.get(device_id)
        if sess is not None and sess.modes and sess.ws.state is State.OPEN:
            with contextlib.suppress(ConnectionClosed):
                await self._send(sess.ws, P.msg("working", count=count))

    def _spawn_handoff(
        self,
        device: DeviceInfo,
        text: str,
        on_it_done: asyncio.Event,
    ) -> asyncio.Task:
        device_id = device.device_id
        count = self._handoff_counts.get(device_id, 0) + 1
        self._handoff_counts[device_id] = count
        task = asyncio.create_task(
            self._run_handoff(device, text, on_it_done, count))
        self._handoff_tasks.add(task)
        task.add_done_callback(self._handoff_tasks.discard)
        return task

    async def _run_handoff(
        self,
        device: DeviceInfo,
        text: str,
        on_it_done: asyncio.Event,
        count: int,
    ) -> None:
        device_id = device.device_id
        parts: list[str] = []
        failed = False
        try:
            sess = self.sessions.get(device_id)
            sid, is_new, title = (
                self._resolve_voice_session(sess, text) if sess is not None
                else (None, True, f"Hail Hermes · {character_label(character_info(state_dir=self.store.dir)['id'])}")
            )
            try:
                # A hung full agent must not leave the watch on "Working…" forever.
                async with asyncio.timeout(HANDOFF_AGENT_TIMEOUT_SECONDS):
                    async for delta in _filter_handoff_marker(
                        self._run_full_agent(
                            device, text, session_id=sid, new_session=is_new, title=title)
                    ):
                        if delta:
                            parts.append(delta)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("quick hand-off failed for %s", device_id)
                failed = True

            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(on_it_done.wait(), timeout=10.0)

            reply = "That didn't work." if failed else "".join(parts).strip()
            if not reply:
                return
            if not failed and self.quick_agent is not None:
                record = getattr(self.quick_agent, "record_turn", None)
                if callable(record):
                    record(device_id, text, reply)
            sess = self.sessions.get(device_id)
            if sess is None or sess.ws.state is not State.OPEN:
                pending = self._offline_handoffs.setdefault(device_id, deque(maxlen=3))
                now = time.monotonic()
                while pending and now - pending[0][0] > 900:
                    pending.popleft()
                pending.append((now, reply))
            else:
                sess.delivery_pending = True
                try:
                    await self._start_turn(sess, lambda g: self._say_turn(sess, g, reply))
                except BaseException:
                    sess.delivery_pending = False
                    raise
        finally:
            count = self._handoff_counts.get(device_id, 1) - 1
            if count:
                self._handoff_counts[device_id] = count
            else:
                self._handoff_counts.pop(device_id, None)
            await self._send_working(device_id, count)

    async def _text_turn(self, sess: Session, gen: int, text: str) -> None:
        if not self._live(sess, gen):
            return
        own_timing = sess.timing is None or sess.timing.gen != gen
        timing = _TurnTiming(gen=gen) if own_timing else sess.timing
        if own_timing:
            sess.timing = timing
        try:
            if not await self._emit(sess, gen, P.transcript("user", text, True)):
                return
            await self._log_turn(sess, "ask", gen=gen, source="device", text=text)
            if await self._answer_brief(sess, gen, text):
                # Answered from the local brief: no model, no network, well under a second.
                if own_timing and timing is not None:
                    log.info("turn timing: %s", timing.format())
                return
            if await self._answer_weather(sess, gen, text):
                # The weather fast path returns before the shared timing log below, which
                # left the audit with no hub number to compare for its weather turn.
                if own_timing and timing is not None:
                    log.info("turn timing: %s", timing.format())
                return
            if sess.interrupted_pending:
                # The owner stopped the last answer early. Say so, so the agent can offer
                # to resume instead of behaving as if nothing happened.
                sess.interrupted_pending = False
                text = (INTERRUPTED_NOTE + text)
            await self.set_screen(sess, "thinking", gen)
            if is_goodbye(text):
                sess.voice_session_started = False
                await self._drain_speech(sess, gen, GOODBYE_REPLY)
                if self._live(sess, gen):
                    await self.set_screen(sess, "idle", gen, conversation_end=True)
                return

            cmd_reply = await self._try_voice_command(sess, text)
            if cmd_reply is not None:
                await self._drain_speech(sess, gen, cmd_reply)
                return

            mode, session_id, mode_title = self._effective_mode(sess.device.device_id)
            if mode == "quick" and self.quick_agent is not None:
                agen = self.quick_agent.run(sess.device, text).__aiter__()
                prefix_buf = ""
                is_handoff = False
                try:
                    while True:
                        try:
                            delta = await agen.__anext__()
                        except StopAsyncIteration:
                            break
                        if not self._live(sess, gen):
                            await agen.aclose()
                            return
                        if not delta:
                            continue
                        prefix_buf += delta
                        stripped = prefix_buf.lstrip()
                        if stripped.startswith(HANDOFF_MARKER):
                            is_handoff = True
                            break
                        if stripped and not HANDOFF_MARKER.startswith(stripped):
                            break
                    if is_handoff:
                        await agen.aclose()
                except ConnectionClosed:
                    await agen.aclose()
                    raise
                except asyncio.CancelledError:
                    await agen.aclose()
                    raise
                except Exception as exc:
                    await agen.aclose()
                    code = getattr(exc, "code", None)
                    raise _TurnFailure(
                        code if code in ("stt_failed", "agent_failed", "tts_failed") else "agent_failed"
                    ) from exc

                if is_handoff:
                    on_it_done = asyncio.Event()
                    self._spawn_handoff(sess.device, text, on_it_done)
                    # Tell the watch Hermes is still working BEFORE "On it" ends, so it never looks idle/frozen.
                    await self._send_working(sess.device.device_id, self._handoff_counts.get(sess.device.device_id, 0))
                    try:
                        await self._drain_speech(sess, gen, "On it.")
                    finally:
                        on_it_done.set()
                    return
                deltas: AsyncIterator[str] = _filter_handoff_marker(_chain_prefix(prefix_buf, agen))
            else:
                sid, is_new, title = self._resolve_voice_session(sess, text, explicit_session_id=session_id)
                if hasattr(self.agent, "last_session_id"):
                    self.agent.last_session_id = None
                raw_stream = self._run_full_agent(
                    sess.device, text, session_id=sid, new_session=is_new, title=title
                )
                if mode == "session" and not sid and (title or mode_title):
                    async def _with_slot_bind(src: AsyncIterator[str], slot_lbl: str) -> AsyncIterator[str]:
                        try:
                            async for item in src:
                                yield item
                        finally:
                            self._bind_slot_session(sess, slot_lbl, getattr(self.agent, "last_session_id", None))

                    raw_stream = _with_slot_bind(raw_stream, mode_title or "")
                deltas = _filter_handoff_marker(raw_stream)

            if self.config.stream_speech and self.tts is not None:
                await self._streamed_text_turn(sess, gen, text, deltas)
                if own_timing and timing is not None:
                    log.info("turn timing: %s", timing.format())
                return
            parts: list[str] = []
            try:
                async for delta in deltas:
                    # Includes a delta yielded after the agent swallowed cancellation.
                    if not self._live(sess, gen):
                        if delta:
                            parts.append(delta)
                        await self._finish_offline(sess, gen, deltas, parts)
                        return
                    if not delta:
                        continue
                    parts.append(delta)
                    if not await self._emit(sess, gen, P.transcript("assistant", delta, False)):
                        await self._finish_offline(sess, gen, deltas, parts)
                        return
            except ConnectionClosed:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                code = getattr(exc, "code", None)
                raise _TurnFailure(
                    code if code in ("stt_failed", "agent_failed", "tts_failed") else "agent_failed",
                    detail=getattr(exc, "detail", "") or str(exc),
                ) from exc
            if not self._live(sess, gen):
                return
            reply = "".join(parts).strip()
            await self._drain_speech(sess, gen, reply)
            if own_timing and timing is not None:
                log.info("turn timing: %s", timing.format())
        finally:
            if own_timing and sess.timing is timing:
                sess.timing = None

    async def _streamed_text_turn(
        self,
        sess: Session,
        gen: int,
        text: str,
        deltas: AsyncIterator[str] | None = None,
    ) -> None:
        parts: list[str] = []
        buf = ""
        text_q: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue()
        pcm_q: asyncio.Queue[tuple[str, bytes] | None] = asyncio.Queue()
        synth_task: asyncio.Task | None = None
        play_task: asyncio.Task | None = None
        started_reply_speech = False
        if deltas is None:
            deltas = self._run_full_agent(sess.device, text)

        def ensure_pipeline() -> None:
            nonlocal synth_task, play_task, started_reply_speech
            if started_reply_speech:
                return
            started_reply_speech = True
            if sess.pending_say:
                pre_say = sess.pending_say
                sess.pending_say = []
                for item in pre_say:
                    if item.strip():
                        text_q.put_nowait(("say", item))
            synth_task = asyncio.create_task(self._synth_worker(sess, gen, text_q, pcm_q))
            play_task = asyncio.create_task(self._play_worker(sess, gen, pcm_q))

        try:
            try:
                async for delta in deltas:
                    if not self._live(sess, gen):
                        if delta:
                            parts.append(delta)
                        await self._finish_offline(sess, gen, deltas, parts)
                        return
                    if not delta:
                        continue
                    parts.append(delta)
                    if not await self._emit(sess, gen, P.transcript("assistant", delta, False)):
                        await self._finish_offline(sess, gen, deltas, parts)
                        return
                    buf += delta
                    ready, buf = _split_sentences(buf, final=False)
                    for chunk in ready:
                        ensure_pipeline()
                        text_q.put_nowait(("reply", chunk))
            except ConnectionClosed:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                code = getattr(exc, "code", None)
                raise _TurnFailure(
                    code if code in ("stt_failed", "agent_failed", "tts_failed") else "agent_failed",
                    detail=getattr(exc, "detail", "") or str(exc),
                ) from exc

            if not self._live(sess, gen):
                return
            reply = "".join(parts).strip()
            if not started_reply_speech:
                await self._drain_speech(sess, gen, reply)
                return

            tail, _ = _split_sentences(buf, final=True)
            for chunk in tail:
                text_q.put_nowait(("reply", chunk))
            text_q.put_nowait(None)

            if reply and not await self._emit(sess, gen, P.transcript("assistant", reply, True)):
                return
            if synth_task is not None:
                await synth_task
            if play_task is not None:
                await play_task
            if not self._live(sess, gen):
                return
            await self._drain_post_reply_say(sess, gen, reply)
        finally:
            for task in (synth_task, play_task):
                if task is not None and not task.done():
                    task.cancel()
            for task in (synth_task, play_task):
                if task is not None:
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task

    async def _synth_worker(
        self,
        sess: Session,
        gen: int,
        text_q: asyncio.Queue[tuple[str, str] | None],
        pcm_q: asyncio.Queue[tuple[str, bytes] | None],
    ) -> None:
        try:
            while self._live(sess, gen):
                item = await text_q.get()
                if item is None:
                    break
                kind, chunk = item
                if not chunk or not self._live(sess, gen) or self.tts is None:
                    continue
                try:
                    pcm = await self._synthesize(chunk)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    code = getattr(exc, "code", None)
                    raise _TurnFailure(
                        code if code in ("stt_failed", "agent_failed", "tts_failed") else "tts_failed"
                    ) from exc
                if pcm and self._live(sess, gen):
                    await pcm_q.put((kind, pcm))
        finally:
            pcm_q.put_nowait(None)

    async def _play_worker(
        self,
        sess: Session,
        gen: int,
        pcm_q: asyncio.Queue[tuple[str, bytes] | None],
    ) -> None:
        opened = False
        start = 0.0
        frame_idx = 0
        try:
            while self._live(sess, gen):
                item = await pcm_q.get()
                if item is None:
                    break
                kind, pcm = item
                if not pcm or not self._live(sess, gen):
                    continue
                if kind == "say":
                    await self._send_pcm_envelope(sess, gen, pcm)
                    continue
                if not opened:
                    await self.set_screen(sess, "speaking", gen)
                    if not await self._emit(sess, gen, P.audio_start()):
                        return
                    opened = True
                    start = time.monotonic()
                else:
                    start = max(start, time.monotonic() - frame_idx * P.FRAME_SECONDS)
                for frame in P.frames(pcm):
                    if not self._live(sess, gen):
                        return
                    await sess.ws.send(frame)
                    frame_idx += 1
                    if self.config.audio_pacing:
                        ahead = frame_idx * P.FRAME_SECONDS - (time.monotonic() - start) - 0.1
                        if ahead > 0:
                            await asyncio.sleep(ahead)
        finally:
            if opened and self._live(sess, gen):
                with contextlib.suppress(ConnectionClosed):
                    await self._send(sess.ws, P.audio_end())

    async def _drain_post_reply_say(self, sess: Session, gen: int, reply: str) -> None:
        reply_key = reply.strip()
        matched_reply = False
        while sess.pending_say and self._live(sess, gen):
            batch = sess.pending_say
            sess.pending_say = []
            for text in batch:
                key = text.strip()
                if not key:
                    continue
                if reply_key and key == reply_key and not matched_reply:
                    matched_reply = True
                    continue
                await self._speak_audio(sess, gen, text)
                if not self._live(sess, gen):
                    return

    async def _answer_brief(self, sess: Session, gen: int, text: str) -> bool:
        """Answer schedule / inbox / "what was I doing" from the local brief, with no model.

        A watch answer that takes fifteen seconds is not an answer. These questions have a known,
        small answer already on disk, so they are spoken in well under a second — the same shape as
        the weather fast path, and the reason the demo feels instant.
        """
        from . import brief as brief_api

        try:
            sentence = brief_api.answer(text, self.store.dir)
        except Exception:
            log.warning("brief answer failed", exc_info=True)
            return False
        if not sentence:
            return False
        await self._drain_speech(sess, gen, sentence)
        await self._log_turn(sess, "answer", gen=gen, source="brief", text=sentence)
        return True

    async def _answer_weather(self, sess: Session, gen: int, text: str) -> bool:
        """Answer a weather question from Open-Meteo instead of waking the agent.

        A forecast is one GET; a spoken agent turn is seconds of model time. The owner asked for
        "use my watch for the weather", so the fast path has to be genuinely fast.
        """
        from . import weather as weather_api

        if not weather_api.is_weather_question(text):
            return False
        # A town named in the question wins over the saved home town. Live probe: "what is the
        # weather in Denver right now?" was answered from home, or not at all.
        asked = weather_api.extract_place(text)
        if asked:
            named = await asyncio.to_thread(weather_api.geocode_place, asked)
            if not named:
                return False  # unknown town or slow lookup: the agent answers
            sentence = await asyncio.to_thread(
                weather_api.describe_weather,
                float(named["lat"]),
                float(named["lon"]),
                str(named.get("label") or asked),
            )
            if not sentence:
                return False  # unusable forecast: let the agent answer properly
            await self._drain_speech(sess, gen, sentence)
            await self._log_turn(sess, "answer", gen=gen, source="weather", text=sentence)
            return True
        saved = read_settings(self.store.dir) or {}
        # A pinned project session does NOT skip the weather path: a forecast is a device fact,
        # not project work, and skipping it sent "what's the weather" to the agent, which answered
        # "On it." and stopped. Observed live on the owner's watch with voice_session=pinned.
        place = saved.get("home_place")
        place = place if isinstance(place, dict) else {}
        lat, lon = place.get("lat"), place.get("lon")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            # Nothing set yet: work it out from the network once, then remember it. Nobody
            # should have to type their town into a watch.
            place = await asyncio.to_thread(weather_api.detect_home_place) or {}
            lat, lon = place.get("lat"), place.get("lon")
            if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
                try:
                    update_settings(self.store.dir, {"home_place": place})
                    log.info("home place detected as %s", place.get("label"))
                except Exception:
                    log.warning("home place detected but not saved", exc_info=True)
            else:
                return False
        # A forecast does not change between two questions a minute apart, and the network call is
        # the entire 5-6 s cost of a weather turn. Ten minutes is short enough to stay honest.
        cache_key = (round(float(lat), 2), round(float(lon), 2))
        cached = self._weather_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < 600:
            sentence = cached[1]
        else:
            sentence = await asyncio.to_thread(
                weather_api.describe_weather, float(lat), float(lon), str(place.get("label") or "")
            )
            if sentence:
                self._weather_cache[cache_key] = (time.monotonic(), sentence)
        if not sentence:
            return False  # unusable forecast: let the agent answer properly
        await self._drain_speech(sess, gen, sentence)
        await self._log_turn(sess, "answer", gen=gen, source="weather", text=sentence)
        return True

    async def _say_turn(self, sess: Session, gen: int, text: str) -> None:
        if not self._live(sess, gen):
            return
        try:
            await self._log_turn(sess, "ask", gen=gen, source="say", text=text)
            await self._drain_speech(sess, gen, text)
            await self._log_turn(sess, "answer", gen=gen, source="say", text=text)
        finally:
            sess.delivery_pending = False
            sess.stop_speech = False

    async def _drain_speech(self, sess: Session, gen: int, reply: str) -> None:
        """Publish one assistant final and speak queued lines without overlap.

        A queued line matching the reply accounts for the reply's audio once.
        Repeated tool calls remain separate utterances.
        """
        reply_key = reply.strip()
        batch = sess.pending_say
        sess.pending_say = []

        # Streaming deltas belong to the reply, so only the reply can finalize
        # that transcript. With no reply, expose the initially queued speech in
        # a single final transcript.
        transcript = reply if reply_key else "\n".join(text for text in batch if text.strip())
        if transcript and not await self._emit(sess, gen, P.transcript("assistant", transcript, True)):
            return
        if transcript:
            await self._log_turn(sess, "answer", gen=gen, source="agent", text=transcript)

        reply_spoken = False
        matched_reply = False
        while self._live(sess, gen) and not sess.stop_speech:
            # An interrupt during a delivery wants silence, not the loss of the text above.
            if sess.stop_speech:
                return
            for text in batch:
                key = text.strip()
                if not key:
                    continue
                if reply_key and key == reply_key:
                    if reply_spoken and not matched_reply:
                        # The reply already supplied this one tool call's audio.
                        matched_reply = True
                        continue
                    matched_reply = True
                await self._speak_audio(sess, gen, text)
                if not self._live(sess, gen):
                    return

            if reply_key and not reply_spoken and not matched_reply and not sess.stop_speech:
                await self._speak_audio(sess, gen, reply)
                if not self._live(sess, gen):
                    return
                reply_spoken = True
            elif matched_reply:
                reply_spoken = True

            if not sess.pending_say:
                return
            batch = sess.pending_say
            sess.pending_say = []

    async def _speak_audio(self, sess: Session, gen: int, text: str) -> None:
        if self.tts is None or not text or not self._live(sess, gen):
            return
        # Speak long text in sentence-sized pieces. One huge synthesis request is slow,
        # arrives as a single blob, and a failure loses the whole answer; pieces start
        # playing sooner and survive a chunk failing.
        for piece in _speech_pieces(text):
            if not piece or not self._live(sess, gen):
                continue
            await self._speak_piece(sess, gen, piece)

    async def _synthesize_remembered(self, text: str, voice: str) -> bytes:
        """Synthesize once per (voice, text) and keep the audio on disk.

        Synthesis is the whole remaining wait: for a brief answer the text is ready in 0 ms and the
        first audio arrives about 700 ms later, all of it text-to-speech. The demo asks the same few
        questions, so the second time one is asked it should be near-instant.
        """
        key = hashlib.sha1((voice + "|" + text).encode("utf-8")).hexdigest()[:32]
        cache_dir = Path(self.store.dir) / "tts-cache"
        path = cache_dir / (key + ".pcm")
        try:
            if path.is_file():
                return path.read_bytes()
        except OSError as exc:
            log.debug("tts cache read failed for %s: %s", path, exc)
        pcm = await self._synthesize(text)
        if pcm:
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                path.write_bytes(pcm)
                # Bounded: keep the newest 200 clips so a long-lived hub cannot fill the disk.
                clips = sorted(cache_dir.glob("*.pcm"), key=lambda f: f.stat().st_mtime)
                if len(clips) > 200:
                    for old_file in clips[:50]:
                        old_file.unlink(missing_ok=True)
            except OSError as exc:
                log.debug("tts cache trim failed in %s: %s", cache_dir, exc)
        return pcm

    async def _speak_piece(self, sess: Session, gen: int, text: str) -> None:
        try:
            pcm = await self._synthesize_remembered(text, self._character_voice())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            detail = getattr(exc, "detail", None)
            if detail:
                log.error("turn failed for %s: %s", sess.device.device_id, detail)
            code = getattr(exc, "code", None)
            raise _TurnFailure(code if code in ("stt_failed", "agent_failed", "tts_failed") else "tts_failed") from exc
        if not pcm or not self._live(sess, gen):
            return
        await self._send_pcm_envelope(sess, gen, pcm)

    async def _send_pcm_envelope(self, sess: Session, gen: int, pcm: bytes) -> None:
        opened = False
        try:
            await self.set_screen(sess, "speaking", gen)
            # Audio is flowing, so the turn is no longer late: a long answer must not be
            # guillotined at the 120 s ceiling while it is being spoken. From here the
            # watch's own 12 s audio-silence check is what protects against a dead stream.
            handle = getattr(sess, "ceiling_handle", None)
            if handle is not None:
                handle.cancel()
                sess.ceiling_handle = None
            if not await self._emit(sess, gen, P.audio_start()):
                return
            opened = True
            start = time.monotonic()
            for i, frame in enumerate(P.frames(pcm)):
                if not self._live(sess, gen):
                    break
                await sess.ws.send(frame)
                if self.config.audio_pacing:
                    # Stay ~100 ms ahead of real time so the client buffer never starves.
                    ahead = (i + 1) * P.FRAME_SECONDS - (time.monotonic() - start) - 0.1
                    if ahead > 0:
                        await asyncio.sleep(ahead)
        finally:
            if opened and self._live(sess, gen):
                with contextlib.suppress(ConnectionClosed):
                    await self._send(sess.ws, P.audio_end())

    # -- device-tool operations (used by the control channel) -----------------
    def _target(self, device_id: str | None, cap: str | None) -> Session:
        candidates = [s for s in self.sessions.values() if cap is None or s.device.caps.get(cap)]
        if device_id:
            sess = self.sessions.get(device_id)
            if sess is None:
                raise LookupError(f"device {device_id!r} is not connected")
            if cap and not sess.device.caps.get(cap):
                raise LookupError(f"device {device_id!r} does not support {cap!r}")
            return sess
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            raise LookupError("no connected device" + (f" supports {cap!r}" if cap else ""))
        raise LookupError("several devices connected; pass device_id: "
                          + ", ".join(s.device.device_id for s in candidates))

    async def notify(self, device_id: str | None, title: str, body: str) -> dict[str, Any]:
        sess = self._target(device_id, "push")
        await self._send(sess.ws, P.msg("notify", title=title, body=body))
        return {"device_id": sess.device.device_id, "delivered": True}

    async def haptic(self, device_id: str | None, pattern: str) -> dict[str, Any]:
        sess = self._target(device_id, "haptic")
        await self._send(sess.ws, P.msg("haptic", pattern=pattern))
        return {"device_id": sess.device.device_id, "pattern": pattern}

    async def say(self, device_id: str | None, text: str) -> dict[str, Any]:
        sess = self._target(device_id, None)
        # From inside the running turn (the agent called device_say), queue and return.
        # Waiting on sess.turn here would deadlock: that turn is blocked in this call.
        # The turn drains the queue before idle, so the utterance is not spoken over
        # the reply and not after state=idle. Interrupt clears the queue.
        if self._turn_busy(sess):
            sess.pending_say.append(text)
            return {"device_id": sess.device.device_id, "queued": True, "audio": self.tts is not None}
        await self._start_turn(sess, lambda gen: self._say_turn(sess, gen, text))
        return {"device_id": sess.device.device_id, "queued": False, "audio": self.tts is not None}

    async def battery(self, device_id: str | None) -> dict[str, Any]:
        sess = self._target(device_id, "battery")
        if sess.battery is None:
            return {"device_id": sess.device.device_id, "mv": None, "charging": None,
                    "note": "device has not reported battery yet"}
        return {"device_id": sess.device.device_id, **sess.battery}

    def devices(self) -> list[dict[str, Any]]:
        return [{"device_id": s.device.device_id, "name": s.device.name, "kind": s.device.kind,
                 "caps": s.device.caps, "screen": s.screen} for s in self.sessions.values()]

    def _turn_log_path(self) -> Path:
        return self.store.dir / "turns.jsonl"

    async def _log_turn(self, sess: Session, event: str, **fields: Any) -> None:
        """Append one auditable ask/answer/error line to ``<state>/turns.jsonl``.

        One JSON object per line, so ``tail -f`` and jq both work. Fire-and-forget
        on purpose: an audit write must never delay or reorder a turn, and a log
        failure must never break one.
        """
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "event": event,
            "device": sess.device.name,
            "device_id": sess.device.device_id,
            "kind": sess.device.kind,
            **fields,
        }
        try:
            task = asyncio.create_task(asyncio.to_thread(_append_jsonl, self._turn_log_path(), record))
        except RuntimeError:  # no running loop (tests, shutdown)
            return
        self._log_tasks.add(task)
        task.add_done_callback(self._log_tasks.discard)

    # -- control channel --------------------------------------------------------
    async def _handle_control(self, ws: ServerConnection) -> None:
        ops = {
            "devices": lambda a: self.devices(),
            "notify": lambda a: self.notify(a.get("device_id"), str(a.get("title", "")), str(a.get("body", ""))),
            "say": lambda a: self.say(a.get("device_id"), str(a.get("text", ""))),
            "haptic": lambda a: self.haptic(a.get("device_id"), str(a.get("pattern", "short"))),
            "battery": lambda a: self.battery(a.get("device_id")),
            "pair_list": lambda a: self._pair_list(),
            "pair_approve": lambda a: self._pair_decide(a, "approve"),
            "pair_deny": lambda a: self._pair_decide(a, "deny"),
            "revoke": lambda a: self._revoke(a.get("device_id")),
            "reload_settings": lambda a: self._reload_settings(),
            "sessions": lambda a: self._fetch_sessions(limit=int(a.get("limit", 30))),
        }
        with contextlib.suppress(ConnectionClosed):
            async for raw in ws:
                req_id = None
                try:
                    req = json.loads(raw)
                    if not isinstance(req, dict):
                        raise ValueError("request must be an object")
                    req_id = req.get("id")
                    args = req.get("args") or {}
                    if not isinstance(args, dict):
                        raise ValueError("args must be an object")
                    fn = ops[req["op"]]
                    result = fn(args)
                    if asyncio.iscoroutine(result):
                        result = await result
                    if isinstance(result, dict):
                        result = {k: v for k, v in result.items() if not k.startswith("_")}
                    reply = {"id": req_id, "ok": True, "result": result}
                except (ValueError, LookupError) as exc:
                    reply = {"id": req_id, "ok": False, "error": str(exc)}
                except (KeyError, TypeError):
                    reply = {"id": req_id, "ok": False, "error": "Invalid request or target"}
                except Exception:
                    log.exception("control request failed")
                    reply = {"id": req_id, "ok": False, "error": "Internal server error"}
                await ws.send(json.dumps(reply))


_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def _allowed_device_origin(origin: str) -> bool:
    """Whether a browser or desktop origin may open the device endpoint.

    Quality review: this used to allow *any* scheme it did not recognise, so "untrusted origin" only
    meant two literal spellings — `file://`, `data:`, `blob:`, `chrome-extension:` and anything else all
    walked in. The accepted set is explicit now:

    * a loopback http(s) page with an explicit port — the pane in a browser, a dev server;
    * `file://` — the packaged Electron renderer, which no remote page can forge;
    * no Origin at all — the watch, `adb`, a script (handled by the caller: an absent header is allowed).

    Everything else is refused, including `null` (a sandboxed frame, a data: URL), remote http(s) pages
    and unknown schemes.

    The origin is a speed bump, not the lock. What actually guards the hub is the owner's pairing
    approval and the per-device token afterwards; a desktop origin we cannot attest to still has to be
    approved in the panel before it can do anything.
    """
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    scheme = parsed.scheme.lower()
    if scheme in ("http", "https"):
        try:
            port = parsed.port
        except ValueError:
            # A malformed port ("http://127.0.0.1:8765.evil.example") is not a loopback page.
            return False
        return parsed.hostname in _LOOPBACK_HOSTS and port is not None
    if scheme == "file":
        # `file:///path` has no host; `file://host/path` would name a share, which is not the app.
        return parsed.hostname in (None, "", "localhost")
    return False


def _battery(raw: dict[str, Any]) -> dict[str, Any]:
    mv = raw.get("mv")
    return {"mv": int(mv) if isinstance(mv, (int, float)) else None, "charging": bool(raw.get("charging"))}


def _normalize_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    ip = ipaddress.ip_address(host.split("%", 1)[0])
    mapped = getattr(ip, "ipv4_mapped", None)
    return mapped if mapped is not None else ip


def _speech_pieces(text: str) -> list[str]:
    """Split speech into sentence-sized pieces, capping how long a reply may talk.

    Long answers are read as a summary plus a pointer to the screen. Reading a
    wall of text aloud is the one thing the owner cannot stop fast enough on a
    watch, so the default is short and the full text stays on screen.
    """
    flat = " ".join(text.split())
    if not flat:
        return []
    if len(flat) > MAX_SPOKEN_CHARS:
        cut = flat[:MAX_SPOKEN_CHARS]
        end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
        if end >= 120:
            cut = cut[: end + 1]
        flat = f"{cut.strip()} …That is the short version. The full answer is on your screen."
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", flat) if s.strip()]
    pieces: list[str] = []
    current = ""
    for sentence in sentences:
        if current and len(current) + len(sentence) + 1 > SPEECH_CHUNK_CHARS:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        pieces.append(current)
    return pieces


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)


_FILLERS = {
    "you", "i", "a", "the", "and", "uh", "um", "oh", "okay", "ok", "yeah", "yes", "no",
    "it", "is", "to", "of", "so", "me", "my", "we", "he", "she", "they", "but", "for",
}
DIDNT_CATCH_REPLY = "I only caught part of that — say it again."


def _looks_like_fragment(text: str) -> bool:
    """True when speech-to-text produced noise rather than a question.

    One word, or two filler words, is a mis-hear. Answering it costs a model call and makes
    the assistant sound like it is talking nonsense — "you" once produced "type it on the PC".
    """
    words = [w for w in (text or "").lower().replace("’", "'").split() if w.strip(".,!?;:")]
    if not words:
        return True
    if len(words) == 1:
        # "weather?" or "stop" are real questions even with one word.
        return words[0].strip(".,!?;:") not in {"weather", "time", "news", "stop", "cancel", "help", "battery"}
    if len(words) == 2 and all(w in _FILLERS for w in words):
        return True
    return False


def _is_private(address: str) -> bool:
    """True for loopback and RFC1918/LAN addresses — the owner's own network."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback


def _remote_ip(addr: Any) -> str:
    try:
        host = addr[0] if isinstance(addr, tuple) else str(addr)
        return str(_normalize_ip(host))
    except (ValueError, IndexError, TypeError):
        return ""


def _is_loopback(addr: Any) -> bool:
    try:
        host = addr[0] if isinstance(addr, tuple) else str(addr)
        return _normalize_ip(host).is_loopback
    except (ValueError, IndexError, TypeError):
        return False


async def _chain_prefix(prefix: str, agen: AsyncIterator[str]) -> AsyncIterator[str]:
    try:
        if prefix:
            yield prefix
        async for item in agen:
            if item:
                yield item
    finally:
        if hasattr(agen, "aclose"):
            await agen.aclose()  # type: ignore[attr-defined]


async def _filter_handoff_marker(source: AsyncIterator[str]) -> AsyncIterator[str]:
    """Strip ``[[HERMES]]`` from a delta stream even when split across deltas."""
    carry = ""
    marker = HANDOFF_MARKER
    async for chunk in source:
        data = carry + chunk
        carry = ""
        if marker in data:
            data = data.replace(marker, "")
        for k in range(min(len(marker) - 1, len(data)), 0, -1):
            if data.endswith(marker[:k]):
                carry = data[-k:]
                data = data[:-k]
                break
        if data:
            yield data
    if carry and carry != marker:
        yield carry
