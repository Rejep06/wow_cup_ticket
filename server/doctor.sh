#!/usr/bin/env bash
# Read-only host-side diagnosis. Execute from the project directory.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
echo '== Docker Compose =='
sudo docker compose ps || true
echo '== Published 6080 socket (MUST be 127.0.0.1) =='
sudo docker compose port wowcup 6080 || true
echo '== Browser and watcher processes =='
sudo docker compose exec -T wowcup sh -lc 'ps -eo pid,args | grep -E "Xvfb|x11vnc|websockify|xterm|python.*watcher.py" | grep -v grep' || true
echo '== Last 20 watcher log lines =='
sudo docker compose exec -T wowcup sh -lc 'if [ -f /app/data/watcher.log ]; then tail -n 20 /app/data/watcher.log; else echo "Not started (START_MODE=manual?)"; fi' || true
