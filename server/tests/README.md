# tests

- `fmt_test.py` — 20 formatting cases against the live LLM (run on the GPU box: `.venv/bin/python tests/fmt_test.py`)
- `ws_test.py <port> <wav|pcm> [hold|auto] [speed]` — streams a 16 kHz mono wav (or raw int16 PCM) over `/stream`, prints partials/finals with timings; `auto` exercises hands-free segmentation
- `audio_utils_test.py` — unit tests for the numpy-only helpers in `server/audio_utils.py`; runs anywhere, including the Mac venv (`.venv/bin/python server/tests/audio_utils_test.py`)
- `*.wav` — macOS TTS clips used for client end-to-end runs (`./dictate --fake-audio server/tests/two.wav --no-insert`)

Client-side unit test (Mac): `tests/live_text_test.py` at the repo root checks the live-text diffing.
