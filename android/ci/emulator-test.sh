#!/usr/bin/env bash
# Runs in the Android emulator job: installs the APK, starts the app and checks real downloads on the device.
# A test clip is served by the CI machine, which the emulator reaches at 10.0.2.2.
set -euo pipefail
APK="$1"
PKG=io.github.mdzaman.offlinevideo
PORT=48623
API="http://127.0.0.1:$PORT"

adb install -r "$APK"
adb shell pm grant "$PKG" android.permission.POST_NOTIFICATIONS || true
adb shell am start -W -n "$PKG/.MainActivity"
adb forward tcp:$PORT tcp:$PORT

echo "Waiting for the download engine…"
status=""
for _ in $(seq 1 120); do
  status=$(curl -s --max-time 3 "$API/api/status" || true)
  [ -n "$status" ] && break
  sleep 2
done
echo "status: $status"
if [ -z "$status" ]; then adb logcat -d | grep -iE "OfflineVideo|AndroidRuntime" | tail -80; exit 1; fi
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d["android"] and d["ffmpeg"], d' "$status"

# Queue a download; prints the finished job as JSON.
download() {
  local id
  id=$(curl -s -X POST -H 'Content-Type: application/json' -d "$1" "$API/api/downloads" \
       | python3 -c 'import json,sys; print(json.load(sys.stdin)["jobs"][0]["id"])')
  for _ in $(seq 1 ${2:-90}); do
    job=$(curl -s "$API/api/jobs" | python3 -c '
import json, sys
j = [j for j in json.load(sys.stdin)["jobs"] if j["id"] == sys.argv[1]][0]
print(json.dumps(j) if j["status"] in ("done", "error", "cancelled") else "")' "$id")
    if [ -n "$job" ]; then echo "$job"; return; fi
    sleep 2
  done
  echo '{"status": "timeout"}'
}

echo "== MP3 conversion with FFmpeg on the device"
job=$(download '{"url": "http://10.0.2.2:8765/clip.mp4", "output": "mp3"}')
echo "$job"
python3 -c 'import json,sys; j=json.loads(sys.argv[1]); assert j["status"]=="done" and j["files"][0].endswith(".mp3"), j' "$job"

echo "== MKV remux"
job=$(download '{"url": "http://10.0.2.2:8765/clip.mp4", "output": "mkv"}')
echo "$job"
python3 -c 'import json,sys; j=json.loads(sys.argv[1]); assert j["status"]=="done" and j["files"][0].endswith(".mkv"), j' "$job"

echo "== Page loads in the app"
curl -sf "$API/" | grep -q 'is-android'

echo "== Real YouTube video (informational: YouTube often blocks data-centre addresses)"
download '{"url": "https://www.youtube.com/watch?v=jNQXAC9IVRw", "output": "mp4", "quality": "360"}' 120 || true

sleep 3
adb exec-out screencap -p > emulator-screenshot.png || true
adb logcat -d -s OfflineVideo:* | tail -40 || true
echo "All Android checks passed"
