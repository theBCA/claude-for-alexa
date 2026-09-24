#!/usr/bin/env python3
"""Stands in for the real `claude` CLI in tests."""
import json, os, subprocess, sys

args = sys.argv[1:]
assert "-p" in args and "stream-json" in args
assert "ANTHROPIC_API_KEY" not in os.environ, "API key leaked to CLI"
prompt = sys.stdin.read()
system = args[args.index("--system-prompt") + 1]
cfg = json.load(open(args[args.index("--mcp-config") + 1]))["mcpServers"]["voiceagent"]

# act like the CLI: spawn the MCP server and use it
srv = subprocess.Popen([cfg["command"], *cfg["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                       text=True, env={**os.environ, **cfg["env"]})
def rpc(i, method, params=None):
    srv.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}) + "\n")
    srv.stdin.flush()
    return json.loads(srv.stdout.readline())
assert rpc(1, "initialize", {"protocolVersion": "2025-06-18"})["result"]["serverInfo"]["name"] == "voiceagent"
names = [t["name"] for t in rpc(2, "tools/list")["result"]["tools"]]
res = rpc(3, "tools/call", {"name": "remember", "arguments": {"fact": "prompt-len=" + str(len(prompt))}})
srv.stdin.close(); srv.wait()

def ev(e): print(json.dumps({"type": "stream_event", "event": e}), flush=True)
ev({"type": "message_start"})
for chunk in ["Done, ", "I saved that. ", "Tools: " + ",".join(sorted(names)) + ". ",
              "Turkish=" + str("Reply in Turkish" in system) + "."]:
    ev({"type": "content_block_delta", "delta": {"type": "text_delta", "text": chunk}})
ev({"type": "message_stop"})
print(json.dumps({"type": "result", "is_error": res["result"]["isError"], "result": "ok"}))
