"""Ears and mouth: mic stream, wake word, end-of-speech detection, STT and TTS.

These sit behind small interfaces on purpose. Later the "satellite" (mic,
speaker, wake word) can move to a phone or a cheap device while the "brain"
(agent, memory, tools) stays on a server, without rewriting either side.
"""
from __future__ import annotations

import logging
import queue
import threading

import numpy as np

log = logging.getLogger(__name__)

FRAME_SAMPLES = 1280          # 80 ms at 16 kHz, what openWakeWord expects


# --------------------------------------------------------------------------- mic
class MicStream:
    def __init__(self, sample_rate: int = 16000, device=None):
        self.sample_rate = sample_rate
        self.device = device
        self.q: queue.Queue[bytes] = queue.Queue(maxsize=250)
        self._muted = threading.Event()
        self._stream = None

    def _callback(self, indata, frames, time_info, status):
        if status:
            log.debug("mic status: %s", status)
        if self._muted.is_set():
            return
        try:
            self.q.put_nowait(bytes(indata))
        except queue.Full:
            try:
                self.q.get_nowait()
            except queue.Empty:
                pass

    def start(self) -> None:
        import sounddevice as sd

        self._stream = sd.RawInputStream(
            samplerate=self.sample_rate, blocksize=FRAME_SAMPLES, dtype="int16",
            channels=1, device=self.device, callback=self._callback,
        )
        self._stream.start()

    def read(self, timeout: float = 0.5) -> bytes | None:
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None

    def clear(self) -> None:
        while not self.q.empty():
            try:
                self.q.get_nowait()
            except queue.Empty:
                break

    def mute(self) -> None:
        self._muted.set()

    def unmute(self) -> None:
        self.clear()
        self._muted.clear()


# ---------------------------------------------------------------------- wake word
class WakeWord:
    def __init__(self, model: str, threshold: float):
        import openwakeword
        from openwakeword.model import Model

        if not model.endswith((".onnx", ".tflite")):
            openwakeword.utils.download_models(model_names=[model])
        self.model = Model(wakeword_models=[model], inference_framework="onnx")
        self.threshold = threshold

    def detect(self, frame: bytes) -> bool:
        scores = self.model.predict(np.frombuffer(frame, dtype=np.int16))
        return any(s >= self.threshold for s in scores.values())

    def reset(self) -> None:
        self.model.reset()


# ------------------------------------------------------------------ utterance capture
def record_utterance(mic: MicStream, cfg, start_timeout: float) -> bytes | None:
    """Capture one spoken request from a local mic (single-process mode)."""
    from .listening import UtteranceCollector

    col = UtteranceCollector(cfg, mic.sample_rate, start_timeout)
    while True:
        chunk = mic.read(timeout=0.3)
        if chunk is None:
            continue
        result = col.feed(chunk)
        if result == "done":
            return col.audio()
        if result == "timeout":
            return None


# --------------------------------------------------------------------------- STT
class SpeechToText:
    def __init__(self, cfg):
        from faster_whisper import WhisperModel

        self.language = cfg.language
        self.languages = list(cfg.languages or [])
        self.beam_size = getattr(cfg, "beam_size", 5)
        self.initial_prompt = getattr(cfg, "initial_prompt", None)
        self.model = WhisperModel(cfg.model, device=cfg.device, compute_type=cfg.compute_type)

    def _run(self, audio, language):
        segments, info = self.model.transcribe(
            audio, language=language, beam_size=self.beam_size, vad_filter=True,
            initial_prompt=self.initial_prompt, condition_on_previous_text=False)
        return list(segments), info

    def transcribe(self, pcm: bytes) -> tuple[str, str | None]:
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        segments, info = self._run(audio, self.language)
        lang = getattr(info, "language", None)
        if not self.language and self.languages and lang not in self.languages:
            # small models mistake short Turkish for Russian and the like: redo it in the likeliest allowed language
            probs = dict(getattr(info, "all_language_probs", None) or [])
            lang = max(self.languages, key=lambda code: probs.get(code, 0.0))
            segments, info = self._run(audio, lang)
        # Whisper invents words on noise; these thresholds are the ones OpenAI's reference code uses
        kept = [s for s in segments if not (s.no_speech_prob > 0.6 and s.avg_logprob < -1.0)]
        return " ".join(s.text.strip() for s in kept).strip(), lang




from .tts import Speaker, chime, make_tts  # noqa: E402,F401  (re-exported)
