"""Tool registry. Each tool is a plain Python function plus a JSON schema.

Tools receive a shared `ctx` dict (memory, announce callback, config) so they
stay decoupled from the audio pipeline. Adding a capability means adding one
file here; the agent picks it up automatically.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    fn: Callable[..., Any]


class ToolRegistry:
    def __init__(self) -> None:
        self.tools: dict[str, Tool] = {}
        self.ctx: dict[str, Any] = {}

    def register(self, name: str, description: str, input_schema: dict):
        def deco(fn: Callable[..., Any]):
            self.tools[name] = Tool(name, description, input_schema, fn)
            return fn
        return deco

    def schemas(self) -> list[dict]:
        return [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in self.tools.values()
        ]

    def run(self, name: str, args: dict) -> str:
        tool = self.tools.get(name)
        if tool is None:
            return f"error: unknown tool {name}"
        try:
            result = tool.fn(self.ctx, **(args or {}))
        except Exception as e:  # tools must never crash the assistant
            log.exception("tool %s failed", name)
            return f"error: {e}"
        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False)
