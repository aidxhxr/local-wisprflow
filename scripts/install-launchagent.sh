#!/bin/sh
# Install the Mac client as a LaunchAgent: starts at login, restarted by launchd if it dies,
# output in ~/Library/Logs/local-wisprflow.log.
#
# Usage: scripts/install-launchagent.sh [client flags...]      e.g. --mic builtin --key f18
#        DRY_RUN=1 scripts/install-launchagent.sh ...           print the plist and the
#                                                               launchctl steps, change nothing
#
# Reads DICTATE_SSH from the environment and bakes it into the plist (launchd does not read
# ~/.zshrc). Re-run after changing the host, the flags, or moving the repo.
#
# Permissions: launchd runs the venv's python directly, so Microphone, Accessibility and
# Input Monitoring must be granted to that python binary, not to your terminal. The script
# prints the real path to add in System Settings.
set -eu
repo=$(cd "$(dirname "$0")/.." && pwd)
label=com.local-wisprflow.dictate
template="$repo/launchd/$label.plist"
dst="$HOME/Library/LaunchAgents/$label.plist"
log="$HOME/Library/Logs/local-wisprflow.log"
domain="gui/$(id -u)"
host="${DICTATE_SSH:-}"
dry="${DRY_RUN:-}"

die() { echo "install-launchagent: $*" >&2; exit 2; }
[ -n "$host" ] || die "DICTATE_SSH is not set (export DICTATE_SSH=user@gpu-box)"
[ -x "$repo/.venv/bin/python" ] || die "no venv at $repo/.venv (python3.12 -m venv .venv && .venv/bin/python -m pip install -r requirements-client.txt)"
[ -f "$template" ] || die "template missing: $template"
python_real=$("$repo/.venv/bin/python" -c 'import os, sys; print(os.path.realpath(sys.executable))')

# XML-escape a value, then escape it for use on the right side of a sed s||| command.
esc() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' | sed -e 's/[|\\]/\\&/g' -e 's/&/\\\&/g'; }
args_xml=""
for a in "$@"; do
  args_xml="$args_xml    <string>$(printf '%s' "$a" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g')</string>
"
done

plist=$(sed -e "s|__REPO__|$(esc "$repo")|g" -e "s|__HOME__|$(esc "$HOME")|g" -e "s|__DICTATE_SSH__|$(esc "$host")|g" "$template" \
  | ARGS_XML="$args_xml" awk '/^__ARGS__$/ { printf "%s", ENVIRON["ARGS_XML"]; next } { print }')

tmp=$(mktemp "${TMPDIR:-/tmp}/$label.XXXXXX")
printf '%s\n' "$plist" > "$tmp"
plutil -lint -s "$tmp" || { rm -f "$tmp"; die "generated plist does not parse"; }

if [ -n "$dry" ]; then
  echo "# DRY_RUN: would write $dst with:"
  cat "$tmp"; rm -f "$tmp"
  echo "# then run:"
  echo "#   launchctl bootout $domain/$label   (ignore 'not found')"
  echo "#   launchctl bootstrap $domain $dst"
  echo "# and grant Microphone, Accessibility, Input Monitoring to: $python_real"
  exit 0
fi

if other=$(pgrep -f '^(dictate|[^ ]*python[0-9.]*) [^ ]*client\.py' | head -1) && [ -n "$other" ]; then
  rm -f "$tmp"
  die "a client is already running (pid $other). Stop it first (scripts/dictate-tmux stop, or Ctrl-C in its terminal), or two of them will fight over the hotkey."
fi

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
mv "$tmp" "$dst"
launchctl bootout "$domain/$label" 2>/dev/null || true
launchctl bootstrap "$domain" "$dst"
cat <<EOF
installed $dst and started it. Log: $log
  status:   scripts/status.sh    (or: launchctl print $domain/$label | head -20)
  restart:  launchctl kickstart -k $domain/$label
  remove:   scripts/uninstall-launchagent.sh

Permissions (once): System Settings > Privacy & Security. Microphone is prompted for on the
first recording. Accessibility and Input Monitoring are not prompted for: press "+" under each,
Cmd-Shift-G, and add this file (the binary behind .venv/bin/python):
  $python_real
Then: launchctl kickstart -k $domain/$label
EOF
