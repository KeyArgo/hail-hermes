import asyncio, json, os, secrets, time
from pathlib import Path
import websockets

S = Path(os.environ["LOCALAPPDATA"]) / "hermes" / "hail-hermes"
T = (S / "control.token").read_text().strip()

QUESTIONS = [
    ("schedule",   "What's on my schedule today?"),
    ("emails",     "Any important emails?"),
    ("work",       "What was I working on?"),
    ("trip plan",  "Give me the three-day plan for my France trip."),
    ("weather",    "What's the weather today?"),
    ("general",    "How tall is Mount Everest?"),
]

async def main():
    ctrl = await websockets.connect("ws://127.0.0.1:8767/control",
                                    additional_headers={"Authorization": f"Bearer {T}"})
    ws = await websockets.connect("ws://127.0.0.1:8767/pocket", ping_interval=None)
    inbox = []

    async def pump():
        async for r in ws:
            if isinstance(r, (bytes, bytearray)):
                inbox.append({"type": "audio_frame"})
                continue
            try:
                inbox.append(json.loads(r))
            except Exception:
                pass
    asyncio.create_task(pump())

    async def send(**m):
        m.setdefault("v", 2)
        await ws.send(json.dumps(m))

    async def wait(kind, to=20):
        d = time.monotonic() + to
        while time.monotonic() < d:
            for m in inbox:
                if m.get("type") == kind:
                    return m
            await asyncio.sleep(0.05)

    did = "bench-" + secrets.token_hex(2)
    await send(type="hello", v=2, device_id=did, kind="sim", name="bench",
               caps={"push": True}, modes=False)
    p = await wait("pair_pending")
    await ctrl.send(json.dumps({"id": "1", "op": "pair_approve", "args": {"request_id": p["request_id"]}}))
    await wait("paired"); await wait("welcome")

    print(f"{'label':10} {'first_audio':>11} {'spoken':>7}  answer")
    rows = []
    for label, q in QUESTIONS:
        inbox.clear()
        t0 = time.monotonic()
        await send(type="text", text=q)
        first = None
        end = None
        for _ in range(240):
            for m in inbox:
                if m.get("type") == "audio_start" and first is None:
                    first = time.monotonic() - t0
                if m.get("type") == "audio_end" and first is not None and end is None:
                    end = time.monotonic() - t0
            if end is not None:
                break
            await asyncio.sleep(0.1)
        text = "".join(m.get("text", "") for m in inbox
                       if m.get("type") == "transcript" and m.get("role") == "assistant")
        fa = f"{first:.2f}s" if first else "none"
        spoken = f"{end - first:.1f}s" if (first and end) else "?"
        rows.append((label, first, end, text))
        print(f"{label:10} {fa:>11} {spoken:>7}  {text[:70]!r}")
        await asyncio.sleep(1.0)

    await ctrl.send(json.dumps({"id": "r", "op": "revoke", "args": {"device_id": did}}))
    await ws.close(); await ctrl.close()

asyncio.run(main())
