#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
cd /app

if [[ -z "${VNC_PASSWORD:-}" || ! "${VNC_PASSWORD:-}" =~ ^[A-Za-z0-9]{8}$ ]]; then
  echo 'ERROR: VNC_PASSWORD must contain exactly 8 ASCII letters/digits. Never expose port 6080 publicly.' >&2
  exit 2
fi
if [[ "${START_MODE:-manual}" != "manual" && "${START_MODE:-manual}" != "watch" ]]; then
  echo 'ERROR: START_MODE must be manual or watch.' >&2
  exit 2
fi
if [[ ! -w /app/data ]]; then
  echo 'ERROR: /app/data is not writable by UID 10001. On the VPS: sudo chown -R 10001:10001 data' >&2
  exit 2
fi

mkdir -p "$HOME/.vnc"
chmod 700 "$HOME/.vnc"
touch "$XAUTHORITY"
chmod 600 "$XAUTHORITY"
xauth -f "$XAUTHORITY" add "$DISPLAY" . "$(openssl rand -hex 16)" >/dev/null
x11vnc -storepasswd "$VNC_PASSWORD" "$HOME/.vnc/passwd" >/dev/null 2>&1
unset VNC_PASSWORD

pids=()
cleanup() {
  trap - EXIT INT TERM
  for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

Xvfb "$DISPLAY" -screen 0 1440x900x24 -nolisten tcp -auth "$XAUTHORITY" >/tmp/xvfb.log 2>&1 &
pids+=("$!")
for _ in $(seq 1 60); do
  if xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then break; fi
  sleep 0.2
done
if ! xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
  echo 'ERROR: Xvfb failed to start.' >&2
  exit 2
fi
fluxbox >/tmp/fluxbox.log 2>&1 &
pids+=("$!")
x11vnc -display "$DISPLAY" -auth "$XAUTHORITY" -rfbport 5900 \
    -listen 127.0.0.1 -rfbauth "$HOME/.vnc/passwd" \
    -forever -shared -noxdamage -quiet >/tmp/x11vnc.log 2>&1 &
pids+=("$!")
websockify --web=/usr/share/novnc/ 0.0.0.0:6080 127.0.0.1:5900 >/tmp/websockify.log 2>&1 &
pids+=("$!")

# A visible xterm is essential: the v6 watcher waits for Enter after CAPTCHA.
xterm -title 'WOW CUP / Telegram / checkout' -fa 'DejaVu Sans Mono' -fs 12 \
  -geometry 117x39+15+15 -e /app/server/console.sh &
pids+=("$!")
echo 'noVNC started on the CONTAINER port 6080; host Compose publishes ONLY 127.0.0.1:6080.'
echo "START_MODE=${START_MODE:-manual}. No CAPTCHA solving or payment is automated."
wait -n "${pids[@]}" || true
