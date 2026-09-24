"""End-to-end protocol test: real websocket server, fake speech components."""
import asyncio
import json
import unittest

import websockets

from voiceagent.config import load_config
from voiceagent.listening import UtteranceCollector
from voiceagent.server import Brain

WAKE = b"W" * 2560
SPEECH = b"S" * 2560
SILENCE = b"\x00" * 2560


class FakeVad:
    def is_speech(self, sub, sr):
        return sub[:1] == b"S"


class FakeWake:
    def detect(self, chunk):
        return chunk == WAKE

    def reset(self):
        pass


class FakeSTT:
    def transcribe(self, pcm):
        return ("turn the lights warm", "tr")


class FakeAgent:
    def __init__(self):
        self.calls = []

    def respond(self, text, on_sentence, lang):
        self.calls.append((text, lang))
        on_sentence("Tamam.")
        on_sentence("Işıkları sıcak yaptım.")
        return "Tamam. Işıkları sıcak yaptım."


def make_cfg(port):
    cfg = load_config("/nonexistent")
    cfg["server"].update(host="127.0.0.1", port=port, token="secret")
    cfg["listen"].update(silence_ms=200, start_timeout_s=1)
    return cfg


class TestCollector(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config("/nonexistent")

    def test_done_after_silence(self):
        c = UtteranceCollector(self.cfg.listen, 16000, 5, vad=FakeVad())
        results = [c.feed(SPEECH) for _ in range(5)] + [c.feed(SILENCE) for _ in range(12)]
        self.assertIn("done", results)
        self.assertGreater(len(c.audio()), 5 * 2560 - 1)

    def test_timeout_when_silent(self):
        c = UtteranceCollector(self.cfg.listen, 16000, 1, vad=FakeVad())
        results = [c.feed(SILENCE) for _ in range(20)]
        self.assertIn("timeout", results)


class TestProtocol(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.port = 18765
        self.cfg = make_cfg(self.port)
        self.agent = FakeAgent()
        self.brain = Brain(
            self.cfg, agent=self.agent, stt=FakeSTT(), wake_factory=FakeWake,
            collector_factory=lambda t: UtteranceCollector(self.cfg.listen, 16000, t, vad=FakeVad()),
        )
        ready = asyncio.Event()
        self.server = asyncio.create_task(self.brain.serve(ready))
        await asyncio.wait_for(ready.wait(), 5)

    async def asyncTearDown(self):
        self.server.cancel()
        try:
            await self.server
        except asyncio.CancelledError:
            pass

    async def recv(self, ws):
        return json.loads(await asyncio.wait_for(ws.recv(), 3))

    async def test_rejects_bad_token(self):
        async with websockets.connect(f"ws://127.0.0.1:{self.port}") as ws:
            await ws.send(json.dumps({"type": "hello", "id": "x", "token": "wrong"}))
            with self.assertRaises(websockets.ConnectionClosed) as cm:
                await ws.recv()
            self.assertEqual(cm.exception.rcvd.code, 4001)

    async def test_full_conversation(self):
        async with websockets.connect(f"ws://127.0.0.1:{self.port}") as ws:
            await ws.send(json.dumps({"type": "hello", "id": "phone", "token": "secret"}))
            await ws.send(SILENCE)
            await ws.send(WAKE)
            self.assertEqual(await self.recv(ws), {"type": "wake"})
            for f in [SPEECH] * 5 + [SILENCE] * 12:
                await ws.send(f)
            msgs = [await self.recv(ws) for _ in range(3)]
            self.assertEqual(msgs[0], {"type": "say", "text": "Tamam.", "lang": "tr"})
            self.assertEqual(msgs[1]["text"], "Işıkları sıcak yaptım.")
            self.assertEqual(msgs[2], {"type": "turn_end"})
            self.assertEqual(self.agent.calls, [("turn the lights warm", "tr")])

            # timer announcement from a tool thread reaches the satellite
            await asyncio.to_thread(self.brain.announce, "Your tea is done.")
            self.assertEqual((await self.recv(ws))["type"], "announce")

            # follow-up window: satellite finished speaking, user stays silent
            await ws.send(json.dumps({"type": "speech_done"}))
            await asyncio.sleep(0.05)
            for _ in range(80):
                await ws.send(SILENCE)
            self.assertEqual(await self.recv(ws), {"type": "conversation_end"})


if __name__ == "__main__":
    unittest.main()
