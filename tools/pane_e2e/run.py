#!/usr/bin/env python3
"""One-command isolated E2E for the hermes-pocket Desktop pane.

Python stdlib port of run.sh, working on Windows and Linux.
Uses a temp HOME/HERMES_HOME and the Desktop Webapp on port 19120.
Never touches ~/.hermes or the default webapp port (9120).
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
PLUGIN_SRC = REPO_ROOT / "plugin" / "hail_hermes"
OUT_DIR = HERE / "out"
LOG = OUT_DIR / "webapp.log"

CONFIG_YAML = """\
webapp:
  enabled: true
  host: 127.0.0.1
  port: 19120
plugins:
  enabled:
    - hail-hermes
"""

LINK_RE = re.compile(r"https?://127\.0\.0\.1(?::\d+)?/\S+")


def wait_for_link(log_path, proc, timeout_s):
    """Poll the log for the last one-use link; None if the wait times out."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        matches = LINK_RE.findall(text)
        if matches:
            return matches[-1]
        if proc.poll() is not None:
            print("webapp exited early; log:", file=sys.stderr)
            print(text, file=sys.stderr)
            return None
        time.sleep(1)
    return None


def main():
    if not (PLUGIN_SRC / "plugin.yaml").is_file():
        print(f"plugin not found: {PLUGIN_SRC}", file=sys.stderr)
        return 1
    hermes = shutil.which("hermes")
    if not hermes:
        print("hermes not on PATH", file=sys.stderr)
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp_home = Path(tempfile.mkdtemp(prefix="hermes-pane-e2e-"))
    hermes_home = tmp_home / ".hermes"
    (hermes_home / "plugins").mkdir(parents=True, exist_ok=True)

    proc = None
    try:
        plugin_dst = hermes_home / "plugins" / "hail-hermes"
        shutil.copytree(PLUGIN_SRC, plugin_dst, dirs_exist_ok=True)
        # Hermes's Electron app does this copy at startup; the browser webapp may not.
        desktop_dst = hermes_home / "desktop-plugins" / "hail-hermes"
        shutil.copytree(PLUGIN_SRC / "desktop", desktop_dst, dirs_exist_ok=True)
        (desktop_dst / ".hermes-package.json").write_text(
            json.dumps(
                {"package": "hail-hermes", "source": str(plugin_dst), "sourceMtimeMs": 0}
            ),
            encoding="utf-8",
        )
        (hermes_home / "config.yaml").write_text(CONFIG_YAML, encoding="utf-8")

        env = os.environ.copy()
        env["HOME"] = str(tmp_home)
        env["USERPROFILE"] = str(tmp_home)
        env["HERMES_HOME"] = str(hermes_home)

        LOG.write_text("", encoding="utf-8")
        with LOG.open("a", encoding="utf-8") as log_fh:
            proc = subprocess.Popen(
                [hermes, "webapp", "--no-open"],
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=str(tmp_home),
            )

        print("waiting for one-use link (expires after 60s)...")
        link = wait_for_link(LOG, proc, timeout_s=60)
        if not link:
            print("error: no link found", file=sys.stderr)
            return 1

        spec_env = os.environ.copy()
        spec_env["LINK"] = link
        spec_env["OUT_DIR"] = str(OUT_DIR)
        result = subprocess.run(
            ["node", str(HERE / "pane.spec.mjs")],
            env=spec_env,
            cwd=str(REPO_ROOT),
        )
        if result.returncode != 0:
            return result.returncode
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        shutil.rmtree(tmp_home, ignore_errors=True)

    print(f"done. screenshots in {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
