"""Pure helpers for the dictation server: PCM conversion, resampling, cut-point search and the
hallucination filter. Only numpy, so they can be unit tested on a machine without torch or NeMo.
"""
import numpy as np

SR = 16000

# What Parakeet produces from breath, keyboard clatter or near-silence: a lone short word or
# one of the stock phrases every ASR model learned from subtitle data. The whole transcript
# must be one of these, and it is only believed if the VAD also heard speech.
HALLUCINATIONS = {"okay", "ok", "yeah", "yes", "yep", "nope", "no", "hmm", "mm", "mhm", "huh", "uh", "um", "oh",
                  "so", "you", "the", "and", "bye", "thanks", "thank you", "i", "a", "right", "well", "sure",
                  "alright", "all right", "what", "oh okay", "okay okay", "the end", "subtitles by",
                  "thanks for watching", "thank you for watching"}


def looks_hallucinated(text: str) -> bool:
    words = "".join(c for c in text.lower() if c.isalpha() or c == " ").split()
    return " ".join(words) in HALLUCINATIONS


def pcm16_to_f32(raw: bytes) -> np.ndarray:
    """Little-endian int16 bytes -> float32 in [-1, 1). A trailing odd byte (a frame split by a
    proxy or a truncated upload) is dropped instead of raising from inside numpy."""
    return np.frombuffer(raw[:len(raw) & ~1], dtype="<i2").astype(np.float32) / 32768.0


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
