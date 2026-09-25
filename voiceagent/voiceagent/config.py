"""Configuration: YAML file deep-merged over sane defaults."""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "assistant": {
        "name": "Jarvis",
        "user_name": None,
        # After a reply, keep listening this long without needing the wake word.
        "followup_seconds": 6,
        # Saying one of these ends the conversation and returns to wake-word mode.
        "stop_phrases": ["stop", "thanks that's all", "that's all", "goodbye", "tamam", "teşekkürler", "danke"],
        # Sleep mode: ignore everything, even the wake word, until "hey Jarvis, <wake phrase>".
        # Matching ignores filler like "okay", "please" and the assistant's name.
        "sleep_phrases": ["go to sleep", "stop listening", "sleep", "sleep mode",
                          "uyu", "uyku moduna geç", "dinlemeyi bırak",
                          "schlaf", "geh schlafen", "hör auf zuzuhören"],
        "wake_phrases": ["wake up", "start listening", "uyan", "dinlemeye başla", "wach auf", "hör zu"],
        "persona": "",
    },
    "audio": {
        "sample_rate": 16000,
        "input_device": None,   # None = system default; see `python -m voiceagent audio-devices`
        "chime": True,
        # Mic stays muted this long after speech ends so a Bluetooth speaker's tail isn't heard.
        "post_speech_mute_ms": 600,
    },
    "wakeword": {
        "model": "hey_jarvis",   # built-in openWakeWord models, or a path to a custom .onnx
        "threshold": 0.5,
    },
    "listen": {
        "vad_aggressiveness": 2,  # 0-3, higher = stricter about what counts as speech
        "silence_ms": 1000,       # end of utterance after this much silence (higher = fewer cut-off sentences)
        "max_seconds": 20,
        "start_timeout_s": 5,     # give up if nothing is said after the wake word
    },
    "stt": {
        "model": "small",         # tiny / base / small / medium / large-v3
        "device": "auto",
        "compute_type": "int8",
        "language": None,         # None = auto-detect per utterance
        "languages": ["en", "tr", "de"],  # auto-detect only picks among these; [] = any language
        "beam_size": 5,           # 1 is fastest, 5 is Whisper's default and more accurate for tr/de
        # nudges spelling of names and mixed languages; keep it short
        "initial_prompt": "Conversation with the assistant Jarvis in English, Turkish or German.",
    },
    "tts": {
        "engine": "say",          # "say" (macOS) or "piper"
        "rate": 190,
        "voices": {"en": "Samantha", "tr": "Yelda", "de": "Anna"},
        "piper_models": {},       # e.g. {"en": "~/piper/en_US-amy-medium.onnx"}
    },
    "llm": {
        "provider": "anthropic",
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 600,
        "history_turns": 16,
        "history_max_age_hours": 6,
        "api_key": None,          # falls back to ANTHROPIC_API_KEY
        # provider "claude_cli": runs the official `claude` CLI logged in with your own
        # Pro/Max account. Personal use on your own machine only, never for other users.
        "cli_path": "claude",
        "cli_model": "haiku",
        "cli_args": [],           # extra flags for the claude CLI
        # Separate Claude Code login for the assistant: set a folder, then log in once with
        # CLAUDE_CONFIG_DIR=<folder> claude auth login. None = the normal `claude` login on this machine.
        "cli_config_dir": None,
        "cli_session_idle_s": 900,  # keep the CLI session this long after the last turn (fast replies vs ~200 MB)
        "cli_thinking": False,    # extended thinking: smarter on hard questions, ~0.7 s slower to start talking
        # Your own MCP servers (claude_cli mode), same format as Claude Code's "mcpServers":
        # {"home": {"command": "npx", "args": ["-y", "some-mcp-server"]}} or {"x": {"type": "http", "url": "..."}}
        "mcp_servers": {},
        # Weather, news, opening hours...: web search in both modes (CLI: WebSearch/WebFetch, API: web_search tool)
        "web_search": True,
        "cli_timeout_s": 120,
    },
    "memory": {"path": "~/.voiceagent/memory.db"},
    "server": {
        "host": "0.0.0.0",
        "port": 8765,
        "token": None,            # shared secret satellites must send; set this
        # Admin web UI at http://<brain-ip>:<admin_port>, same token. Without a token it only listens on localhost.
        "admin": True,
        "admin_port": 8766,
    },
    "satellite": {
        "server": "ws://127.0.0.1:8765",
        "id": "satellite",
        "token": None,
        "mic": "sounddevice",     # "sounddevice" (Mac/Linux) or "command" (Android/Termux, Raspberry Pi)
        "mic_command": "parec --raw --format=s16le --rate=16000 --channels=1 --latency-msec=80",
        "input_device": None,
        "tts_engine": None,       # None = use tts.engine; "termux" on Android
        "chime": True,
        "post_speech_mute_ms": 600,
    },
    "tools": {
        "govee": {
            "enabled": True,
            "devices": {},        # friendly name -> LAN IP, e.g. {"desk": "192.168.1.40"}
            "auto_discover": True,
        },
        "timers": {"enabled": True},
        # active once `python -m voiceagent roborock-login` has saved credentials
        "roborock": {"enabled": True},
        # Google Calendar and Gmail, active once `python -m voiceagent google-login client_secret.json` ran
        "google": {"enabled": True},
        # Lepro lights via Lepro's cloud, active once `python -m voiceagent lepro-login` ran
        "lepro": {"enabled": True},
        # active once `python -m voiceagent spotify-login` has saved a token (needs Spotify Premium)
        "spotify": {
            "enabled": True,
            "device": None,       # default Spotify Connect device name, e.g. "Galaxy" or "Echo"; None = the active one
            "duck_volume": 20,    # turn music down to this while you talk to the assistant; 0 = off
        },
    },
}


class Config(dict):
    """Dict with attribute access for nested sections."""

    def __getattr__(self, key: str) -> Any:
        try:
            value = self[key]
        except KeyError as e:
            raise AttributeError(key) from e
        return Config(value) if isinstance(value, dict) else value


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | None = None) -> Config:
    candidates = [path] if path else ["config.yaml", "~/.voiceagent/config.yaml"]
    data: dict = {}
    source = Path(os.path.expanduser(candidates[0] or "config.yaml"))
    for c in candidates:
        if not c:
            continue
        p = Path(os.path.expanduser(c))
        if p.exists():
            data = yaml.safe_load(p.read_text()) or {}
            source = p
            break
    cfg = Config(_deep_merge(DEFAULTS, data))
    cfg.source = source.resolve()  # where the admin UI saves edits
    return cfg
