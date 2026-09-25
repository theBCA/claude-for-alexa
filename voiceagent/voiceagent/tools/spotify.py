"""Spotify through the official Web API: search, play on any Spotify Connect device, pause, skip, volume.

Setup once:
  1. Create an app at https://developer.spotify.com/dashboard (Web API), and add the
     redirect URI http://127.0.0.1:8888/callback. Copy its Client ID.
  2. python -m voiceagent spotify-login   (opens the browser; PKCE, so no client secret)
Tokens go to ~/.voiceagent/spotify.json (mode 600) and refresh themselves.
Since February 2026 development-mode apps only work while the app owner has
Spotify Premium, and search returns at most 10 results.

Song understanding is the model's job: the tool description asks it to turn
"Tarkan'ın Şımarık şarkısını aç" or "spiel was von Rammstein" into a search
query with the title and artist as they appear on Spotify.

Ducking: while someone talks to the assistant, whatever Spotify is playing is
turned down to tools.spotify.duck_volume and restored when the conversation ends.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from . import ToolRegistry

log = logging.getLogger(__name__)

CREDENTIALS = Path("~/.voiceagent/spotify.json").expanduser()
REDIRECT = "http://127.0.0.1:8888/callback"
SCOPES = ("user-read-playback-state user-modify-playback-state user-read-currently-playing "
          "playlist-read-private user-library-read user-library-modify")
API = "https://api.spotify.com/v1"
ACCOUNTS = "https://accounts.spotify.com"


class SpotifyError(Exception):
    pass


def _save(creds: dict) -> None:
    CREDENTIALS.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(CREDENTIALS, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(creds, f)


def _token_request(form: dict) -> dict:
    req = urllib.request.Request(f"{ACCOUNTS}/api/token", data=urllib.parse.urlencode(form).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise SpotifyError(f"token request failed: {e.read().decode(errors='replace')}") from None


def login(client_id: str, open_browser=True) -> None:
    """Authorization code flow with PKCE, catching the redirect on a one-shot local server."""
    import webbrowser

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    url = f"{ACCOUNTS}/authorize?" + urllib.parse.urlencode({
        "client_id": client_id, "response_type": "code", "redirect_uri": REDIRECT, "scope": SCOPES,
        "code_challenge_method": "S256", "code_challenge": challenge, "state": state})
    result: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            ok = q.get("state", [""])[0] == state and "code" in q
            result.update(code=q.get("code", [None])[0] if ok else None, error=q.get("error", [None])[0])
            body = ("Spotify is connected. You can close this tab." if ok
                    else "Spotify login failed, check the terminal.").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 8888), Handler)
    print(f"Opening Spotify in the browser. If it doesn't open, visit:\n{url}\n")
    if open_browser:
        webbrowser.open(url)
    while "code" not in result:
        server.handle_request()
    server.server_close()
    if not result.get("code"):
        raise SystemExit(f"Spotify login failed: {result.get('error') or 'state mismatch'}")
    tok = _token_request({"grant_type": "authorization_code", "code": result["code"], "redirect_uri": REDIRECT,
                          "client_id": client_id, "code_verifier": verifier})
    _save({"client_id": client_id, "refresh_token": tok["refresh_token"], "access_token": tok["access_token"],
           "expires_at": time.time() + tok.get("expires_in", 3600) - 60})


class Spotify:
    def __init__(self, creds: dict, default_device: str | None = None, save=_save):
        self.creds, self.default_device, self._persist = creds, default_device, save
        self._lock = threading.Lock()
        self._ducked: tuple[str, int] | None = None  # (device id, volume to restore)

    # ------------------------------------------------------------- http
    def _token(self) -> str:
        with self._lock:
            if time.time() >= self.creds.get("expires_at", 0):
                tok = _token_request({"grant_type": "refresh_token", "refresh_token": self.creds["refresh_token"],
                                      "client_id": self.creds["client_id"]})
                self.creds.update(access_token=tok["access_token"],
                                  expires_at=time.time() + tok.get("expires_in", 3600) - 60)
                if tok.get("refresh_token"):
                    self.creds["refresh_token"] = tok["refresh_token"]
                self._persist(self.creds)
            return self.creds["access_token"]

    def api(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {self._token()}",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read()).get("error", {})
            except ValueError:
                err = {}
            reason = err.get("reason") or err.get("message") or e.reason
            if reason == "PREMIUM_REQUIRED":
                raise SpotifyError("Spotify Premium is needed to control playback") from None
            if reason == "NO_ACTIVE_DEVICE":
                raise SpotifyError("no Spotify device is active") from None
            raise SpotifyError(f"Spotify said {e.code}: {reason}") from None

    # ---------------------------------------------------------- helpers
    def devices(self) -> list[dict]:
        return (self.api("GET", "/me/player/devices") or {}).get("devices", [])

    def pick_device(self, name: str | None) -> dict:
        devices = self.devices()
        if not devices:
            raise SpotifyError("no Spotify device is online. Open Spotify on the phone or another speaker first")
        for wanted in (name, self.default_device):
            if wanted:
                for d in devices:
                    if wanted.lower() in d["name"].lower():
                        return d
                if wanted == name:
                    raise SpotifyError(f"no device called '{name}'. Online: {', '.join(d['name'] for d in devices)}")
        return next((d for d in devices if d.get("is_active")), devices[0])

    def search(self, query: str, kind: str, limit: int = 5) -> list[dict]:
        res = self.api("GET", "/search", {"q": query, "type": kind, "limit": limit, "market": "from_token"})
        items = [i for i in (res or {}).get(f"{kind}s", {}).get("items", []) if i]
        out = []
        for i in items:
            by = ", ".join(a["name"] for a in i.get("artists", [])) or (i.get("owner") or {}).get("display_name", "")
            out.append({"uri": i["uri"], "name": i["name"], "by": by, "type": kind})
        return out

    @staticmethod
    def describe(item: dict) -> str:
        return f"{item['name']}" + (f" by {item['by']}" if item.get("by") else "")

    # ---------------------------------------------------------- actions
    def play(self, query=None, kind="track", uri=None, device=None, shuffle=None) -> str:
        d = self.pick_device(device)
        if uri:
            item = {"uri": uri, "name": uri, "type": uri.split(":")[1]}
        elif query:
            found = self.search(query, kind, limit=1)
            if not found:
                return f"error: nothing found on Spotify for '{query}' ({kind})"
            item = found[0]
        else:
            self.api("PUT", "/me/player/play", {"device_id": d["id"]})
            return f"ok: resumed on {d['name']}"
        body = {"uris": [item["uri"]]} if item["type"] == "track" else {"context_uri": item["uri"]}
        if shuffle is not None:
            self.api("PUT", "/me/player/shuffle", {"state": str(bool(shuffle)).lower(), "device_id": d["id"]})
        self.api("PUT", "/me/player/play", {"device_id": d["id"]}, body)
        return f"ok: playing {self.describe(item)} on {d['name']}"

    def play_liked(self, device=None) -> str:
        d = self.pick_device(device)
        tracks = (self.api("GET", "/me/tracks", {"limit": 50}) or {}).get("items", [])
        uris = [t["track"]["uri"] for t in tracks if t.get("track")]
        if not uris:
            return "error: no liked songs"
        self.api("PUT", "/me/player/shuffle", {"state": "true", "device_id": d["id"]})
        self.api("PUT", "/me/player/play", {"device_id": d["id"]}, {"uris": uris})
        return f"ok: playing liked songs on {d['name']}"

    def now_playing(self) -> str:
        st = self.api("GET", "/me/player")
        if not st or not st.get("item"):
            return "nothing is playing"
        it = st["item"]
        by = ", ".join(a["name"] for a in it.get("artists", []))
        state = "playing" if st.get("is_playing") else "paused"
        return f"{state}: {it['name']} by {by} on {st['device']['name']} at volume {st['device'].get('volume_percent')}"

    def volume(self, level: int, device=None) -> str:
        d = self.pick_device(device)
        self.api("PUT", "/me/player/volume", {"volume_percent": max(0, min(100, int(level))), "device_id": d["id"]})
        self._ducked = None  # an explicit change wins over restoring after ducking
        return f"ok: volume {level} on {d['name']}"

    def transfer(self, device: str) -> str:
        d = self.pick_device(device)
        self.api("PUT", "/me/player", body={"device_ids": [d["id"]], "play": True})
        return f"ok: moved playback to {d['name']}"

    def save_current(self) -> str:
        st = self.api("GET", "/me/player/currently-playing")
        if not st or not st.get("item"):
            return "error: nothing is playing"
        self.api("PUT", "/me/library", {"uris": st["item"]["uri"]})  # /me/tracks writes were retired Feb 2026
        return f"ok: saved {st['item']['name']} to liked songs"

    # ---------------------------------------------------------- ducking
    def duck(self, level: int) -> None:
        st = self.api("GET", "/me/player")
        dev = (st or {}).get("device") or {}
        vol = dev.get("volume_percent")
        if st and st.get("is_playing") and vol is not None and vol > level and dev.get("id"):
            self._ducked = (dev["id"], vol)
            self.api("PUT", "/me/player/volume", {"volume_percent": level, "device_id": dev["id"]})

    def unduck(self) -> None:
        ducked, self._ducked = self._ducked, None
        if ducked:
            self.api("PUT", "/me/player/volume", {"volume_percent": ducked[1], "device_id": ducked[0]})


def register_spotify_tools(reg: ToolRegistry, cfg) -> bool:
    if not CREDENTIALS.exists():
        return False
    sp = Spotify(json.loads(CREDENTIALS.read_text()), cfg.device)

    @reg.register(
        "spotify",
        "Control Spotify music. Understand requests in any language (Turkish, German, English...) and put the "
        "song, artist, album or playlist into 'query' the way it is titled on Spotify, usually the original "
        "title plus the artist: 'Tarkan'ın Şımarık şarkısını aç' -> action play, type track, query "
        "'Şımarık Tarkan'; 'spiel was von Rammstein' -> type artist, query 'Rammstein'; 'put on some jazz for "
        "cooking' -> type playlist, query 'jazz cooking'. If unsure which song is meant, use 'search' first and "
        "play the right result by its uri. 'play' without query or uri resumes. 'device' is a Spotify Connect "
        "device name (phone, Echo, Mac); leave it empty to use the default or active one.",
        {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["play", "search", "pause", "next", "previous", "volume",
                                                      "now_playing", "devices", "transfer", "play_liked",
                                                      "save_current", "queue"]},
                "query": {"type": "string"},
                "type": {"type": "string", "enum": ["track", "artist", "album", "playlist"]},
                "uri": {"type": "string", "description": "spotify:track:... from a search result"},
                "device": {"type": "string"},
                "volume": {"type": "integer", "minimum": 0, "maximum": 100},
                "shuffle": {"type": "boolean"},
            },
            "required": ["action"],
        },
    )
    def spotify(ctx, action: str, query=None, type="track", uri=None, device=None, volume=None, shuffle=None):
        try:
            if action == "play":
                return sp.play(query, type, uri, device, shuffle)
            if action == "search":
                return sp.search(query or "", type) or f"nothing found for '{query}'"
            if action == "pause":
                sp.api("PUT", "/me/player/pause")
                return "ok: paused"
            if action in ("next", "previous"):
                sp.api("POST", f"/me/player/{action}")
                return f"ok: {action}"
            if action == "volume":
                return sp.volume(volume if volume is not None else 50, device)
            if action == "now_playing":
                return sp.now_playing()
            if action == "devices":
                return [f"{d['name']} ({d['type']}{', active' if d.get('is_active') else ''})" for d in sp.devices()]
            if action == "transfer":
                return sp.transfer(device or "")
            if action == "play_liked":
                return sp.play_liked(device)
            if action == "save_current":
                return sp.save_current()
            if action == "queue":
                item = {"uri": uri} if uri else (sp.search(query or "", "track", 1) or [None])[0]
                if not item:
                    return f"error: nothing found for '{query}'"
                sp.api("POST", "/me/player/queue", {"uri": item["uri"]})
                return f"ok: queued {Spotify.describe(item) if 'name' in item else item['uri']}"
            return f"error: unknown action {action}"
        except SpotifyError as e:
            return f"error: {e}"

    if cfg.duck_volume:
        def safe(fn):
            def run():
                try:
                    fn()
                except Exception as e:  # never let music control break a conversation
                    log.debug("spotify ducking: %s", e)
            return run
        reg.ctx.setdefault("on_wake", []).append(safe(lambda: sp.duck(cfg.duck_volume)))
        reg.ctx.setdefault("on_conversation_end", []).append(safe(sp.unduck))
    return True
