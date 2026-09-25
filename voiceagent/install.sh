#!/usr/bin/env bash
# One-command setup for the voiceagent brain on macOS or Linux.
#   ./install.sh
# Re-running is safe: it keeps an existing config.yaml, venv and credentials.
set -euo pipefail
cd "$(dirname "$0")"

echo "voiceagent setup in $(pwd)"

# 1. Python 3.11+ ------------------------------------------------------------
PY=""
for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1; then
        v=$("$c" -c 'import sys;print("%d.%d"%sys.version_info[:2])')
        major=${v%.*}; minor=${v#*.}
        if [ "$major" -eq 3 ] && [ "$minor" -ge 11 ]; then PY="$c"; break; fi
    fi
done
if [ -z "$PY" ]; then
    echo "Need Python 3.11 or newer. Install it:"
    echo "  macOS:  brew install python@3.11"
    echo "  Debian/Ubuntu:  sudo apt install python3.11 python3.11-venv"
    exit 1
fi
echo "using $PY ($($PY --version))"

# 2. venv + dependencies -----------------------------------------------------
[ -d .venv ] || "$PY" -m venv .venv
./.venv/bin/pip install -q --upgrade pip
echo "installing dependencies (a few hundred MB the first time)..."
./.venv/bin/pip install -q -r requirements.txt

# 3. config.yaml with a fresh token -----------------------------------------
if [ ! -f config.yaml ]; then
    cp config.example.yaml config.yaml
    TOKEN=$(./.venv/bin/python -c 'import secrets;print(secrets.token_hex(24))')
    ./.venv/bin/python - "$TOKEN" <<'PY'
import re, sys
p = "config.yaml"; s = open(p).read()
s = re.sub(r"token:.*", f"token: {sys.argv[1]}", s, count=1)
open(p, "w").write(s)
PY
    # macOS speaks with `say`; Linux needs piper, so start in text-friendly mode there
    if [ "$(uname)" != "Darwin" ]; then
        ./.venv/bin/python - <<'PY'
import re
p = "config.yaml"; s = open(p).read()
s = re.sub(r"engine: say.*", "engine: piper   # set piper_models per language, see README", s, count=1)
open(p, "w").write(s)
PY
    fi
    echo "created config.yaml with a random token"
else
    echo "keeping existing config.yaml"
fi

# 4. credentials + .env ------------------------------------------------------
mkdir -p "$HOME/.voiceagent"; chmod 700 "$HOME/.voiceagent"
[ -f .env ] || { [ -f .env.example ] && cp .env.example .env && echo "created .env (fill in the optional secrets)"; }

echo
echo "done. Next:"
echo "  1. Pick who pays for Claude in config.yaml (llm.provider)."
echo "     claude_cli: run 'claude' once and log in. anthropic: export ANTHROPIC_API_KEY."
echo "  2. Check everything:   ./.venv/bin/python -m voiceagent doctor"
echo "  3. Start the brain:    ./.venv/bin/python -m voiceagent serve"
echo "     Keep it running:    ./.venv/bin/python -m voiceagent install-service"
