#!/usr/bin/env python3
import json
import os
import sys
import time

stdin_text = ""
if "--query-file" in sys.argv:
    qidx = sys.argv.index("--query-file")
    if qidx + 1 < len(sys.argv) and sys.argv[qidx + 1] == "-":
        stdin_text = sys.stdin.read()

pid = os.getpid()

pid_file = os.environ.get("FAKE_HERMES_PID_FILE")
if pid_file:
    with open(pid_file, "w", encoding="utf-8") as f:
        f.write(str(pid))

record_log = os.environ.get("FAKE_RECORD_LOG")
if record_log:
    entry = {
        "argv": sys.argv[1:],
        "stdin": stdin_text,
        "pid": pid,
    }
    with open(record_log, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

mode = os.environ.get("FAKE_HERMES_MODE", "replay")

if mode == "hang":
    print(json.dumps({"type": "system", "subtype": "init", "model": "", "session_id": ""}), flush=True)
    time.sleep(60)
    sys.exit(0)

if mode == "crash":
    sys.stderr.write("unhandled crash\n")
    sys.stderr.flush()
    sys.exit(3)

if mode == "nostream":
    print(json.dumps({"type": "system", "subtype": "init", "model": "", "session_id": ""}), flush=True)
    print(json.dumps({"type": "result", "session_id": "sess-nostream", "exit_code": 0, "text": "whole reply"}), flush=True)
    sys.exit(0)

fixture = os.environ.get("FAKE_HERMES_FIXTURE")
exit_code = 0
if fixture and os.path.exists(fixture):
    with open(fixture, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            print(line, flush=True)
            try:
                data = json.loads(line)
                if data.get("type") == "result":
                    exit_code = data.get("exit_code", 0)
            except Exception:
                pass
sys.exit(exit_code)
