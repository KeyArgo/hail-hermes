#!/usr/bin/env bash
# Run every tests directory in this repo and write RESULT.txt.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
root="$PWD"
# The virtual environment lives OUTSIDE the repo: it is 92 MB of symlinks that must never be copied
# into an installed plugin, which is exactly what happened once and blocked the next deploy.
venv="${TMPDIR:-/tmp}/hail-hermes-venv-test"
python3 -m venv "$venv" 2>/dev/null || true
py="$venv/bin/python"
[[ -x "$py" ]] || py=$(command -v python3)
"$py" -m pip install --quiet --upgrade pip
"$py" -m pip install --quiet pytest

mapfile -t suites < <(find . -type d -name tests -not -path "*/.venv*" -not -path "*/node_modules/*" -not -path "*/__pycache__/*" | sort)

# Install the dependencies of the project each suite belongs to — the directory one level up.
# Hail has no manifest at the repo root: they live in plugin/ and tools/*/ , and the parser tests
# need packages declared only in the `test` extra (numpy). A root-only install leaves a fresh
# machine unable to import the tests at all, which looks like a code failure and is not one.
declare -A done_project=()
for s in "${suites[@]}"; do
  proj=$(dirname "$s")
  [[ -n "${done_project[$proj]:-}" ]] && continue
  done_project[$proj]=1
  for f in requirements.txt requirements-dev.txt; do
    [[ -f "$proj/$f" ]] && "$py" -m pip install --quiet -r "$proj/$f"
  done
  if [[ -f "$proj/pyproject.toml" ]]; then
    "$py" -m pip install --quiet -e "$proj[test]" 2>/dev/null \
      || "$py" -m pip install --quiet -e "$proj" 2>/dev/null
  fi
done

overall=0; rows=()
for s in "${suites[@]}"; do
  out=$("$py" -m pytest "$s" -o addopts="" -q -p no:cacheprovider 2>&1); code=$?
  [[ $code -ne 0 && $code -ne 5 ]] && overall=$code
  line=$(printf '%s\n' "$out" | grep -oE '[0-9]+ (passed|failed|error)[^=]*' | tail -1)
  rows+=("${s#./} :: ${line:-exit $code}")
done
{
  echo "plugin:    $(basename "$root")"
  echo "host:      $(uname -srm)"
  echo "python:    $("$py" --version 2>&1)"
  echo "commit:    $(git rev-parse --short HEAD 2>/dev/null)"
  echo "exit:      $overall"
  echo "suites:"
  for r in "${rows[@]}"; do echo "  $r"; done
  echo "run at:    $(date -Iseconds)"
} | tee RESULT.txt
exit $overall
