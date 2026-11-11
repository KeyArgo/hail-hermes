#!/usr/bin/env bash
# Deploy Hail Hermes: install this tree, stamp the build, prove what is installed.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALLED="$HOME/.hermes/plugins/hail-hermes"
cd "$REPO"

VERSION="$(grep -m1 '^version:' plugin.yaml | cut -d: -f2- | tr -d '"' | xargs)"
COMMIT="$(git rev-parse HEAD)"
COMMIT_DATE="$(git log -1 --format=%cI)"
BUILT_AT="$(date -Iseconds)"
# One source for the number: the same module the runtime reads, so a tagged release deploys as
# "1.1.0" here and reports "1.1.0" from the hub. Computing it in the shell made a tagged tree deploy
# as 1.1.0-dev.N while version_stamp called it a release — two answers to one question.
DISPLAY="$("$REPO/.venv/bin/python" -c "import sys; sys.path.insert(0, '$REPO/plugin'); from hail_hermes.version_stamp import describe; print(describe().split(' · ')[0])" 2>/dev/null || echo "${VERSION}-dev.$(git rev-list --count HEAD)")"
DIRTY="false"
# Build outputs and the stamp itself are excluded: counting them would mark every deploy modified.
[ -n "$(git status --porcelain -- . ':!dist' ':!BUILD_INFO.json' ':!*.pyc')" ] && DIRTY="true"

echo "==> installing $COMMIT"
hermes plugins install KeyArgo/hail-hermes-dev --ref "$COMMIT" --force --yes-deps --enable 2>&1 | tail -2

python3 - "$INSTALLED" "$DISPLAY" "$VERSION" "$COMMIT" "$COMMIT_DATE" "$BUILT_AT" "$DIRTY" <<'PY'
import json, pathlib, sys
installed, display, release, commit, date, built, dirty = sys.argv[1:8]
payload = {"version": display, "release": release, "commit": commit, "commit_date": date,
           "built_at": built, "dirty": dirty == "true",
           "channel": "dev" if "-dev." in display else "release"}
pathlib.Path(installed, "BUILD_INFO.json").write_text(json.dumps(payload, indent=2) + "\n")
print(f"==> stamped {display} · {commit[:8]}")
PY

# Hermes re-applies local edits on top of an install; sync afterwards so the tree is what runs.
rsync -a --exclude '.git' --exclude '__pycache__' --exclude '*.pyc' --exclude 'BUILD_INFO.json' \
  --exclude 'node_modules' "$REPO/" "$INSTALLED/"

python3 scripts/verify-served.py
