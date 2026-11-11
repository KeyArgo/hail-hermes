import argparse
import asyncio
import contextlib
import hmac
import ipaddress
import json
import logging
import secrets
import time

import websockets
from websockets.exceptions import ConnectionClosed

from .protocol import decode, encode, msg

log = logging.getLogger("pocket_sim.hub")

CHUNK = 640
CONTROL_PATH = "/control"
DEFAULT_PATH = "/pocket"

CLOSE_VERSION = 4000
CLOSE_REPLACED = 4001
CLOSE_IDLE = 4002
CLOSE_PAIRING = 4003
CLOSE_BAD_HELLO = 4004
CLOSE_REVOKED = 4005

KINDS = ("phone", "wear", "desktop", "sim")

ERROR_MESSAGES = {
    "version": "Protocol version mismatch. Expected v2.",
    "bad_frame": "Audio frame length must be even.",
    "too_long": "Utterance exceeds maximum length limit.",
    "not_paired": "Device is not paired or pairing is pending.",
    "pair_timeout": "Pairing request expired.",
    "busy": "Maximum pending pairing requests reached.",
    "denied": "Pairing request was denied by the owner.",
    "revoked": "Device token has been revoked.",
    "stt_failed": "Speech-to-text processing failed.",
    "agent_failed": "Agent execution encountered an error.",
    "tts_failed": "Text-to-speech synthesis failed.",
    "internal": "Internal server error.",
}

PAIR_TIMEOUT = 300.0
MAX_PENDING = 8
MAX_PENDING_PER_IP = 3


def error_message(code: str) -> dict:
    if code not in ERROR_MESSAGES:
        code = "internal"
    return msg("error", code=code, message=ERROR_MESSAGES[code])


class Device:
    def __init__(self, device_id, name, kind, caps, ws):
        self.device_id = device_id
        self.name = name
        self.kind = kind
        self.caps = caps or {}
        self.ws = ws
        self.token = None
        self.state = "idle"
        self.last_seen = time.time()
        self.paired = False


class PendingPair:
    __slots__ = (
        "ws",
        "device_id",
        "name",
        "kind",
        "caps",
        "request_id",
        "match_code",
        "remote",
        "created",
        "deadline",
        "decision",
    )

    def __init__(
        self,
        ws,
        device_id,
        name,
        kind,
        caps,
        request_id,
        match_code,
        remote,
        created,
        deadline,
        decision,
    ):
        self.ws = ws
        self.device_id = device_id
        self.name = name
        self.kind = kind
        self.caps = caps
        self.request_id = request_id
        self.match_code = match_code
        self.remote = remote
        self.created = created
        self.deadline = deadline
        self.decision = decision


class Hub:
    def __init__(
        self,
        host="127.0.0.1",
        port=8767,
        echo_replies=None,
        stt_text="[audio]",
        tts=True,
        auto_approve=False,
        pair_timeout=PAIR_TIMEOUT,
        control_token=None,
    ):
        self.host = host
        self.port = port
        self.echo_replies = echo_replies or ["Hello from hub."]
        self.stt_text = stt_text
        self.tts = tts
        # SIMULATOR ONLY: auto-approve pending pairing (never use in the real plugin hub).
        self.auto_approve = auto_approve
        self.pair_timeout = pair_timeout
        self.devices = {}
        self.tokens = {}
        self.revoked = set()
        self.pending = {}
        self._control_token = control_token or secrets.token_urlsafe(32)
        self.server = None

    @property
    def control_token(self) -> str:
        return self._control_token

    def _ws_path(self, ws) -> str:
        if hasattr(ws, "request"):
            return ws.request.path.split("?", 1)[0]
        path = getattr(ws, "path", DEFAULT_PATH)
        return path.split("?", 1)[0]

    def _remote_ip(self, ws) -> str:
        addr = getattr(ws, "remote_address", None)
        try:
            host = addr[0] if isinstance(addr, tuple) else str(addr)
            return str(ipaddress.ip_address(host.split("%", 1)[0]))
        except (ValueError, IndexError, TypeError):
            return ""

    def _is_loopback(self, ws) -> bool:
        addr = getattr(ws, "remote_address", None)
        try:
            host = addr[0] if isinstance(addr, tuple) else str(addr)
            return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
        except (ValueError, IndexError, TypeError):
            return False

    def _control_authorized(self, ws) -> bool:
        if not self._is_loopback(ws):
            return False
        request = getattr(ws, "request", None)
        headers = getattr(request, "headers", None) or {}
        auth = headers.get("Authorization", "")
        return hmac.compare_digest(auth, f"Bearer {self._control_token}")

    async def send(self, ws, m):
        await ws.send(encode(m))

    async def _protocol_error(self, ws, code: str) -> bool:
        await self.send(ws, error_message(code))
        if code == "version":
            await ws.close(CLOSE_VERSION, "protocol version mismatch")
            return True
        return False

    async def _decode_or_error(self, ws, raw) -> dict | None:
        try:
            return decode(raw)
        except ValueError as exc:
            if "version" in str(exc).lower():
                await self._protocol_error(ws, "version")
            else:
                await self.send(ws, error_message("internal"))
            return None

    def _prune_pending(self) -> None:
        now = time.monotonic()
        for request_id, pending in list(self.pending.items()):
            if pending.deadline <= now:
                del self.pending[request_id]

    def _pair_list(self) -> list[dict]:
        self._prune_pending()
        now = time.monotonic()
        return [
            {
                "request_id": p.request_id,
                "match_code": p.match_code,
                "device_id": p.device_id,
                "name": p.name,
                "kind": p.kind,
                "remote": p.remote,
                "age_s": max(0.0, now - p.created),
                "expires_in": max(0.0, p.deadline - now),
            }
            for p in self.pending.values()
        ]

    def _pair_decide(self, args: dict, decision: str) -> dict:
        self._prune_pending()
        request_id = args.get("request_id")
        match_code = args.get("match_code")
        if request_id:
            matches = [p for p in self.pending.values() if p.request_id == request_id]
        elif match_code:
            matches = [p for p in self.pending.values() if p.match_code == match_code]
        else:
            raise ValueError("request_id or match_code is required")
        if len(matches) != 1:
            raise LookupError("pairing request not found or ambiguous")
        pending = matches[0]
        if pending.decision.done():
            raise LookupError("pairing request is no longer available")
        del self.pending[pending.request_id]
        pending.decision.set_result(decision)
        return {"device_id": pending.device_id}

    def _revoke(self, device_id):
        if not isinstance(device_id, str) or not device_id:
            raise ValueError("device_id is required")
        self.revoked.add(device_id)
        for tok, did in list(self.tokens.items()):
            if did == device_id:
                del self.tokens[tok]
        device = self.devices.get(device_id)
        if device is not None and device.ws is not None:
            asyncio.create_task(self._close_revoked(device))
        return {"device_id": device_id, "revoked": True}

    async def _close_revoked(self, device: Device) -> None:
        ws = device.ws
        if ws is None:
            return
        with contextlib.suppress(Exception):
            await self.send(ws, error_message("revoked"))
            await ws.close(CLOSE_REVOKED, "device revoked")

    async def _pair(self, ws, hello: dict) -> tuple[Device | None, bool]:
        did = hello["device_id"]
        name = hello.get("name") or did
        kind = hello.get("kind", "sim")
        caps = hello.get("caps") or {}
        if kind not in KINDS:
            await self.send(ws, error_message("internal"))
            await ws.close(CLOSE_BAD_HELLO, "invalid hello")
            return None, False

        remote = self._remote_ip(ws)
        self._prune_pending()
        if len(self.pending) >= MAX_PENDING or sum(p.remote == remote for p in self.pending.values()) >= MAX_PENDING_PER_IP:
            await self.send(ws, error_message("busy"))
            await ws.close(CLOSE_PAIRING, "pairing busy")
            return None, False

        used_codes = {p.match_code for p in self.pending.values()}
        while True:
            match_code = f"{secrets.randbelow(10000):04d}"
            if match_code not in used_codes:
                break
        request_id = secrets.token_urlsafe(16)
        while request_id in self.pending:
            request_id = secrets.token_urlsafe(16)

        created = time.monotonic()
        deadline = created + self.pair_timeout
        decision = asyncio.get_running_loop().create_future()
        pending = PendingPair(
            ws, did, name, kind, caps, request_id, match_code, remote, created, deadline, decision
        )
        self.pending[request_id] = pending
        recv_task = None
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                await self.send(ws, error_message("pair_timeout"))
                await ws.close(CLOSE_PAIRING, "pairing timed out")
                return None, False
            await self.send(
                ws,
                msg("pair_pending", request_id=request_id, match_code=match_code, expires_in=remaining),
            )
            print(f"[hub] pair pending for {did}: match_code={match_code} request_id={request_id}", flush=True)
            if self.auto_approve:
                pending.decision.set_result("approve")

            recv_task = asyncio.create_task(ws.recv())
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    await self.send(ws, error_message("pair_timeout"))
                    await ws.close(CLOSE_PAIRING, "pairing timed out")
                    return None, False
                done, _ = await asyncio.wait(
                    (recv_task, pending.decision),
                    timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    await self.send(ws, error_message("pair_timeout"))
                    await ws.close(CLOSE_PAIRING, "pairing timed out")
                    return None, False

                if recv_task in done:
                    try:
                        raw = recv_task.result()
                    except ConnectionClosed:
                        return None, False
                    if isinstance(raw, (bytes, bytearray)):
                        await self.send(ws, error_message("not_paired"))
                    else:
                        message = await self._decode_or_error(ws, raw)
                        if message is None:
                            return None, False
                        if message["type"] == "ping":
                            await self.send(ws, msg("pong", t=message.get("t")))
                        else:
                            await self.send(ws, error_message("not_paired"))
                    recv_task = asyncio.create_task(ws.recv())

                if pending.decision in done:
                    if pending.decision.result() == "deny":
                        await self.send(ws, error_message("denied"))
                        await ws.close(CLOSE_PAIRING, "pairing denied")
                        return None, False
                    token = secrets.token_urlsafe(24)
                    while token in self.tokens:
                        token = secrets.token_urlsafe(24)
                    device = Device(did, name, kind, caps, ws)
                    device.token = token
                    device.paired = True
                    self.devices[did] = device
                    self.tokens[token] = did
                    self.revoked.discard(did)
                    await self.send(ws, msg("paired", token=token))
                    print(f"[hub] paired {did}", flush=True)
                    return device, True
        finally:
            if self.pending.get(request_id) is pending:
                del self.pending[request_id]
            if recv_task is not None:
                recv_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, ConnectionClosed):
                    await recv_task

    async def _welcome(self, ws, device: Device) -> None:
        device.ws = ws
        device.state = "idle"
        device.last_seen = time.time()
        await self.send(ws, msg("welcome", device_id=device.device_id, state=device.state, mascot_hash="sim"))
        await self.push_state(device, device.state)

    async def _hello(self, ws, hello: dict) -> Device | None:
        did = hello["device_id"]
        token = hello.get("token")
        if (
            token
            and token in self.tokens
            and self.tokens[token] == did
            and did not in self.revoked
        ):
            device = self.devices.get(did)
            if device is None:
                device = Device(
                    did,
                    hello.get("name", did),
                    hello.get("kind", "sim"),
                    hello.get("caps", {}),
                    ws,
                )
                self.devices[did] = device
            old_ws = device.ws
            device.ws = ws
            device.token = token
            device.paired = True
            device.state = "idle"
            device.last_seen = time.time()
            if old_ws is not None and old_ws is not ws:
                with contextlib.suppress(Exception):
                    await old_ws.close(CLOSE_REPLACED, "replaced by a newer connection")
            await self._welcome(ws, device)
            return device

        device, ok = await self._pair(ws, hello)
        if not ok or device is None:
            return None
        await self._welcome(ws, device)
        return device

    async def handler(self, ws):
        path = self._ws_path(ws)
        if path == CONTROL_PATH:
            await self._control_handler(ws)
            return
        if path != DEFAULT_PATH:
            await ws.close(1002, "unknown path")
            return
        await self._pocket_handler(ws)

    async def _control_handler(self, ws):
        if not self._control_authorized(ws):
            await ws.close(1008, "unauthorized")
            return
        ops = {
            "devices": lambda a: [
                {
                    "device_id": d.device_id,
                    "name": d.name,
                    "kind": d.kind,
                    "caps": d.caps,
                    "screen": d.state,
                }
                for d in self.devices.values()
                if d.paired and d.ws is not None
            ],
            "pair_list": lambda a: self._pair_list(),
            "pair_approve": lambda a: self._pair_decide(a, "approve"),
            "pair_deny": lambda a: self._pair_decide(a, "deny"),
            "revoke": lambda a: self._revoke(a.get("device_id")),
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
                    reply = {"id": req_id, "ok": True, "result": result}
                except (KeyError, TypeError, ValueError, LookupError):
                    reply = {"id": req_id, "ok": False, "error": "Invalid request or target"}
                except Exception:
                    log.exception("control request failed")
                    reply = {"id": req_id, "ok": False, "error": "Internal server error"}
                await ws.send(json.dumps(reply))

    async def _pocket_handler(self, ws):
        device = None
        try:
            raw = await ws.recv()
            if isinstance(raw, (bytes, bytearray)):
                await self.send(ws, error_message("not_paired"))
                return
            hello = await self._decode_or_error(ws, raw)
            if hello is None:
                return
            if hello.get("type") != "hello":
                await self.send(ws, error_message("not_paired"))
                return
            device = await self._hello(ws, hello)
            if device is None:
                return

            async for raw in ws:
                if isinstance(raw, (bytes, bytearray)):
                    device.last_seen = time.time()
                    device._audio = getattr(device, "_audio", b"") + bytes(raw)
                    continue
                message = await self._decode_or_error(ws, raw)
                if message is None:
                    return
                t = message.get("type")
                if t == "text":
                    if not device.paired:
                        await self.send(ws, error_message("not_paired"))
                        continue
                    await self.run_agent(device, message.get("text", ""))
                elif t == "audio_start":
                    device._audio = b""
                    await self.push_state(device, "listening")
                elif t == "audio_end":
                    text = self.stt_text
                    await self.run_agent(device, text, audio=getattr(device, "_audio", b""))
                    device._audio = b""
                elif t == "interrupt":
                    await self.push_state(device, "idle")
                elif t == "ping":
                    await self.send(ws, msg("pong", t=message.get("t")))
                device.last_seen = time.time()
        except ConnectionClosed:
            pass
        finally:
            if device is not None:
                device.ws = None

    async def push_state(self, device, state):
        device.state = state
        if device.ws:
            await self.send(device.ws, msg("state", screen=state))

    async def run_agent(self, device, user_text, audio=None):
        await self.push_state(device, "thinking")
        await self.send(device.ws, msg("transcript", role="user", text=user_text, final=True))
        reply = self.echo_replies[0] if self.echo_replies else ""
        for chunk in self._chunks(reply):
            await self.send(device.ws, msg("transcript", role="assistant", text=chunk, final=False))
            await asyncio.sleep(0.01)
        await self.send(device.ws, msg("transcript", role="assistant", text=reply, final=True))
        if self.tts:
            await self.push_state(device, "speaking")
            total = max(640, (len(reply) // 10 + 1) * CHUNK)
            await self.send(device.ws, msg("audio_start", codec="pcm16le", rate=16000))
            for i in range(0, total, CHUNK):
                await device.ws.send(b"\x00" * CHUNK)
                await asyncio.sleep(0.005)
            await self.send(device.ws, msg("audio_end"))
        await self.push_state(device, "idle")

    def _chunks(self, text):
        words = text.split()
        if not words:
            return [""]
        out = []
        cur = ""
        for w in words:
            if len(cur) + len(w) + 1 > 20:
                out.append(cur.strip())
                cur = w + " "
            else:
                cur += w + " "
        if cur.strip():
            out.append(cur.strip())
        return out if out else [text]

    async def serve(self):
        async def _handler(ws):
            await self.handler(ws)

        self.server = await websockets.serve(_handler, self.host, self.port)
        bind = self.host if self.host not in ("0.0.0.0", "::") else "127.0.0.1"
        print(f"[hub] listening on ws://{bind}:{self.port}{DEFAULT_PATH}", flush=True)
        if self.auto_approve:
            print(
                "[hub] WARNING: --auto-approve is enabled (simulator only; never use in production plugin)",
                flush=True,
            )
        await self.server.wait_closed()

    def run(self):
        asyncio.run(self.serve())


def main():
    ap = argparse.ArgumentParser(description="Hermes Pocket simulator hub (protocol v2)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8767)
    ap.add_argument("--reply", default="Hello from hub.")
    ap.add_argument(
        "--auto-approve",
        action="store_true",
        help="SIMULATOR ONLY: approve every pair_pending without owner action (never in the real plugin)",
    )
    args = ap.parse_args()
    hub = Hub(
        host=args.host,
        port=args.port,
        echo_replies=[args.reply],
        auto_approve=args.auto_approve,
    )
    hub.run()


if __name__ == "__main__":
    main()
