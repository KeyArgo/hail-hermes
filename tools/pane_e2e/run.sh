#!/usr/bin/env bash
# One-command isolated E2E for the hermes-pocket Desktop pane.
# Uses a temp HOME/HERMES_HOME and the Desktop Webapp on port 19120.
# Never touches ~/.hermes or the default webapp port (9120).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
PLUGIN_SRC="$REPO_ROOT/plugin/hail_hermes"
OUT_DIR="$HERE/out"
LOG="$OUT_DIR/webapp.log"
mkdir -p "$OUT_DIR"
TMP_HOME="$(mktemp -d)"
export HOME="$TMP_HOME"
export HERMES_HOME="$TMP_HOME/.hermes"
cleanup() {
  if [[ -n "${WEBAPP_PID:-}" ]]; then kill "$WEBAPP_PID" 2>/dev/null || true; wait "$WEBAPP_PID" 2>/dev/null || true; fi
  hermes gateway stop >/dev/null 2>&1 || true
  rm -rf "$TMP_HOME"
}
trap cleanup EXIT
[[ -f "$PLUGIN_SRC/plugin.yaml" ]] || { echo "plugin not found: $PLUGIN_SRC" >&2; exit 1; }
command -v hermes >/dev/null || { echo "hermes not on PATH" >&2; exit 1; }
mkdir -p "$HERMES_HOME/plugins"
ln -s "$PLUGIN_SRC" "$HERMES_HOME/plugins/hail-hermes"
cat > "$HERMES_HOME/config.yaml" <<'YAML'
webapp:
  enabled: true
  host: 127.0.0.1
  port: 19120
plugins:
  enabled:
    - hail-hermes
YAML

# --- Start the webapp in the background; capture the one-use link. ---
: > "$LOG"
hermes webapp --no-open >>"$LOG" 2>&1 &
WEBAPP_PID=$!

echo "waiting for one-use link (expires after 60s)..."
for _ in $(seq 1 60); do
  if [[ -s "$LOG" ]]; then
    LINK="$(grep -Eo 'https?://127\.0\.0\.1(:[0-9]+)?/[^[:space:]]+' "$LOG" | tail -n 1 || true)"
    [[ -n "$LINK" ]] && break
  fi
  kill -0 "$WEBAPP_PID" 2>/dev/null || { echo "webapp exited early; log:" >&2; cat "$LOG" >&2; exit 1; }
  sleep 1
done
[[ -n "$LINK" ]] || { echo "error: no link found; log:" >&2; cat "$LOG" >&2; exit 1; }


# --- Run the Playwright spec. ---
LINK="$LINK" OUT_DIR="$OUT_DIR" node "$HERE/pane.spec.mjs"

echo "done. screenshots in $OUT_DIR"
