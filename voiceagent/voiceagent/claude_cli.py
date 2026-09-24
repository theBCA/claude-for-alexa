"""Personal mode: use your own Claude Pro/Max subscription through the official CLI.

The brain runs `claude -p` (Claude Code's documented headless mode) as a
subprocess on your own machine, logged in with your account via `claude login`.
It never reads or reuses the CLI's login token, which is what Anthropic bans.

Your tools reach the CLI through MCP: the CLI launches `voiceagent.mcp_bridge`,
which forwards each tool call over localhost to the ToolRelay in this process.
That way timers, memory and announcements behave exactly like API mode.

Only for you, on your machine. Anything other people use goes through API keys.
Expect one to three seconds of extra startup per reply compared to the API.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from .agent import SentenceSplitter, build_system_prompt, clean_for_speech
from .memory import Memory
from .tools import ToolRegistry

log = logging.getLogger(__name__)

PACKAGE_PARENT = str(Path(__file__).resolve().parent.parent)


class ToolRelay:
    """Localhost HTTP endpoint that exposes a ToolRegistry to the MCP bridge process."""

    def __init__(self, tools: ToolRegistry):
        self.tools = tools
        self.token = secrets.token_urlsafe(24)
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _auth(self) -> bool:
                if self.headers.get("X-Token") != relay.token:
                    self.send_error(403)
                    return False
                return True

            def _json(self, obj) -> None:
                body = json.dumps(obj, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self._auth() and self.path == "/tools":
                    self._json(relay.tools.schemas())

            def do_POST(self):
                if not self._auth() or self.path != "/call":
                    return
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                out = relay.tools.run(req.get("name", ""), req.get("arguments") or {})
                log.info("tool %s(%s) -> %s", req.get("name"), req.get("arguments"), out)
                self._json({"result": out, "is_error": out.startswith("error")})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()


def format_prompt(history: list[dict], user_text: str) -> str:
    """The CLI call is stateless, so earlier turns go into the prompt as a transcript."""
    earlier = history[:-1] if history and history[-1]["role"] == "user" else history
    if not earlier:
        return user_text
    lines = [f"{'User' if m['role'] == 'user' else 'You'}: {m['content']}" for m in earlier]
    return "Conversation so far:\n" + "\n".join(lines) + f"\n\nThe user now says:\n{user_text}"


class ClaudeCLIAgent:
    def __init__(self, cfg, tools: ToolRegistry, memory: Memory):
        self.cfg, self.tools, self.memory = cfg, tools, memory
        llm = cfg.llm
        self.cli = shutil.which(llm.cli_path) or llm.cli_path
        if not os.path.exists(self.cli):
            raise SystemExit("claude CLI not found. Install Claude Code, then run `claude` once and log in.")
        self.relay = ToolRelay(tools) if tools.tools else None
        self.mcp_config = None
        if self.relay:
            env = {"VOICEAGENT_TOOLS_URL": self.relay.url, "VOICEAGENT_TOOLS_TOKEN": self.relay.token,
                   "PYTHONPATH": PACKAGE_PARENT}
            fd, self.mcp_config = tempfile.mkstemp(suffix=".json", prefix="voiceagent-mcp-")
            with os.fdopen(fd, "w") as f:
                json.dump({"mcpServers": {"voiceagent": {
                    "command": sys.executable, "args": ["-m", "voiceagent.mcp_bridge"], "env": env}}}, f)
            os.chmod(self.mcp_config, 0o600)

    def command(self, system: str) -> list[str]:
        llm = self.cfg.llm
        cmd = [self.cli, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
               "--model", llm.cli_model, "--system-prompt", system]
        if self.mcp_config:
            cmd += ["--mcp-config", self.mcp_config, "--strict-mcp-config", "--allowedTools", "mcp__voiceagent"]
        return cmd + list(llm.cli_args or [])

    def respond(self, user_text: str, on_sentence: Callable[[str], None], lang: str | None = None) -> str:
        llm = self.cfg.llm
        self.memory.add_turn("user", user_text)
        history = self.memory.recent_messages(llm.history_turns, llm.history_max_age_hours)
        prompt = format_prompt(history, user_text)

        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)  # otherwise the CLI bills the API instead of your subscription

        spoken: list[str] = []
        splitter = SentenceSplitter()

        def emit(sentences: list[str]) -> None:
            for s in sentences:
                s = clean_for_speech(s)
                if s:
                    spoken.append(s)
                    on_sentence(s)

        proc = subprocess.Popen(self.command(build_system_prompt(self.cfg, self.memory, lang)),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=env)
        timer = threading.Timer(llm.cli_timeout_s, proc.kill)
        timer.start()
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
            saw_delta, error = False, None
            for line in proc.stdout:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                kind = ev.get("type")
                if kind == "stream_event":
                    e = ev.get("event", {})
                    if e.get("type") == "content_block_delta" and e.get("delta", {}).get("type") == "text_delta":
                        saw_delta = True
                        emit(splitter.feed(e["delta"]["text"]))
                    elif e.get("type") == "message_stop":
                        emit(splitter.flush())
                elif kind == "assistant" and not saw_delta:
                    for block in ev.get("message", {}).get("content", []):
                        if block.get("type") == "text":
                            emit(splitter.feed(block["text"] + "\n"))
                elif kind == "result" and ev.get("is_error"):
                    error = ev.get("result") or "claude CLI reported an error"
            emit(splitter.flush())
            proc.wait()
            stderr = proc.stderr.read()
        finally:
            timer.cancel()

        if not spoken:
            raise RuntimeError(error or stderr.strip() or f"claude CLI exited with {proc.returncode}")
        reply = " ".join(spoken)
        self.memory.add_turn("assistant", reply)
        return reply
