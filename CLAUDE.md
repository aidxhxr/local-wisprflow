# CLAUDE.md — setting up local-wisprflow

This file is for an AI agent (Claude Code or similar) that has been asked to install and run
local-wisprflow for its user. Follow it top to bottom. Every step has a check; do not move on
until the check passes. Where a value depends on the user's machines (hostnames, usernames,
GPU size), ask once, then keep going.

## What you are installing

Push-to-talk dictation for macOS, in the style of Wispr Flow, with nothing leaving the user's
own machines.

```
Mac (client.py)  --ssh -L 8765--> GPU box: server.py :8765  --http--> vLLM :8766
  mic, hotkey,                    Parakeet TDT 0.6B v2 ASR          Qwen3-4B-Instruct-2507
  overlay, paste                  Silero VAD, FastAPI               formats the transcript
```

- The Mac records audio, streams 16 kHz PCM over a websocket to the GPU box, shows live partials
  in a floating pill, and pastes the formatted result at the cursor.
- The GPU box runs two systemd user services: `dictate` (ASR + API, `server/server.py`) and
  `dictate-llm` (vLLM serving the formatter model).
- Both services bind to `127.0.0.1` only. The Mac reaches them through an SSH tunnel the client
  opens itself. Nothing is exposed on the network.

## Requirements

| Side | Needs |
|---|---|
| Mac | macOS 13+, Python 3.12 (`brew install python@3.12`), SSH key auth to the GPU box (no password prompt; the client uses `BatchMode=yes`) |
| GPU box | Linux with systemd, NVIDIA driver + CUDA 12.x, Python 3.10–3.12, an NVIDIA GPU with at least 16 GB VRAM (24 GB is comfortable; the units are tuned for an RTX 5090), ~15 GB disk for models, internet for the first model download |

Decide these values now and use them consistently:

- `GPU_SSH`: the ssh target of the GPU box as seen from the Mac, e.g. `damian@gpu-box`.
- The GPU-box login user must be able to run user services after logout (`loginctl enable-linger`).

## Part 1 — GPU box (server)

Run these on the GPU box (ssh in, or run them through `ssh GPU_SSH '...'` from the Mac).

### 1.1 Copy the server code

```
mkdir -p ~/dictate
# from the Mac, inside the repo:
scp -r server/server.py server/formatter.py server/vad.py server/requirements.txt server/tests GPU_SSH:~/dictate/
```

The systemd units hardcode `%h/dictate` as the working directory and `%h/dictate/.venv` and
`%h/vllm-env` as the interpreters. Keep those paths, or edit both `.service` files.

### 1.2 ASR venv

```
cd ~/dictate
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt
```

NeMo pulls a lot (torch, lightning, transformers). Expect 5–10 minutes. If `nemo_toolkit[asr]`
fails to build, install `torch` first from https://pytorch.org/get-started/locally/ for the
box's CUDA version, then re-run the requirements install.

Check:

```
.venv/bin/python -c "import nemo.collections.asr, silero_vad, fastapi, torch; print(torch.cuda.is_available())"
# must print: True
```

### 1.3 vLLM venv (separate; NeMo and vLLM pin conflicting torch/transformers)

```
python3 -m venv ~/vllm-env
~/vllm-env/bin/pip install -U pip
~/vllm-env/bin/pip install vllm
```

Check:

```
~/vllm-env/bin/vllm --version
```

### 1.4 Install and start the services

```
mkdir -p ~/.config/systemd/user
# from the Mac, inside the repo:
scp server/dictate.service server/dictate-llm.service GPU_SSH:~/.config/systemd/user/
```

Then on the GPU box:

```
loginctl enable-linger $USER          # services keep running with no one logged in
systemctl --user daemon-reload
systemctl --user enable --now dictate-llm dictate
```

First start downloads both models from Hugging Face (Parakeet ~2.5 GB, Qwen3-4B ~8 GB). Watch
with `journalctl --user -u dictate-llm -f` and `journalctl --user -u dictate -f`. The ASR
service logs `model nvidia/parakeet-tdt-0.6b-v2 ready in Ns` when it is up.

Check (both must succeed before moving on):

```
curl -s localhost:8765/health
curl -s localhost:8766/v1/models | grep -o '"id":"formatter"'
```

If the GPU has less than 24 GB VRAM and `dictate-llm` fails with an out-of-memory error, lower
`--gpu-memory-utilization 0.40` in `dictate-llm.service` to `0.30`, or drop `--max-model-len`
to `4096`, then `systemctl --user daemon-reload && systemctl --user restart dictate-llm`.
The ASR model needs roughly 3 GB on top of whatever vLLM takes.

### 1.5 Server-side tests

```
cd ~/dictate
.venv/bin/python tests/fmt_test.py                     # 20 formatting cases against the live LLM
.venv/bin/python tests/ws_test.py 8765 tests/two.wav   # streams a wav over /stream, prints partials/finals
```

`fmt_test.py` prints `used_llm=True` on every case when vLLM is healthy. If it prints
`used_llm=False`, the formatter timed out and fell back to regex cleanup; look at
`journalctl --user -u dictate-llm`.

## Part 2 — Mac (client)

Run these on the Mac, in a clone of this repo.

### 2.1 SSH key auth to the GPU box

The client opens the tunnel with `ssh -o BatchMode=yes`, so a password prompt means it will
never connect.

```
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N ""     # skip if a key exists
ssh-copy-id GPU_SSH
ssh -o BatchMode=yes GPU_SSH true && echo ok          # must print ok
```

### 2.2 Python environment

```
cd <repo>
python3.12 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements-client.txt
```

The `./dictate` launcher runs `.venv/bin/python client.py`, so the venv must be at `./.venv`.

### 2.3 Tell the client where the GPU box is

```
echo 'export DICTATE_SSH=GPU_SSH' >> ~/.zshrc      # substitute the real value
export DICTATE_SSH=GPU_SSH
```

`--host user@gpu-box` on the command line overrides the variable. Without either, the client
exits with "no GPU host".

### 2.4 macOS permissions (the user has to click these; you cannot)

In System Settings → Privacy & Security, grant the terminal app the client will be launched
from (Terminal, iTerm2, Ghostty, the IDE's terminal, …):

- Microphone
- Accessibility (needed to read the focused app, selection, and to paste)
- Input Monitoring (needed for the global hotkey)

Quit and reopen the terminal app afterwards. Tell the user exactly which app to grant and that
the restart is required; this is the most common reason the hotkey does nothing.

### 2.5 First run

```
./dictate
```

Expected log lines, in order: `opening tunnel to ...`, `server ok. checking microphone ...`,
`ready. hold [alt_r] to dictate ...`. A floating pill appears at the bottom of the screen.

End-to-end check without a microphone or any clicking:

```
./dictate --fake-audio server/tests/two.wav
```

It streams the bundled TTS clip as if it were spoken and shows the formatted result in the pill.

Then have the user hold Right Option, speak, release: text is pasted into the frontmost app.
Flags: `--key cmd_r|ctrl_r|shift_r|f13|f18|f19` (hotkey), `--quiet`, `--no-overlay`,
`--type` (type characters instead of pasting; use if an app rejects the paste).

To run it on login, add `<repo>/dictate` as a Login Item, or wrap it in a LaunchAgent. Keep in
mind the permissions above are granted per launching app.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `server not reachable on the tunnel` | `ssh -o BatchMode=yes GPU_SSH true` fails, or `systemctl --user status dictate` on the box is not active. Check that `curl localhost:8765/health` works on the box. |
| Hotkey does nothing, no pill | Input Monitoring / Accessibility not granted to the launching app, or terminal not restarted. |
| Pill shows text but nothing is pasted | Accessibility missing, or the app blocks synthetic paste: try `--type`. |
| Raw transcript inserted, no formatting | vLLM down or slower than 3 s (`DICTATE_LLM_TIMEOUT`). `journalctl --user -u dictate-llm`. |
| `Server error: ... CUDA error` | Sticky CUDA fault. The server exits on purpose and systemd restarts it in ~15 s. If it repeats, `nvidia-smi` and the `dictate` journal. |
| Port 8765 already in use on the Mac | A previous client's tunnel is still up. `pkill -f "ssh -N.*8765"`. |

## Where things are configured

- Models and ports: `Environment=` lines in `server/dictate.service` and the vLLM args in
  `server/dictate-llm.service`. Env vars read by the code: `DICTATE_MODEL`, `DICTATE_HOST`,
  `DICTATE_PORT` (server), `DICTATE_LLM_URL`, `DICTATE_LLM_MODEL`, `DICTATE_LLM_TIMEOUT` (formatter),
  `DICTATE_SSH` (client).
- Formatting behaviour, app categories, few-shot examples: `server/formatter.py`. After
  editing, `scp` it to `~/dictate/` and `systemctl --user restart dictate`.
- Hands-free segmentation thresholds: constants at the top of `server/server.py`.
- Hotkey, overlay, paste logic, app-context detection: `client.py`.

## Rules for agents working in this repo

- Never commit hostnames, IPs, usernames or keys. The GPU target lives only in `DICTATE_SSH`.
- Keep the two venvs on the GPU box separate; do not try to install vLLM into the NeMo venv.
- Do not change the `%h/dictate` and `%h/vllm-env` paths in the units without updating this file.
- The Mac venv is `.venv` at the repo root and is gitignored; so is `server/.venv` on the box.
