"""Lepro lights (the Lepro / LampUX app) through Lepro's cloud.

Lepro bulbs expose nothing on the local network, so this does what the app does:
log in over HTTPS, download the account's MQTT certificate, and publish commands
over MQTT with mutual TLS. The TLS private key is the one built into the Lepro
app; it is not in this repo and must be at ~/.voiceagent/lepro_client_key.pem
(extracted from your own installed app).

Setup once:
  1. Put your Lepro login in voiceagent/.env (gitignored):
       LEPRO_EMAIL=you@example.com
       LEPRO_PASSWORD=...
       # LEPRO_REGION=eu        (eu, na, us or fe; default eu)
  2. python -m voiceagent lepro-login

Protocol facts this is written from (endpoints, topics, field meanings):
  POST /user/login             -> data.token
  GET  /user/profile           -> data.uid, data.mqtt {host, port, root, cert}
  GET  /family/list/timestamp/{ts}              -> data.list[].fid
  GET  /v3/device/list/fid/{fid}/timestamp/{ts} -> data.list[] (did, name)
  MQTT publish le/{did}/prp/set  {"id": n, "t": ts, "d": {...}}
    d1 on/off (1/0), d2 mode (0 white, 1 colour), d3 brightness 0-1000,
    d4 colour temperature 0-1000 (0 = 2700 K .. 1000 = 6500 K),
    d5 colour as "HHHHSSSSVVVV" (hue 0-359, saturation 0-1000, value 0-1000).
"""
from __future__ import annotations

import colorsys
import hashlib
import json
import logging
import os
import secrets
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import ToolRegistry

log = logging.getLogger(__name__)

HOME = Path("~/.voiceagent").expanduser()
CREDENTIALS = HOME / "lepro.json"
KEY = HOME / "lepro_client_key.pem"
ROOT_CA, CLIENT_CERT = HOME / "lepro_root.pem", HOME / "lepro_cert.pem"
APP_VERSION = "1.0.9.269"


class LeproError(Exception):
    pass


def load_env(path: Path) -> dict:
    """Tiny KEY=VALUE reader so we don't add a dependency just for .env."""
    out = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def _fake_mac(email: str) -> str:
    """The app sends a stable per-account MAC derived from the email; the device only needs it to be constant."""
    h = hashlib.md5(email.encode()).hexdigest()
    return "02:" + ":".join(h[i:i + 2] for i in range(0, 10, 2))


def _headers(token: str | None = None) -> dict:
    h = {"App-Version": APP_VERSION, "Platform": "2", "Language": "en", "Slanguage": "en", "GMT": "+0",
         "Timestamp": str(int(time.time())), "Device-Model": "voiceagent", "Device-System": "Android 15",
         "Screen-Size": "1080*2340", "User-Agent": f"LE/{APP_VERSION} (Android; voiceagent)",
         "Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


class LeproCloud:
    def __init__(self, email: str, region: str = "eu"):
        self.email = email
        self.base = f"https://api-{region}-iot.lepro.com"
        self.token: str | None = None

    def request(self, method: str, path: str, body: dict | None = None, want_bytes: bool = False):
        url = path if path.startswith("http") else self.base + path
        req = urllib.request.Request(url, method=method, headers=_headers(self.token),
                                     data=None if body is None else json.dumps(body).encode())
        try:
            with urllib.request.urlopen(req, timeout=12) as r:
                blob = r.read()
        except urllib.error.HTTPError as e:
            raise LeproError(f"Lepro HTTP {e.code} on {path}") from None
        except urllib.error.URLError as e:
            raise LeproError(f"cannot reach Lepro ({e.reason}); check LEPRO_REGION") from None
        if want_bytes:
            return blob
        data = json.loads(blob)
        if data.get("code") not in (0, 200, None):
            raise LeproError(f"Lepro said {data.get('code')}: {data.get('msg')}")
        return data.get("data", data)

    def login(self, password: str) -> None:
        body = {"platform": "2", "account": self.email, "password": password, "mac": _fake_mac(self.email),
                "timestamp": str(int(time.time())), "language": "en", "fcmToken": secrets.token_hex(16)}
        data = self.request("POST", "/user/login", body)
        self.token = data.get("token") or data.get("accessToken")
        if not self.token:
            raise LeproError("login returned no token; check the email and password")

    def profile(self) -> dict:
        return self.request("GET", "/user/profile")

    def families(self) -> list:
        return (self.request("GET", f"/family/list/timestamp/{int(time.time())}") or {}).get("list", [])

    def devices(self, fid) -> list:
        return (self.request("GET", f"/v3/device/list/fid/{fid}/timestamp/{int(time.time())}") or {}).get("list", [])


def _do_login(env: dict) -> dict:
    """Log in, download the account's certificates, and return the data cached in lepro.json."""
    email, password = env.get("LEPRO_EMAIL"), env.get("LEPRO_PASSWORD")
    if not (email and password):
        raise LeproError("set LEPRO_EMAIL and LEPRO_PASSWORD in voiceagent/.env")
    if not KEY.exists():
        raise LeproError(f"missing {KEY} (the app's TLS key, extracted from your installed Lepro app)")
    cloud = LeproCloud(email, env.get("LEPRO_REGION", "eu"))
    cloud.login(password)
    prof = cloud.profile()
    mqtt = prof.get("mqtt") or {}
    if mqtt.get("root"):
        _write_private(ROOT_CA, cloud.request("GET", mqtt["root"], want_bytes=True))
    if mqtt.get("cert"):
        _write_private(CLIENT_CERT, cloud.request("GET", mqtt["cert"], want_bytes=True))
    devices = {}
    for fam in cloud.families():
        for d in cloud.devices(fam.get("fid")):
            did = d.get("did") or d.get("deviceId")
            name = d.get("name") or d.get("deviceName") or did
            if did:
                devices[name] = did
    cache = {"email": email, "region": env.get("LEPRO_REGION", "eu"), "uid": prof.get("uid"),
             "mqtt": {"host": mqtt.get("host"), "port": int(mqtt.get("port", 8883))}, "devices": devices}
    fd = os.open(CREDENTIALS, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cache, f)
    return cache


def login(env_path: Path) -> dict:
    cache = _do_login(load_env(env_path))
    print(f"Connected to Lepro. Lights found: {', '.join(cache['devices']) or 'none'}")
    return cache


def hex_to_hsv_field(hex_color: str) -> str:
    v = hex_color.strip().lstrip("#")
    if len(v) != 6:
        raise ValueError(f"color must be #RRGGBB, got {hex_color}")
    r, g, b = (int(v[i:i + 2], 16) / 255 for i in (0, 2, 4))
    h, s, val = colorsys.rgb_to_hsv(r, g, b)
    return f"{round(h * 359):04X}{round(s * 1000):04X}{round(val * 1000):04X}"


class Lepro:
    """Keeps one MQTT connection to Lepro's broker and publishes device commands."""

    def __init__(self, cache: dict, env_path: Path):
        self.cache, self.env_path = cache, env_path
        self.devices: dict[str, str] = cache.get("devices", {})
        self._client = None
        self._id = 0

    def _connect(self):
        import paho.mqtt.client as mqtt

        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="lepro-app-" + secrets.token_hex(16))
        c.tls_set(ca_certs=str(ROOT_CA), certfile=str(CLIENT_CERT), keyfile=str(KEY))
        host, port = self.cache["mqtt"]["host"], self.cache["mqtt"]["port"]
        if not host:
            raise LeproError("no MQTT host cached; run lepro-login again")
        c.connect(host, port, keepalive=45)
        c.loop_start()
        return c

    def client(self):
        if self._client is None or not self._client.is_connected():
            self._client = self._connect()
        return self._client

    def refresh_login(self) -> None:
        """Certificates and token expire; re-login with the .env password and reconnect."""
        self.cache = _do_login(load_env(self.env_path))
        self.devices = self.cache.get("devices", {})
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass
            self._client = None

    def send(self, did: str, fields: dict) -> None:
        self._id += 1
        payload = json.dumps({"id": self._id, "t": int(time.time()), "d": fields})
        info = self.client().publish(f"le/{did}/prp/set", payload, qos=1)
        info.wait_for_publish(timeout=5)

    def targets(self, target: str) -> list[str]:
        if target.lower() == "all":
            return list(self.devices.values())
        for name, did in self.devices.items():
            if target.lower() in name.lower():
                return [did]
        raise LeproError(f"unknown light '{target}'. Known: {', '.join(self.devices) or 'none'}")

    def apply(self, target: str, action: str, brightness=None, color=None, kelvin=None) -> str:
        if action == "on":
            fields = {"d1": 1}
        elif action == "off":
            fields = {"d1": 0}
        elif action == "brightness":
            fields = {"d1": 1, "d2": 0, "d3": max(10, min(1000, int(brightness) * 10))}
        elif action == "color":
            fields = {"d1": 1, "d2": 1, "d5": hex_to_hsv_field(color)}
        elif action == "temperature":
            k = max(2700, min(6500, int(kelvin)))
            fields = {"d1": 1, "d2": 0, "d4": round((k - 2700) / (6500 - 2700) * 1000)}
        else:
            raise LeproError(f"unknown action {action}")
        dids = self.targets(target)
        for did in dids:
            self.send(did, fields)
        return f"ok: {action} on {len(dids)} Lepro light(s)"


def register_lepro_tools(reg: ToolRegistry, env_path: Path) -> bool:
    if not CREDENTIALS.exists():
        return False
    lepro = Lepro(json.loads(CREDENTIALS.read_text()), env_path)
    if not lepro.devices:
        log.warning("lepro: no lights cached; run lepro-login again")
        return False
    names = ", ".join(lepro.devices)

    @reg.register(
        "lepro_lights",
        f"Control the Lepro lights (via Lepro's cloud). Known Lepro lights: {names}. Use target 'all' for every "
        "Lepro light. For a colour, turn the user's words into a hex colour (warm orange -> #FF8C32). For white, "
        "use action 'temperature' with kelvin (2700 warm, 4000 neutral, 6500 cool). "
        "These are separate from any Govee lights.",
        {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": f"a Lepro light name or 'all' ({names})"},
                "action": {"type": "string", "enum": ["on", "off", "brightness", "color", "temperature"]},
                "brightness": {"type": "integer", "minimum": 1, "maximum": 100},
                "color": {"type": "string", "description": "#RRGGBB"},
                "kelvin": {"type": "integer", "minimum": 2700, "maximum": 6500},
            },
            "required": ["target", "action"],
        },
    )
    def lepro_lights(ctx, target: str, action: str, brightness=None, color=None, kelvin=None):
        try:
            return lepro.apply(target, action, brightness, color, kelvin)
        except LeproError as e:
            # token or certificate may have expired; try one re-login before giving up
            if action in ("on", "off", "brightness", "color", "temperature"):
                try:
                    lepro.refresh_login()
                    return lepro.apply(target, action, brightness, color, kelvin)
                except LeproError as e2:
                    return f"error: {e2}"
            return f"error: {e}"

    return True
