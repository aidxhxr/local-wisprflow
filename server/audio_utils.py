"""Pure helpers for the dictation server: PCM conversion, resampling, cut-point search and the
hallucination filter. Only numpy, so they can be unit tested on a machine without torch or NeMo.
"""
import numpy as np

SR = 16000

# What Parakeet produces from breath, keyboard clatter or near-silence: a lone short word.
# A lone one of these is only believed if the VAD also heard speech.
HALLUCINATIONS = {"okay", "ok", "yeah", "yes", "yep", "no", "hmm", "mm", "mhm", "huh", "uh", "um", "oh", "so",
                  "you", "the", "and", "bye", "thanks", "thank you", "i", "a", "right", "well"}


def looks_hallucinated(text: str) -> bool:
    words = "".join(c for c in text.lower() if c.isalpha() or c == " ").split()
    return len(words) <= 2 and " ".join(words) in HALLUCINATIONS


def pcm16_to_f32(raw: bytes) -> np.ndarray:
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def resample(x: np.ndarray, sr: int) -> np.ndarray:
    if sr == SR:
        return x
    n = int(round(len(x) * SR / sr))
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


def quietest_point(x: np.ndarray, lo: int, hi: int, frame: int = SR // 20) -> int:
    """Sample index of the lowest-energy 50 ms frame in x[lo:hi]; a safe place to cut audio."""
    lo, hi = max(0, lo), min(len(x), hi)
    if hi - lo <= frame:
        return hi
    seg = x[lo:hi]
    n = (len(seg) // frame) * frame
    e = (seg[:n].reshape(-1, frame) ** 2).mean(axis=1)
    return lo + int(e.argmin()) * frame + frame // 2


def join_text(a: str, b: str) -> str:
    a, b = a.strip(), b.strip()
    return f"{a} {b}" if a and b else (a or b)
