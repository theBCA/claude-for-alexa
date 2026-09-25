#!/usr/bin/env python3
"""Stands in for the real `claude` CLI in tests: a stream-json session that stays up across turns."""
import json, os, subprocess, sys

args = sys.argv[1:]
assert "-p" in args and args[args.index("--input-format") + 1] == "stream-json"
assert "ANTHROPIC_API_KEY" not in os.environ, "API key leaked to CLI"
system = args[args.index("--system-prompt") + 1]
cfg = json.load(open(args[args.index("--mcp-config") + 1]))["mcpServers"]["voiceagent"]

# act like the CLI: spawn the MCP server once per session and use it
srv = subprocess.Popen([cfg["command"], *cfg["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                       text=True, env={**os.environ, **cfg["env"]})
def rpc(i, method, params=None):
    srv.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}) + "\n")
    srv.stdin.flush()
    return json.loads(srv.stdout.readline())
assert rpc(1, "initialize", {"protocolVersion": "2025-06-18"})["result"]["serverInfo"]["name"] == "voiceagent"
names = [t["name"] for t in rpc(2, "tools/list")["result"]["tools"]]

def ev(e): print(json.dumps({"type": "stream_event", "event": e}), flush=True)

for n, line in enumerate(sys.stdin, start=3):
    prompt = json.loads(line)["message"]["content"]
    res = rpc(n, "tools/call", {"name": "remember", "arguments": {"fact": "prompt-len=" + str(len(prompt))}})
    ev({"type": "message_start"})
    for chunk in ["Done, ", "I saved that. ", "Tools: " + ",".join(sorted(names)) + ". ",
                  "Turkish=" + str("Reply in Turkish" in system + prompt) + ". ",
                  "History=" + str("Conversation so far" in prompt) + ". ",
                  "Pid=" + str(os.getpid()) + "."]:
        ev({"type": "content_block_delta", "delta": {"type": "text_delta", "text": chunk}})
    ev({"type": "message_stop"})
    print(json.dumps({"type": "result", "is_error": res["result"]["isError"], "result": "ok"}), flush=True)

srv.stdin.close(); srv.wait()
