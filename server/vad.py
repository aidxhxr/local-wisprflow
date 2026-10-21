"""Silero VAD wrapper for 16 kHz mono float32 audio, frame-by-frame (512 samples = 32 ms)."""
import numpy as np
import torch
from silero_vad import load_silero_vad

FRAME = 512


class VAD:
    def __init__(self, threshold: float = 0.5):
        self.model = load_silero_vad()
        self.threshold = threshold
        self.reset()

    def reset(self):
        self.model.reset_states()
        self._rem = np.zeros(0, dtype=np.float32)
        self.speech_ms = 0.0        # total speech seen in this segment
        self.silence_ms = 0.0       # trailing silence since last speech
        self.last_prob = 0.0

    def feed(self, pcm: np.ndarray):
        """Feed float32 audio; updates speech_ms / silence_ms counters."""
        x = np.concatenate([self._rem, pcm]) if len(self._rem) else pcm
        n = (len(x) // FRAME) * FRAME
        frames, self._rem = x[:n], x[n:]
        with torch.inference_mode():
            for i in range(0, n, FRAME):
                p = float(self.model(torch.from_numpy(frames[i:i + FRAME]), 16000).item())
                self.last_prob = p
                if p >= self.threshold:
                    self.speech_ms += 32
                    self.silence_ms = 0.0
                else:
                    self.silence_ms += 32
