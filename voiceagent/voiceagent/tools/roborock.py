"""Roborock robot vacuums through python-roborock.

The library talks to the vacuum over the local network when it can and falls
back to Roborock's cloud (MQTT) otherwise. The cloud is needed once to log in
and fetch the device keys: run `python -m voiceagent roborock-login`, which
emails you a code. Credentials go to ~/.voiceagent/roborock.json (mode 600) and
device keys, IPs and rooms are cached next to it, so later starts stay local.

python-roborock is async; it runs on its own event loop thread here because
tools are called from plain threads.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from pathlib import Path

from . import ToolRegistry

log = logging.getLogger(__name__)

CREDENTIALS = Path("~/.voiceagent/roborock.json").expanduser()
CACHE = Path("~/.voiceagent/roborock.cache").expanduser()

COMMANDS = {"start": "app_start", "stop": "app_stop", "pause": "app_pause", "dock": "app_charge", "find": "find_me"}


async def _login(email: str, ask_code, password: str | None) -> dict:
    from roborock.web_api import RoborockApiClient

    client = RoborockApiClient(email)
    if password:
        user_data = await client.pass_login(password)
    else:
        await client.request_code_v4()
        user_data = await client.code_login_v4(ask_code())
    return {"email": email, "base_url": await client.base_url, "user_data": user_data.as_dict()}


def login(email: str, password: str | None = None,
          ask_code=lambda: input("Code from the Roborock email: ").strip()) -> None:
    """Email code by default; a password works too for accounts that have one set in the Roborock app."""
    from roborock.exceptions import RoborockException, RoborockTooFrequentCodeRequests

    try:
        creds = asyncio.run(_login(email, ask_code, password))
    except RoborockTooFrequentCodeRequests:
        raise SystemExit("Roborock refused to send another code because too many were requested recently. "
                         "Wait about an hour without requesting codes (the Roborock app counts too), "
                         "or log in with your password: python -m voiceagent roborock-login --password") from None
    except RoborockException as e:
        raise SystemExit(f"Roborock login failed: {e}") from None
    CREDENTIALS.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(CREDENTIALS, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(creds, f)


class Vacuums:
    def __init__(self, creds: dict):
        self.creds = creds
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True, name="roborock").start()
        self.manager = None
        self.started: set[str] = set()

    def call(self, coro, timeout: float = 30):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    async def devices(self) -> list:
        if self.manager is None:
            from roborock.data import UserData
            from roborock.devices.device_manager import UserParams, create_device_manager
            from roborock.devices.file_cache import FileCache

            params = UserParams(username=self.creds["email"], base_url=self.creds.get("base_url"),
                                user_data=UserData.from_dict(self.creds["user_data"]))
            self.manager = await create_device_manager(params, cache=FileCache(CACHE))
        return await self.manager.get_devices()

    async def props(self, device):
        """v1 properties, started once. Newer protocol models (Q7, Q10) aren't handled yet."""
        if device.v1_properties is None:
            raise ValueError(f"{device.name} uses a protocol this tool doesn't support yet")
        if device.duid not in self.started:
            await device.v1_properties.start()
            self.started.add(device.duid)
        return device.v1_properties

    async def pick(self, name: str | None):
        devices = await self.devices()
        if not devices:
            raise ValueError("no Roborock devices on this account")
        if not name or len(devices) == 1:
            return devices[0]
        for d in devices:
            if name.lower() in d.name.lower():
                return d
        raise ValueError(f"unknown vacuum '{name}'. Known: {', '.join(d.name for d in devices)}")

    async def rooms(self, props) -> dict[str, int]:
        await props.rooms.refresh()
        return {r.name: r.segment_id for r in props.rooms.rooms or []}

    async def run(self, action: str, rooms: list[str] | None, device: str | None, repeat: int) -> str:
        d = await self.pick(device)
        props = await self.props(d)
        if action == "status":
            await props.status.refresh()
            s = props.status
            known = await self.rooms(props)
            return (f"{d.name}: {s.state_name or 'unknown state'}, battery {s.battery}%"
                    + (f", error {s.error_code_name}" if s.error_code_name not in (None, "none") else "")
                    + (f". Rooms: {', '.join(known)}" if known else ""))
        if action == "clean_rooms":
            known = await self.rooms(props)
            lower = {n.lower(): seg for n, seg in known.items()}
            wanted = rooms or []
            segments, missing = [], []
            for r in wanted:
                seg = lower.get(r.lower())
                if seg is None:  # partial match: "kitchen area" -> "kitchen"
                    seg = next((v for n, v in lower.items() if r.lower() in n or n in r.lower()), None)
                if seg is None:
                    missing.append(r)
                else:
                    segments.append(seg)
            if missing or not segments:
                return f"error: unknown room(s) {', '.join(missing) or '(none given)'}. Known rooms: {', '.join(known)}"
            await props.command.send("app_segment_clean", [{"segments": segments, "repeat": max(1, repeat)}])
            return f"ok: {d.name} is cleaning {', '.join(wanted)}"
        await props.command.send(COMMANDS[action])
        return f"ok: {d.name} {action}"


def register_roborock_tools(reg: ToolRegistry) -> bool:
    if not CREDENTIALS.exists():
        return False
    try:
        import roborock  # noqa: F401
    except ImportError:
        log.warning("roborock credentials found but python-roborock isn't installed (pip install python-roborock)")
        return False
    vacuums = Vacuums(json.loads(CREDENTIALS.read_text()))

    @reg.register(
        "vacuum",
        "Control the Roborock robot vacuum. 'status' reports state, battery and the room names the vacuum knows. "
        "'clean_rooms' cleans only the given rooms, using the vacuum's room names (these may be in Turkish, "
        "e.g. Mutfak, Salon; call 'status' first if unsure). 'dock' sends it back to charge, "
        "'find' makes it play a sound.",
        {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["start", "stop", "pause", "dock", "status", "find", "clean_rooms"]},
                "rooms": {"type": "array", "items": {"type": "string"}, "description": "for clean_rooms"},
                "repeat": {"type": "integer", "minimum": 1, "maximum": 3, "description": "passes per room"},
                "device": {"type": "string", "description": "only if there are several vacuums"},
            },
            "required": ["action"],
        },
    )
    def vacuum(ctx, action: str, rooms=None, repeat: int = 1, device=None):
        return vacuums.call(vacuums.run(action, rooms, device, repeat))

    return True
