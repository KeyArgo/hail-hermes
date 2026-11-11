import argparse
import asyncio
import json
import wave

import websockets

from .protocol import decode, encode, msg


async def run_device(url, kind="wear", name="sim-device", script=None):
    import uuid

    device_id = str(uuid.uuid4())
    async with websockets.connect(url) as ws:
        await ws.send(encode(msg(
            "hello",
            device_id=device_id,
            name=name,
            kind=kind,
            caps={"push": True, "haptic": True, "led": True, "battery": True},
        )))
        welcomed = asyncio.Event()

        async def recv_loop():
            async for raw in ws:
                if isinstance(raw, (bytes, bytearray)):
                    print(f"[device] audio chunk {len(raw)} bytes", flush=True)
                    continue
                message = decode(raw)
                print(f"[device] <- {message}", flush=True)
                if message["type"] == "pair_pending":
                    print(
                        f"[device] Approve on the hub: {message['match_code']}",
                        flush=True,
                    )
                elif message["type"] == "welcome":
                    welcomed.set()

        async def script_runner():
            if not script:
                return
            await welcomed.wait()
            with open(script) as f:
                data = json.load(f)
            for step in data.get("steps", []):
                await asyncio.sleep(step.get("delay", 0.5))
                kind_step = step.get("action")
                if kind_step == "text":
                    await ws.send(encode(msg("text", text=step["text"])))
                elif kind_step == "audio":
                    # Read a PCM file (raw) or WAV.
                    path = step["path"]
                    if path.endswith(".wav"):
                        with wave.open(path, "rb") as w:
                            frames = w.readframes(w.getnframes())
                    else:
                        with open(path, "rb") as f:
                            frames = f.read()
                    await ws.send(encode(msg("audio_start", codec="pcm16le", rate=16000)))
                    for i in range(0, len(frames), 640):
                        await ws.send(frames[i:i + 640])
                        await asyncio.sleep(0.02)
                    await ws.send(encode(msg("audio_end")))
                elif kind_step == "interrupt":
                    await ws.send(encode(msg("interrupt")))
                elif kind_step == "ping":
                    await ws.send(encode(msg("ping", t=step.get("t", 1))))

        receiver = asyncio.create_task(recv_loop())
        runner = asyncio.create_task(script_runner())
        try:
            await receiver
        finally:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://localhost:8767/pocket")
    ap.add_argument("--kind", default="wear")
    ap.add_argument("--name", default="sim-device")
    ap.add_argument("--script", default=None)
    args = ap.parse_args()
    asyncio.run(run_device(args.url, kind=args.kind, name=args.name, script=args.script))


if __name__ == "__main__":
    main()
