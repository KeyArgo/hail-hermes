#!/usr/bin/env python3
"""Break-it test for Hail Hermes on a real Wear OS watch (adb).

Rule under test: the watch never sits on "Thinking…" with nothing happening. After every disruption, a Talk press must
end in speech or a visible error within LIMIT seconds. We read the screen with screencap and look at the big Talk button:
amber = thinking, anything else = moved on.

Usage: watch_chaos.py --serial IP:PORT --hub-cmd "<shell command that (re)starts the hub>" [--only NAME]
The hub must be reachable from the watch (adb reverse tcp:8767 tcp:8767 is re-applied by this script).
"""
import argparse, io, os, signal, subprocess, sys, time, zlib, struct, shlex

ADB = os.path.expanduser("~/Android/Sdk/platform-tools/adb")
PKG = "com.hermes.pocket.wear"
BUTTON = (225, 310)          # centre of the big Talk button on a 450x450 round screen (used for presses)
PROBE = (120, 300)           # a pixel inside the button but clear of its label (used to read the colour)
LIMIT = 20.0                 # seconds a Talk press may stay "thinking" before we call it stuck


def adb(serial, *args, timeout=30, binary=False):
    out = subprocess.run([ADB, "-s", serial, *args], capture_output=True, timeout=timeout)
    return out.stdout if binary else out.stdout.decode(errors="replace")


def pixel(serial, x, y):
    """Decode one pixel from a PNG screencap without third-party libraries."""
    png = adb(serial, "exec-out", "screencap", "-p", binary=True)
    if len(png) < 100:
        return None  # screen off
    pos, w, h, data = 8, 0, 0, b""
    while pos < len(png):
        ln, typ = struct.unpack(">I4s", png[pos:pos + 8]); body = png[pos + 8:pos + 8 + ln]; pos += 12 + ln
        if typ == b"IHDR": w, h, depth, ctype = struct.unpack(">IIBB", body[:10])
        elif typ == b"IDAT": data += body
    raw = zlib.decompress(data); bpp = 4 if ctype == 6 else 3; stride = w * bpp
    prev = bytearray(stride); row = None
    for r in range(y + 1):
        f = raw[r * (stride + 1)]; line = bytearray(raw[r * (stride + 1) + 1:(r + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0; b = prev[i]; c = prev[i - bpp] if i >= bpp else 0
            if f == 1: line[i] = (line[i] + a) & 255
            elif f == 2: line[i] = (line[i] + b) & 255
            elif f == 3: line[i] = (line[i] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c; pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        prev = line
    return tuple(prev[x * bpp:x * bpp + 3])


def is_thinking(rgb):
    return rgb is not None and rgb[0] > 150 and rgb[0] > rgb[1] > rgb[2] and rgb[2] < 120 and rgb[0] - rgb[2] > 100   # amber


def wake_and_open(serial):
    adb(serial, "shell", "input", "keyevent", "KEYCODE_WAKEUP")
    adb(serial, "shell", "am", "start", "-n", f"{PKG}/.MainActivity")
    adb(serial, "reverse", "tcp:8767", "tcp:8767")
    time.sleep(3)


def hold_talk(serial, ms=1500):
    adb(serial, "shell", "input", "swipe", *map(str, BUTTON + BUTTON), str(ms))


def ask_and_check(serial, name):
    """Hold Talk (the watch hears silence -> hub must say "didn't catch that" or the watch must show an error)."""
    hold_talk(serial)
    start = time.time(); thinking_since = None
    while time.time() - start < LIMIT + 10:
        rgb = pixel(serial, *PROBE)
        if is_thinking(rgb):
            thinking_since = thinking_since or time.time()
            if time.time() - thinking_since > LIMIT:
                return False, f"stuck on Thinking for > {LIMIT:.0f}s"
        elif thinking_since is not None or time.time() - start > 4:
            return True, f"moved on after {time.time() - start:.1f}s (button rgb {rgb})"
        time.sleep(1)
    return False, "no change seen"


class Hub:
    def __init__(self, cmd): self.cmd, self.proc = cmd, None
    def start(self):
        self.proc = subprocess.Popen(shlex.split(self.cmd), start_new_session=True,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); time.sleep(6)
    def stop(self):
        if self.proc and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM); self.proc.wait(10)


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--serial", required=True); ap.add_argument("--hub-cmd", required=True)
    ap.add_argument("--only"); a = ap.parse_args(); s = a.serial; hub = Hub(a.hub_cmd)
    scenarios = {
        "baseline": lambda: None,
        "hub_restart_before_ask": lambda: (hub.stop(), hub.start()),
        "hub_down_during_ask": lambda: hub.stop(),          # ask while the hub is gone -> must show an error
        "screen_off_then_ask": lambda: (adb(s, "shell", "input", "keyevent", "KEYCODE_SLEEP"), time.sleep(40)),
        "link_dropped": lambda: adb(s, "reverse", "--remove", "tcp:8767"),
        "app_killed": lambda: adb(s, "shell", "am", "force-stop", PKG),
    }
    results = []
    hub.start()
    for name, disrupt in scenarios.items():
        if a.only and name != a.only: continue
        wake_and_open(s)
        disrupt()
        if name not in ("link_dropped", "hub_down_during_ask"): wake_and_open(s)
        else:
            adb(s, "shell", "input", "keyevent", "KEYCODE_WAKEUP")
        ok, why = ask_and_check(s, name)
        results.append((name, ok, why)); print(f"{'PASS' if ok else 'FAIL'}  {name}: {why}", flush=True)
        if hub.proc is None or hub.proc.poll() is not None: hub.start()
        adb(s, "reverse", "tcp:8767", "tcp:8767"); time.sleep(3)
    hub.stop()
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} scenarios passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
