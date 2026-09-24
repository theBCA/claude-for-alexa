"""Govee lights over the local network (Govee LAN API).

No cloud round-trip, so commands land in ~50 ms and keep working if Govee's
servers are down. Enable "LAN Control" per device in the Govee Home app first.

Protocol: scan request to multicast 239.255.255.250:4001, devices reply to
port 4002, commands go to <device ip>:4003 as JSON over UDP.
"""
from __future__ import annotations

import json
import socket
import time

from . import ToolRegistry

MULTICAST = ("239.255.255.250", 4001)
REPLY_PORT = 4002
CONTROL_PORT = 4003


def build_command(cmd: str, data: dict) -> bytes:
    return json.dumps({"msg": {"cmd": cmd, "data": data}}).encode()


def hex_to_rgb(value: str) -> dict:
    v = value.strip().lstrip("#")
    if len(v) != 6:
        raise ValueError(f"color must be #RRGGBB, got {value}")
    return {"r": int(v[0:2], 16), "g": int(v[2:4], 16), "b": int(v[4:6], 16)}


def discover(timeout: float = 2.0) -> list[dict]:
    """Return [{ip, sku, device}] for Govee devices with LAN control enabled."""
    found: dict[str, dict] = {}
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    rx.bind(("", REPLY_PORT))
    rx.settimeout(0.3)
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    try:
        tx.sendto(build_command("scan", {"account_topic": "reserve"}), MULTICAST)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw, _ = rx.recvfrom(4096)
            except socket.timeout:
                continue
            try:
                data = json.loads(raw)["msg"]["data"]
                found[data["ip"]] = {"ip": data["ip"], "sku": data.get("sku"), "device": data.get("device")}
            except (ValueError, KeyError):
                continue
    finally:
        rx.close()
        tx.close()
    return list(found.values())


def send(ip: str, cmd: str, data: dict) -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.sendto(build_command(cmd, data), (ip, CONTROL_PORT))
    finally:
        s.close()


def apply_action(ip: str, action: str, brightness=None, color=None, kelvin=None) -> None:
    if action == "on":
        send(ip, "turn", {"value": 1})
    elif action == "off":
        send(ip, "turn", {"value": 0})
    elif action == "brightness":
        send(ip, "brightness", {"value": max(1, min(100, int(brightness)))})
    elif action == "color":
        send(ip, "colorwc", {"color": hex_to_rgb(color), "colorTemInKelvin": 0})
    elif action == "temperature":
        send(ip, "colorwc", {"color": {"r": 0, "g": 0, "b": 0}, "colorTemInKelvin": max(2000, min(9000, int(kelvin)))})
    else:
        raise ValueError(f"unknown action {action}")


def register_govee_tools(reg: ToolRegistry, devices: dict[str, str], auto_discover: bool = True) -> dict[str, str]:
    devices = dict(devices or {})
    if not devices and auto_discover:
        for i, d in enumerate(discover(timeout=1.5), 1):
            devices[f"{(d.get('sku') or 'light').lower()}-{i}"] = d["ip"]
    if not devices:
        return devices

    names = ", ".join(devices)

    @reg.register(
        "control_lights",
        f"Control Govee lights. Known lights: {names}. Use target 'all' for every light. "
        "For color, convert the user's words into a hex color (warm orange -> #FF8C32). "
        "For white light, use action 'temperature' with kelvin (2700 warm, 4000 neutral, 6500 cool). "
        "You can call this several times in a row for multi-step requests.",
        {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "A light name or 'all'"},
                "action": {"type": "string", "enum": ["on", "off", "brightness", "color", "temperature"]},
                "brightness": {"type": "integer", "minimum": 1, "maximum": 100},
                "color": {"type": "string", "description": "#RRGGBB"},
                "kelvin": {"type": "integer", "minimum": 2000, "maximum": 9000},
            },
            "required": ["target", "action"],
        },
    )
    def control_lights(ctx, target: str, action: str, brightness=None, color=None, kelvin=None):
        if target.lower() == "all":
            targets = list(devices.values())
        elif target in devices:
            targets = [devices[target]]
        else:
            return f"error: unknown light '{target}'. Known: {names}"
        for ip in targets:
            apply_action(ip, action, brightness, color, kelvin)
        return f"ok: {action} on {len(targets)} light(s)"

    return devices
