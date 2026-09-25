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
    def __init__(self):
        self.script = []  # (text, lang) to return next; falls back to the default request

    def transcribe(self, pcm):
        return self.script.pop(0) if self.script else ("turn the lights warm", "tr")


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
    cfg["server"].update(host="127.0.0.1", port=port, token="secret", admin=False)
    cfg["listen"].update(silence_ms=200, start_timeout_s=1)
    return cfg


class TestCollector(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config("/nonexistent")

    def test_done_after_silence(self):
        c = UtteranceCollector(self.cfg.listen, 16000, 5, vad=FakeVad())
        results = [c.feed(SPEECH) for _ in range(5)] + [c.feed(SILENCE) for _ in range(16)]
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
        self.stt = FakeSTT()
        self.brain = Brain(
            self.cfg, agent=self.agent, stt=self.stt, wake_factory=FakeWake,
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

    async def utter(self, ws):
        await ws.send(WAKE)
        for f in [SPEECH] * 5 + [SILENCE] * 12:
            await ws.send(f)

    async def test_sleep_mode(self):
        async with websockets.connect(f"ws://127.0.0.1:{self.port}") as ws:
            await ws.send(json.dumps({"type": "hello", "id": "phone", "token": "secret"}))

            # "okay Jarvis, go to sleep please" -> goodbye, conversation ends, sleep broadcast
            self.stt.script.append(("Okay Jarvis, go to sleep please.", "en"))
            await self.utter(ws)
            self.assertEqual(await self.recv(ws), {"type": "wake"})
            say = await self.recv(ws)
            self.assertEqual(say["type"], "say")
            self.assertIn("stop listening", say["text"])
            self.assertEqual(await self.recv(ws), {"type": "conversation_end"})
            self.assertEqual(await self.recv(ws), {"type": "sleep"})
            self.assertTrue(self.brain.asleep)

            # asleep: wake word + an ordinary request is ignored silently, the agent never runs
            await self.utter(ws)
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(ws.recv(), 0.5)
            self.assertEqual(self.agent.calls, [])

            # "hey Jarvis, uyan" wakes it up and answers in Turkish
            self.stt.script.append(("Uyan!", "tr"))
            await self.utter(ws)
            msgs = [await self.recv(ws) for _ in range(4)]
            self.assertEqual(msgs[0], {"type": "awake"})
            self.assertEqual(msgs[1], {"type": "wake"})
            self.assertEqual(msgs[2], {"type": "say", "text": "Tekrar dinliyorum.", "lang": "tr"})
            self.assertEqual(msgs[3], {"type": "turn_end"})
            self.assertFalse(self.brain.asleep)

    def test_phrase_matching(self):
        from voiceagent.assistant import _normalize
        self.assertTrue(self.brain.is_phrase(_normalize("Jarvis, stop listening."), "sleep"))
        self.assertTrue(self.brain.is_phrase(_normalize("Dinlemeyi bırak lütfen"), "sleep"))
        self.assertFalse(self.brain.is_phrase(_normalize("I can't sleep tonight"), "sleep"))
        self.assertTrue(self.brain.is_phrase(_normalize("Hey Jarvis, wake up!"), "wake"))


class TestAdmin(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import tempfile
        from pathlib import Path
        from voiceagent.memory import Memory
        self.cfg = make_cfg(18767)
        self.cfg["server"].update(admin=True, admin_port=18768)
        self.cfg.source = Path(tempfile.mkdtemp()) / "config.yaml"
        self.agent = FakeAgent()
        self.agent.memory = Memory(tempfile.mktemp(suffix=".db"))
        self.brain = Brain(self.cfg, agent=self.agent, stt=FakeSTT(), wake_factory=FakeWake,
                           collector_factory=lambda t: UtteranceCollector(self.cfg.listen, 16000, t, vad=FakeVad()))
        ready = asyncio.Event()
        self.server = asyncio.create_task(self.brain.serve(ready))
        await asyncio.wait_for(ready.wait(), 5)

    async def asyncTearDown(self):
        self.server.cancel()
        try:
            await self.server
        except asyncio.CancelledError:
            pass

    def call(self, path, body=None, token="secret"):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:18768/api/{path}",
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    async def test_admin_api(self):
        code, _ = await asyncio.to_thread(self.call, "status", token="wrong")
        self.assertEqual(code, 401)

        code, st = await asyncio.to_thread(self.call, "status")
        self.assertEqual((code, st["asleep"], st["satellites"]), (200, False, []))

        await asyncio.to_thread(self.call, "sleep", {"asleep": True})
        await asyncio.sleep(0.05)
        self.assertTrue(self.brain.asleep)

        code, r = await asyncio.to_thread(self.call, "chat", {"text": "hello"})
        self.assertEqual(r["reply"], "Tamam. Işıkları sıcak yaptım.")

        _, m = await asyncio.to_thread(self.call, "memory", {"add": "likes tea"})
        self.assertEqual(m["facts"], ["likes tea"])
        _, m = await asyncio.to_thread(self.call, "memory", {"remove": "likes tea"})
        self.assertEqual(m["facts"], [])

        code, r = await asyncio.to_thread(self.call, "config", {"yaml": "- not a mapping"})
        self.assertEqual(code, 400)
        code, _ = await asyncio.to_thread(self.call, "config", {"yaml": "assistant:\n  name: Jarvis\n"})
        self.assertEqual(code, 200)
        self.assertIn("Jarvis", self.cfg.source.read_text())


if __name__ == "__main__":
    unittest.main()
