#!/usr/bin/env python3
"""Fake hermes that ignores SIGTERM (cancellation must escalate to SIGKILL)."""
import json
import os
import signal
import sys
import time


def _ignore_sigterm(_signum, _frame):
    pass


signal.signal(signal.SIGTERM, _ignore_sigterm)

if "--query-file" in sys.argv:
    qidx = sys.argv.index("--query-file")
    if qidx + 1 < len(sys.argv) and sys.argv[qidx + 1] == "-":
        sys.stdin.read()

pid_file = os.environ.get("FAKE_HERMES_PID_FILE")
if pid_file:
    with open(pid_file, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))

print(json.dumps({"type": "system", "subtype": "init", "model": "", "session_id": ""}), flush=True)
print(json.dumps({"type": "text", "text": "start"}), flush=True)
while True:
    time.sleep(0.05)
