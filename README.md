# Hail Hermes

**Talk to your Hermes agent out loud — in the desktop panel, or from a watch it pairs with in four digits.**

![tests](https://img.shields.io/badge/tests-403%20passing-brightgreen)
![version](https://img.shields.io/badge/version-1.0.0-blue)
![licence](https://img.shields.io/badge/licence-MIT-green)
![platform](https://img.shields.io/badge/desktop%20%7C%20Wear%20OS-4.6%2B-informational)

<p align="center"><img src="assets/screenshots/watch-ready.png" width="300" alt="Hail Hermes on a Galaxy Watch, ready to talk"/> <img src="assets/screenshots/watch-speaking.png" width="300" alt="Hail Hermes answering out loud on the watch"/></p>

Hail Hermes is a Hermes Agent plugin: a hub, a desktop panel, and phone/watch clients, so your
self-hosted agent gets a voice, a face, proactive nudges, and device sensors as agent tools. **The
panel is the product and needs no hardware**; the watch is the extension you pair later.

```bash
hermes plugins install KeyArgo/hail-hermes --yes-deps --enable
```

Then restart Hermes Desktop and click **Hail Hermes** in the left rail — the plugin is a page in the
app, not a window behind a chip (the status-bar ⌚ and `Alt+Shift+H` are the quick paths). Press **Run
the 60-second demo**: three real questions, answered aloud through the real hub, with no watch
involved. Pairing a
watch is optional — [docs/INSTALL.md](docs/INSTALL.md) has the steps, and the panel does everything
without one.

Requires Hermes Agent and ffmpeg for speech (`hermes pm install --tools-only` once if the setup check
flags it). Local network only: the watch finds this computer on the same Wi-Fi.

## Why it is not a chat box with extra steps

- **Voice that is honest about itself.** The reply is spoken; the face moves only while audio is
  actually playing. Kill the hub mid-sentence and both stop on the same frame.
- **The agent starts the conversation.** Scheduled nudges reach you: "You said you'd reply to Sam by
  Friday. It's Thursday."
- **Sessions on the wrist.** "Continue the project I started on Tuesday" resolves to a real folder,
  not a new chat.
- **Any model, including local.** Hermes subscription, LM Studio, Ollama, vLLM — switch live, and the
  panel's header says which one is answering.
- **A weather fast path** that answers "will it rain?" in about three seconds without a model round
  trip.
- **Built to be checked, not believed.** 373 tests passed in the recorded suites (plugin + simulator,
  1 skipped), 30 more for the face generator, a simulator that needs no hardware, a headless harness
  that clicks every control, and a static-analysis gate that fails the build on new findings. The panel header names the
  build it is running, read from the hub itself — so the number you see is the number that answers.


## What's in the panel

- **Talk:** the face of the product and the screen it opens on — the live watch mirror drawn in CSS,
  hold to talk, a text box, and a one-press guided demo. A watch asking to pair raises a banner here.
- **Sessions:** your past conversations, with Open in Hermes.
- **Character:** personality templates, voice with ▶ preview, and a face of your own.
- **Settings:** the setup check, brain and voice, everything else.
- **Watch:** behind **More** — let my watch connect, approve the code, paired devices. The panel is the
  product; the watch is the extension, so it is not a peer tab.
- **Header:** one pill says what the panel is doing and opens Settings — `Ready · Quick ask`, or
  `Hub not running` when it cannot reach the hub.

## Layout

| Path | What | Built by |
|---|---|---|
| `protocol/PROTOCOL.md` | **The contract.** Every component implements this. | human |
| `plugin/` | Hermes hub plugin: WS server, pairing, agent bridge, device tools | Claude Code |
| `desktop/plugin.js` | **The desktop pane the app loads.** Byte-identical to the module below — see the note. | Hermes |
| `plugin/hail_hermes/desktop/plugin.js` | The pane's source, beside the Python package (pip layout and tests expect it there) | Hermes |
| `android/` | Phone hub client (Kotlin/Compose): voice, TTS, notifications, share-sheet | Codex |
| `wear/` | WearOS watch app: push-to-talk, mascot face states | Gemini CLI |
| `simulator/` | Device/hub simulator + pytest suite — test with zero hardware | Hermes/beam |
| `tools/facesmith/` | Mascot frame generator (1-bit idle/blink/talk) + contact sheet | Hermes/ornith |

## The one-line demo

Watch: hold button → speak → Hermes replies aloud. Phone: share any link → Hermes acts.
Hub: Hermes prods the watch — "You said you'd reply to Sam by Friday. It's Thursday."

Hail Hermes uses `<profile>/hail-hermes` for new state. Existing `<profile>/pocket` directories remain in use. Set `HAIL_HERMES_STATE` to override the location; `HERMES_POCKET_STATE` remains supported as a fallback. Use `hermes hail` to manage the hub; `hermes pocket` remains a compatibility alias.

## Rules for all contributors

1. `protocol/PROTOCOL.md` is law. Changes go through a PR against it, never silently.
2. Each component is self-contained; cross-component deps are the protocol only.
3. Every component ships tests and a README section. Python components must pass `pytest` locally.
4. Commit with a descriptive message. One component per branch is fine; `main` is the integration target.
5. Kotlin components must include the Gradle wrapper and a GitHub Actions workflow (`.github/workflows/android.yml`, `wear.yml`) — local Android SDK is not guaranteed.
6. **Never turn `desktop/plugin.js` into a re-export.** Hermes Desktop reads a runtime plugin as source
   and rejects any import other than `@hermes/plugin-sdk` or `react`, so a shim is a hard load failure
   ("unsupported import: ../plugin/hail_hermes/desktop/plugin.js"). Keep it byte-identical to
   `plugin/hail_hermes/desktop/plugin.js`; `plugin/tests/test_desktop_bundle.py` enforces both.

## Known limits

- **Speech rides on Hermes' internals.** The hub imports `tools.tts_*`, `tools.transcription_tools`,
  the CLI audio routes and `agent.auxiliary_client` because Hermes has no public plugin API for
  "speak this" yet. Those imports are lazy and guarded: if a release moves them, the panel keeps
  working as text and says speech is unavailable, rather than failing to load.
- **Model discovery is deliberately shallow.** The hub reads no credential file at all (catalog rule
  11): it offers the configured model, any local endpoint, and the model *names* Hermes has cached.
  A name in that list is visible, not authorised — Hermes decides whether your account may use it,
  and says so if it cannot.
- **The desktop bundle exists twice.** `desktop/plugin.js` and `plugin/hail_hermes/desktop/plugin.js`
  are the same file: the desktop runtime evaluates a plugin as source and allows no imports beyond
  `@hermes/plugin-sdk` and `react`, so there is no module to import from a shared location. A test
  fails if the two copies differ.
- **The seven Nous looks** ship idle/blink/talk frames only; `listening` and `thinking` have no
  artwork yet, and the watch falls back to idle for those two states.
