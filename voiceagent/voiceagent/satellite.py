"""A satellite: the ears and mouth of the assistant in one room.

It streams mic audio to the brain and speaks whatever comes back. Deliberately
thin: on Android it needs only Python, websockets, pyyaml and Termux. Runs the
same way on a Mac, a Linux box or a Raspberry Pi.
"""
from __future__ import annotations

import asyncio
import json
import logging
import queue
import shlex
import threading
import time

from .tts import chime, make_tts

log = logging.getLogger(__name__)

CHUNK_BYTES = 2560  # 80 ms of 16 kHz mono int16


# ------------------------------------------------------------------------ mic sources
class SoundDeviceMic:
    def __init__(self, sample_rate: int = 16000, device=None):
        self.sample_rate, self.device = sample_rate, device
        self.stream = None

    async def start(self) -> None:
        import sounddevice as sd

        self.q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)
        loop = asyncio.get_running_loop()

        def cb(indata, frames, t, status):
            loop.call_soon_threadsafe(self._put, bytes(indata))

        self.stream = sd.RawInputStream(samplerate=self.sample_rate, blocksize=CHUNK_BYTES // 2,
                                        dtype="int16", channels=1, device=self.device, callback=cb)
        self.stream.start()

    def _put(self, b: bytes) -> None:
        if self.q.full():
            self.q.get_nowait()
        self.q.put_nowait(b)

    async def read(self) -> bytes:
        return await self.q.get()

    async def stop(self) -> None:
        if self.stream:
            self.stream.stop()
            self.stream.close()


class CommandMic:
    """Reads raw 16 kHz mono s16le PCM from any command's stdout (parec, arecord, ...)."""

    def __init__(self, command: str):
        self.command = command
        self.proc = None

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            *shlex.split(self.command), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)

    async def read(self) -> bytes:
        return await self.proc.stdout.readexactly(CHUNK_BYTES)

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()


# ----------------------------------------------------------------------------- voice
class Voice:
    """Speaks queued sentences in order and keeps the mic muted while it does,
    plus a short tail so a Bluetooth speaker's delayed audio isn't streamed back."""

    def __init__(self, tts, tail_ms: int):
        self.tts, self.tail = tts, tail_ms / 1000
        self.q: queue.Queue = queue.Queue()
        self.muted = threading.Event()
        self.on_done = None
        threading.Thread(target=self._worker, daemon=True).start()

    def speak(self, text: str, lang: str | None) -> None:
        self.muted.set()
        self.q.put(("speak", text, lang))

    def end(self, report_done: bool) -> None:
        self.q.put(("end", report_done, None))

    def pause(self, seconds: float) -> None:
        self.muted.set()
        self.q.put(("pause", seconds, None))

    def _worker(self) -> None:
        while True:
            kind, a, b = self.q.get()
            try:
                if kind == "speak":
                    self.tts.speak(a, b)
                elif kind == "pause":
                    time.sleep(a)
                    if self.q.empty():
                        self.muted.clear()
                elif kind == "end":
                    time.sleep(self.tail)
                    if self.q.empty():
                        self.muted.clear()
                    if a and self.on_done:
                        self.on_done()
            except Exception:
                log.exception("voice worker")


# ------------------------------------------------------------------------- main loop
def make_mic(sc, sample_rate: int):
    if sc.mic == "command":
        return CommandMic(sc.mic_command)
    return SoundDeviceMic(sample_rate, sc.input_device)


async def _session(ws, cfg, voice: Voice) -> None:
    sc = cfg.satellite
    loop = asyncio.get_running_loop()
    await ws.send(json.dumps({"type": "hello", "id": sc.id, "token": sc.token}))
    voice.on_done = lambda: asyncio.run_coroutine_threadsafe(ws.send(json.dumps({"type": "speech_done"})), loop)

    mic = make_mic(sc, cfg.audio.sample_rate)
    await mic.start()

    async def pump() -> None:
        while True:
            chunk = await mic.read()
            if not voice.muted.is_set():
                await ws.send(chunk)

    async def receive() -> None:
        async for raw in ws:
            msg = json.loads(raw)
            kind = msg.get("type")
            if kind == "wake":
                if sc.chime:
                    voice.pause(0.35)  # don't stream our own chime
                    chime("start")
            elif kind == "say":
                voice.speak(msg["text"], msg.get("lang"))
            elif kind == "announce":
                voice.speak(msg["text"], msg.get("lang"))
                voice.end(report_done=False)
            elif kind == "turn_end":
                voice.end(report_done=True)
            elif kind == "conversation_end" and sc.chime:
                chime("end")

    tasks = [asyncio.create_task(pump()), asyncio.create_task(receive())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            t.result()  # surface mic or socket errors
    finally:
        for t in tasks:
            t.cancel()
        await mic.stop()


async def run_satellite(cfg) -> None:
    import websockets

    sc = cfg.satellite
    tts_cfg = dict(cfg.tts)
    if sc.tts_engine:
        tts_cfg["engine"] = sc.tts_engine
    from .config import Config
    voice = Voice(make_tts(Config(tts_cfg)), sc.post_speech_mute_ms)

    backoff = 1
    while True:
        try:
            async with websockets.connect(sc.server, max_size=2**22, ping_interval=20) as ws:
                print(f"Connected to {sc.server} as '{sc.id}'. Say the wake word.")
                backoff = 1
                await _session(ws, cfg, voice)
        except websockets.InvalidStatus as e:
            log.error("server rejected connection: %s", e)
        except (OSError, websockets.ConnectionClosed, asyncio.IncompleteReadError) as e:
            if isinstance(e, websockets.ConnectionClosed) and e.rcvd and e.rcvd.code == 4001:
                raise SystemExit("Brain rejected the token. Check satellite.token / server.token.")
            log.warning("connection lost (%s), retrying in %ss", e, backoff)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)
