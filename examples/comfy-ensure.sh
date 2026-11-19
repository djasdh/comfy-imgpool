#!/usr/bin/env bash
# Minimal comfy-ensure.sh for imggen.
#
# imggen calls this helper when ComfyUI is not answering. It must:
#   - exit 0 when ComfyUI is reachable (start it if needed)
#   - exit non-zero on failure
#
# This example assumes a systemd --user unit. Adapt it to your setup
# (tmux, docker, a plain background process, ...).

set -uo pipefail

URL="${COMFY_URL:-http://127.0.0.1:8188}"
UNIT="${COMFY_UNIT:-comfyui}"
WAIT="${COMFY_START_TIMEOUT:-180}"
ACTIVITY="${COMFY_ACTIVITY_FILE:-${IMGPOOL_STATE_DIR:-/tmp/comfy-imgpool}/comfy-last-activity}"

up() { curl -sS -m 2 "$URL/system_stats" >/dev/null 2>&1; }
touch_activity() {
  mkdir -p "$(dirname "$ACTIVITY")" 2>/dev/null
  touch "$ACTIVITY" 2>/dev/null || true
}

if up; then
  touch_activity
  echo "already-running: $URL"
  exit 0
fi

systemctl --user start "$UNIT" || exit 3

for i in $(seq 1 "$WAIT"); do
  if up; then
    touch_activity
    echo "started in ${i}s"
    exit 0
  fi
  systemctl --user is-active --quiet "$UNIT" || exit 4
  sleep 1
done

echo "timeout: ${WAIT}s elapsed, $URL still not answering" >&2
exit 5
