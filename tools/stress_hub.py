"""Stress the live hub: many devices, many turns, interruptions and reconnects.

This is not a unit test. It talks the real protocol to the running hub and reports what a
person would feel: did every question come back, how long did it take, and what broke.

Usage:
  python tools/stress_hub.py                     # 4 devices x 3 turns
  python tools/stress_hub.py --devices 6 --turns 5 --url ws://127.0.0.1:8767/pocket
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field

import websockets

QUESTIONS = [
    "Reply with exactly: one",
    "What is two plus two?",
    "Name one colour.",
    "Say hello in five words or fewer.",
    "Reply with exactly: five",
    "What day comes after Monday?",
    "Give me one word for water falling from the sky.",
    "Reply with exactly: eight",
]


@dataclass
class Stats:
    sent: int = 0
    answered: int = 0
    failed: int = 0
    latencies: list[float] = field(default_factory=list)
    errors: dict[str, int] = field(default_factory=dict)

    def note_error(self, code: str) -> None:
        self.errors[code] = self.errors.get(code, 0) + 1


class Device:
    def __init__(self, url: str, name: str) -> None:
        self.url = url
        self.name = name
        self.id = str(uuid.uuid4())
        self.token = ""
        self.ws = None

    async def pair(self) -> None:
        self.ws = await websockets.connect(self.url, open_timeout=10)
        await self.ws.send(json.dumps({"v": 2, "type": "hello", "device_id": self.id,
                                      "name": self.name, "kind": "wear", "caps": {"push": True}}))
        for _ in range(6):
            frame = await asyncio.wait_for(self.ws.recv(), timeout=20)
            if isinstance(frame, (bytes, bytearray)):
                continue
            msg = json.loads(frame)
            if msg.get("type") == "pair_pending":
                subprocess.run(["hermes", "hail", "pair", "approve", msg["match_code"]],
                               capture_output=True, timeout=60)
            elif msg.get("type") == "paired":
                self.token = msg.get("token") or ""
                return
            elif msg.get("type") in ("welcome", "hello_ok"):
                return

    async def reconnect(self) -> None:
        if self.ws is not None:
            await self.ws.close()
        self.ws = await websockets.connect(self.url, open_timeout=10)
        await self.ws.send(json.dumps({"v": 2, "type": "hello", "device_id": self.id,
                                      "token": self.token, "name": self.name,
                                      "kind": "wear", "caps": {"push": True}}))
        await asyncio.sleep(0.5)

    async def ask(self, text: str, stats: Stats, timeout: float = 120) -> None:
        if self.ws is None:
            return
        started = time.monotonic()
        stats.sent += 1
        try:
            await self.ws.send(json.dumps({"v": 2, "type": "text", "text": text}))
        except Exception as exc:  # noqa: BLE001 - report, never crash the run
            stats.failed += 1
            stats.note_error(f"send:{type(exc).__name__}")
            return
        deadline = started + timeout
        while time.monotonic() < deadline:
            try:
                frame = await asyncio.wait_for(self.ws.recv(), timeout=deadline - time.monotonic())
            except asyncio.TimeoutError:
                break
            if isinstance(frame, (bytes, bytearray)):
                continue
            msg = json.loads(frame)
            if msg.get("type") == "error":
                stats.failed += 1
                stats.note_error(str(msg.get("code") or "error"))
                return
            if msg.get("type") == "transcript" and msg.get("role") == "assistant" and msg.get("final"):
                stats.answered += 1
                stats.latencies.append(time.monotonic() - started)
                return
        stats.failed += 1
        stats.note_error("timeout")


async def run_device(index: int, args, stats: Stats) -> None:
    device = Device(args.url, f"stress-{index}")
    await device.pair()
    await asyncio.sleep(0.5)
    for turn in range(args.turns):
        question = QUESTIONS[(index + turn) % len(QUESTIONS)]
        await device.ask(question, stats)
        if args.reconnect_every and (turn + 1) % args.reconnect_every == 0:
            await device.reconnect()
    if args.interrupt:
        # Ask, then abandon the socket mid-answer: a restart must not wedge anything.
        if device.ws is not None:
            await device.ws.send(json.dumps({"v": 2, "type": "text",
                                            "text": "Count slowly from one to twenty."}))
            await asyncio.sleep(0.7)
            await device.ws.close()
            device.ws = None
    if device.ws is not None:
        await device.ws.close()


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://127.0.0.1:8767/pocket")
    parser.add_argument("--devices", type=int, default=4)
    parser.add_argument("--turns", type=int, default=3)
    parser.add_argument("--reconnect-every", type=int, default=0,
                        help="drop and reopen the socket every N turns (0 = never)")
    parser.add_argument("--interrupt", action="store_true",
                        help="also abandon a socket mid-answer")
    args = parser.parse_args()

    stats = Stats()
    started = time.monotonic()
    await asyncio.gather(*(run_device(i, args, stats) for i in range(args.devices)))
    elapsed = time.monotonic() - started

    print(f"devices={args.devices} turns_each={args.turns} wall={elapsed:.1f}s")
    print(f"sent={stats.sent} answered={stats.answered} failed={stats.failed}")
    if stats.latencies:
        ordered = sorted(stats.latencies)
        p50 = statistics.median(ordered)
        p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
        print(f"latency p50={p50:.1f}s p95={p95:.1f}s max={ordered[-1]:.1f}s")
    if stats.errors:
        print("errors: " + ", ".join(f"{k}={v}" for k, v in sorted(stats.errors.items())))
    ok = stats.failed == 0 and stats.answered == stats.sent
    print("RESULT:", "all turns answered" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
