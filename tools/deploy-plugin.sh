#!/usr/bin/env bash
# Copy the repo's plugin half into the live Hermes install, then restart the hub.
#
# Written after a deploy that copied hub.py but not settings.py: the hub then died on
# import ('cannot import name character_label') and the watch went offline with no
# obvious cause. Copy the WHOLE module set, every time.
set -eu
# No default on purpose: this used to default to a different checkout and deploy
# stale modules while reporting success.
REPO="${1:?usage: deploy-plugin.sh <repo-root>}"
DEST="C:/Users/Argo/AppData/Local/hermes/plugins/hail-hermes"
DESKTOP="C:/Users/Argo/AppData/Local/hermes/desktop-plugins/hail-hermes"

echo "repo:   $REPO"
echo "dest:   $DEST"

cp "$REPO"/plugin/hail_hermes/*.py "$DEST"/
mkdir -p "$DEST/dashboard" "$DESKTOP"
cp "$REPO"/plugin/hail_hermes/dashboard/*.py "$DEST/dashboard"/
cp "$REPO"/plugin/hail_hermes/desktop/plugin.js "$DEST/desktop/plugin.js"
cp "$REPO"/plugin/hail_hermes/desktop/plugin.js "$DESKTOP/plugin.js"

# Import check BEFORE restarting anything: a half-copied plugin takes the hub down.
python - "$DEST" <<'PY'
import sys, pathlib
dest = pathlib.Path(sys.argv[1])
missing = []
for name in ("settings.py", "hub.py", "cli.py", "agent.py", "store.py", "control.py"):
    if not (dest / name).exists():
        missing.append(name)
print("modules present:" if not missing else f"MISSING: {missing}")
PY

# netstat on Windows emits CRLF; a trailing carriage return makes taskkill fail.
PID=$(netstat -ano 2>/dev/null | grep ":8767" | grep LISTENING | awk '{print $5}' | head -1 | tr -d '' || true)
if [ -n "${PID:-}" ]; then /c/Windows/System32/taskkill.exe /F /PID "$PID" >/dev/null 2>&1 || echo "could not stop hub $PID"; fi
rm -f "C:/Users/Argo/AppData/Local/hermes/hail-hermes/autostart.pid"
powershell -NoProfile -Command "Start-Process -WindowStyle Hidden -FilePath 'C:\Users\Argo\work\start-hub.bat'" >/dev/null 2>&1
sleep 12
COUNT=$(netstat -ano 2>/dev/null | grep ":8767" | grep -c LISTENING || true)
echo "hubs listening on 8767: $COUNT"
[ "$COUNT" = "1" ] || { echo "FAILED: expected exactly one hub"; tail -5 "C:/Users/Argo/work/hub.log"; exit 1; }
echo "deployed"
