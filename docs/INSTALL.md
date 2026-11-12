# Install Hail Hermes

_Install the plugin, turn on the desktop part, and optionally pair a watch._

Applies to Hail Hermes 1.0.0 · revised 2026-11-11

Two commands and one switch. You do not copy files anywhere.

## 1. Install the plugin

```bash
hermes plugins install KeyArgo/hail-hermes --yes-deps --enable
```

That is the real repo — source, tests, watch app and all. One command, dependencies included, plugin
turned on. If Hermes shows a security scan, read it: this build reports no findings.

Or do the whole computer side at once — install, start the hub, check it answers:

```bash
bash tools/quickstart.sh
```

## 2. Turn on the desktop part

Hermes Desktop → **Settings → Capabilities → Plugins** → **Hail Hermes** → on, including its
**Desktop part**. Restart Hermes Desktop once if the ⌚ mark does not appear.

## 3. Open it

Click the **⌚ Hail Hermes** mark in the status bar at the bottom of the Hermes window. That panel is
the product: talk, character, sessions, watch pairing, settings.

Press **Check my setup** first. It says, in plain words, anything still missing — usually just ffmpeg
for speech (`hermes pm install --tools-only` once, then `hermes hail restart`).

## 4. Talk

Hold the button in the pane and speak, or type. The answer is spoken aloud. **No watch is required** —
the pane is the same agent and the same voice.

## 5. A watch, if you have one

The watch app is a separate Android app; the plugin only contains the computer half, and no plugin can
install a second Android app on your behalf.

Download it: **https://github.com/KeyArgo/hail-hermes/releases/latest** (`hail-wear-*.apk`).

Installing it needs adb with wireless debugging on the watch — there is no Play Store listing yet:

```bash
adb connect <watch-ip>:<port>        # the port is shown in the watch's developer options
adb install -r hail-wear-tonight.apk
```

Then, with the app on the watch:

1. In the pane, press **Let my watch connect**.
2. Open **Hermes Wear** on the watch; it finds this computer on the same Wi-Fi.
3. It shows a 4-digit code — match it in the pane, then **Approve**.
4. Keep the watch screen awake while you approve. Codes expire after 5 minutes.

Pairing once is permanent for that watch. To move to another computer, open the ⚙ on the watch and
choose **Pair a new computer**.

**No watch?** Everything works in the pane: same agent, same voice, same sessions. That is the
intended way to try it before pairing hardware.

## If something looks wrong

| Symptom | What to do |
|---|---|
| No ⌚ mark in the status bar | Plugins → Hail Hermes → turn on the Desktop part, restart Hermes Desktop |
| "hub is not running" | `hermes hail restart` |
| No sound | `hermes pm install --tools-only`, then `hermes hail restart` |
| Model list looks empty | that list is *optional* Quick Ask endpoints; the picker above it is what replies |
| Watch will not pair | codes expire — press **Let my watch connect** again for a fresh one |

## Upgrade, uninstall, and your data

Upgrade by running the install command again, or with `hermes plugins update hail-hermes`. The
watch APK is updated separately from the latest release.

```bash
hermes plugins remove hail-hermes
```

Removing the plugin leaves its state behind: settings, pairings, logs, `brief.json`, `soul.md` and
any uploaded face live under `<profile>/hail-hermes` (or `$HAIL_HERMES_STATE`; legacy
`<profile>/pocket` is still used when it exists). Delete that directory to remove everything, or
revoke one device with `hermes hail devices` and `hermes hail revoke <device-id>`. The full
file-by-file list is in [ARCHITECTURE.md](ARCHITECTURE.md).

---

Back to [docs/README.md](README.md).
