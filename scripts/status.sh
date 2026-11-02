#!/bin/sh
# One-screen health check for the whole dictation path, Mac side first, then the GPU box
# over ssh. Exits non-zero if anything is down. Needs DICTATE_SSH (or pass user@gpu-box).
# For microphone and macOS permission checks run `./dictate --doctor` instead; that one
# has to run inside the client process.
#
# Usage: scripts/status.sh [user@gpu-box]
set -u
host="${1:-${DICTATE_SSH:-}}"
port=8765
down=0
ok()   { printf '  ok    %s\n' "$1"; }
bad()  { printf '  DOWN  %s\n' "$1"; down=1; }
skip() { printf '  --    %s\n' "$1"; }
ssh_q() { ssh -o BatchMode=yes -o ConnectTimeout=5 -o LogLevel=ERROR "$host" "$@"; }

echo "Mac"
# The launcher execs python under the name "dictate"; match either, but not `vim client.py`.
if pid=$(pgrep -f '^(dictate|[^ ]*python[0-9.]*) [^ ]*client\.py' | head -1) && [ -n "$pid" ]; then
  ok "client running (pid $pid, $(ps -o etime= -p "$pid" | tr -d ' ') up)"
else
  bad "client not running: ./dictate, scripts/dictate-tmux, or the LaunchAgent"
fi
if tun=$(lsof -nP -iTCP:$port -sTCP:LISTEN 2>/dev/null | awk 'NR==2 {print $1, $2}') && [ -n "$tun" ]; then
  ok "tunnel listening on 127.0.0.1:$port ($tun)"
  if curl -sf -m 3 "http://127.0.0.1:$port/health" 2>/dev/null | grep -q '"ok":true'; then
    ok "/health through the tunnel"
  else
    bad "/health through the tunnel (tunnel up but the server does not answer)"
  fi
else
  bad "no tunnel on 127.0.0.1:$port (the client opens it; if the client is up, check ssh below)"
fi

echo "GPU box${host:+ ($host)}"
if [ -z "$host" ]; then
  bad "DICTATE_SSH not set; pass user@gpu-box or export it in ~/.zshrc"
  exit 1
fi
if ssh_q true 2>/dev/null; then
  ok "ssh in BatchMode"
else
  bad "ssh -o BatchMode=yes $host fails: key not loaded (ssh-add), wrong host, or box offline"
  exit 1
fi
for unit in dictate dictate-llm; do
  state=$(ssh_q "systemctl --user is-active $unit" 2>/dev/null || true)
  if [ "$state" = "active" ]; then ok "$unit.service active"; else bad "$unit.service: ${state:-unknown}  (journalctl --user -u $unit -n 50)"; fi
done
if ssh_q "curl -sf -m 5 localhost:$port/health" 2>/dev/null | grep -q '"ok":true'; then
  ok "/health on the box"
else
  bad "/health on the box (model not loaded yet, or a CUDA fault; see journalctl --user -u dictate)"
fi
if ssh_q "curl -sf -m 5 localhost:8766/v1/models" 2>/dev/null | grep -q '"id":"formatter"'; then
  ok "vLLM serving the formatter"
else
  bad "vLLM not answering on 8766: raw transcripts only (journalctl --user -u dictate-llm)"
fi
gpu=$(ssh_q "nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu --format=csv,noheader" 2>/dev/null || true)
if [ -n "$gpu" ]; then ok "gpu: $gpu"; else skip "nvidia-smi not available"; fi

[ "$down" -eq 0 ] && echo "all good" || echo "something is down"
exit "$down"
