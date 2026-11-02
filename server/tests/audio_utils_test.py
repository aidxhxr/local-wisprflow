"""Unit tests for server/audio_utils.py. Needs only numpy, so it runs on the Mac too:
    .venv/bin/python server/tests/audio_utils_test.py
Exits non-zero if any check fails."""
import os, sys
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from audio_utils import SR, join_text, looks_hallucinated, pcm16_to_f32, quietest_point, resample

failures = 0


def check(name, cond):
    global failures
    print(("ok  " if cond else "FAIL"), name)
    if not cond:
        failures += 1


# pcm16_to_f32: little-endian int16 -> float32 in [-1, 1)
raw = np.array([0, 16384, -32768, 32767], dtype="<i2").tobytes()
x = pcm16_to_f32(raw)
check("pcm16 dtype", x.dtype == np.float32)
check("pcm16 values", np.allclose(x, [0.0, 0.5, -1.0, 32767 / 32768]))
check("pcm16 empty", len(pcm16_to_f32(b"")) == 0)
check("pcm16 odd byte dropped", np.array_equal(pcm16_to_f32(raw + b"\x7f"), x))
check("pcm16 single byte", len(pcm16_to_f32(b"\x01")) == 0)

# resample: same rate is a no-op, other rates scale the length and keep the endpoints
y = np.linspace(0, 1, 48000, dtype=np.float32)
check("resample noop", resample(y, SR) is y)
r = resample(y, 48000)
check("resample length", len(r) == SR)
check("resample endpoints", abs(r[0]) < 1e-6 and abs(r[-1] - 1.0) < 1e-6)
check("resample dtype", r.dtype == np.float32)

# quietest_point: finds the silent gap, clamps the range, falls back to hi when too short
sig = np.ones(SR * 2, dtype=np.float32)
sig[SR:SR + SR // 10] = 0.0                       # 100 ms of silence starting at 1.0 s
cut = quietest_point(sig, 0, len(sig))
check("quietest in the gap", SR <= cut <= SR + SR // 10)
check("quietest clamps lo/hi", quietest_point(sig, -5000, 10 * SR) == cut)
check("quietest short range -> hi", quietest_point(sig, 100, 150) == 150)
check("quietest empty -> 0", quietest_point(np.zeros(0, np.float32), 0, 100) == 0)

# join_text: strips, joins with one space, tolerates empties
check("join both", join_text(" a ", " b ") == "a b")
check("join left empty", join_text("", "b") == "b")
check("join right empty", join_text("a", "  ") == "a")
check("join both empty", join_text("", "") == "")

# looks_hallucinated: punctuation and case do not matter; real sentences pass through
check("halluc Okay.", looks_hallucinated("Okay."))
check("halluc Thank you.", looks_hallucinated("Thank you."))
check("halluc Um", looks_hallucinated("Um"))
check("halluc All right.", looks_hallucinated("All right."))
check("halluc Thanks for watching!", looks_hallucinated("Thanks for watching!"))
check("not halluc sentence", not looks_hallucinated("Okay, send the report tomorrow."))
check("not halluc thanks for the help", not looks_hallucinated("Thanks for the help."))
check("not halluc empty", not looks_hallucinated(""))
check("not halluc two real words", not looks_hallucinated("Send it"))

print(f"{failures} failure(s)")
sys.exit(1 if failures else 0)
