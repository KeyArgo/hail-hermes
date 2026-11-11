#!/usr/bin/env bash
# install-when-visible.sh [apk] [seconds] — install the watch build the moment the watch appears on adb.
#
# The watch sleeps, drops Wi-Fi and leaves adb without warning, so "install failed, no devices" is
# almost always "not awake yet" rather than a broken build. Polling in the foreground burns the
# session; this waits in the background and reports once.
set -uo pipefail
apk="${1:-/c/Users/Argo/work/hail-wear-tonight.apk}"
deadline=$(( $(date +%s) + ${2:-2400} ))
adb="/c/Users/Argo/work/platform-tools/adb.exe"

while [ "$(date +%s)" -lt "$deadline" ]; do
  serial="$("$adb" devices | awk 'NR>1 && $2=="device" {print $1; exit}')"
  if [ -n "${serial:-}" ]; then
    echo "watch visible: $serial"
    "$adb" -s "$serial" install -r -d "$apk" 2>&1 | tail -1
    "$adb" -s "$serial" shell input keyevent KEYCODE_WAKEUP >/dev/null 2>&1
    "$adb" -s "$serial" shell am force-stop com.hermes.pocket.wear >/dev/null 2>&1
    "$adb" -s "$serial" shell am start -n com.hermes.pocket.wear/.MainActivity >/dev/null 2>&1
    sleep 8
    echo "installed and launched; hub sees:"
    (cd /c/Users/Argo && hermes hail devices 2>&1 | tail -2)
    exit 0
  fi
  sleep 15
done
echo "gave up: the watch never appeared on adb within $(( ${2:-2400} / 60 )) minutes"
exit 1
