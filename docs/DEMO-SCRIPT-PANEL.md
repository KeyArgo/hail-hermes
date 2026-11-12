# Hail Hermes — 90-second demo, panel first

_The 90-second recording script that leads with the desktop panel and needs no hardware._

Applies to Hail Hermes 1.0.0 · revised 2026-11-11

The recording script for the hackathon submission. The watch appears at 1:10 and never before it:
**the panel alone carries this video**, so it records the same way with or without hardware.

The older watch-first script is kept as `docs/DEMO-SCRIPT.md` for the device walkthrough.

## Before the clock starts (not counted)

- Hermes Desktop open, the Hail Hermes pane as a big window, **Talk** showing.
- A model configured. If the header pill says `Hub not running`, start the hub first.
- Screen recorder at 1080p or better, cursor visible, notifications silenced.
- Watch charged and on the wrist, **only** for the 1:10 beat. No watch means dropping that beat; the
  video is complete without it.

Clock starts on the first frame of the panel.

## Beat 1 — 0:00–0:12 — What this is

Screen: the pane, Talk open. The drawn watch is the biggest thing on screen; the pill reads
`Ready · Quick ask`; the build stamp sits at the right.
Say: "This is Hail Hermes — my Hermes agent with a voice, in the desktop panel. No watch needed."

## Beat 2 — 0:12–0:26 — One press, three real questions

Screen: press **Run the 60-second demo**. The face goes *Thinking*, then *Speaking* while the answer is
read aloud; the transcript fills in.
Say: "One press asks three real questions through the real hub — weather, what I was working on, and
what it can do for me."

## Beat 3 — 0:26–0:40 — The fast path

Screen: type or say "Will it rain?". The answer lands in about three seconds, spoken.
Say: "Weather is a fast path — three seconds, no model round trip. The agent answers like a device, not
like a chatbot."

## Beat 4 — 0:40–0:52 — Honest voice

Screen: while an answer is being spoken, kill the hub (terminal, port 8767) mid-sentence. Audio stops and
the face leaves *Speaking* on the same frame; the pill becomes `Hub not running`.
Say: "The face moves only while audio is really playing — kill the hub mid-sentence and it stops dead.
That is the difference between a face and a lie."

## Beat 5 — 0:52–1:10 — It comes back, and it remembers

Screen: press **Retry** or let it reconnect. Then **Sessions** → the project from Tuesday → Open in
Hermes; the real folder opens.
Say: "It reconnects by itself, and 'the project I started on Tuesday' is a real session on disk, not a
new chat."

## Beat 6 — 1:10–1:26 — The wrist, if you have one

Screen: picture-in-picture. The watch shows a four-digit code; the panel shows the same four as a banner
on Talk; click **Approve**; ask the same agent from the wrist and the reply is spoken.
Say: "Pairing is four digits, once. Same agent, same session, from the wrist."
No watch: skip the beat and say "and it pairs with a Wear OS watch in four digits, when you have one."

## Beat 7 — 1:26–1:30 — The close

Screen: the panel, build stamp readable.
Say: "Hail Hermes. 409 tests, no npm dependencies, and a build stamp that proves which one you are
looking at."

## Rules

- Do not open with "Wear OS only, no Apple Watch". That is a hardware limitation, not the product; it
  belongs in the caption if anywhere.
- Never claim the face is driven by real audio without showing beat 4 in the same take.
- One take, no cuts mid-beat. A visible edit in beat 4 reads as a fake.
- If a beat fails, keep going: a shown recovery is more convincing than a perfect run.

---

Back to [docs/README.md](README.md).
