import json
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

from voiceagent.agent import Agent, SentenceSplitter, clean_for_speech
from voiceagent.config import load_config
from voiceagent.memory import Memory
from voiceagent.tools import ToolRegistry
from voiceagent.tools import govee
from voiceagent.tools.core import register_core_tools


class TestSplitter(unittest.TestCase):
    def test_streams_sentences(self):
        s = SentenceSplitter(min_chars=10)
        out = []
        for tok in ["Hello there", ", how are", " you today? I turned", " the lights off. Ok"]:
            out += s.feed(tok)
        out += s.flush()
        self.assertEqual(out, ["Hello there, how are you today?", "I turned the lights off.", "Ok"])

    def test_decimal_not_split(self):
        s = SentenceSplitter(min_chars=5)
        self.assertEqual(s.feed("It is 3.5 degrees outside. "), ["It is 3.5 degrees outside."])

    def test_clean(self):
        self.assertEqual(clean_for_speech("**Sure!**\n- one\n- two"), "Sure! one two")


class TestMemory(unittest.TestCase):
    def setUp(self):
        self.m = Memory(tempfile.mktemp(suffix=".db"))

    def test_alternation(self):
        self.m.add_turn("assistant", "orphan")
        self.m.add_turn("user", "a")
        self.m.add_turn("user", "b")
        self.m.add_turn("assistant", "c")
        msgs = self.m.recent_messages(10, 1)
        self.assertEqual([x["role"] for x in msgs], ["user", "assistant"])
        self.assertEqual(msgs[0]["content"], "a\nb")

    def test_facts(self):
        self.m.add_fact("Partner is Ceren")
        self.m.add_fact("Likes warm light")
        self.assertEqual(self.m.remove_facts("Ceren"), 1)
        self.assertEqual(self.m.facts(), ["Likes warm light"])


class TestTools(unittest.TestCase):
    def test_registry_and_errors(self):
        r = ToolRegistry()
        r.ctx["memory"] = Memory(tempfile.mktemp(suffix=".db"))
        register_core_tools(r)
        self.assertEqual({t["name"] for t in r.schemas()}, {"remember", "forget", "set_timer"})
        self.assertEqual(r.run("remember", {"fact": "x"}), "saved")
        self.assertTrue(r.run("nope", {}).startswith("error"))

    def test_timer_announces(self):
        r = ToolRegistry()
        got = []
        r.ctx.update(memory=None, announce=got.append)
        register_core_tools(r)
        r.run("set_timer", {"seconds": 1, "label": "tea"})
        time.sleep(1.3)
        self.assertEqual(got, ["Your tea is done."])

    def test_govee_packets(self):
        sent = []
        with mock.patch.object(govee, "send", lambda ip, cmd, data: sent.append((ip, cmd, data))):
            r = ToolRegistry()
            govee.register_govee_tools(r, {"desk": "10.0.0.2", "tv": "10.0.0.3"}, auto_discover=False)
            self.assertIn("ok", r.run("control_lights", {"target": "all", "action": "color", "color": "#FF8C32"}))
            r.run("control_lights", {"target": "desk", "action": "brightness", "brightness": 150})
            self.assertIn("unknown light", r.run("control_lights", {"target": "kitchen", "action": "on"}))
        self.assertEqual(sent[0], ("10.0.0.2", "colorwc", {"color": {"r": 255, "g": 140, "b": 50}, "colorTemInKelvin": 0}))
        self.assertEqual(sent[2], ("10.0.0.2", "brightness", {"value": 100}))
        self.assertEqual(json.loads(govee.build_command("turn", {"value": 1})), {"msg": {"cmd": "turn", "data": {"value": 1}}})


# ---- Agent tool loop with a fake Anthropic client -------------------------------------
class Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def model_dump(self, exclude_none=False):
        return dict(self.__dict__)


class FakeStream:
    def __init__(self, chunks, final):
        self.text_stream, self.final = chunks, final

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.final


class TestAgentLoop(unittest.TestCase):
    def test_tool_round_trip(self):
        responses = [
            FakeStream([], Block(stop_reason="tool_use", content=[
                Block(type="tool_use", id="t1", name="remember", input={"fact": "likes tea"})])),
            FakeStream(["Got it, ", "I'll remember that. "], Block(stop_reason="end_turn", content=[
                Block(type="text", text="Got it, I'll remember that.")])),
        ]
        calls = []

        class Messages:
            def stream(self, **kw):
                calls.append({**kw, "messages": [dict(m) for m in kw["messages"]]})
                return responses[len(calls) - 1]

        fake = types.SimpleNamespace(Anthropic=lambda api_key: types.SimpleNamespace(messages=Messages()))
        with mock.patch.dict(sys.modules, {"anthropic": fake}):
            cfg = load_config("/nonexistent")
            cfg["llm"]["api_key"] = "test"
            mem = Memory(tempfile.mktemp(suffix=".db"))
            reg = ToolRegistry()
            reg.ctx["memory"] = mem
            register_core_tools(reg, timers_enabled=False)
            spoken = []
            reply = Agent(cfg, reg, mem).respond("remember I like tea", spoken.append, lang="tr")
        self.assertEqual(reply, "Got it, I'll remember that.")
        self.assertEqual(mem.facts(), ["likes tea"])
        self.assertEqual(calls[1]["messages"][-1]["content"][0]["content"], "saved")
        self.assertIn("Reply in Turkish", calls[0]["system"])


if __name__ == "__main__":
    unittest.main()
