#!/usr/bin/env bash
# stamp-build.sh [--commit] — write BUILD_INFO.json so an INSTALLED copy reports the real build.
#
# Why: version_stamp derives X.Y.Z-dev.<commit count> from git. That is right in a dev checkout (339
# commits) and wrong in an installed copy, which is shallow-cloned with ONE commit — the same code
# reported 1.0.0-dev.339 in one place and 1.0.0-dev.1 in another. BUILD_INFO.json is the precedence-1
# source in version_stamp, so stamping it at publish time makes every copy tell the same truth.
#
# Run before you hand the repo to anyone (submission, demo, release). With --commit it stages the file
# too, which pins the artifact identity to this exact commit.
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

count="$(git rev-list --count HEAD)"
commit="$(git rev-parse HEAD)"
date_iso="$(git log -1 --format=%cI)"
declared="$(grep -m1 '^version' plugin.yaml | sed 's/version: *//; s/"//g')"
# If the manifest already declares -dev.N use it verbatim; otherwise append the count.
# The manifest is the single source of truth: no tag is allowed (rule: the first public release is
# the first non-dev version), and a computed -dev.N made two copies of the same code disagree.
case "$declared" in
  *) version="$declared" ;;

esac

cat > plugin/hail_hermes/BUILD_INFO.json <<JSON
{
  "version": "$version",
  "commit": "$commit",
  "commit_short": "${commit:0:8}",
  "commit_date": "$date_iso",
  "built_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "channel": "dev",
  "dirty": false
}
JSON

echo "plugin/hail_hermes/BUILD_INFO.json -> version $version  (commit ${commit:0:8})"
echo "every copy that installs from this commit now reports that version, whatever its git depth."

if [ "${1:-}" = "--commit" ]; then
  git add plugin/hail_hermes/BUILD_INFO.json
  echo "staged. commit it to pin this artifact, or leave it untracked for local verification."
fi
