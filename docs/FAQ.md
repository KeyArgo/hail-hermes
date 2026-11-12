# FAQ

_Short answers to the questions the code and the README raise most often._

Applies to Hail Hermes 1.0.0 · revised 2026-11-11

**Do I need a watch?**
No. The panel is the product: it talks, speaks, lists sessions and changes character with no
hardware. The watch is an optional extension.

**What watch does it support?**
Wear OS 3 or newer. There is no Apple Watch build. The APK is a separate install from the plugin;
see [INSTALL.md](INSTALL.md).

**Does it work with my phone?**
`android/` contains a phone client, but it still speaks protocol v1 and the hub closes it with code
4000. It is not part of the 1.0.0 path. Use the panel or the Wear OS app.

**Is anything sent to the cloud?**
Only what your configured model provider receives, plus one HTTPS request to `api.open-meteo.com`
when you ask a weather question. The hub itself has no telemetry and no self-updater.

**Can I use it on my LAN?**
Yes. Turn on **Let my watch connect** (`lan_enabled`). The hub then listens on the machine's LAN
addresses and broadcasts a discovery beacon on UDP 8768. LAN traffic is cleartext; use it on a
network you trust. See [SECURITY.md](SECURITY.md).

**Why does the model list look empty?**
The list is optional Quick ask endpoints plus model names Hermes has cached. If no local endpoint is
running and Hermes has no cache, the picker is empty. The model picker above it is what replies. A
name in the list is visible, not proof that your account may use it; Hermes decides that when it
answers.

**Why does it say speech is unavailable?**
Speech is the one part that binds to Hermes' internal TTS/STT modules. If a Hermes release moves
them, the panel keeps working as text and says speech is unavailable. Check `ffmpeg` first
(`hermes pm install --tools-only`), then the backend with `hermes hail settings tts`.

**Why did the face stop when the audio stopped?**
That is the point. The mascot is driven by real playback, not a timer. Kill the hub mid-sentence and
the face leaves `Speaking` on the same frame.

**Where is my data?**
Under `<profile>/hail-hermes`, or `$HAIL_HERMES_STATE`. The file-by-file list is in
[ARCHITECTURE.md](ARCHITECTURE.md). Delete that directory to remove everything.

**How do I forget one watch?**
`hermes hail devices` then `hermes hail revoke <device-id>`. Pairing again is a four-digit code and
one approval.

**Why are there two `plugin.js` files?**
The desktop runtime evaluates a plugin as source and allows no imports beyond `@hermes/plugin-sdk`
and `react`, so there is no shared module to import. The two copies must stay byte-identical; a test
fails if they differ.

**How do I upgrade or uninstall?**
Run the install command again, or `hermes plugins update hail-hermes`; uninstall with `hermes
plugins remove hail-hermes`. Settings, pairings and logs are left in the state directory unless you
delete them.

**Why is the phone client in the repo if it does not work?**
It is kept as the start of a future client and as a protocol example. The ROADMAP records the state
honestly; do not install it expecting a working phone app.

---

Back to [docs/README.md](README.md).
