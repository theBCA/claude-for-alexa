"""`python -m voiceagent doctor`: check a setup before or after moving machines.

Prints a line per check so a fresh install (or a restored backup on another
computer) can be verified without talking to it. It never changes anything.
"""
from __future__ import annotations

import os
import shutil
import socket
from pathlib import Path

HOME = Path("~/.voiceagent").expanduser()
OK, WARN, BAD = "  ok ", " !! ", "FAIL"


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1" if host == "0.0.0.0" else host, port))
            return True
        except OSError:
            return False


def _lan_ip() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def _claude_logged_in(cli: str, config_dir: str | None) -> tuple[bool, str]:
    import json
    import subprocess
    env = dict(os.environ)
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = os.path.expanduser(config_dir)
    try:
        out = subprocess.run([cli, "auth", "status"], capture_output=True, text=True, timeout=15, env=env).stdout
        data = json.loads(out)
        return bool(data.get("loggedIn")), data.get("email") or data.get("authMethod", "logged in")
    except Exception:
        return False, "could not check"


def run(cfg) -> int:
    rows: list[tuple[str, str, str]] = []

    def check(name, status, detail=""):
        rows.append((status, name, detail))

    # config + token
    check("config.yaml", OK if getattr(cfg, "source", None) and Path(cfg.source).exists() else WARN,
          str(getattr(cfg, "source", "using defaults")))
    check("server token", OK if cfg.server.token else WARN,
          "set" if cfg.server.token else "not set: anyone on the LAN could connect")

    # Claude access
    llm = cfg.llm
    if llm.provider == "claude_cli":
        cli = shutil.which(llm.cli_path) or llm.cli_path
        found = os.path.exists(cli)
        check("claude CLI", OK if found else BAD, cli if found else "not found; install Claude Code")
        if found:
            logged, who = _claude_logged_in(cli, llm.cli_config_dir)
            hint = who if logged else "run 'claude auth login'" + (
                f" with CLAUDE_CONFIG_DIR={os.path.expanduser(llm.cli_config_dir)}" if llm.cli_config_dir else "")
            check("claude login", OK if logged else BAD, hint)
    else:
        key = llm.api_key or os.environ.get("ANTHROPIC_API_KEY")
        check("ANTHROPIC_API_KEY", OK if key else BAD, "set" if key else "export it or set llm.api_key")

    # speech models cache (downloaded on first run)
    hf = Path("~/.cache/huggingface").expanduser()
    check("whisper model cache", OK if hf.exists() else WARN,
          f"stt.model={cfg.stt.model}" + ("" if hf.exists() else "; downloads on first run"))

    # TTS engine for host-side speech (run / chat --speak); satellites speak on their own
    engine = cfg.tts.engine
    tool = {"say": "say", "piper": "piper"}.get(engine)
    if tool:
        check(f"tts engine '{engine}'", OK if shutil.which(tool) else WARN,
              "found" if shutil.which(tool) else f"'{tool}' not on PATH (only needed for host-side speech)")

    # tool credentials
    creds = {"Spotify": "spotify.json", "Google": "google.json", "Roborock": "roborock.json", "Lepro": "lepro.json"}
    for label, fname in creds.items():
        present = (HOME / fname).exists()
        check(f"{label}", OK if present else WARN, "linked" if present else "not linked (optional)")
    if (HOME / "lepro.json").exists():
        check("Lepro TLS key", OK if (HOME / "lepro_client_key.pem").exists() else BAD,
              "present" if (HOME / "lepro_client_key.pem").exists() else "missing lepro_client_key.pem")

    # ports
    check(f"port {cfg.server.port} (brain)", OK if _port_free(cfg.server.host, cfg.server.port) else WARN,
          "free" if _port_free(cfg.server.host, cfg.server.port) else "in use (brain already running?)")
    if cfg.server.admin:
        check(f"port {cfg.server.admin_port} (admin)", OK if _port_free(cfg.server.host, cfg.server.admin_port) else WARN,
              "free" if _port_free(cfg.server.host, cfg.server.admin_port) else "in use")

    # print
    ip = _lan_ip()
    print(f"\nvoiceagent doctor  (this machine: {ip})\n")
    worst = 0
    for status, name, detail in rows:
        print(f"  [{status}] {name:28} {detail}")
        worst = max(worst, {OK: 0, WARN: 1, BAD: 2}[status])
    print()
    print(f"Satellites connect to:  ws://{ip}:{cfg.server.port}   (token from config.yaml)")
    print(f"Admin UI:               http://{ip}:{cfg.server.admin_port}" if cfg.server.admin else "Admin UI: disabled")
    if worst == 2:
        print("\nSomething essential is missing (FAIL above). The brain won't answer until it's fixed.")
    elif worst == 1:
        print("\nUsable. The !! lines are optional integrations or warnings.")
    else:
        print("\nAll good.")
    return worst
