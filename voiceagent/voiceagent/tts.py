"""Text to speech backends and the ordered speaker queue.

Kept free of heavy dependencies so a phone satellite only needs websockets and pyyaml.
"""
from __future__ import annotations

import logging
import os
import queue
import shutil
import subprocess
import tempfile
import threading

log = logging.getLogger(__name__)


class SayTTS:
    """macOS built-in voices. Zero setup, decent quality, per-language voices."""

    def __init__(self, cfg):
        if not shutil.which("say"):
            raise SystemExit("'say' not found. On non-macOS set tts.engine: piper")
        self.rate = str(cfg.rate)
        out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True).stdout
        installed = {line.split()[0] for line in out.splitlines() if line.strip()}
        self.voices = {k: v for k, v in dict(cfg.voices).items() if v.split()[0] in installed}
        self._proc: subprocess.Popen | None = None

    def speak(self, text: str, lang: str | None = None) -> None:
        cmd = ["say", "-r", self.rate]
        voice = self.voices.get(lang or "en") or self.voices.get("en")
        if voice:
            cmd += ["-v", voice]
        self._proc = subprocess.Popen(cmd + [text])
        self._proc.wait()

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()


class PiperTTS:
    """Local neural voices (github.com/rhasspy/piper). More natural than `say`."""

    def __init__(self, cfg):
        if not shutil.which("piper"):
            raise SystemExit("piper CLI not found: pip install piper-tts")
        self.models = {k: os.path.expanduser(v) for k, v in dict(cfg.piper_models).items()}
        if not self.models:
            raise SystemExit("tts.piper_models is empty")
        self.player = "afplay" if shutil.which("afplay") else "aplay"
        self._proc: subprocess.Popen | None = None

    def speak(self, text: str, lang: str | None = None) -> None:
        model = self.models.get(lang or "en") or next(iter(self.models.values()))
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            subprocess.run(["piper", "--model", model, "--output_file", f.name],
                           input=text, text=True, capture_output=True, check=True)
            self._proc = subprocess.Popen([self.player, f.name])
            self._proc.wait()

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()


class TermuxTTS:
    """Android system TTS through Termux:API. Plays on the phone's media output,
    so a Bluetooth-paired Echo Dot works as the speaker."""

    def __init__(self, cfg):
        if not shutil.which("termux-tts-speak"):
            raise SystemExit("termux-tts-speak not found: install the Termux:API app and `pkg install termux-api`")

    def speak(self, text: str, lang: str | None = None) -> None:
        subprocess.run(["termux-tts-speak", "-l", lang or "en", text])

    def stop(self) -> None:
        pass


class PrintTTS:
    """No audio, just prints. Handy for testing a satellite over SSH."""

    def __init__(self, cfg=None):
        pass

    def speak(self, text: str, lang: str | None = None) -> None:
        print(f"[{lang or '?'}] {text}", flush=True)

    def stop(self) -> None:
        pass


def make_tts(cfg):
    engines = {"say": SayTTS, "piper": PiperTTS, "termux": TermuxTTS, "print": PrintTTS}
    if cfg.engine not in engines:
        raise SystemExit(f"unknown tts engine {cfg.engine}; choose from {', '.join(engines)}")
    return engines[cfg.engine](cfg)


class Speaker:
    """Plays sentences in order on a background thread so the LLM can keep streaming."""

    def __init__(self, tts):
        self.tts = tts
        self.q: queue.Queue[tuple[str, str | None]] = queue.Queue()
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self) -> None:
        while True:
            text, lang = self.q.get()
            try:
                self.tts.speak(text, lang)
            except Exception:
                log.exception("tts failed")
            finally:
                self.q.task_done()

    def say(self, text: str, lang: str | None = None) -> None:
        self.q.put((text, lang))

    def wait(self) -> None:
        self.q.join()


def chime(kind: str = "start") -> None:
    sound = {"start": "/System/Library/Sounds/Tink.aiff", "end": "/System/Library/Sounds/Pop.aiff"}[kind]
    if shutil.which("afplay") and os.path.exists(sound):
        subprocess.Popen(["afplay", sound])
