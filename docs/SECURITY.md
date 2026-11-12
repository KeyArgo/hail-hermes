# Security model

_How pairing, tokens, the network surface and the watch's permissions work._

Applies to Hail Hermes 1.0.0 · revised 2026-11-11

Short, because the surface is small: one machine, one hub, devices the owner paired.

## Pairing

A device is let in only when a person approves it.

1. The watch asks to pair and shows a **four-digit match code** on its own screen.
2. The hub holds the request for `pair_timeout` seconds (default 300) and shows the same code
   in the pane's Watch tab or via `hermes hail pair list`.
3. The owner approves **on the computer**, over the loopback control channel, protected by a
   token in the state directory.

There is deliberately **no automatic approval path** — not for loopback, not for the LAN, not
for a device that "looks like a watch". A shortcut that approved devices on the local network
was written and then removed: on shared Wi-Fi it would have handed a stranger the owner's agent,
and an agent can run tools. `plugin/tests/test_pairing.py` fails the build if such a path comes
back.

Refused, with tests: expired codes, codes approved after the device disconnected, replayed
codes, unknown codes, and more than a handful of pending requests (8 total, 3 per address).

## Tokens

- The device receives a random token on approval; the hub stores only its SHA-256.
- Every later connection must present it. A revoked device is refused immediately.
- Tokens are per device, not shared, and never displayed after pairing.

## What the watch may change

`watch_can_change` gates each setting (character, voice, model, session, speak). A key not in
that map is refused, and the voice setter whitelists its own target key — a client cannot name
an arbitrary setting. Model changes are limited to models Hermes has already authorised, and a
provider entry with no concrete model is refused outright: that pairing produced a session that
failed on every turn.

## Network

- The hub listens on the loopback address unless LAN mode is on.
- LAN mode binds the machine's own interface addresses; the discovery beacon is a UDP broadcast
  carrying hostname, port and a short fingerprint — no tokens, no session data.
- Speech and text cross the LAN in the protocol's own frames. Traffic is not encrypted; run it on
  a network you trust, or keep LAN mode off and use the loopback path. Encrypted transport is a
  known gap, listed as future work rather than pretended.

## What is not protected

- The computer itself: anyone with a shell on it can read the state directory.
- The model provider: what you say to the watch goes to whichever provider is configured.
- Denial of service: a device on the LAN can ask to pair repeatedly. It will never be approved,
  but it can make the pane noisy until the pending cap kicks in.

---

Back to [docs/README.md](README.md).
