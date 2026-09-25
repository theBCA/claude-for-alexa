"""Personal mode: use your own Claude Pro/Max subscription through the official CLI.

The brain runs `claude -p` (Claude Code's documented headless mode) as a
subprocess on your own machine, logged in with your account via `claude login`.
It never reads or reuses the CLI's login token, which is what Anthropic bans.

Your tools reach the CLI through MCP: the CLI launches `voiceagent.mcp_bridge`,
which forwards each tool call over localhost to the ToolRelay in this process.
That way timers, memory and announcements behave exactly like API mode.

Only for you, on your machine. Anything other people use goes through API keys.

Latency: one CLI process stays alive across turns (--input-format stream-json),
so a reply only waits for the model: first token in about 0.6 s instead of 1.3 to
2 s for a fresh `claude -p` per turn, which only initialises after it has read
its prompt. The session is started at the wake word if none is running, and is
replaced after llm.cli_session_idle_s of silence (frees ~200 MB), after
llm.history_turns turns, or when it dies. A new session gets the recent
conversation from memory in its first message, so nothing is lost.

Extended thinking is off (llm.cli_thinking) since it delays the first word by
most of a second. The CLI runs with --setting-sources "" from ~/.voiceagent, so
your own Claude Code hooks, plugins and CLAUDE.md files don't load into it.
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
    """First message of a session: earlier turns go in as a transcript, since the session starts empty.
    The reply language and the time go in every message; the system prompt is fixed when the session starts."""
    earlier = history[:-1] if history and history[-1]["role"] == "user" else history
    if not earlier:
        return user_text + turn_note(lang)
    lines = [f"{'User' if m['role'] == 'user' else 'You'}: {m['content']}" for m in earlier]
    return "Conversation so far:\n" + "\n".join(lines) + f"\n\nThe user now says:\n{user_text}" + turn_note(lang)


def turn_note(lang: str | None) -> str:
    note = f"(Local time {time.strftime('%H:%M')}."
    if lang in LANG_NAMES:
        note += f" The user spoke {LANG_NAMES[lang]}. Reply in {LANG_NAMES[lang]}."
    return "\n\n" + note + ")"


def _stop(proc: subprocess.Popen) -> None:
    """Close stdin (the CLI exits cleanly and stops its MCP servers), then make sure, in the background."""
    def reap():
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    threading.Thread(target=reap, daemon=True).start()


class _Session:
    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self.last_used = time.monotonic()
        self.turns = 0

    def alive(self) -> bool:
        return self.proc.poll() is None


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
        self._session: _Session | None = None
        self._session_lock = threading.Lock()   # guards self._session
        self._turn_lock = threading.Lock()      # one turn at a time per session
        threading.Thread(target=self._reaper, daemon=True, name="cli-reaper").start()

    def command(self, system: str) -> list[str]:
        llm = self.cfg.llm
        builtin = WEB_TOOLS if llm.web_search else []
        cmd = [self.cli, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
               "--include-partial-messages", "--model", llm.cli_model, "--system-prompt", system,
               "--setting-sources", "", "--tools", ",".join(builtin)]
        allowed = builtin + [f"mcp__{name}" for name in self.mcp_servers]
        if self.mcp_config:
            cmd += ["--mcp-config", self.mcp_config, "--strict-mcp-config"]
        if allowed:
            cmd += ["--allowedTools", *allowed]
        return cmd + list(llm.cli_args or [])

    def _spawn(self) -> _Session:
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)  # otherwise the CLI bills the API instead of your subscription
        if not self.cfg.llm.cli_thinking:
            env["MAX_THINKING_TOKENS"] = "0"  # extended thinking costs ~0.7 s before the first word
        # stderr to a file: a long-lived process would block once a pipe nobody reads fills up
        errlog = open(self.workdir / "claude-cli.log", "a")
        proc = subprocess.Popen(self.command(build_system_prompt(self.cfg, self.memory, None)),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errlog,
                                text=True, bufsize=1, env=env, cwd=self.workdir)
        errlog.close()
        log.info("claude session started (pid %s)", proc.pid)
        return _Session(proc)

    def _current(self) -> tuple[_Session, bool]:
        """The live session, or a new one. Returns (session, is_new)."""
        with self._session_lock:
            s = self._session
            if s and s.alive() and s.turns < self.cfg.llm.history_turns:
                return s, s.turns == 0
            if s:
                _stop(s.proc)
            self._session = self._spawn()
            return self._session, True

    def prewarm(self) -> None:
        """Called at the wake word: make sure a session is booting while the user talks."""
        self._current()

    def close(self) -> None:
        with self._session_lock:
            s, self._session = self._session, None
        if s:
            _stop(s.proc)

    def _reaper(self) -> None:
        while True:
            time.sleep(15)
            with self._session_lock:
                s = self._session
                idle = s and time.monotonic() - s.last_used > self.cfg.llm.cli_session_idle_s
                if s and (idle or not s.alive()) and not self._turn_lock.locked():
                    self._session = None
                    _stop(s.proc)
                    log.info("claude session closed (%s)", "idle" if s.alive() else "exited")

    def respond(self, user_text: str, on_sentence: Callable[[str], None], lang: str | None = None) -> str:
        with self._turn_lock:
            try:
                return self._respond(user_text, on_sentence, lang)
            except Exception:
                self.close()  # never reuse a session in an unknown state
                raise

    def _respond(self, user_text: str, on_sentence: Callable[[str], None], lang: str | None) -> str:
        llm = self.cfg.llm
        self.memory.add_turn("user", user_text)
        session, new = self._current()
        if new:
            history = self.memory.recent_messages(llm.history_turns, llm.history_max_age_hours)
            prompt = format_prompt(history, user_text, lang)
        else:
            prompt = user_text + turn_note(lang)

        spoken: list[str] = []
        splitter = SentenceSplitter()

        def emit(sentences: list[str]) -> None:
            for s in sentences:
                s = clean_for_speech(s)
                if s:
                    spoken.append(s)
                    on_sentence(s)

        proc = session.proc
        timer = threading.Timer(llm.cli_timeout_s, proc.kill)
        timer.start()
        error, finished = None, False
        try:
            proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n")
            proc.stdin.flush()
            saw_delta = False
            for line in proc.stdout:  # until this turn's "result"; the process stays up for the next turn
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
                elif kind == "result":
                    finished = True
                    if ev.get("is_error"):
                        error = ev.get("result") or "claude CLI reported an error"
                    break
            emit(splitter.flush())
        except (BrokenPipeError, OSError) as e:
            error = f"claude session broke: {e}"
        finally:
            timer.cancel()
        session.turns += 1
        session.last_used = time.monotonic()

        if not finished or error:
            self.close()
        if not spoken:
            raise RuntimeError(error or f"claude CLI ended without a reply (exit {proc.poll()}), "
                               f"see {self.workdir / 'claude-cli.log'}")
        reply = " ".join(spoken)
        self.memory.add_turn("assistant", reply)
        return reply
