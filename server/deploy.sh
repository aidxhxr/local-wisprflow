#!/bin/sh
# Copy the server code and units to the GPU box, restart the ASR service and wait for /health.
# Usage: server/deploy.sh [user@gpu-box]   (default: $DICTATE_SSH)
set -eu
host="${1:-${DICTATE_SSH:-}}"
[ -n "$host" ] || { echo "usage: server/deploy.sh user@gpu-box  (or set DICTATE_SSH)" >&2; exit 2; }
cd "$(dirname "$0")"
scp -q server.py formatter.py vad.py requirements.txt "$host:~/dictate/"
scp -q -r tests "$host:~/dictate/"
scp -q dictate.service dictate-llm.service "$host:~/.config/systemd/user/"
ssh "$host" 'systemctl --user daemon-reload && systemctl --user restart dictate'
echo "restarted dictate; waiting for the model ..."
for i in $(seq 1 60); do
  if ssh "$host" 'curl -sf -m 3 localhost:8765/health' 2>/dev/null | grep -q '"ok":true'; then
    echo "ok after ~$((i * 3))s"; exit 0
  fi
  sleep 3
done
echo "server did not come up; see: ssh $host journalctl --user -u dictate -n 50" >&2
exit 1
