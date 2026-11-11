#!/usr/bin/env bash
# validate-repo.sh — check exactly what a judge gets when they install this repo from git.
#
# The old flow published a flattened build to a second repo. That repo is retired: the source repo
# installs directly now (root plugin.yaml + __init__.py wrapper), so the thing to verify is the
# tracked tree — not the working directory, which is full of venvs, builds and scratch files that a
# clone would never contain.
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

cd "$repo"
git archive HEAD | tar -x -C "$tmp"

echo "tracked tree: $(du -sh "$tmp" | cut -f1)"

# The two halves a plugin needs at its ROOT, because that is what an install copies. Missing either one
# installs a plugin that loads and does nothing visible: no pane, or a pane whose backend 500s.
for half in desktop/plugin.js dashboard/manifest.json dashboard/plugin_api.py; do
  if [ -f "$tmp/$half" ]; then echo "present: $half"; else echo "MISSING: $half" >&2; fi
done
# Native hermes cannot read MSYS paths.
hermes plugins validate "$(cygpath -w "$tmp")" 2>&1 | grep -E "loadable|security scan|✗|Validation"

if hermes plugins validate "$(cygpath -w "$tmp")" 2>&1 | grep -q "Validation passed"; then
  echo "OK: a git clone of this repo is installable"
else
  echo "FAIL: the tracked tree does not validate — do not tell anyone to install it" >&2
  exit 1
fi
