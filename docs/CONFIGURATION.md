# Configuration

_Every setting, environment variable and serve-config key the code actually reads._

Applies to Hail Hermes 1.0.0 · revised 2026-11-11

Most people only change settings from the pane's **Settings** and **Character** tabs. Everything
below can also be read or written from the command line:

```bash
hermes hail settings                 # print every key
hermes hail settings tts piper       # set one key
```

Values are validated in `plugin/hail_hermes/settings.py`. Unknown keys are refused, so a typo does
not silently sit in the file doing nothing.

## Persistent settings

Stored in `settings.json` in the hub state directory.

| Key | Default | Meaning |
|---|---|---|
| `lan_enabled` | `false` | Listen on the LAN and broadcast the discovery beacon. |
| `model` | `""` | Model passed to Hermes for full turns. |
| `provider` | `""` | Provider passed to Hermes for full turns. |
| `toolsets` | `""` | Hermes toolsets for a turn; the serve default is `clarify,pocket`. |
| `quick_url` | `""` | OpenAI-compatible base URL for Quick ask. |
| `quick_backend` | derived | `hermes` or `url`; derived from whether `quick_url` is set. |
| `quick_model` | `""` | Model name for Quick ask. |
| `quick_endpoints` | `[]` | Endpoints probed for the model list. |
| `voice_session` | `"new"` | `new` starts a fresh session; `pinned` resumes `pinned_session_id`. |
| `pinned_session_id` | `""` | Session id used when `voice_session` is `pinned`. |
| `slots` | `[]` | Up to three watch session slots, each `{id, name, title, slot?}`. |
| `stt_threads` | CPU count | Worker threads for local speech-to-text. |
| `soul_path` | `<state>/soul.md` | Where the personality text lives. |

| `stt` | `"auto"` | `auto`, `parakeet` or `hermes`. |
| `tts` | `"auto"` | `auto`, `piper`, `kokoro`, `edge` or `none`. |
| `character` | `"nous-warm"` | Active look: `hermes`, `nous`, `nous-dark`, `nous-light`, `nous-white`, `nous-soft`, `nous-teal`, `nous-warm`. |
| `features` | `{"sessions": false, "approval": false, "cards": false}` | Feature switches; turning one off is a complete rollback. |
| `watch_can_change` | all `true` | What the watch may change: `character`, `voice`, `model`, `session`, `speak`. |
| `watch_model_presets` | `[]` | Up to five `source::model` picks the watch may switch between. |
| `home_place` | `{}` | `{label, lat, lon}` for the weather fast path; empty asks the agent. |
| `voice_hermes` | `am_puck:0.5,am_adam:0.5` | Kokoro voice mix for the Hermes look. |
| `voice_nous` | `af_heart` | Kokoro voice for the Nous looks. |
| `voice_rate` | `"-5%"` | Speed adjustment, as edge-tts spells it (`+5%`, `-5%`). |

### Validation rules worth knowing

- `slots` holds at most three entries; a slot id must be non-empty and must not start with `-`.
- `watch_model_presets` holds at most five `source::model` strings.
- `home_place` must have numeric `lat` in −90…90 and `lon` in −180…180; empty is legal and means
  "no home town yet".
- `features` and `watch_can_change` accept only their known keys and boolean values.
- `voice_rate` must match `+5%` / `-5%`.
- Settings are written atomically under `settings.lock`; a failed write leaves the old file intact.

## Serve config file

`hermes hail serve --config config.yaml` (or `.json`) reads these keys in `hub.py` and the builders.
Unknown keys are ignored.

| Key | Default | Meaning |
|---|---|---|
| `host` | `127.0.0.1` | Bind address. `--lan` sets `0.0.0.0`. |
| `port` | `8767` | WebSocket port. |
| `path` | `/pocket` | WebSocket path. |
| `state_dir` | profile state dir | Hub state directory. |
| `idle_timeout` | `60.0` | Seconds of silence before a device is treated as gone. |
| `hello_timeout` | `15.0` | Seconds allowed for the first frame. |
| `pair_timeout` | `300.0` | Seconds a pending pairing request lives. |
| `audio_pacing` | `true` | Send TTS frames at about real time. |
| `stream_speech` | `true` | Synthesise and stream sentences as text arrives. |
| `max_audio_seconds` | `60.0` | Longest accepted utterance. |
| `mascot_hash` | `"default"` | Mascot asset identity sent in `welcome`. |
| `warm_voice` | `true` in `build_hub` | Load TTS at start so the first reply is not slower. |
| `agent` | `{backend: "hermes"}` | Agent backend and options (below). |
| `stt` | `auto` | STT backend and options (below). |
| `tts` | `auto` | TTS backend and options (below). |
| `quick_url`, `quick_model` | saved settings | Quick ask endpoint and model. |
| `soul_path` | state `soul.md` | Personality file. |

### Agent options (`agent:`)

| Key | Default | Meaning |
|---|---|---|
| `backend` | `hermes` | `hermes` (full agent) or `echo` (tests). |
| `binary` | `hermes` on PATH | CLI to run. |
| `extra_args` | none | Extra CLI arguments. |
| `source` | `cli` | Session source label. |
| `timeout` | `600` | Turn timeout in seconds. |
| `toolsets` | `clarify,pocket` | Hermes toolsets. |
| `model`, `provider` | settings | Override the turn's model/provider. |
| `env` | none | Extra environment for the child process. |
| `prefix`, `delay` | `You said: `, `0` | Echo backend only. |

### STT options (`stt:`) and TTS options (`tts:`)

STT accepts `backend` (`auto`, `stub`, `command`, `hermes`, `parakeet`), plus `text`, `command`,
`timeout`, `python` and `model_dir`. TTS accepts `backend` (`auto`, `none`, `silence`, `espeak`,
`command`, `hermes`, `edge`, `kokoro`, `piper`), plus `voice`, `speed`, `timeout`, `python`,
`model`, `model_dir`, `kokoro_python`, `kokoro_model_dir`, `prefer` and `kokoro_gpu_python`.
The `auto` choices prefer Hermes' own voice stack first, then a local Kokoro/Piper install, then
espeak; the file header in `audio.py` documents each fallback.

## Environment variables

| Variable | Used by | Effect |
|---|---|---|
| `HAIL_HERMES_STATE` | `store.py` | Hub state directory; highest priority. |
| `HERMES_POCKET_STATE` | `store.py` | Legacy fallback for the state directory. |
| `HERMES_HOME` | `models.py`, `plugin_api.py`, `audio.py` | Active Hermes profile; also locates Kokoro models. |
| `LOCALAPPDATA` | `models.py`, tools | Windows Hermes home lookup. |
| `POCKET_TTS_DEVICE` | `kokoro_worker.py` | `cpu` forces CPU; anything else allows GPU. |
| `POCKET_TTS_THREADS` | `kokoro_worker.py` | Worker thread count. |
| `ONNX_PROVIDER` | `piper_worker.py` | Set to `CPUExecutionProvider` by the worker. |
| `HAIL_AGENT_CWD` | `agent.py` | Working directory for a Hermes turn. |
| `HERMES_POCKET_AUTOSTART` | `autostart.py` | Set to `1` for a hub started by the pane. |

The `FAKE_*` variables in `plugin/tests/` belong to test fixtures, not to the product.

## Where data lives, and how to delete it

Everything is under `<profile>/hail-hermes` by default:

```text
~/.hermes/profiles/<profile>/hail-hermes/
```

Delete that directory to remove settings, pairings, logs, the brief and any uploaded face. Revoke a
single device first if you only want to forget one:

```bash
hermes hail devices
hermes hail revoke <device-id>
```

The legacy `<profile>/pocket` directory is still used when it exists, so upgrading does not move
anyone's data silently. See [ARCHITECTURE.md](ARCHITECTURE.md) for a file-by-file table.

---

Back to [docs/README.md](README.md).
