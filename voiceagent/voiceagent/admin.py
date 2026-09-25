"""Admin web UI: http://<brain-ip>:8766, protected by server.token.

A small JSON API on the standard library's HTTP server, running in its own
thread next to the websocket brain, plus one static page (admin.html). Anything
that touches brain state owned by the event loop (sleep mode, satellites) is
handed to the loop with call_soon_threadsafe.

  GET  /api/status          satellites, sleep mode, tools, recent log lines
  POST /api/sleep           {"asleep": true|false}
  POST /api/tool            {"name": "...", "arguments": {...}}   run any tool directly
  POST /api/chat            {"text": "...", "speak": false}      talk to the assistant by typing
  POST /api/announce        {"text": "...", "lang": "tr"}        speak on every satellite
  GET  /api/memory          long-term facts
  POST /api/memory          {"add": "..."} or {"remove": "..."}
  GET  /api/config          config.yaml as text (read from disk)
  POST /api/config          {"yaml": "..."}  validated, previous version kept as .bak
  POST /api/restart         re-executes the brain so config changes apply
"""
from __future__ import annotations

import collections
import hmac
import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

PAGE = Path(__file__).with_name("admin.html")


class LogBuffer(logging.Handler):
    """Keeps the last lines from the voiceagent loggers for the live log view."""

    def __init__(self, size: int = 300):
        super().__init__(logging.INFO)
        self.lines: collections.deque[dict] = collections.deque(maxlen=size)
        self.seq = 0

    def emit(self, record: logging.LogRecord) -> None:
        self.seq += 1
        self.lines.append({"seq": self.seq, "t": time.strftime("%H:%M:%S", time.localtime(record.created)),
                           "level": record.levelname, "src": record.name.removeprefix("voiceagent."),
                           "msg": record.getMessage()})


class AdminServer:
    def __init__(self, brain, loop):
        self.brain, self.loop, self.cfg = brain, loop, brain.cfg
        self.token = self.cfg.server.token
        self.logs = LogBuffer()
        logging.getLogger("voiceagent").addHandler(self.logs)
        host = self.cfg.server.host if self.token else "127.0.0.1"
        if not self.token:
            log.warning("admin UI only on localhost because server.token is not set")
        admin = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, obj, code: int = 200) -> None:
                self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json")

            def _authorized(self) -> bool:
                if not admin.token:
                    return True
                given = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
                if hmac.compare_digest(given.encode(), str(admin.token).encode()):
                    return True
                self._json({"error": "unauthorized"}, 401)
                return False

            def do_GET(self):
                if self.path in ("/", "/index.html"):
                    self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
                elif self.path.startswith("/api/") and self._authorized():
                    self._route("GET", {})

            def do_POST(self):
                if not self.path.startswith("/api/") or not self._authorized():
                    return
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                except ValueError:
                    return self._json({"error": "invalid JSON"}, 400)
                self._route("POST", body)

            def _route(self, method: str, body: dict) -> None:
                fn = getattr(admin, f"{method.lower()}_{self.path[5:].split('?')[0].replace('/', '_')}", None)
                if fn is None:
                    return self._json({"error": "not found"}, 404)
                try:
                    self._json(fn(body))
                except ValueError as e:
                    self._json({"error": str(e)}, 400)
                except Exception as e:
                    log.exception("admin %s %s failed", method, self.path)
                    self._json({"error": str(e)}, 500)

        self.server = ThreadingHTTPServer((host, self.cfg.server.admin_port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True, name="admin").start()
        print(f"Admin UI on http://{host}:{self.cfg.server.admin_port}")

    # ------------------------------------------------------------------ API
    def get_status(self, _):
        b, llm = self.brain, self.cfg.llm
        return {
            "name": self.cfg.assistant.name,
            "asleep": b.asleep,
            "satellites": [{"id": s.id, "state": s.state, "lang": s.lang} for s in list(b.sessions)],
            "provider": llm.provider,
            "model": llm.cli_model if llm.provider == "claude_cli" else llm.model,
            "wakeword": self.cfg.wakeword.model,
            "tools": b.tools.schemas() if b.tools else [],
            "log": list(self.logs.lines),
        }

    def post_sleep(self, body):
        asleep = bool(body.get("asleep"))
        self.loop.call_soon_threadsafe(self.brain.set_asleep, asleep)
        return {"asleep": asleep}

    def post_tool(self, body):
        if not self.brain.tools:
            raise ValueError("no tools available")
        name, args = body.get("name", ""), body.get("arguments") or {}
        out = self.brain.tools.run(name, args)
        log.info("admin tool %s(%s) -> %s", name, args, out)
        return {"result": out}

    def post_chat(self, body):
        text = (body.get("text") or "").strip()
        if not text:
            raise ValueError("empty message")
        lang = body.get("lang") or None
        sentences: list[str] = []

        def on_sentence(s: str) -> None:
            sentences.append(s)
            if body.get("speak"):
                self.brain.announce(s, lang)

        log.info("admin chat: %s", text)
        self.brain.agent.respond(text, on_sentence, lang)
        return {"reply": " ".join(sentences)}

    def post_announce(self, body):
        text = (body.get("text") or "").strip()
        if not text:
            raise ValueError("empty text")
        self.brain.announce(text, body.get("lang") or None)
        return {"ok": True, "satellites": len(self.brain.sessions)}

    def get_memory(self, _):
        return {"facts": self.brain.memory.facts() if self.brain.memory else []}

    def post_memory(self, body):
        mem = self.brain.memory
        if mem is None:
            raise ValueError("memory not available")
        if body.get("add"):
            mem.add_fact(body["add"])
        elif body.get("remove"):
            mem.remove_fact(body["remove"])
        return self.get_memory(None)

    def get_config(self, _):
        path = self.cfg.source
        return {"path": str(path), "yaml": path.read_text() if path.exists() else ""}

    def post_config(self, body):
        text = body.get("yaml", "")
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise ValueError(f"not valid YAML: {e}") from e
        if data is not None and not isinstance(data, dict):
            raise ValueError("config.yaml must be a mapping of sections")
        path = self.cfg.source
        if path.exists():
            path.with_suffix(path.suffix + ".bak").write_text(path.read_text())
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        log.info("admin saved %s", path)
        return {"saved": str(path)}

    def post_restart(self, _):
        log.info("admin requested a restart")

        def restart():
            time.sleep(0.5)  # let the HTTP response go out first
            if os.name == "nt":
                import subprocess
                subprocess.Popen([sys.executable, *sys.orig_argv[1:]], cwd=os.getcwd(),
                                 creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
                os._exit(0)
            os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])

        threading.Thread(target=restart, daemon=True).start()
        return {"restarting": True}
