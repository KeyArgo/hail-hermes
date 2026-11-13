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
HERMES="${HERMES_HOME:-$HOME/.hermes}"
DEST="${DEST:-$HERMES/plugins/hail-hermes}"
DESKTOP="${DESKTOP:-$HERMES/desktop-plugins/hail-hermes}"

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

# Restart through the product's own command, not a per-machine launcher: the old version hard-coded
# a Windows batch file in one person's home directory, which is neither portable nor publishable.
if ! hermes hail restart >/dev/null 2>&1; then
  echo "FAILED: could not restart the hub (run: hermes hail restart)" >&2
  tail -5 "${HUB_LOG:-$HERMES/hail-hermes/hub.log}" 2>/dev/null || true
  exit 1
fi

# Ask the hub itself, instead of counting `hermes hail devices` lines (quality review): that command lists
# devices, so two paired watches failed the check and no paired devices let a dead hub pass. This waits for
# the product's own /status to say running, and fails loudly if it never does.
if ! python "$REPO/tools/hub-health.py" "${HAIL_HUB_URL:-http://127.0.0.1:8767/status}" \
     --timeout "${HAIL_HEALTH_TIMEOUT:-40}"; then
  echo "FAILED: the hub is not answering after the restart" >&2
  tail -5 "${HUB_LOG:-$HERMES/hail-hermes/hub.log}" 2>/dev/null || true
  exit 1
fi
echo "deployed"
