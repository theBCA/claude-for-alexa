"""The brain: builds context, streams the LLM reply sentence by sentence, runs tools.

Streaming matters for voice. Instead of waiting for the whole reply, the first
sentence goes to TTS as soon as it is complete, which cuts perceived latency
by one to three seconds on longer answers.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import re
from typing import Callable

from .memory import Memory
from .tools import ToolRegistry

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 6

_BOUNDARY = re.compile(r"[.!?…]+[\"'”’)\]]*\s+|\n+")
_MARKDOWN = re.compile(r"[*_#`>]+")


def clean_for_speech(text: str) -> str:
    text = _MARKDOWN.sub("", text)
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.M)
    return re.sub(r"\s+", " ", text).strip()


class SentenceSplitter:
    """Accumulates streamed text and emits speakable sentences."""

    def __init__(self, min_chars: int = 24):
        self.min_chars = min_chars
        self.buf = ""

    def feed(self, text: str) -> list[str]:
        self.buf += text
        out, start = [], 0
        for m in _BOUNDARY.finditer(self.buf):
            piece = self.buf[start:m.end()].strip()
            if len(piece) >= self.min_chars:
                out.append(piece)
                start = m.end()
        self.buf = self.buf[start:]
        return out

    def flush(self) -> list[str]:
        piece, self.buf = self.buf.strip(), ""
        return [piece] if piece else []


LANG_NAMES = {"en": "English", "tr": "Turkish", "de": "German"}


def build_system_prompt(cfg, memory: Memory, lang: str | None) -> str:
    a = cfg.assistant
    who = f" for {a.user_name}" if a.user_name else ""
    now = dt.datetime.now().strftime("%A %d %B %Y, %H:%M")
    parts = [
        f"You are {a.name}, a voice assistant{who} running on a home computer. "
        "Everything you write is converted to speech and played through a speaker.",
        "Speak like a person in conversation: usually one to three short sentences. "
        "Never use markdown, lists, emojis, URLs or code. Write numbers the way they are spoken when it helps.",
        "When you use a tool, do not announce it first. Do the action, then confirm in a few words.",
        "Transcripts come from speech recognition and can contain mistakes. "
        "If a request is unclear, ask one short question instead of guessing on anything with consequences.",
        f"Current local time: {now}.",
    ]
    if lang and lang in LANG_NAMES:
        parts.append(f"The user just spoke {LANG_NAMES[lang]}. Reply in {LANG_NAMES[lang]}.")
    else:
        parts.append("Reply in the language the user speaks.")
    facts = memory.facts()
    if facts:
        parts.append("Things you know about the user:\n" + "\n".join(f"- {f}" for f in facts))
    if a.persona:
        parts.append(a.persona)
    return "\n\n".join(parts)


class Agent:
    def __init__(self, cfg, tools: ToolRegistry, memory: Memory):
        import anthropic  # imported lazily so tests run without the SDK

        self.cfg = cfg
        self.tools = tools
        self.memory = memory
        api_key = cfg.llm.api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise SystemExit("Set ANTHROPIC_API_KEY or llm.api_key in config.yaml")
        self.client = anthropic.Anthropic(api_key=api_key)

    def system_prompt(self, lang: str | None) -> str:
        return build_system_prompt(self.cfg, self.memory, lang)

    def respond(self, user_text: str, on_sentence: Callable[[str], None], lang: str | None = None) -> str:
        llm = self.cfg.llm
        self.memory.add_turn("user", user_text)
        messages = self.memory.recent_messages(llm.history_turns, llm.history_max_age_hours)
        if not messages or messages[-1]["role"] != "user":
            messages.append({"role": "user", "content": user_text})

        system = self.system_prompt(lang)
        spoken: list[str] = []

        def emit(sentences: list[str]) -> None:
            for s in sentences:
                s = clean_for_speech(s)
                if s:
                    spoken.append(s)
                    on_sentence(s)

        for _ in range(MAX_TOOL_ROUNDS):
            splitter = SentenceSplitter()
            kwargs = dict(model=llm.model, max_tokens=llm.max_tokens, system=system, messages=messages)
            if self.tools.tools:
                kwargs["tools"] = self.tools.schemas()
            with self.client.messages.stream(**kwargs) as stream:
                for delta in stream.text_stream:
                    emit(splitter.feed(delta))
                final = stream.get_final_message()
            emit(splitter.flush())

            messages.append({"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in final.content]})
            if final.stop_reason != "tool_use":
                break

            results = []
            for block in final.content:
                if block.type == "tool_use":
                    out = self.tools.run(block.name, block.input)
                    log.info("tool %s(%s) -> %s", block.name, block.input, out)
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": out})
            messages.append({"role": "user", "content": results})

        reply = " ".join(spoken)
        self.memory.add_turn("assistant", reply)
        return reply


def make_agent(cfg, tools: ToolRegistry, memory: Memory):
    """anthropic = API key (fast, for anything other people use).
    claude_cli = your own Claude subscription through the official CLI, personal use on your own machine."""
    if cfg.llm.provider == "claude_cli":
        from .claude_cli import ClaudeCLIAgent
        return ClaudeCLIAgent(cfg, tools, memory)
    return Agent(cfg, tools, memory)
