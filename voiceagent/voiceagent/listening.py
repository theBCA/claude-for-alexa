"""End-of-speech detection as a pure streaming state machine.

It counts audio time rather than wall-clock time, so it behaves the same on a
local mic or on frames arriving over the network, and it is fully testable.
"""
from __future__ import annotations

import collections

SUB_MS = 20


class UtteranceCollector:
    def __init__(self, cfg, sample_rate: int = 16000, start_timeout: float = 5.0, vad=None):
        if vad is None:
            import webrtcvad
            vad = webrtcvad.Vad(int(cfg.vad_aggressiveness))
        self.vad = vad
        self.sr = sample_rate
        self.sub_bytes = int(sample_rate * SUB_MS / 1000) * 2
        self.silence_limit = cfg.silence_ms
        self.max_ms = cfg.max_seconds * 1000
        self.start_limit = start_timeout * 1000
        self.preroll: collections.deque[bytes] = collections.deque(maxlen=15)  # 300 ms before onset
        self.voiced: list[bytes] = []
        self.pending = b""
        self.started = False
        self.run = self.silence = self.elapsed = self.speech_ms = 0

    def feed(self, chunk: bytes) -> str | None:
        """Returns None to keep going, "done" when the user stopped talking, "timeout" if they never started."""
        data = self.pending + chunk
        usable = len(data) // self.sub_bytes * self.sub_bytes
        self.pending = data[usable:]
        for i in range(0, usable, self.sub_bytes):
            sub = data[i:i + self.sub_bytes]
            self.elapsed += SUB_MS
            speech = self.vad.is_speech(sub, self.sr)
            if not self.started:
                if self.elapsed > self.start_limit:
                    return "timeout"
                self.preroll.append(sub)
                self.run = self.run + 1 if speech else 0
                if self.run >= 4:  # 80 ms of continuous speech
                    self.started = True
                    self.voiced.extend(self.preroll)
                continue
            self.voiced.append(sub)
            self.speech_ms += SUB_MS
            self.silence = 0 if speech else self.silence + SUB_MS
            if self.silence >= self.silence_limit or self.speech_ms >= self.max_ms:
                return "done"
        return None

    def audio(self) -> bytes:
        return b"".join(self.voiced)
