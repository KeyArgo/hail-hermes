#!/usr/bin/env bash
# pack-plugin.sh [outdir] — build the installable, flat plugin directory from source.
#
# Why a separate flat layout exists: `hermes plugins install <owner/repo>` loads plugin.yaml and
# expects loadable code *beside* it (__init__.py or desktop/plugin.js). The source repo keeps a pip
# layout (plugin/hail_hermes/...) because that is what the tests import, and rewriting it would break
# 300+ imports for no product gain. So the repo ships sources and this script emits the exact
# directory the installer wants — the same flattening tools/deploy-plugin.sh does locally.
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
out="${1:-$repo/build/plugin-dist}"

rm -rf "$out"
mkdir -p "$out"
cp -r "$repo/plugin/hail_hermes/." "$out/"
cp "$repo/plugin/plugin.yaml" "$out/"
cp "$repo/README.md" "$out/README.md" 2>/dev/null || true
cp "$repo/LICENSE" "$out/LICENSE" 2>/dev/null || true
find "$out" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$out" -name '*.pyc' -delete 2>/dev/null || true

# The manifest must describe what the code actually registers, or `validate` fails and a judge
# installs a plugin that does nothing. Fail here instead of at submission.
# Native hermes cannot resolve MSYS paths (/c/...), so hand it a Windows path.
hermes plugins validate "$(cygpath -w "$out")" 2>&1 | tail -3

# Scrub caches AFTER validating: `validate` imports the plugin (register() probe), which writes
# __pycache__ straight back into the tree. Committed bytecode is 20 HIGH findings in the installer's
# security scan, and enough of them block the install outright for anyone who is not us.
find "$out" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$out" -name '*.pyc' -delete 2>/dev/null || true
printf '__pycache__/\n*.pyc\n' > "$out/.gitignore"
echo "packed -> $out"
