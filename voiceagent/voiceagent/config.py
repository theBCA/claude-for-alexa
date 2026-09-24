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
        "silence_ms": 800,        # end of utterance after this much silence
        "max_seconds": 20,
        "start_timeout_s": 5,     # give up if nothing is said after the wake word
    },
    "stt": {
        "model": "small",         # tiny / base / small / medium / large-v3
        "device": "auto",
        "compute_type": "int8",
        "language": None,         # None = auto-detect per utterance
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
        "cli_args": ["--tools", ""],   # disables Claude Code's coding tools; drop if your CLI version rejects it
        "cli_timeout_s": 120,
    },
    "memory": {"path": "~/.voiceagent/memory.db"},
    "server": {
        "host": "0.0.0.0",
        "port": 8765,
        "token": None,            # shared secret satellites must send; set this
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
    for c in candidates:
        if not c:
            continue
        p = Path(os.path.expanduser(c))
        if p.exists():
            data = yaml.safe_load(p.read_text()) or {}
            break
    return Config(_deep_merge(DEFAULTS, data))
