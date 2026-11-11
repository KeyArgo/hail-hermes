#!/usr/bin/env bash
# Press the watch's talk button N times and prove each press actually records.
#
# The owner's complaint was "one question works, the next does not", which cannot be judged by
# looking at a screenshot. This drives real presses with adb and reads the app's own log lines:
#   startHold: state=IDLE        the press reached the controller
#   startRecorder: started=true  the microphone actually opened
#   audio_end sent=true          the utterance was handed to the hub
#
# Usage: bash tools/stress_watch.sh [presses] [gap_seconds]
set -uo pipefail

PRESSES="${1:-4}"
GAP="${2:-18}"
ADB="${ADB:-C:/Users/Argo/work/platform-tools/adb.exe}"
SERIAL="${HAIL_ADB_SERIAL:-$("$ADB" devices | awk 'NR>1 && $2=="device" {print $1; exit}')}"
BUTTON_X="${BUTTON_X:-233}"
BUTTON_Y="${BUTTON_Y:-303}"

if [ -z "$SERIAL" ]; then
  echo "No watch connected. Turn on Wireless debugging and: $ADB connect <ip>:<port>" >&2
  exit 2
fi

say() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

"$ADB" -s "$SERIAL" shell input keyevent KEYCODE_WAKEUP >/dev/null 2>&1
"$ADB" -s "$SERIAL" shell am force-stop com.hermes.pocket.wear >/dev/null 2>&1
"$ADB" -s "$SERIAL" shell am start -n com.hermes.pocket.wear/.MainActivity >/dev/null 2>&1
say "app started; giving it time to connect"
sleep 12
"$ADB" -s "$SERIAL" logcat -c

ok=0
for i in $(seq 1 "$PRESSES"); do
  say "press $i of $PRESSES"
  "$ADB" -s "$SERIAL" shell input swipe "$BUTTON_X" "$BUTTON_Y" "$BUTTON_X" "$BUTTON_Y" 1500 >/dev/null 2>&1
  sleep "$GAP"
  if "$ADB" -s "$SERIAL" logcat -d 2>/dev/null | grep -q "startRecorder: started=true"; then
    ok=$((ok + 1))
    say "  press $i recorded"
  else
    say "  press $i did NOT open the microphone"
    "$ADB" -s "$SERIAL" logcat -d 2>/dev/null | grep -iE "startRecorder|startHold" | tail -3
  fi
  # Keep a fresh window for the next press's lines.
  "$ADB" -s "$SERIAL" logcat -c
done

echo
echo "$ok of $PRESSES presses opened the microphone"
"$ADB" -s "$SERIAL" logcat -d 2>/dev/null | grep -cE "audio_end sent=true" | xargs -I{} echo "audio_end sent: {}"
[ "$ok" -eq "$PRESSES" ]
