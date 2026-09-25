"""The conversation loop: IDLE -> (wake word) -> LISTEN -> THINK+SPEAK -> FOLLOW-UP -> ...

Follow-up mode is what makes it feel like talking to someone instead of issuing
commands: after each reply the mic stays open for a few seconds, so you can
keep going without repeating the wake word.
"""
from __future__ import annotations

import logging
import re
import threading
import time

from .agent import make_agent
from .audio import MicStream, SpeechToText, WakeWord, record_utterance
from .tts import Speaker, chime, make_tts
from .memory import Memory
from .tools import ToolRegistry
from .tools.core import register_core_tools
from .tools.govee import register_govee_tools
from .tools.roborock import register_roborock_tools
from .tools.spotify import register_spotify_tools

log = logging.getLogger(__name__)

# Whisper hallucinates these on silence or noise.
NOISE = {"", "you", "thank you.", "thanks for watching!", "altyazı m.k.", "..."}


def build_tools(cfg, memory: Memory) -> ToolRegistry:
    reg = ToolRegistry()
    reg.ctx["memory"] = memory
    register_core_tools(reg, timers_enabled=cfg.tools.timers.enabled)
    if cfg.tools.govee.enabled:
        found = register_govee_tools(reg, dict(cfg.tools.govee.devices), cfg.tools.govee.auto_discover)
        log.info("govee lights: %s", found or "none found")
    if cfg.tools.roborock.enabled and register_roborock_tools(reg):
        log.info("roborock: enabled")
    if cfg.tools.spotify.enabled and register_spotify_tools(reg, cfg.tools.spotify):
        log.info("spotify: enabled")
    return reg


def _normalize(text: str) -> str:
    return re.sub(r"[^\w\s']", "", text.lower()).strip()


class Assistant:
    def __init__(self, cfg):
        self.cfg = cfg
        self.memory = Memory(cfg.memory.path)
        self.tools = build_tools(cfg, self.memory)
        self.agent = make_agent(cfg, self.tools, self.memory)
        log.info("loading speech models...")
        self.mic = MicStream(cfg.audio.sample_rate, cfg.audio.input_device)
        self.wake = WakeWord(cfg.wakeword.model, cfg.wakeword.threshold)
        self.stt = SpeechToText(cfg.stt)
        self.speaker = Speaker(make_tts(cfg.tts))
        self._voice_lock = threading.Lock()
        self._lang: str | None = None
        self.tools.ctx["announce"] = self.announce
        self.stop_phrases = {_normalize(p) for p in cfg.assistant.stop_phrases}

    # speaking with the mic muted so we never transcribe ourselves
    def _speak_block(self, fn) -> None:
        with self._voice_lock:
            self.mic.mute()
            try:
                fn()
                self.speaker.wait()
                time.sleep(self.cfg.audio.post_speech_mute_ms / 1000)
            finally:
                self.mic.unmute()

    def announce(self, text: str) -> None:
        self._speak_block(lambda: self.speaker.say(text, self._lang))

    def run(self) -> None:
        self.mic.start()
        print(f"Ready. Say the wake word ({self.cfg.wakeword.model}). Ctrl+C to quit.")
        while True:
            frame = self.mic.read()
            if frame is None or not self.wake.detect(frame):
                continue
            log.info("wake word")
            self.conversation()
            self.wake.reset()
            self.mic.clear()

    def conversation(self) -> None:
        if self.cfg.audio.chime:
            chime("start")
        timeout = self.cfg.listen.start_timeout_s
        while True:
            pcm = record_utterance(self.mic, self.cfg.listen, start_timeout=timeout)
            if pcm is None:
                break
            t0 = time.monotonic()
            text, lang = self.stt.transcribe(pcm)
            log.info("heard (%s, %.2fs): %s", lang, time.monotonic() - t0, text)
            if _normalize(text) in NOISE or text.lower().strip() in NOISE:
                break
            if _normalize(text) in self.stop_phrases:
                break
            self._lang = lang

            first = {"t": None}

            def on_sentence(s: str) -> None:
                if first["t"] is None:
                    first["t"] = time.monotonic() - t0
                    log.info("first audio after %.2fs", first["t"])
                self.speaker.say(s, lang)

            try:
                self._speak_block(lambda: self.agent.respond(text, on_sentence, lang))
            except Exception:
                log.exception("agent failed")
                self.announce("Sorry, something went wrong on my side.")
            timeout = self.cfg.assistant.followup_seconds
        if self.cfg.audio.chime:
            chime("end")
