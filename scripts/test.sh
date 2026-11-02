#!/bin/sh
# Run every test that needs no GPU: the client's live-text diff, the formatter's pure parts and
# the audio helpers, all from the Mac venv. Exits non-zero if any of them fails.
# The LLM cases (server/tests/fmt_test.py) and the websocket streamer (ws_test.py) run on the box.
set -u
cd "$(dirname "$0")/.." || exit 2
py=.venv/bin/python
[ -x "$py" ] || { echo "test.sh: no venv at ./.venv (python3.12 -m venv .venv && .venv/bin/pip install -r requirements-client.txt)" >&2; exit 2; }
rc=0
for t in tests/live_text_test.py server/tests/formatter_unit_test.py server/tests/audio_utils_test.py; do
  echo "== $t"
  "$py" "$t" || rc=1
done
if command -v ruff >/dev/null 2>&1; then
  echo "== ruff check ."
  ruff check . || rc=1
fi
[ "$rc" -eq 0 ] && echo "all tests passed" || echo "TESTS FAILED"
exit "$rc"
