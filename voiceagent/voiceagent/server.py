"""The brain as a network service.

Satellites (phones, a Mac, later a Pi or a native app) connect over a websocket
and stream raw mic audio. The brain runs wake word, end-of-speech detection,
Whisper and the agent per satellite, and sends back sentences to speak.

Protocol (JSON text frames unless noted):
  satellite -> brain   {"type":"hello","id":"living-room","token":"..."}   first message
                       <binary> 16 kHz mono int16 PCM, ~80 ms per frame
                       {"type":"speech_done"}   finished speaking after a turn_end
  brain -> satellite   {"type":"wake"}                        play a chime, user is talking
                       {"type":"say","text":"...","lang":"tr"}
                       {"type":"turn_end"}                    reply complete, will listen again after speech_done
                       {"type":"announce","text":"...","lang":"en"}   timers etc., any time
                       {"type":"conversation_end"}
                       {"type":"sleep"} / {"type":"awake"}   sleep mode changed (all satellites)

Sleep mode: "Jarvis, go to sleep" makes the brain ignore everything, including
the wake word, until someone says "Hey Jarvis, wake up". The satellite keeps
streaming; only Whisper runs after a wake word, to check for the wake phrase.

Wake word runs on the brain for now, which keeps satellites tiny (a phone only
needs a mic, websockets and TTS). The trade-off is continuous audio over the
LAN; a native satellite app with on-device wake word removes it later without
changing this protocol.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time

from .assistant import NOISE, _normalize
from .listening import UtteranceCollector

log = logging.getLogger(__name__)

FALLBACK = "Sorry, something went wrong on my side."

REPLIES = {
    "sleep": {"en": "Okay, I'll stop listening. Say hey Jarvis, wake up, when you need me.",
              "tr": "Tamam, dinlemeyi bırakıyorum. İhtiyacın olunca hey Jarvis, uyan de.",
              "de": "Okay, ich höre nicht mehr zu. Sag hey Jarvis, wach auf, wenn du mich brauchst."},
    "awake": {"en": "I'm listening again.", "tr": "Tekrar dinliyorum.", "de": "Ich höre wieder zu."},
}
# Words ignored when matching sleep and wake phrases, so "okay Jarvis, go to sleep please" still matches.
FILLER = {"hey", "hi", "ok", "okay", "please", "now", "lütfen", "hadi", "bitte", "jetzt"}


def reply_text(key: str, lang: str | None) -> str:
    return REPLIES[key].get(lang or "en", REPLIES[key]["en"])


class Session:
    def __init__(self, brain: "Brain", ws, sat_id: str):
        self.brain, self.ws, self.id = brain, ws, sat_id
        self.loop = asyncio.get_running_loop()
        self.outbox: asyncio.Queue[dict] = asyncio.Queue()
        self.wake = brain.wake_factory()
        self.state = "idle"          # idle -> listening -> thinking -> speaking -> listening ...
        self.collector = None
        self.lang: str | None = None

    def post(self, msg: dict) -> None:
        """Thread-safe send."""
        self.loop.call_soon_threadsafe(self.outbox.put_nowait, msg)

    async def sender(self) -> None:
        while True:
            msg = await self.outbox.get()
            await self.ws.send(json.dumps(msg, ensure_ascii=False))

    def _listen(self, timeout: float) -> None:
        self.collector = self.brain.collector_factory(timeout)
        self.state = "listening"

    def _end(self, notify: bool = True) -> None:
        self.state, self.collector = "idle", None
        self.wake.reset()
        self.brain.cancel_prewarm()
        if notify:
            self.outbox.put_nowait({"type": "conversation_end"})

    async def on_audio(self, chunk: bytes) -> None:
        if self.state == "idle":
            if self.wake.detect(chunk):
                self.wake.reset()
                if self.brain.asleep:
                    # silent: no chime, only listen for the wake phrase
                    log.info("[%s] wake word while asleep", self.id)
                else:
                    log.info("[%s] wake word", self.id)
                    self.outbox.put_nowait({"type": "wake"})
                    self.brain.prewarm()
                self._listen(self.brain.cfg.listen.start_timeout_s)
        elif self.state == "listening":
            result = self.collector.feed(chunk)
            if result == "timeout":
                self._end(notify=not self.brain.asleep)
            elif result == "done":
                pcm = self.collector.audio()
                self.state, self.collector = "thinking", None
                asyncio.create_task(self._turn(pcm, time.monotonic()))
        # thinking / speaking: audio is ignored

    async def _turn(self, pcm: bytes, t0: float) -> None:
        text, lang = await asyncio.to_thread(self.brain.transcribe, pcm)
        log.info("[%s] heard (%s, %.1fs): %s", self.id, lang, time.monotonic() - t0, text)
        norm = _normalize(text)
        if self.brain.asleep:
            if self.brain.is_phrase(norm, "wake"):
                self.brain.set_asleep(False)
                self.lang = lang
                for msg in ({"type": "wake"}, {"type": "say", "text": reply_text("awake", lang), "lang": lang},
                            {"type": "turn_end"}):
                    self.outbox.put_nowait(msg)
                self.state = "speaking"
            else:
                self._end(notify=False)
            return
        if norm in NOISE or text.lower().strip() in NOISE or norm in self.brain.stop_phrases:
            self._end()
            return
        if self.brain.is_phrase(norm, "sleep"):
            self.outbox.put_nowait({"type": "say", "text": reply_text("sleep", lang), "lang": lang})
            self._end()
            self.brain.set_asleep(True)
            return
        self.lang = lang

        first = []

        def on_sentence(s: str) -> None:
            if not first:
                first.append(s)
                log.info("[%s] first words after %.1fs", self.id, time.monotonic() - t0)
            self.post({"type": "say", "text": s, "lang": lang})

        try:
            await asyncio.to_thread(self.brain.agent.respond, text, on_sentence, lang)
        except Exception:
            log.exception("[%s] agent failed", self.id)
            on_sentence(FALLBACK)
        # on_sentence callbacks were queued before this line resumes, so order holds
        self.state = "speaking"
        self.outbox.put_nowait({"type": "turn_end"})

    def on_speech_done(self) -> None:
        if self.state == "speaking":
            self.brain.prewarm()
            self._listen(self.brain.cfg.assistant.followup_seconds)


class Brain:
    def __init__(self, cfg, *, agent=None, stt=None, wake_factory=None, collector_factory=None):
        self.cfg = cfg
        if agent is None:
            from .agent import make_agent
            from .assistant import build_tools
            from .memory import Memory

            memory = Memory(cfg.memory.path)
            tools = build_tools(cfg, memory)
            tools.ctx["announce"] = self.announce
            agent = make_agent(cfg, tools, memory)
        self.agent = agent
        self.tools = getattr(agent, "tools", None)    # for the admin UI
        self.memory = getattr(agent, "memory", None)
        if stt is None:
            from .audio import SpeechToText
            log.info("loading whisper model...")
            stt = SpeechToText(cfg.stt)
        self.stt = stt
        if wake_factory is None:
            from .audio import WakeWord
            wake_factory = lambda: WakeWord(cfg.wakeword.model, cfg.wakeword.threshold)  # noqa: E731
        self.wake_factory = wake_factory
        self.collector_factory = collector_factory or (
            lambda timeout: UtteranceCollector(cfg.listen, cfg.audio.sample_rate, timeout))
        self.stop_phrases = {_normalize(p) for p in cfg.assistant.stop_phrases}
        self.filler = FILLER | {_normalize(cfg.assistant.name)}
        self.phrases = {
            "sleep": {self._strip_filler(_normalize(p)) for p in cfg.assistant.sleep_phrases},
            "wake": {self._strip_filler(_normalize(p)) for p in cfg.assistant.wake_phrases},
        }
        self.asleep = False
        self.sessions: set[Session] = set()
        self._stt_lock = threading.Lock()

    def _strip_filler(self, norm: str) -> str:
        return " ".join(w for w in norm.split() if w not in self.filler)

    def is_phrase(self, norm: str, kind: str) -> bool:
        return self._strip_filler(norm) in self.phrases[kind]

    def set_asleep(self, asleep: bool) -> None:
        """Called on the event loop."""
        self.asleep = asleep
        log.info("sleep mode %s", "on" if asleep else "off")
        for s in list(self.sessions):
            s.outbox.put_nowait({"type": "sleep" if asleep else "awake"})

    def prewarm(self) -> None:
        """Start the next agent call while the user is still talking (claude_cli: hides CLI startup)."""
        fn = getattr(self.agent, "prewarm", None)
        if fn:
            threading.Thread(target=fn, daemon=True).start()

    def cancel_prewarm(self) -> None:
        fn = getattr(self.agent, "cancel_prewarm", None)
        if fn:
            fn()

    def transcribe(self, pcm: bytes):
        with self._stt_lock:
            return self.stt.transcribe(pcm)

    def announce(self, text: str, lang: str | None = None) -> None:
        """Called from tool threads (timers). Goes to every connected satellite."""
        for s in list(self.sessions):
            s.post({"type": "announce", "text": text, "lang": lang or s.lang})

    async def handler(self, ws) -> None:
        import websockets

        try:
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        except Exception:
            return
        token = self.cfg.server.token
        if hello.get("type") != "hello" or (token and hello.get("token") != token):
            await ws.close(code=4001, reason="unauthorized")
            return
        session = Session(self, ws, str(hello.get("id", "satellite")))
        self.sessions.add(session)
        log.info("satellite connected: %s", session.id)
        if self.asleep:
            session.post({"type": "sleep"})
        sender = asyncio.create_task(session.sender())
        try:
            async for msg in ws:
                if isinstance(msg, bytes):
                    await session.on_audio(msg)
                elif json.loads(msg).get("type") == "speech_done":
                    session.on_speech_done()
        except websockets.ConnectionClosed:
            pass
        finally:
            self.sessions.discard(session)
            sender.cancel()
            log.info("satellite disconnected: %s", session.id)

    async def serve(self, ready: asyncio.Event | None = None) -> None:
        import websockets

        s = self.cfg.server
        if not s.token:
            log.warning("server.token is not set: anyone on your network can connect")
        if s.admin:
            from .admin import AdminServer
            AdminServer(self, asyncio.get_running_loop())
        async with websockets.serve(self.handler, s.host, s.port, max_size=2**22, ping_interval=20):
            print(f"Brain listening on ws://{s.host}:{s.port}")
            if ready:
                ready.set()
            await asyncio.Future()
