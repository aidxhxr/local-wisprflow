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
./dictate                               # in a terminal; Ctrl-C quits
scripts/dictate-tmux                    # same, detached in a tmux session ("log" / "stop" subcommands)
scripts/install-launchagent.sh          # at login, no terminal; read Permissions below first
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
`--type` (type the final instead of pasting), `--mic default|builtin|<name>` (see below),
`--doctor` (check mic and permissions, then exit), `--fake-audio clip.wav` (smoke test, no hotkey needed),
`--no-insert` (log instead of typing; for testing).

### Microphone

By default the client records from whatever macOS currently uses as input, re-checked on every press,
so AirPods put on after launch are picked up. A Bluetooth headset's mic flips the headset into the
hands-free profile while recording (music pauses, the switch takes about a second), and some headsets
deliver plain silence in that mode. `--mic builtin` (or `DICTATE_MIC=builtin`) records from the Mac's
own microphone and leaves the headphones in music mode; `--mic <part of a device name>` picks a
specific device.

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

**Microphone**, **Accessibility** and **Input Monitoring**. macOS keys each grant on the process that
launched the client, so what you grant depends on how you run it:

- `./dictate` or `scripts/dictate-tmux` from a terminal: grant all three to the terminal app (Terminal,
  iTerm2, Ghostty, the IDE's terminal) and restart it afterwards. The most common reason the hotkey does
  nothing is a terminal that was not restarted after the grant.
- LaunchAgent: there is no terminal. launchd runs `.venv/bin/python` directly, so the grants belong to the
  python binary behind that symlink (`readlink -f .venv/bin/python`; the install script prints it). The
  Microphone prompt appears on the first recording. Accessibility and Input Monitoring never prompt: press
  "+" under each list, Cmd-Shift-G, paste the path, then `launchctl kickstart -k gui/$(id -u)/com.local-wisprflow.dictate`.
  With a uv-managed Python the file is `~/.local/share/uv/python/cpython-3.12.*/bin/python3.12`. With
  Homebrew's it sits inside `.../Frameworks/Python.framework/Versions/3.12/`; that layout is unverified
  here, and if the hotkey stays dead after adding `bin/python3.12`, add the `Resources/Python.app` bundle
  from the same directory instead.

The LaunchAgent route has been linted and dry-run but not yet exercised through a full login on a real
machine; if it misbehaves, `scripts/uninstall-launchagent.sh` and fall back to `scripts/dictate-tmux`.

## Daily use / when it stops working

Three checks, in this order:

```
scripts/status.sh            # client, tunnel, ssh, both services, /health, vLLM, GPU memory; non-zero if anything is down
./dictate --doctor           # inside the client: input device, levels, macOS permissions
scripts/dictate-tmux log     # what the client last said (tmux attach -t dictate for the live view)
```

The causes seen so far, most frequent first:

| Symptom | Cause / fix |
|---|---|
| Nothing happens on the hotkey, no pill | The client is not running (terminal closed, Mac rebooted). `scripts/dictate-tmux` or the LaunchAgent. If it is running: permissions, see above. |
| Pill listens, transcript stays empty | A Bluetooth headset is the system input and its mic delivers silence. `--mic builtin`, or pick the mic in the macOS Sound settings. `./dictate --doctor` shows the level. |
| `server not reachable on the tunnel` at start | `ssh -o BatchMode=yes $DICTATE_SSH true` fails: key not loaded (`ssh-add`, or `ssh-add --apple-use-keychain` for a passphrase key), box off, or `dictate.service` down. `scripts/status.sh` says which. |
| `port 8765 is held by another ssh process` | A previous client's tunnel outlived it. `pkill -f 'ssh -N.*8765'` and start again. |
| Raw transcript inserted, no formatting | vLLM down or slower than 3 s. `scripts/status.sh` checks 8766; `journalctl --user -u dictate-llm` on the box. |
| Live text garbled in one app | Autocomplete or smart quotes fighting the keystrokes. `--no-live` for that workflow. |
| Live text stops mid-sentence | You switched apps; the client stops typing on purpose and pastes the final into the new app. |
| `Server error: ... CUDA error` | Sticky CUDA fault on the box; the server exits and systemd restarts it in ~15 s. Repeats: `nvidia-smi`, `journalctl --user -u dictate`. |

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
scripts/dictate-tmux      run the client detached in tmux (log / stop)
scripts/status.sh         one-screen health check, Mac side and GPU box
scripts/*-launchagent.sh  install / remove the login LaunchAgent
launchd/                  LaunchAgent plist template (placeholders filled by the install script)
tests/live_text_test.py   client unit test for the live-text diff
server/server.py          streaming ASR server
server/formatter.py       LLM formatting
server/vad.py             voice activity detection
server/requirements.txt   ASR server deps (vLLM goes in its own venv)
server/*.service          systemd units for the GPU box
server/tests/             LLM formatting cases, websocket streamer, TTS wav fixtures
CLAUDE.md                 setup guide for both machines, written for an AI agent
```
