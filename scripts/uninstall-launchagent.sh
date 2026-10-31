#!/bin/sh
# Stop the LaunchAgent installed by scripts/install-launchagent.sh and remove its plist.
# The log in ~/Library/Logs/local-wisprflow.log is left in place.
set -eu
label=com.local-wisprflow.dictate
dst="$HOME/Library/LaunchAgents/$label.plist"
domain="gui/$(id -u)"
if [ -n "${DRY_RUN:-}" ]; then
  echo "# DRY_RUN: would run launchctl bootout $domain/$label and remove $dst"; exit 0
fi
if launchctl bootout "$domain/$label" 2>/dev/null; then echo "stopped $label"; else echo "$label was not loaded"; fi
if [ -f "$dst" ]; then rm -f "$dst"; echo "removed $dst"; else echo "no $dst"; fi
