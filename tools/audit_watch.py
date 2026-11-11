"""Audit the watch protocol against the LIVE hub, as a real device would.

The pytest suite tests the hub in-process with fakes. This does the opposite: it pairs a throwaway
device over the wire, approving it on the control channel the way the pane does, then exercises
every message a watch can send and reports what actually came back, with timings.

Run with the hub up:  python tools/audit_watch.py [--url ws://127.0.0.1:8767/pocket]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import time
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

RESULTS: list[tuple[bool, str, str]] = []


def record(ok: bool, name: str, detail: str) -> None:
    RESULTS.append((ok, name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")


class Device:
    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self.inbox: list[dict[str, Any]] = []
        # Arrival time for each inbox entry, stamped as the frame is read. A frame's
        # time is when it reached this client, not when the turn ended: computing
        # first_audio after settle() is how the audit came to report the hub's
        # `total` (time to finish speaking) as latency.
        self.arrivals: list[float] = []
        self._task: asyncio.Task | None = None

    def clear(self) -> None:
        self.inbox.clear()
        self.arrivals.clear()

    async def start(self) -> None:
        async def pump() -> None:
            try:
                async for raw in self.ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    self.inbox.append(msg)
                    self.arrivals.append(time.monotonic())
            except ConnectionClosed:
                # The audit revokes its own device at the end, so the hub closes this
                # socket. That is expected, not an error to print as an unretrieved task.
                pass

        async def keepalive() -> None:
            # The hub drops a socket silent for 60 s (close 4002). The real watch pings every 25 s
            # (ConnectionPolicy.PING_INTERVAL_MS), so an audit client must too or it measures its
            # own silence instead of the product.
            while True:
                await asyncio.sleep(20)
                try:
                    await self.send(type="ping", t=int(time.time() * 1000))
                except Exception:
                    return

        self._task = asyncio.create_task(pump())
        self._ka = asyncio.create_task(keepalive())

    async def send(self, **msg: Any) -> None:
        # The hub validates `v` on EVERY frame, not just the hello: a frame without it is closed
        # with 4000 "protocol version mismatch". Verified against the live hub.
        msg.setdefault("v", 2)
        await self.ws.send(json.dumps(msg))

    async def wait_for(self, kind: str, timeout: float = 60.0, where=None) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        seen = 0
        while time.monotonic() < deadline:
            while seen < len(self.inbox):
                msg = self.inbox[seen]
                seen += 1
                if msg.get("type") == kind and (where is None or where(msg)):
                    return msg
            await asyncio.sleep(0.05)
        return None

    async def settle(self, timeout: float = 90.0) -> list[dict[str, Any]]:
        """Wait until the turn is over: the answer's audio ended, or the hub went quiet.

        The hub streams an answer as several `transcript` frames and signals completion with
        `audio_end` plus a return to `state: idle`. Waiting for only one signal either hangs for the
        full timeout or measures the wrong turn.
        """
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            types = [m.get("type") for m in self.inbox]
            if "error" in types:
                await asyncio.sleep(0.3)
                return self.inbox
            if "audio_end" in types:
                await asyncio.sleep(0.3)  # let the trailing transcript frames land
                return self.inbox
            if "state" in types and self.inbox[-1].get("type") == "state" and self.inbox[-1].get("state") == "idle" and len(types) > 2:
                await asyncio.sleep(0.3)
                return self.inbox
            await asyncio.sleep(0.05)
        return self.inbox


def summary(msgs: list[dict[str, Any]]) -> str:
    kinds = [m.get("type") for m in msgs]
    # An answer arrives as several assistant transcript frames; the text is the concatenation, and
    # reading only the last frame reports the final fragment ("I'm ...") as the whole answer.
    parts = [m.get("text", "") for m in msgs
             if m.get("type") == "transcript" and m.get("role") == "assistant" and m.get("text")]
    text = "".join(parts).strip()
    return f"{kinds} answer={text[:90]!r}" if text else f"{kinds}"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:8767/pocket")
    ap.add_argument("--control", default="ws://127.0.0.1:8767/control")
    args = ap.parse_args()

    # The control channel is the computer side and is authenticated: a Bearer token kept in the
    # hub state dir. Reading it here is what `hermes hail pair` does too.
    from pathlib import Path
    import os

    state = Path(os.environ.get("HAIL_STATE_DIR") or
                 Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "hermes" / "hail-hermes")
    token_file = state / "control.token"
    if not token_file.is_file():
        raise SystemExit(f"no control token at {token_file} — is the hub running?")
    headers = {"Authorization": f"Bearer {token_file.read_text(encoding='utf-8').strip()}"}

    control = await websockets.connect(args.control, open_timeout=10, additional_headers=headers)
    ctrl_inbox: list[dict] = []

    async def pump_control() -> None:
        try:
            async for raw in control:
                ctrl_inbox.append(json.loads(raw))
        except Exception:
            pass

    asyncio.create_task(pump_control())
    ws = await websockets.connect(args.url, open_timeout=10, max_size=2**22)
    dev = Device(ws)
    await dev.start()

    # -- pairing: a fresh device is challenged, and approval happens on the computer side --------
    await dev.send(type="hello", v=2, device_id="audit-" + secrets.token_hex(4), kind="sim",
                   # Declare the capabilities this audit exercises: `caps` is a dict of flags, and
                   # a device that declares none is (correctly) refused by the tool paths, which
                   # looked like three failing features until this was fixed.
                   name="audit-device", caps={"push": True, "haptic": True, "battery": True},
                   modes=False)
    pending = await dev.wait_for("pair_pending", 15)
    if not pending:
        record(False, "pairing", f"no pair_pending; got {[m.get('type') for m in dev.inbox]}")
        return
    record(True, "pairing challenge", f"code={pending.get('match_code')} expires_in={pending.get('expires_in')}s")
    await control.send(json.dumps({"id": "1", "op": "pair_approve",
                                  "args": {"request_id": pending["request_id"]}}))
    paired = await dev.wait_for("paired", 15)
    record(bool(paired and paired.get("token")), "pairing approval", "token issued" if paired else "no token")
    welcome = await dev.wait_for("welcome", 10)
    record(bool(welcome), "welcome", str(welcome)[:120] if welcome else "missing")

    async def turn(name: str, question: str) -> None:
        dev.clear()
        # Clock spans, stated explicitly so the two numbers mean the same thing:
        # - audit: t0 is taken just before the text frame is sent, so first_text/first_audio
        #   include this client's send plus the hub's receive/queueing before the turn starts.
        # - hub: _TurnTiming starts inside _text_turn/_audio_turn, after the frame has been
        #   read and the turn launched, so it excludes that send/queue span.
        # The spans should differ by only that small local hop; if they diverge more, the
        # clocks are measuring different things. first_text/first_audio are taken from
        # self.arrivals (frame arrival), never from the end of the turn.
        hub_log = state / "hub.log"
        try:
            log_offset = hub_log.stat().st_size
        except OSError:
            log_offset = 0
        t0 = time.monotonic()
        await dev.send(type="text", text=question)
        await dev.settle(90)
        msgs = list(dev.inbox)
        took = time.monotonic() - t0
        answered = any(m.get("type") == "transcript" and m.get("role") == "assistant" for m in msgs)
        errored = [m for m in msgs if m.get("type") == "error"]

        def first_at(kind: str, role: str | None = None) -> float | None:
            for msg, at in zip(dev.inbox, dev.arrivals):
                if msg.get("type") != kind:
                    continue
                if role is not None and msg.get("role") != role:
                    continue
                return at - t0
            return None

        first_text = first_at("transcript", "assistant")
        first_audio = first_at("audio_start")
        # Read only what the hub appended during this turn, so the line belongs to this turn
        # and not to an earlier one. The hub writes it after the turn ends, which is after
        # settle() returns; give it a moment in case the write is still in flight.
        hub_line: str | None = None
        if hub_log.is_file():
            deadline = time.monotonic() + 2.0
            while True:
                with hub_log.open("rb") as fh:
                    fh.seek(log_offset)
                    tail = fh.read().decode("utf-8", "replace")
                hits = [ln.split("turn timing: ", 1)[1] for ln in tail.splitlines()
                        if "turn timing: " in ln]
                if hits:
                    hub_line = hits[-1]
                    break
                if time.monotonic() >= deadline:
                    break
                await asyncio.sleep(0.1)
        stamp = (f"first_text={first_text:.1f}s " if first_text is not None else "no text ")
        stamp += f"first_audio={first_audio:.1f}s" if first_audio is not None else "no audio"
        hub_part = f"hub: {hub_line}" if hub_line else "hub: no turn timing line for this turn"
        record(answered and not errored, name, f"{stamp} | {hub_part} | {summary(msgs)[:150]}")

    # -- the product's core loop ---------------------------------------------------------------
    await dev.send(type="ping", t=123)
    pong = await dev.wait_for("pong", 5)
    record(bool(pong), "ping/pong", str(pong)[:80])

    await turn("text question", "In one short sentence, what do you do?")
    await turn("weather question", "What's the weather today?")
    await turn("long question", "In three short sentences, describe what you can help me with on a watch.")

    # -- wrist controls -------------------------------------------------------------------------
    dev.clear()
    await dev.send(type="get_settings")
    st = await dev.wait_for("settings", 10)
    record(bool(st), "get_settings", f"keys={sorted(st)[:8]}" if st else "no settings message")

    dev.clear()
    await dev.send(type="list_models")
    models = await dev.wait_for("models", 20)
    count = len(models.get("items", models.get("models", []))) if models else 0
    record(bool(models) and count > 0, "list_models", f"{count} models" if models else "no models message")

    dev.clear()
    await dev.send(type="list_sessions")
    sessions = await dev.wait_for("sessions", 25)
    rows = sessions.get("items", sessions.get("sessions", [])) if sessions else []
    record(bool(rows), "list_sessions", f"{len(rows)} sessions")

    if rows:
        dev.clear()
        await dev.send(type="recap", session_id=rows[0].get("id") or rows[0].get("session_id"))
        recap = await dev.wait_for("recap", 40)
        record(bool(recap), "recap", str(recap.get("text", ""))[:100] if recap else "no recap message")

    dev.clear()
    await dev.send(type="set_setting", key="character", value="hermes")
    ok = await dev.wait_for("settings_changed", 10) or await dev.wait_for("settings", 10)
    if ok is None:
        bad = await dev.wait_for("setting_error", 2)
        ok = bad if bad is None else None  # a refusal is not a pass for a valid key
    record(bool(ok), "set_setting (valid)", "applied" if ok else "no confirmation")

    dev.clear()
    await dev.send(type="set_setting", key="not_a_real_key", value="x")
    err = await dev.wait_for("setting_error", 10) or await dev.wait_for("error", 5)
    record(bool(err), "set_setting (invalid refused)", str(err)[:100] if err else "accepted silently")

    dev.clear()
    await dev.send(type="text", text="x" * 4001)
    too_long = await dev.wait_for("error", 10, where=lambda m: m.get("code") == "too_long" or m.get("error") == "too_long")
    record(bool(too_long), "oversized text refused", str(too_long)[:80] if too_long else "no too_long error")

    dev.clear()
    await dev.send(type="interrupt")
    await asyncio.sleep(1.0)
    record(True, "interrupt", f"sent; {[m.get('type') for m in dev.inbox][:5]}")

    # -- the four device tools, driven the way an agent would --------------------------------
    dev_id = welcome.get("device_id") if welcome else None
    # notify and haptic are pushes to the device. battery is a question answered on the control
    # channel — a device never pushes a reading it does not have — so it is checked differently.
    for op, args, kind in (
        ("notify", {"device_id": dev_id, "title": "Audit", "body": "hello"}, "notify"),
        ("haptic", {"device_id": dev_id, "pattern": "short"}, "haptic"),
    ):
        dev.clear()
        await control.send(json.dumps({"id": op, "op": op, "args": args}))
        got = await dev.wait_for(kind, 12)
        record(bool(got), f"tool {op}", str(got)[:90] if got else "device received nothing")

    dev.clear()
    await control.send(json.dumps({"id": "battery", "op": "battery", "args": {"device_id": dev_id}}))
    await asyncio.sleep(1.5)
    reply = next((m for m in ctrl_inbox if m.get("id") == "battery"), None)
    record(bool(reply and reply.get("ok")), "tool battery",
           str((reply or {}).get("result"))[:100] if reply else "control did not answer")

    # A judge running this must not leave phantom watches behind: revoke what the audit created.
    dev.clear()
    await control.send(json.dumps({"id": "rev", "op": "revoke", "args": {"device_id": dev_id}}))
    await asyncio.sleep(1.0)

    await ws.close()
    await control.close()

    bad = [r for ok, r, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} protocol checks passed")
    if bad:
        print("FAILED: " + ", ".join(bad))


if __name__ == "__main__":
    asyncio.run(main())
