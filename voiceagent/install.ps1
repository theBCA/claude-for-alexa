# One-command setup for the voiceagent brain on Windows (PowerShell).
#   Right-click -> Run with PowerShell, or:  powershell -ExecutionPolicy Bypass -File install.ps1
# Re-running is safe: it keeps an existing config.yaml, venv and credentials.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
Write-Host "voiceagent setup in $(Get-Location)"

# 1. Python 3.11+ ------------------------------------------------------------
function Find-Python {
    foreach ($cmd in @("py -3.13", "py -3.12", "py -3.11", "python")) {
        $parts = $cmd.Split(" ")
        try {
            $v = & $parts[0] $parts[1..($parts.Length-1)] -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
            if ($v -match "^3\.(\d+)$" -and [int]$Matches[1] -ge 11) { return $cmd }
        } catch {}
    }
    return $null
}
$PY = Find-Python
if (-not $PY) {
    Write-Host "Need Python 3.11 or newer. Install it from https://www.python.org/downloads/ (tick 'Add to PATH')."
    Write-Host "or:  winget install Python.Python.3.12"
    exit 1
}
Write-Host "using $PY"

# 2. venv + dependencies -----------------------------------------------------
if (-not (Test-Path ".venv")) { & $PY.Split(" ") -m venv .venv }
$VENVPY = ".\.venv\Scripts\python.exe"
& $VENVPY -m pip install -q --upgrade pip
Write-Host "installing dependencies (a few hundred MB the first time)..."
& $VENVPY -m pip install -q -r requirements.txt

# 3. config.yaml with a fresh token -----------------------------------------
if (-not (Test-Path "config.yaml")) {
    Copy-Item config.example.yaml config.yaml
    $TOKEN = & $VENVPY -c "import secrets;print(secrets.token_hex(24))"
    & $VENVPY -c @"
import re,sys
s=open('config.yaml',encoding='utf-8').read()
s=re.sub(r'token:.*', 'token: '+sys.argv[1], s, count=1)
# Windows has no `say`; host-side speech (run/chat --speak) needs piper, but the brain itself never speaks
s=re.sub(r'engine: say.*', 'engine: piper   # only for host-side speech; the phone speaks on its own', s, count=1)
open('config.yaml','w',encoding='utf-8').write(s)
"@ $TOKEN
    Write-Host "created config.yaml with a random token"
} else {
    Write-Host "keeping existing config.yaml"
}

# 4. credentials + .env ------------------------------------------------------
$HOMEDIR = Join-Path $env:USERPROFILE ".voiceagent"
New-Item -ItemType Directory -Force -Path $HOMEDIR | Out-Null
if ((-not (Test-Path ".env")) -and (Test-Path ".env.example")) {
    Copy-Item .env.example .env
    Write-Host "created .env (fill in the optional secrets)"
}

Write-Host ""
Write-Host "done. Next:"
Write-Host "  1. Pick who pays for Claude in config.yaml (llm.provider)."
Write-Host "     claude_cli: run 'claude' once and log in. anthropic: set ANTHROPIC_API_KEY."
Write-Host "  2. Check everything:   .\.venv\Scripts\python.exe -m voiceagent doctor"
Write-Host "  3. Start the brain:    .\.venv\Scripts\python.exe -m voiceagent serve"
Write-Host "     Keep it running:    .\.venv\Scripts\python.exe -m voiceagent install-service"
Write-Host ""
Write-Host "Allow it through the firewall when Windows asks, so your phone can connect."
