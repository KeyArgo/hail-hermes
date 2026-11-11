"""Live watch mirror: the hub mirrors the last watch screen and final transcripts into live.json."""

from __future__ import annotations

import json

from conftest import Client, FakeAgent, is_type, running_hub


def _mirror(state_dir) -> dict | None:
    live = json.loads((state_dir / "live.json").read_text())
    return live.get("mirror")


async def test_mirror_updates_on_screen_change_and_final_transcript(state_dir):
    async with running_hub(state_dir, agent=FakeAgent(chunks=("Hello ", "from ", "Hermes."))) as hub:
        async with Client.open(hub) as watch:
            await watch.pair(kind="wear", name="Watch")
            await watch.send("text", text="what time is it")
            msgs = await watch.until(is_type("state", screen="idle"))

            assert any(
                isinstance(m, dict) and m.get("type") == "transcript" and m.get("role") == "user" and m.get("final")
                for m in msgs
            )
            assert any(
                isinstance(m, dict) and m.get("type") == "transcript" and m.get("role") == "assistant" and m.get("final")
                for m in msgs
            )

            mirror = _mirror(state_dir)
            assert mirror is not None
            assert mirror["device"] == "Watch"
            assert mirror["user"] == "what time is it"
            assert mirror["reply"] == "Hello from Hermes."
            assert isinstance(mirror["at"], float)
            assert mirror["screen"] == "idle"


async def test_mirror_trims_user_and_reply_to_last_300_chars(state_dir):
    long_user = "u" * 400
    async with running_hub(state_dir, agent=FakeAgent(chunks=("r" * 400,))) as hub:
        async with Client.open(hub) as watch:
            await watch.pair(kind="wear", name="Watch")
            await watch.send("text", text=long_user)
            await watch.until(is_type("state", screen="idle"))
            mirror = _mirror(state_dir)
            assert mirror["user"] == "u" * 300
            assert mirror["reply"] == "r" * 300
