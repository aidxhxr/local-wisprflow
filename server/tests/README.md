# tests

Run on the GPU box (they need the live services):

- `fmt_test.py` — 20 formatting cases against the live LLM: `.venv/bin/python tests/fmt_test.py`. Prints
  `used_llm=True` per case when vLLM is healthy and exits non-zero if any case fell back to the regex cleanup.
- `ws_test.py <port> <wav|pcm> [hold|auto] [speed]` — streams a 16 kHz mono wav (or raw int16 PCM) over
  `/stream`, prints partials/finals with timings; `auto` exercises hands-free segmentation.

Run anywhere, including the Mac venv (no GPU, torch or NeMo needed):

- `formatter_unit_test.py` — app categorisation, the regex fallback, output post-processing, prompt assembly,
  the vocabulary file and the length/code guards, with a fake LLM client.
- `audio_utils_test.py` — the numpy-only helpers in `server/audio_utils.py`.

`scripts/test.sh` at the repo root runs both of those plus the client's `tests/live_text_test.py` and ruff.

- `*.wav` — macOS TTS clips used for client end-to-end runs (`./dictate --fake-audio server/tests/two.wav --no-insert`)
