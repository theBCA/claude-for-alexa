"""Minimal MCP server (stdio, JSON-RPC 2.0) that the claude CLI launches.

It holds no state and runs no tools itself: it lists the tools the relay exposes
and forwards each call to it. Standard library only, so it starts fast.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

URL = os.environ.get("VOICEAGENT_TOOLS_URL", "")
TOKEN = os.environ.get("VOICEAGENT_TOOLS_TOKEN", "")
DEFAULT_PROTOCOL = "2025-06-18"


def _http(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(URL + path, data=data, method=method,
                                 headers={"X-Token": TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def handle(msg: dict) -> dict | None:
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:  # notification, e.g. notifications/initialized
        return None
    try:
        if method == "initialize":
            result = {
                "protocolVersion": msg.get("params", {}).get("protocolVersion", DEFAULT_PROTOCOL),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "voiceagent", "version": "0.2"},
            }
        elif method == "tools/list":
            result = {"tools": [
                {"name": t["name"], "description": t["description"], "inputSchema": t["input_schema"]}
                for t in _http("GET", "/tools")
            ]}
        elif method == "tools/call":
            p = msg.get("params", {})
            out = _http("POST", "/call", {"name": p.get("name"), "arguments": p.get("arguments") or {}})
            result = {"content": [{"type": "text", "text": out["result"]}], "isError": out["is_error"]}
        elif method == "ping":
            result = {}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}
    except Exception as e:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": str(e)}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        reply = handle(msg)
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
