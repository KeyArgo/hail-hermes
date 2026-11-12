# Testing on the watch

_How to build the watch app and run the on-hardware checks._

Applies to Hail Hermes 1.0.0 · revised 2026-11-11

Branch: `lane/test-watch`. It has the full panel, heartbeat, setup check and screen-off audio. The watch app in this branch has not been compiled yet.

## On the computer
    cd hail-hermes
    git status
    git fetch --all --prune && git checkout lane/test-watch && git pull --ff-only
    .venv/bin/pip install -e "plugin[test]" && (cd plugin && ../.venv/bin/pytest -q)
    (cd wear && ./gradlew testDebugUnitTest assembleDebug)

The app is at `wear/app/build/outputs/apk/debug/app-debug.apk`.

On the watch: Developer options, Wireless debugging. Then:

    adb pair <ip:port> <code>
    adb connect <ip:port>
    adb install -r wear/app/build/outputs/apk/debug/app-debug.apk

The hub and Hermes Desktop must be on the same Wi-Fi as the watch. Restart Hermes Desktop so it loads the new panel.

## What to try
1. Find the watch icon in the status bar, then Open big. Do all five tabs show?
2. Watch tab: turn on Let my watch connect. Open Hail Hermes on the watch and approve the code.
3. Talk from the watch. Does the Talk tab show the same thing live?
4. Ask a long question, then let the screen turn off. Does the answer keep playing to the end?
5. Ask something slow. Does it stay on Thinking without getting stuck, and give a plain error after 2 minutes if nothing comes?
6. Character tab: pick a personality, a voice and a face.
7. Sessions tab: Open in Hermes.

For each one, say what you saw. A screenshot or a short note is plenty.

---

Back to [docs/README.md](README.md).
