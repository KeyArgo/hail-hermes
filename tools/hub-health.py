#!/usr/bin/env python3
"""Ask the running hub whether it is up, and say what it answered.

Quality review: the deploy script decided the hub was healthy by counting the lines `hermes hail devices`
printed and expecting exactly one. That command lists *devices*: two paired watches made the check fail, and
no paired devices made a crashed hub look fine. The hub's own `/status` route is the product's answer — the
same one the pane reads — so this asks it directly, with a bounded wait for a restart to come up.

Usage: python tools/hub-health.py [url] [--timeout SECONDS]
Exit 0 when the hub answers with ``running: true``, 1 otherwise (with the last failure on stderr).
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8767/status"
DEFAULT_TIMEOUT = 40.0
POLL_SECONDS = 1.5


def read_status(url: str, timeout: float = 3.0) -> dict:
    """One request. Raises on anything that is not a JSON object."""
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8"))
    if not isinstance(body, dict):
        raise ValueError("status is not a JSON object")
    return body


def wait_for_hub(url: str = DEFAULT_URL, timeout: float = DEFAULT_TIMEOUT,
                 poll: float = POLL_SECONDS, sleep=time.sleep) -> tuple[bool, str]:
    """Poll until the hub reports running, or the deadline passes. Returns (ok, message)."""
    deadline = time.monotonic() + max(0.0, timeout)
    last = "no answer yet"
    while True:
        try:
            body = read_status(url)
            if body.get("running") is True:
                devices = body.get("devices")
                count = len(devices) if isinstance(devices, list) else 0
                return True, (f"hub is up at {url}: v{body.get('version', '?')} "
                              f"mode={body.get('mode', '?')} devices={count}")
            last = f"it answered, but running={body.get('running')!r}"
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
            last = str(exc) or exc.__class__.__name__
        if time.monotonic() >= deadline:
            return False, f"the hub did not answer {url}: {last}"
        sleep(poll)


def main(argv: list[str]) -> int:
    url = DEFAULT_URL
    timeout = DEFAULT_TIMEOUT
    args = list(argv[1:])
    if "--timeout" in args:
        index = args.index("--timeout")
        try:
            timeout = float(args[index + 1])
        except (IndexError, ValueError):
            print("--timeout needs a number of seconds", file=sys.stderr)
            return 2
        del args[index:index + 2]
    if args:
        url = args[0]
    ok, message = wait_for_hub(url, timeout)
    print(message if ok else f"FAILED: {message}", file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
