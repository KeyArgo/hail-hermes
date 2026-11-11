#!/usr/bin/env bash
# quickstart.sh — get Hail Hermes running on this computer with one command.
#
# Everything a person should never have to do by hand: install the plugin, enable it, start the hub
# and say whether the setup is healthy. Works the same for the owner and for a stranger.
set -uo pipefail
profile_args=()
if [ -n "${HAIL_PROFILE:-}" ]; then profile_args=(-p "$HAIL_PROFILE"); fi

echo "1/4 installing the plugin from the published build…"
hermes "${profile_args[@]}" plugins install "${HAIL_SOURCE:-KeyArgo/hail-hermes-dev}" \
  --yes-deps --enable --force 2>&1 | grep -E "Installed|installed|already|error|Error" | tail -2

echo "2/4 starting the hub…"
hermes "${profile_args[@]}" hail restart 2>&1 | tail -1

echo "3/4 checking that it answers…"
sleep 3
# `hermes hail` has no `status` command; the hub answers /status on its own port and `devices`
# proves it is the one holding it.
curl -s -m 5 http://127.0.0.1:8767/status | head -c 200 || echo "   (no answer on 8767 — is another profile's hub already running?)"
echo
hermes "${profile_args[@]}" hail devices 2>&1 | tail -3

echo "4/4 what still needs doing:"
curl -s -m 5 http://127.0.0.1:8767/status | grep -qi "running" \
  && echo "   hub is up. For the full checklist open the pane and press 'Check my setup'." \
  || echo "   hub did not answer — run 'hermes hail restart' and read the error."

cat <<'DONE'

Next, in Hermes Desktop:
  · Settings → Capabilities → Plugins → turn on Hail Hermes AND its Desktop part
  · Click the ⌚ Hail Hermes mark in the status bar to open the pane
  · In the pane: "Let my watch connect" → type the 4 digits on the watch

No watch? The pane still answers by voice and text — the watch is optional.
DONE
