"""Personal mode: use your own Claude Pro/Max subscription through the official CLI.

The brain runs `claude -p` (Claude Code's documented headless mode) as a
subprocess on your own machine, logged in with your account via `claude login`.
It never reads or reuses the CLI's login token, which is what Anthropic bans.

Your tools reach the CLI through MCP: the CLI launches `voiceagent.mcp_bridge`,
which forwards each tool call over localhost to the ToolRelay in this process.
That way timers, memory and announcements behave exactly like API mode.

Only for you, on your machine. Anything other people use goes through API keys.

Latency: the CLI takes about half a second to start plus MCP setup. The brain
calls prewarm() at the wake word, so the process boots while the user is still
talking and only waits for the prompt on stdin. Extended thinking is off
(llm.cli_thinking) since it delays the first word by most of a second. The CLI also runs with
--setting-sources "" from ~/.voiceagent, so your own Claude Code hooks, plugins
and CLAUDE.md files don't load into the voice assistant.
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
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from .agent import LANG_NAMES, SentenceSplitter, build_system_prompt, clean_for_speech
from .memory import Memory
from .tools import ToolRegistry

log = logging.getLogger(__name__)

PACKAGE_PARENT = str(Path(__file__).resolve().parent.parent)
WARM_MAX_AGE_S = 120  # a prewarmed process older than this has a stale clock and memory in its prompt
WEB_TOOLS = ["WebSearch", "WebFetch"]


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


def format_prompt(history: list[dict], user_text: str, lang: str | None = None) -> str:
    """The CLI call is stateless, so earlier turns go into the prompt as a transcript.
    The reply language goes here rather than in the system prompt, which is fixed at prewarm time."""
    earlier = history[:-1] if history and history[-1]["role"] == "user" else history
    note = f"\n\n(The user spoke {LANG_NAMES[lang]}. Reply in {LANG_NAMES[lang]}.)" if lang in LANG_NAMES else ""
    if not earlier:
        return user_text + note
    lines = [f"{'User' if m['role'] == 'user' else 'You'}: {m['content']}" for m in earlier]
    return "Conversation so far:\n" + "\n".join(lines) + f"\n\nThe user now says:\n{user_text}" + note


def _stop(proc: subprocess.Popen) -> None:
    """SIGTERM so the CLI shuts its MCP servers down too, reaped in the background."""
    def reap():
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    threading.Thread(target=reap, daemon=True).start()


class ClaudeCLIAgent:
    def __init__(self, cfg, tools: ToolRegistry, memory: Memory):
        self.cfg, self.tools, self.memory = cfg, tools, memory
        llm = cfg.llm
        self.cli = shutil.which(llm.cli_path) or llm.cli_path
        if not os.path.exists(self.cli):
            raise SystemExit("claude CLI not found. Install Claude Code, then run `claude` once and log in.")
        self.relay = ToolRelay(tools) if tools.tools else None
        servers = {}
        if self.relay:
            env = {"VOICEAGENT_TOOLS_URL": self.relay.url, "VOICEAGENT_TOOLS_TOKEN": self.relay.token,
                   "PYTHONPATH": PACKAGE_PARENT}
            servers["voiceagent"] = {"command": sys.executable, "args": ["-m", "voiceagent.mcp_bridge"], "env": env}
        # your own MCP servers from config.yaml (llm.mcp_servers), same format as Claude Code's mcpServers
        servers.update(dict(llm.mcp_servers or {}))
        self.mcp_servers = list(servers)
        self.mcp_config = None
        if servers:
            fd, self.mcp_config = tempfile.mkstemp(suffix=".json", prefix="voiceagent-mcp-")
            with os.fdopen(fd, "w") as f:
                json.dump({"mcpServers": servers}, f)
            os.chmod(self.mcp_config, 0o600)
        self.workdir = Path(os.path.expanduser(cfg.memory.path)).parent
        self.workdir.mkdir(parents=True, exist_ok=True)
        self._warm: tuple[subprocess.Popen, float] | None = None
        self._warm_lock = threading.Lock()

    def command(self, system: str) -> list[str]:
        llm = self.cfg.llm
        builtin = WEB_TOOLS if llm.web_search else []
        cmd = [self.cli, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
               "--model", llm.cli_model, "--system-prompt", system, "--setting-sources", "",
               "--tools", ",".join(builtin)]
        allowed = builtin + [f"mcp__{name}" for name in self.mcp_servers]
        if self.mcp_config:
            cmd += ["--mcp-config", self.mcp_config, "--strict-mcp-config"]
        if allowed:
            cmd += ["--allowedTools", *allowed]
        return cmd + list(llm.cli_args or [])

    def _spawn(self) -> subprocess.Popen:
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)  # otherwise the CLI bills the API instead of your subscription
        if not self.cfg.llm.cli_thinking:
            env["MAX_THINKING_TOKENS"] = "0"  # extended thinking costs ~0.7 s before the first word
        return subprocess.Popen(self.command(build_system_prompt(self.cfg, self.memory, None)),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=env, cwd=self.workdir)

    def prewarm(self) -> None:
        """Boot a CLI process that waits for its prompt, so the next reply skips the startup."""
        with self._warm_lock:
            warm = self._warm
            if warm and warm[0].poll() is None and time.monotonic() - warm[1] < WARM_MAX_AGE_S:
                return
            self._warm = (self._spawn(), time.monotonic())
        if warm:
            _stop(warm[0])

    def cancel_prewarm(self) -> None:
        with self._warm_lock:
            warm, self._warm = self._warm, None
        if warm:
            _stop(warm[0])

    def _take_process(self) -> subprocess.Popen:
        with self._warm_lock:
            warm, self._warm = self._warm, None
        if warm and warm[0].poll() is None and time.monotonic() - warm[1] < WARM_MAX_AGE_S:
            return warm[0]
        if warm:
            _stop(warm[0])
        return self._spawn()

    def respond(self, user_text: str, on_sentence: Callable[[str], None], lang: str | None = None) -> str:
        llm = self.cfg.llm
        self.memory.add_turn("user", user_text)
        history = self.memory.recent_messages(llm.history_turns, llm.history_max_age_hours)
        prompt = format_prompt(history, user_text, lang)

        spoken: list[str] = []
        splitter = SentenceSplitter()

        def emit(sentences: list[str]) -> None:
            for s in sentences:
                s = clean_for_speech(s)
                if s:
                    spoken.append(s)
                    on_sentence(s)

        proc = self._take_process()
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
