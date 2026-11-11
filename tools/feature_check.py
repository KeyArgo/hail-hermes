"""Exercise the real features against the running hub and print a pass/fail table.

Unit tests prove the code does what the code intends. This proves the product does what the
owner expects: a different voice per character, quick ask across turns, the weather short-cut,
recap, and the model list.

Usage:  python tools/feature_check.py [--url ws://127.0.0.1:8767/pocket]
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import subprocess
import sys
import time
import uuid

import websockets

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")


class Client:
    def __init__(self, url: str, device_id: str | None = None, token: str = "") -> None:
        self.url = url
        self.device_id = device_id or str(uuid.uuid4())
        self.token = token
        self.ws = None

    async def connect(self, name: str = "feature-check") -> None:
        self.ws = await websockets.connect(self.url, open_timeout=10)
        await self.ws.send(json.dumps({"v": 2, "type": "hello", "device_id": self.device_id,
                                       "token": self.token, "name": name, "kind": "sim",
                                       "caps": {"push": True}}))
        if self.token:
            return
        code = None
        for _ in range(6):
            msg = await self.recv_json(15)
            if msg.get("type") == "pair_pending":
                code = msg["match_code"]
                break
            if msg.get("type") in ("welcome", "hello_ok"):
                return
        if code:
            subprocess.run(["hermes", "hail", "pair", "approve", code],
                           capture_output=True, timeout=60)
            for _ in range(3):
                msg = await self.recv_json(20)
                if msg.get("type") == "paired":
                    self.token = msg.get("token") or ""
                    return

    async def send(self, payload: dict) -> None:
        if self.ws is None:
            raise RuntimeError("connect first")
        await self.ws.send(json.dumps(payload))

    async def close(self) -> None:
        if self.ws is not None:
            await self.ws.close()

    async def recv_json(self, timeout: float = 30) -> dict:
        if self.ws is None:
            raise RuntimeError("connect first")
        frame = await asyncio.wait_for(self.ws.recv(), timeout=timeout)
        if isinstance(frame, (bytes, bytearray)):
            return {"type": "audio", "bytes": len(frame), "raw": frame}
        return json.loads(frame)

    async def turn(self, text: str, limit: float = 90) -> tuple[str, str, float, str]:
        """Send a question, read until the final transcript. Returns (text, audio hash, secs, err)."""
        started = time.monotonic()
        await self.send({"v": 2, "type": "text", "text": text})
        digest = hashlib.sha256()
        audio = 0
        deadline = started + limit
        while time.monotonic() < deadline:
            try:
                msg = await self.recv_json(min(45, limit))
            except asyncio.TimeoutError:
                break
            if msg.get("type") == "audio":
                digest.update(msg["raw"])
                audio += msg["bytes"]
                continue
            if msg.get("type") == "error":
                return "", "", time.monotonic() - started, str(msg.get("code") or "error")
            if msg.get("type") == "transcript" and msg.get("role") == "assistant" and msg.get("final"):
                return (str(msg.get("text") or ""), digest.hexdigest() if audio else "",
                        time.monotonic() - started, "")
        return "", digest.hexdigest() if audio else "", time.monotonic() - started, "timeout"


def cli(*args: str, timeout: float = 90) -> subprocess.CompletedProcess:
    return subprocess.run(["hermes", "hail", *args], capture_output=True, timeout=timeout)


def revoke_device(device_id: str) -> None:
    """Forget a device this script paired. Best effort — the hub may already be gone."""
    with contextlib.suppress(Exception):
        subprocess.run(["hermes", "hail", "revoke", device_id],
                       capture_output=True, timeout=60)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://127.0.0.1:8767/pocket")
    args = parser.parse_args()

    # Every client we create here is a probe, not a watch. Keep their ids so the
    # finally below can revoke them even if a check fails or the hub disappears.
    clients: list[Client] = []

    def new_client(**kwargs) -> Client:
        client = Client(args.url, **kwargs)
        clients.append(client)
        return client

    client = new_client()
    quick = new_client()
    try:
        await client.connect()
        await asyncio.sleep(1.5)

        # 1 — model list
        await client.send({"v": 2, "type": "list_models"})
        models: list[dict] = []
        for _ in range(6):
            msg = await client.recv_json(20)
            if msg.get("type") == "models":
                models = msg.get("items") or []
                break
        record("model list", bool(models),
               f"{len(models)} offered: " + ", ".join(str(m.get('id')) for m in models[:5]))

        # 2 — the two characters must not share one voice
        # Comparing over the wire is unreliable for a test client (audio is streamed to a live
        # device), so synthesise both voices through the same path the hub uses and compare bytes.
        import hashlib as _hashlib
        import os as _os
        import sys as _sys
        _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "plugin"))
        _hermes_home = __import__("pathlib").Path(_os.environ.get("HERMES_HOME", "")) if _os.environ.get("HERMES_HOME") else __import__("pathlib").Path(_os.environ.get("LOCALAPPDATA", "")) / "hermes"
        for p in (_hermes_home, _hermes_home / "hermes-agent"):
            if (p / "tools").is_dir():
                _sys.path.insert(0, str(p))
        try:
            from dotenv import load_dotenv as _load_dotenv
            _load_dotenv(str(_hermes_home / ".env"))
        except Exception:
            pass
        digests: dict[str, str] = {}
        try:
            from hail_hermes.audio import build_tts
            from hail_hermes.settings import read_settings
            state = _os.environ.get("HAIL_HERMES_STATE") or str(
                __import__("pathlib").Path(_os.environ.get("LOCALAPPDATA", "")) / "hermes" / "hail-hermes")
            cfg = read_settings(__import__("pathlib").Path(state))
            tts = build_tts({"backend": "hermes"})
            for label, voice in (("hermes", cfg.get("voice_hermes")), ("nous", cfg.get("voice_nous"))):
                data = await tts.synthesize("Testing one two three.", voice=voice) if tts else b""
                digests[label] = _hashlib.sha256(data or b"").hexdigest() if data else "none"
        except Exception as exc:  # noqa: BLE001 - report the reason, do not crash
            digests = {"hermes": f"error:{type(exc).__name__}", "nous": "unknown"}
        heard = all(not v.startswith(("none", "error")) for v in digests.values())
        record("distinct voices", heard and digests["hermes"] != digests["nous"],
               f"hermes={digests['hermes'][:10]} nous={digests['nous'][:10]}")

        # 3 — quick ask, two turns in a row
        await quick.connect("feature-quick")
        await asyncio.sleep(1)
        a1, _, s1, e1 = await quick.turn("Reply with exactly: quick-one")
        a2, _, s2, e2 = await quick.turn("Reply with exactly: quick-two")
        record("quick ask twice", bool(a1) and bool(a2),
               f"{a1[:24]!r} in {s1:.1f}s then {a2[:24]!r} in {s2:.1f}s {e1}{e2}")

        # The hub restarted twice above, which closed our socket: reconnect before asking again.
        await client.close()
        client = new_client(device_id=client.device_id, token=client.token)
        await client.connect()
        await asyncio.sleep(1)

        # 4 — weather is answered from the short cut, not by waking the agent
        w_ans, _, w_secs, w_err = await client.turn("will it rain today?")
        record("weather short cut", bool(w_ans) and w_secs < 6.0,
               f"{w_ans[:60]!r} in {w_secs:.1f}s {w_err}")

        # 5 — a missing session answers instead of hanging
        await client.send({"v": 2, "type": "recap", "session_id": "local-missing"})
        recap = ""
        for _ in range(6):
            msg = await client.recv_json(20)
            if msg.get("type") == "recap":
                recap = str(msg.get("line") or "")
                break
        record("recap answers", bool(recap), recap[:70] or "no recap message")

        print()
        failed = [name for name, ok, _ in RESULTS if not ok]
        print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
        if failed:
            print("failed: " + ", ".join(failed))
        return 0 if not failed else 1
    finally:
        # A check run must not leave phantom watches behind. Close every socket we
        # opened, then revoke every device we paired — even when a check failed or the
        # hub went away mid-run. Without this the list grows by two entries per run.
        for c in clients:
            with contextlib.suppress(Exception):
                await c.close()
        for device_id in dict.fromkeys(c.device_id for c in clients):
            revoke_device(device_id)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
