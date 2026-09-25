import os
import sys
import tempfile
import unittest
from pathlib import Path

from voiceagent.agent import make_agent
from voiceagent.claude_cli import ClaudeCLIAgent, format_prompt
from voiceagent.config import load_config
from voiceagent.memory import Memory
from voiceagent.tools import ToolRegistry
from voiceagent.tools.core import register_core_tools

FAKE = str(Path(__file__).with_name("fake_claude.py"))


class TestClaudeCLI(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config("/nonexistent")
        self.cfg["llm"].update(provider="claude_cli", cli_path=FAKE)
        self.mem = Memory(tempfile.mktemp(suffix=".db"))
        self.reg = ToolRegistry()
        self.reg.ctx["memory"] = self.mem
        register_core_tools(self.reg)

    def test_end_to_end_through_mcp_bridge(self):
        os.environ["ANTHROPIC_API_KEY"] = "must-not-reach-cli"
        agent = make_agent(self.cfg, self.reg, self.mem)
        self.assertIsInstance(agent, ClaudeCLIAgent)
        spoken = []
        reply = agent.respond("remember I like tea", spoken.append, lang="tr")
        self.assertTrue(spoken[0].startswith("Done, I saved that."))
        self.assertIn("Tools: forget,remember,settimer.", reply)  # underscores are stripped for speech
        self.assertIn("Turkish=True", reply)
        self.assertTrue(self.mem.facts()[0].startswith("prompt-len="))  # tool ran in the brain process
        agent.close()

    def test_session_is_reused_across_turns(self):
        agent = make_agent(self.cfg, self.reg, self.mem)
        agent.prewarm()
        a, b = [], []
        agent.respond("first thing", a.append, lang="en")
        agent.respond("second thing", b.append, lang="tr")
        pid = lambda spoken: " ".join(spoken).split("Pid=")[1]  # noqa: E731
        self.assertEqual(pid(a), pid(b))                       # same CLI process both turns
        self.assertIn("History=False", " ".join(b))           # the session already has the context
        self.assertIn("Turkish=True", " ".join(b))
        agent.close()

    def test_new_session_gets_history_and_close_stops_process(self):
        agent = make_agent(self.cfg, self.reg, self.mem)
        agent.respond("first thing", lambda s: None, lang="en")
        proc = agent._session.proc
        agent.close()
        proc.wait(timeout=5)
        spoken = []
        agent.respond("second thing", spoken.append, lang="en")
        self.assertIn("History=True", " ".join(spoken))       # restarted session replays recent turns
        agent.close()

    def test_command_flags(self):
        self.cfg["llm"]["mcp_servers"] = {"home": {"command": "home-mcp"}}
        agent = make_agent(self.cfg, self.reg, self.mem)
        cmd = agent.command("sys")
        self.assertEqual(cmd[cmd.index("--setting-sources") + 1], "")
        self.assertEqual(cmd[cmd.index("--tools") + 1], "WebSearch,WebFetch")
        allowed = cmd[cmd.index("--allowedTools") + 1:]
        self.assertEqual(allowed, ["WebSearch", "WebFetch", "mcp__voiceagent", "mcp__home"])

    def test_prompt_includes_history(self):
        hist = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"},
                {"role": "user", "content": "lights off"}]
        p = format_prompt(hist, "lights off")
        self.assertIn("User: hi\nYou: hello", p)
        self.assertIn("The user now says:\nlights off\n\n(Local time", p)
        self.assertTrue(format_prompt(hist[:1], "hi").startswith("hi\n\n(Local time"))
        self.assertTrue(format_prompt(hist[:1], "selam", "tr").endswith("Reply in Turkish.)"))
        self.assertIn("Local time", format_prompt(hist[:1], "hi"))


if __name__ == "__main__":
    unittest.main()
