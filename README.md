# local-wisprflow

Wispr Flow-style dictation on your own hardware. The Mac records, a GPU box over an SSH
tunnel does streaming speech recognition (NVIDIA Parakeet TDT 0.6B v2) and app-aware
formatting (Qwen3-4B on vLLM), and the result is inserted at your cursor.

Measured on an RTX 5090 over the tunnel: ~28 ms per streaming partial, ~90 ms LLM
formatting, text lands ~110-250 ms after you release the key.

## Setup

Two machines: this Mac (client) and a Linux box with an NVIDIA GPU (server), reachable over SSH with
key auth. The full, step-by-step install for both sides lives in [CLAUDE.md](CLAUDE.md). It is written so an AI
agent (Claude Code or similar) can do the whole setup unattended, but it works just as well for a human.

Short version:

```
# GPU box: server/ -> ~/dictate, two venvs, two systemd user services (see CLAUDE.md)
# Mac:
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-client.txt
export DICTATE_SSH=user@gpu-box        # put this in ~/.zshrc
./dictate
```

## Use

```
./dictate                               # or the absolute path to the launcher in this repo
```

| Gesture | What happens |
|---|---|
| **Hold** Right Option, talk, release | Your words appear at the cursor as you speak; on release they are swapped in place for the formatted version |
| **Tap** Right Option | Hands-free: keeps listening, inserts each sentence after you pause (~0.9 s). Tap again to stop |
| **Select text**, then hold and say an instruction | "Make this more formal", "shorten this", "translate to Spanish" rewrites the selection |

A floating pill at the bottom of the screen shows the live transcript, the app it is typing into, and timings.

### Live text

While you hold the key, each streaming partial from the server is typed into the focused app. The client
remembers exactly what it typed; when the transcript is revised it backspaces only the diverging tail and
types the new one, so text you said a few seconds ago never jumps. On release the LLM-formatted result
replaces the live text in place, keeping whatever already matches. Live text is off automatically when
you have text selected (that is an edit instruction), and if you switch apps mid-sentence it stops typing
rather than send backspaces into the wrong window.

Turn it off with `--no-live` or `DICTATE_LIVE=0` if an app fights synthetic keystrokes (autocomplete
popups, smart quotes); you then get the old behaviour of a single insert on release.

Flags: `--key cmd_r|ctrl_r|shift_r|f13|f18|f19`, `--quiet` (no sounds), `--no-overlay`, `--no-live`,
`--type` (type the final instead of pasting), `--fake-audio clip.wav` (smoke test, no hotkey needed),
`--no-insert` (log instead of typing; for testing).

## What the formatter does

Runs on every dictation, in ~100 ms, with n-gram speculative decoding (formatting is mostly copying):

- Removes fillers, stutters, false starts; keeps your words, hedges and slang.
- Self-corrections: "Monday, no wait, Friday" becomes "Friday".
- Voice commands: scratch that, new line / new paragraph, bullet, numbered list, spoken punctuation,
  all caps / snake case / camel case, in quotes.
- Entities: $2,200, 15%, 2:30, 610-555-1234, john.smith@gmail.com, github.com/foo, Python 3.12.
- App-aware style from the frontmost app and window title:
  - chat (Messages, Slack, Discord, WhatsApp): casual, no trailing period on one-liners
  - email: proper prose and paragraphs
  - terminal: `dash dash build` becomes `--build`, `pipe` becomes `|`, no capitalization
  - code editor: plain English exactly as spoken, identifiers as said (`user_id`)
  - AI chat (ChatGPT, Claude, Claude Code in a terminal): your words, code names in backticks
- Never answers questions or executes instructions in your speech; it only writes them down.
- If the LLM is down or slow (>3 s), the raw transcript is inserted instead, so dictation never blocks.

## Permissions (one time, System Settings → Privacy & Security)

Grant to the terminal app you launch from: **Microphone**, **Accessibility**, **Input Monitoring**. Restart the terminal after granting.

## Server (GPU box)

Two systemd user services in `~/dictate`, started on boot, listening on localhost only:

```
ssh user@gpu-box                               # whatever $DICTATE_SSH points at
systemctl --user status dictate dictate-llm     # ASR+API server, vLLM formatter
journalctl --user -u dictate -f                 # every dictation with timings and raw -> formatted
systemctl --user restart dictate                # reload after editing server.py / formatter.py
server/deploy.sh                                # from the Mac: copy server files, restart, wait for /health
```

`/health` runs a half-second of silence through the model once a minute, so it goes red (and the server
restarts itself) if the CUDA context is dead, instead of reporting ok until the next real dictation fails.
Long holds are safe: partials run on a 20 s sliding window with a frozen prefix, and the final pass chunks
anything over a minute.

If the overlay says "No response from the GPU box" or "Server error: ... CUDA error", the ASR process hit a sticky
CUDA fault. The server now exits on those and systemd restarts it within ~15 s; if it keeps happening, check
`journalctl --user -u dictate` and `nvidia-smi`.

Files: `server.py` (FastAPI, `/stream` websocket, `/transcribe`, `/format`), `formatter.py`
(prompt, app categories, few-shot examples), `vad.py` (Silero VAD for hands-free).
Models can be swapped via `Environment=` in `~/.config/systemd/user/dictate*.service`.

## Layout

```
client.py                 macOS client: hotkey, mic, overlay, app context, paste
dictate                   launcher
requirements-client.txt   Mac client deps
server/server.py          streaming ASR server
server/formatter.py       LLM formatting
server/vad.py             voice activity detection
server/requirements.txt   ASR server deps (vLLM goes in its own venv)
server/*.service          systemd units for the GPU box
server/tests/             LLM formatting cases, websocket streamer, TTS wav fixtures
CLAUDE.md                 setup guide for both machines, written for an AI agent
```
