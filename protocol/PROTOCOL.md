# Hail Hermes Protocol v2

Transport: WebSocket. The hub (plugin) listens on `ws://<host>:8767/pocket` (port configurable).
**Default bind is `127.0.0.1`.** Serving watches on the LAN is an explicit operator choice (`--lan`),
which logs a warning; v2 traffic on the LAN is cleartext `ws://` (documented known limit).
Clients: watch (`wear`), Hermes Desktop pane (`desktop`), simulator (`sim`), phone (`phone`, not in this build).

v2 replaces v1's self-confirmed pairing (the client echoed a code it had just been sent, which let any
client that could reach the hub pair itself). In v2 only the **owner, on the host**, can approve a device.

## Discovery

The pane's **Let my watch connect** switch (`lan_enabled`, default false) enables
listening on local IPv4 LAN interfaces in addition to loopback. While enabled,
the hub broadcasts a UDP JSON beacon every 2 seconds on port 8768:
`{"svc":"hail-hermes","v":1,"name":"<computer name>","port":8767,"fp":"<short fingerprint of hub id>"}`.
The port is the actual WebSocket port; `fp` is the first 12 hexadecimal digits
of SHA-256 of the persistent random hub ID. The beacon contains no tokens or
pairing secrets, and discovery does not approve a device. Turning the switch off
stops the beacon and closes the additional listeners. Explicit `--lan` binding
is independent of the switch. LAN WebSocket traffic is unencrypted.

## Framing

- **Control**: JSON text frames. Every message has `"type"` and `"v": 2`.
  The hub checks `v` on **every** control frame, before and after `welcome`. A frame with `v != 2` gets
  `error {code:"version"}` and the connection closes with 4000.
- **Audio**: binary WS frames, raw PCM **s16le, 16 kHz, mono**, ~20 ms chunks (640 bytes nominal).
  Client→hub = microphone, hub→client = TTS playback. A frame may be shorter than 640 bytes (the last
  one of an utterance) but its length must be **even**; an odd-length frame gets `error {code:"bad_frame"}`
  and is dropped. Audio streams are bracketed by `audio_start` / `audio_end`.
- Limits: `text` ≤ 4000 characters; one utterance ≤ `max_audio_seconds` (default 60 s = 1,920,000 bytes).
  Over the limit → `error {code:"too_long"}`, the utterance is discarded, no agent run.

## Sequence

1. **Hello** — client connects and sends `hello`. Include `token` if paired before.
2. **Valid token** → hub sends `welcome`. Go to 5.
3. **No / unknown / revoked token → pending.** Hub replies `pair_pending {request_id, match_code, expires_in}`.
   - `request_id`: opaque random string (≥ 16 bytes of entropy). It identifies the request; it does **not**
     authorize anything, and the client never needs to send it back.
   - `match_code`: 4 digits. The client shows it ("Approve on your computer: 4821"); the owner sees the
     same code next to the device name in `pair list` / the Desktop pane, so they approve the right device.
   - While pending, the client may only send `ping` (answered with `pong`). Anything else gets
     `error {code:"not_paired"}` and changes nothing. A v1 `pair_confirm` is "anything else".
   - The pending wait is a **hard deadline** (`pair_timeout`, default 300 s) measured from `hello`;
     pings do **not** extend it. On expiry: `error {code:"pair_timeout"}`, close 4003.
   - At most 3 pending requests per remote IP and 8 in total; one more gets `error {code:"busy"}`, close 4003.
4. **Owner approves on the host** through the loopback, bearer-guarded `/control` channel
   (`hermes hail pair approve <match_code|request_id>`, or the Desktop pane's Approve button).
   The hub issues a token **to the exact connection that made the request** and sends
   `paired {token}` then `welcome`. The request is then gone (single use).
   - Deny → `error {code:"denied"}`, close 4003. Client disconnect → the request is dropped.
   - Approving an unknown, expired, already-used or disconnected request is a control error; nothing is issued.
   - Approval of one request never affects another connection, even one using the same `device_id`.
5. **Session** — hub pushes `state` on every change. Client sends text/audio; hub streams transcripts
   and reply audio back. One turn runs per device at a time. Completed text and audio utterances
   wait in FIFO order, up to three per device; when full, the oldest waiting utterance is dropped.
   Starting an audio capture does not cancel the running turn. An explicit `interrupt` cancels
   the running turn and clears waiting utterances; late output from the cancelled turn is suppressed.

## Messages — client → hub

| type | fields | notes |
|---|---|---|
| `hello` | `device_id, name, kind("wear"\|"desktop"\|"sim"\|"phone"), caps{}, token?` | caps: `{push, haptic, led, battery}` |
| `text` | `text` | typed message → agent run |
| `audio_start` | `codec:"pcm16le", rate:16000` | precedes mic binary frames |
| `audio_end` | `{}` | hub runs STT → agent → TTS |
| `interrupt` | `{}` | cancel the current turn and capture, clear waiting turns; no further audio for the cancelled turn |
| `ping` | `t` | hub echoes `pong {t}` |

## Messages — hub → client

| type | fields | notes |
|---|---|---|
| `pair_pending` | `request_id, match_code, expires_in` | v2 |
| `paired` | `token` | only after owner approval |
| `welcome` | `device_id, state, mascot_hash` | |
| `state` | `screen:"idle"\|"listening"\|"thinking"\|"speaking", busy?:true` | mascot renders this; while a turn runs the hub repeats the current screen with `busy:true` every 3 s |
| `transcript` | `role:"user"\|"assistant", text, final:bool` | see below |
| `audio_start` / `audio_end` | | one pair per spoken reply |
| `notify` | `title, body` | agent → wrist |
| `haptic` | `pattern` | agent → wrist |
| `tool_event` | `name, args` | agent used a device tool |
| `error` | `code, message` | `message` is fixed text for that `code`, never an exception or stderr |
| `pong` | `t` | |

**Transcript semantics.** For one role in one turn: `final:false` frames are **deltas** to append;
the `final:true` frame carries the **complete text** and replaces the accumulated draft. A client shows
exactly one line per role per turn. (`"Hello "`, `"world"`, final `"Hello world"` → one line `Hello world`.)

**Error codes.** `version, bad_frame, too_long, too_short, not_paired, pair_timeout, busy, denied, revoked,
stt_failed, agent_failed, tts_failed, internal`.

## Modes (v2.1, additive)

A device is always in one of two modes. Default `quick`. The hub remembers each device's mode across reconnects.

- **`quick`** — fast answers: small prompt, fast model, no tools. Follow-ups keep context in one running "Watch" conversation
  kept by the hub. If the request needs Hermes's tools, files or memory, the hub says a short "On it" and hands the request to a
  full Hermes run in the background; the result is queued for speech. While disconnected, up to three results are kept
  in memory for 15 minutes and queued after the next `welcome`; a busy device also receives a `notify` fallback.
- **`session`** — every turn runs in full Hermes, resuming `session_id`. Slower; everything works.

| direction | type | fields | notes |
|---|---|---|---|
| client → hub | `set_mode` | `mode:"quick"\|"session", session_id?` | `session_id` required for `session` |
| client → hub | `list_sessions` | `limit?` (default 8, max 30) | recent sessions of the active Hermes profile |
| hub → client | `working` | `count` | running hand-offs for this device; on changes and on reconnect if nonzero, only with `modes:true` |
| hub → client | `mode` | `mode, session_id?, title?` | after `welcome` if `hello` had `modes:true`, and after every `set_mode` |
| hub → client | `slots` | `items:[{slot, id, title}]` | after `welcome` if `hello` had `modes:true`, and whenever settings change |
| hub → client | `state` | `screen:"idle", conversation_end?:true` | optional `conversation_end:true` when a goodbye ends the conversation; the hub also repeats the current screen with `busy:true` every 3 s while a turn runs |
| hub → client | `sessions` | `items:[{id, title, age}]` | answer to `list_sessions` |

A client opts in by sending `modes:true` in `hello`. One that does not still works: it just stays in the default mode. New error code: `bad_session`.

## Control channel (`/control`, loopback only, `Authorization: Bearer <control.token>`)

JSON request `{id, op, args}` → reply `{id, ok, result|error}`. Ops:
`devices`, `notify`, `say`, `haptic`, `battery`, and v2's
`pair_list` → `[{request_id, match_code, device_id, name, kind, remote, age_s, expires_in}]`,
`pair_approve {request_id | match_code}` → `{device_id}`, `pair_deny {request_id | match_code}`,
`revoke {device_id}` → closes that device's live connection with 4005 and invalidates its token
**in the running hub** (a revoke written by another process is also honoured on the next `hello`).
A `match_code` that matches more than one pending request is refused (use `request_id`).

## Close codes

4000 version · 4001 replaced by newer connection · 4002 idle · 4003 pairing ended (timeout/denied/busy)
· 4004 bad hello · 4005 revoked.

## Reconnect

Clients reconnect with backoff and re-`hello` with their token. Hub treats a device silent > 60 s as gone;
on reconnect, state resets to `idle`. A turn in progress when the connection drops keeps running; its final reply is spoken to the device's next connection as a new turn (kept up to 15 minutes, last 3 replies).

## Versioning

`v: 2` on every message. Unknown types: ignore. Shared examples of every message live in
`protocol/fixtures/*.json`; every client's codec tests load them.
