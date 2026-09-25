"""Moving machines and running as a service.

  backup <file.tgz>    bundle credentials + config so a new machine is one restore away
  restore <file.tgz>   unpack a bundle on the new machine
  install-service      auto-start the brain on login (launchd on macOS, systemd --user on Linux)

The backup holds real secrets (Claude login, API tokens, the Lepro key). It is
written 0600; keep it somewhere safe and delete it after restoring.
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys
import tarfile
from pathlib import Path

HOME = Path("~/.voiceagent").expanduser()
PKG_DIR = Path(__file__).resolve().parent
PROJECT = PKG_DIR.parent
# Everything that identifies this install. Paths are stored relative to these two roots.
CONFIG_FILES = ["config.yaml", ".env"]
# Inside ~/.voiceagent, skip logs, the memory DB and Claude's session/cache noise.
# From claude-personal we keep only the login files (.claude.json / .credentials.json).
SKIP_NAMES = {"claude-cli.log", "brain.log", "memory.db", ".last-cleanup"}
CLAUDE_KEEP = {".claude.json", ".credentials.json"}


def _include(rel: Path) -> bool:
    if rel.name in SKIP_NAMES:
        return False
    parts = rel.parts
    if parts and parts[0].startswith("claude"):  # a personal Claude config dir
        return len(parts) == 2 and rel.name in CLAUDE_KEEP
    return True


def backup(dest: str) -> None:
    out = Path(dest).expanduser()
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as tar:
        for name in CONFIG_FILES:
            p = PROJECT / name
            if p.exists():
                tar.add(p, arcname=f"config/{name}")
        if HOME.exists():
            for p in sorted(HOME.rglob("*")):
                if p.is_file() and _include(p.relative_to(HOME)):
                    tar.add(p, arcname=f"home/{p.relative_to(HOME)}")
    print(f"wrote {out} ({out.stat().st_size // 1024} KB). Copy it to the new machine, then: "
          f"python -m voiceagent restore {out.name}")
    if platform.system() == "Darwin":
        print("Note: on macOS the Claude subscription login lives in the keychain, not in this file, so on the "
              "new machine run 'claude auth login' (or with CLAUDE_CONFIG_DIR) once. Spotify, Google, Roborock "
              "and Lepro logins are included.")


def restore(src: str) -> None:
    path = Path(src).expanduser()
    if not path.exists():
        raise SystemExit(f"no such file: {path}")
    HOME.mkdir(parents=True, exist_ok=True)
    os.chmod(HOME, 0o700)
    with tarfile.open(path, "r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            if member.name.startswith("config/"):
                target = PROJECT / member.name[len("config/"):]
            elif member.name.startswith("home/"):
                target = HOME / member.name[len("home/"):]
            else:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(member) as fsrc:  # type: ignore[union-attr]
                data = fsrc.read()
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
    print("restored config and credentials. Now: python -m voiceagent doctor")


LAUNCHD_LABEL = "com.voiceagent.brain"


def install_service() -> None:
    python = Path(sys.executable)
    if platform.system() == "Darwin":
        _install_launchd(python)
    elif platform.system() == "Linux":
        _install_systemd(python)
    elif platform.system() == "Windows":
        _install_task_scheduler(python)
    else:
        raise SystemExit("install-service supports macOS, Linux and Windows")


# Runs at logon, restarts up to 999 times a minute apart, no time limit.
_TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{user}</UserId></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{user}</UserId><LogonType>InteractiveToken</LogonType></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author"><Exec><Command>{cmd}</Command><WorkingDirectory>{workdir}</WorkingDirectory></Exec></Actions>
</Task>"""


def _install_task_scheduler(python: Path) -> None:
    import getpass

    # pythonw.exe runs without a console window; fall back to python.exe
    pyw = python.with_name("pythonw.exe")
    exe = pyw if pyw.exists() else python
    task = "VoiceagentBrain"
    log = HOME / "brain.log"
    # a .cmd wrapper keeps logs and gives Task Scheduler one stable command to run
    wrapper = HOME / "run-brain.cmd"
    wrapper.write_text(f'@echo off\r\ncd /d "{PROJECT}"\r\n"{exe}" -m voiceagent serve >> "{log}" 2>&1\r\n')
    xml = HOME / "voiceagent-task.xml"
    xml.write_text(_TASK_XML.format(user=getpass.getuser(), cmd=wrapper, workdir=PROJECT), encoding="utf-16")
    subprocess.run(["schtasks", "/Create", "/TN", task, "/XML", str(xml), "/F"], check=True)
    subprocess.run(["schtasks", "/Run", "/TN", task], check=True)
    print(f"installed Task Scheduler job '{task}'.\nThe brain starts at logon and restarts if it crashes.")
    print(f"  logs:   type {log}")
    print(f"  stop:   schtasks /End /TN {task}")
    print(f"  remove: schtasks /Delete /TN {task} /F")


def _install_launchd(python: Path) -> None:
    plist = Path("~/Library/LaunchAgents").expanduser() / f"{LAUNCHD_LABEL}.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    # caffeinate keeps an Apple Silicon Mac awake; -i only, so the screen can still sleep
    caffeinate = "/usr/bin/caffeinate"
    args = [caffeinate, "-i", str(python), "-m", "voiceagent", "serve"] if Path(caffeinate).exists() \
        else [str(python), "-m", "voiceagent", "serve"]
    items = "".join(f"\n        <string>{a}</string>" for a in args)
    plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key><array>{items}
    </array>
    <key>WorkingDirectory</key><string>{PROJECT}</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>{HOME}/brain.log</string>
    <key>StandardErrorPath</key><string>{HOME}/brain.log</string>
</dict>
</plist>
""")
    subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
    subprocess.run(["launchctl", "load", str(plist)], check=True)
    print(f"installed {plist}\nThe brain now starts at login and restarts if it crashes.")
    print(f"  logs:   tail -f {HOME}/brain.log")
    print(f"  stop:   launchctl unload {plist}")


def _install_systemd(python: Path) -> None:
    unit_dir = Path("~/.config/systemd/user").expanduser()
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit = unit_dir / "voiceagent.service"
    unit.write_text(f"""[Unit]
Description=voiceagent brain
After=network-online.target

[Service]
ExecStart={python} -m voiceagent serve
WorkingDirectory={PROJECT}
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
""")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", "voiceagent"], check=True)
    print(f"installed {unit}\nThe brain now starts on login and restarts if it crashes.")
    print("  logs:   journalctl --user -u voiceagent -f")
    print("  stop:   systemctl --user stop voiceagent")
    print("If it should run without you logged in:  sudo loginctl enable-linger $USER")
