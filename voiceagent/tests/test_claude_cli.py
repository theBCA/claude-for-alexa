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

    def test_prewarmed_process_is_used(self):
        agent = make_agent(self.cfg, self.reg, self.mem)
        agent.prewarm()
        warm = agent._warm[0]
        spoken = []
        agent.respond("remember I like tea", spoken.append, lang="tr")
        self.assertIsNone(agent._warm)
        self.assertEqual(warm.returncode, 0)  # the reply came from the prewarmed process
        self.assertIn("Turkish=True", " ".join(spoken))

    def test_cancel_prewarm_stops_process(self):
        agent = make_agent(self.cfg, self.reg, self.mem)
        agent.prewarm()
        warm = agent._warm[0]
        agent.cancel_prewarm()
        warm.wait(timeout=5)
        self.assertIsNone(agent._warm)

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
        self.assertTrue(p.endswith("The user now says:\nlights off"))
        self.assertEqual(format_prompt(hist[:1], "hi"), "hi")
        self.assertTrue(format_prompt(hist[:1], "selam", "tr").endswith("Reply in Turkish.)"))


if __name__ == "__main__":
    unittest.main()
