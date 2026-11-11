#!/usr/bin/env bash
# sync-desktop-half.sh — copy the canonical pane module to the plugin root.
#
# Hermes mounts a plugin's desktop half from <plugin>/desktop/plugin.js and allows only
# @hermes/plugin-sdk and react as imports, so the root file cannot point at the package copy. It has to
# be the real file; this keeps the two identical and a test fails if they drift.
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cp "$repo/plugin/hail_hermes/desktop/plugin.js" "$repo/desktop/plugin.js"
echo "synced desktop/plugin.js <- plugin/hail_hermes/desktop/plugin.js ($(wc -c < "$repo/desktop/plugin.js") bytes)"
