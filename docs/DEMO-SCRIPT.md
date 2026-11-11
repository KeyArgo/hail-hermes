# Hail Hermes — 90-second live demo script

> **For the hackathon submission use `docs/DEMO-SCRIPT-PANEL.md`** — it leads with the panel and needs
> no hardware. This script is the device walkthrough, kept for recording a real watch.

Hard limit: 90 seconds. Planned run: 86 seconds. If you are behind at any beat,
skip the next fallback line and keep the clock.

Wear OS only. The watch app is Hermes Wear (package `com.hermes.pocket.wear`).
There is no Apple Watch build, and the script says so out loud at 0:00.

## Before the clock starts (not counted)

- Hermes Desktop open, Hail Hermes pane open as a big window, Talk tab showing.
- Hub running and listening on port 8767; discovery on UDP 8768.
- Watch charged, on the wrist, Hermes Wear open, microphone allowed.
- One terminal open, ready to kill the hub process on 8767.
- If there is no watch: run every beat in the pane and say plainly that the pane
  runs the whole thing without hardware.

## Beat 1 — 0:00–0:06 — Open

Screen: pane full window, watch face on wrist, status dot green.
Say: "This is Hail Hermes. The watch app is Hermes Wear, Wear OS only — there is
no Apple Watch build."
Fallback: watch dead or missing — run this beat in the pane and say the pane
needs no hardware.

## Beat 2 — 0:06–0:16 — Pair once

Screen: watch shows a 4-digit code; the pane shows the same 4 digits; click
Approve; the pane shows connected.
Say: "Pairing is one time: the watch shows four digits, I check them against the
laptop and approve."
Fallback: codes don't match — reopen the watch app and pair again; pane shows no
code — restart the hub on 8767 and pair again.

## Beat 3 — 0:16–0:28 — Send a real request from the wrist

Screen: watch, press and hold **Hold to talk**, speak a real question, release.
Say: "From the wrist: press and hold, ask a real question, let go — it sends."
Fallback: mic misses it — use the Conversation button, or the pane's talk button;
same session either way.

## Beat 4 — 0:28–0:40 — Spoken reply

Screen: status dot yellow while thinking, then blue while speaking; Nous girl's
mouth moves only while audio actually plays.
Say: "Yellow while it thinks, blue while it speaks, and her mouth moves only
while audio is really playing."
Fallback: no sound — point at the mouth staying still, and say that is the proof
the animation is tied to real audio, not a timer.

## Beat 5 — 0:40–0:50 — Break it on purpose

Screen: in the terminal, kill the hub process listening on 8767, mid-reply; the
dot goes dark and the audio stops.
Say: "Now I break it on purpose: I kill the hub mid-sentence."
Fallback: reply already finished — ask a second question and kill the hub during
that one.

## Beat 6 — 0:50–1:02 — Watch recovers by itself

Screen: dot returns green with nothing touched on the watch; send a new question
and get an answer.
Say: "Nothing was clicked on the watch — it finds the hub again on its own and
the session is back."
Fallback: not back within ten seconds — restart the hub; still not back — reopen
the watch app and keep going.

## Beat 7 — 1:02–1:12 — Replay a stale pairing code

Screen: replay the 4-digit code from beat 2 for approval; it is refused and the
watch stays unpaired, while the live session keeps working.
Say: "Now I replay the code from the start. Refused — a used code buys nothing."
Fallback: pane accepts it without complaint — show the hub's refusal line in the
terminal log for 8767 instead.

## Beat 8 — 1:12–1:24 — Recover to a working session

Screen: **Conversation** button for hands-free back and forth; ask, get a spoken
answer, mouth animating, dot green then yellow then blue.
Say: "Back to a working session: Conversation gives hands-free back and forth.
One agent, wrist or desk."
Fallback: hands-free stalls — hold to talk once; it is the same session.

## Beat 9 — 1:24–1:26 — Close

Say: "Hail Hermes. Wear OS only. Thank you."
Fallback: stop talking; leave the pane live so judges can try it themselves.

## If the whole thing dies on stage

Switch to the pane only and run beats 2 through 8 there, with no watch. Same
pairing code, same requests, same spoken replies. Say plainly that the pane can
run the whole product without hardware, and that the watch adds the wrist.

## What this script does not claim

- No Apple Watch build. Wear OS only.
- Only these features are shown: one-time pairing with a 4-digit code approved on
  the laptop, Hold to talk, Conversation hands-free mode, self-recovery after the
  hub dies, refusal of a used pairing code, the Nous girl mascot with four ring
  colours and a mouth that animates only while audio plays, and the green,
  yellow, blue status dot. Nothing else is promised on stage.
