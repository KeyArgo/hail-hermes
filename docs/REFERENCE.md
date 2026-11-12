# Reference

_Every tool, CLI command, hook, HTTP route and protocol message the plugin registers._

Applies to Hail Hermes 1.0.0 · revised 2026-11-11

## Agent tools

Registered by `plugin/hail_hermes/hermes_plugin.py` in the `pocket` toolset. Each handler returns a
JSON string and never raises; a failure is `{"ok": false, "error": "..."}`. A tool is offered to the
model only while a connected device supports its capability.

| Tool | Parameters | Returns | Capability |
|---|---|---|---|
| `device_notify` | `title` (string, required), `body` (string, required), `device_id` (string, optional) | `{ok, device_id, delivered}` | `push` |
| `device_say` | `text` (string, required), `device_id` (string, optional) | `{ok, device_id, queued, audio}` | any device |
| `device_haptic` | `pattern` (`short` \| `long` \| `double` \| `sos`, default `short`), `device_id` (optional) | `{ok, device_id, pattern}` | `haptic` |
| `device_battery` | `device_id` (optional) | `{ok, device_id, mv, charging}` or a note when the device has not reported yet | `battery` |

`device_id` may be omitted when exactly one capable device is connected. When more than one is
connected and no id is given, the call fails with an error the model can read.

Example call and result:

```json
{"title": "Sam", "body": "It's Thursday — reply to Sam."}
{"ok": true, "device_id": "9f0c…", "delivered": true}
```

## Hooks and middleware

`plugin.yaml` declares `provides_hooks: []` and the catalog entry declares
`provides_middleware: []`. Hail Hermes registers no hooks or middleware.

## CLI

Entry points from `plugin/pyproject.toml`: `hail-hermes` and `pocket-hub` both call
`hail_hermes.cli:main`. Inside Hermes the same tree is `hermes hail` (and `hermes pocket`, a
suppressed compatibility alias). Global option: `--state-dir`.

| Command | What it does |
|---|---|
| `serve` | Run the hub. Flags: `--config`, `--host`, `--lan`, `--port`, `--agent {hermes,echo}`, `--stt-text`, `--tts {auto,none,silence,espeak,piper,kokoro}`, `-v/--verbose`. |
| `restart` | Stop the running hub and start a fresh one; prints JSON with an `answers` field. |
| `pair list` | List pending pairing requests with code, name, kind, remote and expiry. |
| `pair approve <code\|request_id>` | Approve one pending request. |
| `pair deny <code\|request_id>` | Deny one pending request. |
| `watch [--apk PATH]` | Print the wireless-debugging steps, list adb devices, install the APK, set up `adb reverse tcp:8767`. |
| `devices` | List paired and connected devices with capabilities and status. |
| `revoke <device_id>` | Forget a paired device and close its connection. |
| `notify <title> <body> [--device ID]` | Push a notification to a connected device. |
| `say <text> [--device ID]` | Speak text on a connected device. |
| `settings [key [value]]` | Print every setting, or set one. Booleans accept true/yes/on and false/no/off; list keys accept JSON. |
| `models` | List Quick ask models exposed by the saved endpoints. |
| `slots [set <1\|2\|3> <session_id> \| clear <1\|2\|3>]` | Show or edit the three watch session slots. |

## Hub HTTP and WebSocket routes

Served by `plugin/hail_hermes/hub.py`.

| Method and path | Access | Purpose |
|---|---|---|
| `GET /status` | loopback only | JSON snapshot: running, version, mode, character, devices, pending. |
| `GET /pocket` (WebSocket upgrade) | local network | Protocol v2 device connection. Rejects disallowed `Origin` headers. |
| `GET /control` (WebSocket upgrade) | loopback only, `Authorization: Bearer <control.token>` | JSON control channel used by tools, the CLI and the pane API. |
| anything else | — | `404` with a short hint pointing at `/pocket`. |

## Pane API routes

Mounted by Hermes at `/api/plugins/hail-hermes/` from `plugin/hail_hermes/dashboard/plugin_api.py`.
Unless noted, a route returns a JSON object.

| Method and path | Purpose |
|---|---|
| `GET /v1/version` | Version, commit, commit date, install time, dirty state, channel. |
| `GET /identity` | The pane's persistent pairing device id. |
| `GET /status` | Hub status, devices and the live mirror; starts the hub when needed. |
| `POST /start` | Ask for the hub to start; reports whether it is running or starting. |
| `GET /pending` | Pending pairing requests. |
| `POST /approve-self` | Approve the pane's own loopback desktop request by request id. |
| `POST /approve` | Approve a pending request by `request_id` or `match_code`. |
| `POST /deny` | Deny a pending request by `request_id` or `match_code`. |
| `GET /sample` | The bundled sample WAV as base64 JSON. |
| `GET /sample.wav` | The bundled sample WAV as a file. |
| `POST /voice-sample` | Render a short WAV for a voice specification. |
| `GET /models` | Models exposed by `quick_endpoints` (also supports dict-style `models` lookup). |
| `GET /hermes-models` | Models grouped by provider, plus the configured provider and model. |
| `GET /setup-check` | Plain-language environment checks. |
| `GET /settings` | Every setting, the choices map, voices, characters and watch steps. |
| `POST /settings` | Validate and save settings; supports `add_endpoint`; reports `restart_needed`. |
| `POST /slots` | Replace the session slots (at most three). |
| `GET /sessions` | Recent Hermes sessions, `{id, title}`. |
| `GET /soul` and `POST /soul` | Read or write the personality text; `reset: true` restores the default. |
| `GET /soul-templates` | Personality templates from `content/souls.json`. |
| `GET /face` and `POST /face` | Read or upload a face (`image/png` or `image/jpeg`, under 2 MB). |
| `POST /face/reset` | Delete an uploaded face. |
| `GET /memory` and `POST /memory` | Read the last 20 memory lines, or append one fact. |
| `POST /revoke` | Revoke a paired device. |
| `POST /restart-hub` | Restart the hub after checking that `live.json` names a `hail_hermes` process. |

## Control-channel operations

Sent as `{id, op, args}`; answered with `{id, ok, result|error}`. See `control.py` and
`hub.py::_handle_control`.

| `op` | Arguments | Result |
|---|---|---|
| `devices` | none | list of `{device_id, name, kind, caps, screen}` |
| `notify` | `title`, `body`, `device_id?` | `{device_id, delivered}` |
| `say` | `text`, `device_id?` | `{device_id, queued, audio}` |
| `haptic` | `pattern`, `device_id?` | `{device_id, pattern}` |
| `battery` | `device_id?` | `{device_id, mv, charging}` |
| `pair_list` | none | list of `{request_id, match_code, device_id, name, kind, remote, age_s, expires_in}` |
| `pair_approve` / `pair_deny` | `request_id` or `match_code` | `{device_id}` |
| `revoke` | `device_id` | `{revoked, device_id}` |
| `reload_settings` | none | reloaded settings |
| `sessions` | `limit?` | recent sessions |

## Protocol messages

The full contract is [protocol/PROTOCOL.md](../protocol/PROTOCOL.md); shared examples are in
`protocol/fixtures/`. Summary:

| Direction | Types |
|---|---|
| client → hub | `hello`, `text`, `audio_start`, `audio_end`, `interrupt`, `ping`, `set_mode`, `set_character` / `character`, `list_sessions`, `get_settings`, `set_setting`, `list_models`, `recap`, `battery` |
| hub → client | `pair_pending`, `paired`, `welcome`, `state`, `transcript`, `audio_start`, `audio_end`, `notify`, `haptic`, `tool_event`, `error`, `pong`, `mode`, `working`, `slots`, `sessions`, `settings`, `settings_changed`, `setting_error`, `models`, `recap` |

Every control frame carries `"v": 2`; audio is binary s16le, 16 kHz, mono. Close codes are 4000
version, 4001 replaced, 4002 idle, 4003 pairing ended, 4004 bad hello, 4005 revoked.

## Configuration keys

The full table with defaults lives in [CONFIGURATION.md](CONFIGURATION.md). The validator in
`settings.py` accepts exactly the keys documented there; the serve config accepts the `HubConfig`
fields and the agent/STT/TTS sub-objects.

---

Back to [docs/README.md](README.md).
