"""Built-in tools: long-term memory and timers."""
from __future__ import annotations

import threading

from . import ToolRegistry


def register_core_tools(reg: ToolRegistry, timers_enabled: bool = True) -> None:
    @reg.register(
        "remember",
        "Save a durable fact about the user or their home for future conversations "
        "(preferences, names of people, routines). Only when the user states it or asks you to remember.",
        {"type": "object", "properties": {"fact": {"type": "string"}}, "required": ["fact"]},
    )
    def remember(ctx, fact: str):
        ctx["memory"].add_fact(fact)
        return "saved"

    @reg.register(
        "forget",
        "Delete saved facts that contain the given keyword, when the user asks you to forget something.",
        {"type": "object", "properties": {"keyword": {"type": "string"}}, "required": ["keyword"]},
    )
    def forget(ctx, keyword: str):
        n = ctx["memory"].remove_facts(keyword)
        return f"removed {n} fact(s)"

    if not timers_enabled:
        return

    @reg.register(
        "set_timer",
        "Start a countdown timer. When it ends, the assistant announces it out loud.",
        {
            "type": "object",
            "properties": {
                "seconds": {"type": "integer", "minimum": 1},
                "label": {"type": "string", "description": "What the timer is for, spoken when it ends"},
            },
            "required": ["seconds"],
        },
    )
    def set_timer(ctx, seconds: int, label: str = "timer"):
        announce = ctx.get("announce")

        def fire():
            if announce:
                announce(f"Your {label} is done.")

        t = threading.Timer(seconds, fire)
        t.daemon = True
        t.start()
        return f"timer set for {seconds} seconds"
