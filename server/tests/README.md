# tests

- `fmt_test.py` — 20 formatting cases against the live LLM (run on the GPU box: `.venv/bin/python tests/fmt_test.py`)
- `ws_test.py <port> <pcm> [hold|auto] [speed]` — streams raw 16 kHz PCM over `/stream`, prints partials/finals with timings
- `*.wav` — macOS TTS clips used for client end-to-end runs (`dictate --fake-audio tests/two.wav`)
