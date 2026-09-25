"""Google Calendar, Gmail, Tasks and Contacts through Google's REST APIs, with your own OAuth client.

Setup once (details in README.md):
  1. In Google Cloud Console create a project, enable the Google Calendar, Gmail,
     Google Tasks and People APIs, and create an OAuth client of type "Desktop app".
     Download its JSON.
  2. python -m voiceagent google-login path/to/client_secret.json
Tokens go to ~/.voiceagent/google.json (mode 600) and refresh themselves.

The account's data stays between this machine and Google; nothing goes through
the Claude account the CLI is logged in with, except what the model reads while
answering. Sending mail always needs an explicit confirmation from the user,
which the tool description asks the model to get first.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import html
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

from . import ToolRegistry

log = logging.getLogger(__name__)

CREDENTIALS = Path("~/.voiceagent/google.json").expanduser()
SCOPES = ("https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/calendar.readonly "
          "https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.compose "
          "https://www.googleapis.com/auth/tasks https://www.googleapis.com/auth/contacts.readonly")
AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
CAL = "https://www.googleapis.com/calendar/v3"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
TASKS = "https://tasks.googleapis.com/tasks/v1"
PEOPLE = "https://people.googleapis.com/v1"


class GoogleError(Exception):
    pass


def _save(creds: dict) -> None:
    CREDENTIALS.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(CREDENTIALS, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(creds, f)


def _post_form(url: str, form: dict) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise GoogleError(f"token request failed: {e.read().decode(errors='replace')}") from None


def login(client_file: str, open_browser=True) -> None:
    """Installed-app flow: PKCE plus a loopback redirect on a random local port."""
    import webbrowser

    data = json.loads(Path(os.path.expanduser(client_file)).read_text())
    client = data.get("installed") or data.get("web") or data
    client_id, client_secret = client["client_id"], client.get("client_secret", "")

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    result: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" not in q and "error" not in q:
                self.send_response(404)
                self.end_headers()
                return
            ok = q.get("state", [""])[0] == state and "code" in q
            result.update(code=q["code"][0] if ok else None, error=q.get("error", [None])[0])
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(("Google is connected. You can close this tab." if ok
                              else "Google login failed, check the terminal.").encode())

    server = HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{server.server_address[1]}/"
    url = AUTH + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code", "scope": SCOPES,
        "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    print(f"Opening Google in the browser. If it doesn't open, visit:\n{url}\n")
    if open_browser:
        webbrowser.open(url)
    while "code" not in result:
        server.handle_request()
    server.server_close()
    if not result.get("code"):
        raise SystemExit(f"Google login failed: {result.get('error') or 'state mismatch'}")
    tok = _post_form(TOKEN, {"grant_type": "authorization_code", "code": result["code"], "redirect_uri": redirect,
                             "client_id": client_id, "client_secret": client_secret, "code_verifier": verifier})
    if "refresh_token" not in tok:
        raise SystemExit("Google returned no refresh token. Remove the app's access at "
                         "myaccount.google.com/permissions and run google-login again.")
    _save({"client_id": client_id, "client_secret": client_secret, "refresh_token": tok["refresh_token"],
           "access_token": tok["access_token"], "expires_at": time.time() + tok.get("expires_in", 3600) - 60})


class Google:
    def __init__(self, creds: dict, save=_save):
        self.creds, self._persist = creds, save
        self._lock = threading.Lock()
        self._tz: ZoneInfo | None = None

    def _token(self) -> str:
        with self._lock:
            if time.time() >= self.creds.get("expires_at", 0):
                tok = _post_form(TOKEN, {"grant_type": "refresh_token", "refresh_token": self.creds["refresh_token"],
                                         "client_id": self.creds["client_id"],
                                         "client_secret": self.creds.get("client_secret", "")})
                self.creds.update(access_token=tok["access_token"],
                                  expires_at=time.time() + tok.get("expires_in", 3600) - 60)
                self._persist(self.creds)
            return self.creds["access_token"]

    def api(self, method: str, url: str, params: dict | None = None, body: dict | None = None):
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        req = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {self._token()}",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error", {}).get("message", e.reason)
            except ValueError:
                msg = e.reason
            raise GoogleError(f"Google said {e.code}: {msg}") from None

    # ------------------------------------------------------------ calendar
    @property
    def tz(self) -> ZoneInfo:
        if self._tz is None:
            name = (self.api("GET", f"{CAL}/users/me/settings/timezone") or {}).get("value") or "UTC"
            self._tz = ZoneInfo(name)
        return self._tz

    def _local(self, value: str) -> dt.datetime:
        t = dt.datetime.fromisoformat(value)
        return t if t.tzinfo else t.replace(tzinfo=self.tz)

    def events(self, start: str | None = None, days: int = 1, query: str | None = None) -> list[str]:
        if start:
            begin = self._local(start if "T" in start else start + "T00:00")
        else:
            begin = dt.datetime.now(self.tz)
        params = {"timeMin": begin.isoformat(), "timeMax": (begin + dt.timedelta(days=max(1, days))).isoformat(),
                  "singleEvents": "true", "orderBy": "startTime", "maxResults": 25}
        if query:
            params["q"] = query
        items = (self.api("GET", f"{CAL}/calendars/primary/events", params) or {}).get("items", [])
        out = []
        for e in items:
            s = e.get("start", {})
            if "dateTime" in s:
                when = dt.datetime.fromisoformat(s["dateTime"]).astimezone(self.tz).strftime("%a %d %b %H:%M")
            else:
                when = dt.date.fromisoformat(s["date"]).strftime("%a %d %b") + " all day"
            where = f" @ {e['location']}" if e.get("location") else ""
            out.append(f"{when}: {e.get('summary', '(no title)')}{where} [id {e['id']}]")
        return out

    def create_event(self, title: str, start: str, duration_minutes: int = 60, end: str | None = None,
                     location: str | None = None, all_day: bool = False) -> str:
        if all_day:
            day = dt.date.fromisoformat(start[:10])
            body = {"summary": title, "start": {"date": day.isoformat()},
                    "end": {"date": (day + dt.timedelta(days=1)).isoformat()}}
        else:
            s = self._local(start)
            e = self._local(end) if end else s + dt.timedelta(minutes=duration_minutes or 60)
            body = {"summary": title, "start": {"dateTime": s.isoformat()}, "end": {"dateTime": e.isoformat()}}
        if location:
            body["location"] = location
        ev = self.api("POST", f"{CAL}/calendars/primary/events", body=body) or {}
        return f"ok: created '{title}' [id {ev.get('id')}]"

    def delete_event(self, event_id: str) -> str:
        self.api("DELETE", f"{CAL}/calendars/primary/events/{urllib.parse.quote(event_id)}")
        return "ok: deleted"

    # --------------------------------------------------------------- gmail
    def search_mail(self, query: str, limit: int = 5) -> list[str]:
        ids = (self.api("GET", f"{GMAIL}/messages", {"q": query, "maxResults": min(limit, 10)}) or {}).get("messages", [])
        out = []
        for m in ids:
            msg = self.api("GET", f"{GMAIL}/messages/{m['id']}",
                           {"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]}) or {}
            h = {x["name"].lower(): x["value"] for x in msg.get("payload", {}).get("headers", [])}
            sender = re.sub(r"\s*<[^>]+>", "", h.get("from", "?")).strip('" ')
            out.append(f"From {sender}: {h.get('subject', '(no subject)')} ({h.get('date', '')[:16]}) "
                       f"- {html.unescape(msg.get('snippet', ''))[:140]} [id {m['id']}]")
        return out

    def read_mail(self, message_id: str) -> str:
        msg = self.api("GET", f"{GMAIL}/messages/{message_id}", {"format": "full"}) or {}
        h = {x["name"].lower(): x["value"] for x in msg.get("payload", {}).get("headers", [])}
        body = _body_text(msg.get("payload", {})) or html.unescape(msg.get("snippet", ""))
        return f"From: {h.get('from')}\nSubject: {h.get('subject')}\nDate: {h.get('date')}\n\n{body[:2500]}"

    def compose(self, to: str, subject: str, body: str, send: bool) -> str:
        m = EmailMessage()
        m["To"], m["Subject"] = to, subject
        m.set_content(body)
        raw = base64.urlsafe_b64encode(m.as_bytes()).decode()
        if send:
            self.api("POST", f"{GMAIL}/messages/send", body={"raw": raw})
            return f"ok: sent to {to}"
        self.api("POST", f"{GMAIL}/drafts", body={"message": {"raw": raw}})
        return f"ok: draft to {to} saved in Gmail"


class GoogleMore(Google):
    """Tasks and contacts, kept apart from the calendar and mail code above."""

    # ---------------------------------------------------------------- tasks
    def tasks(self, include_done: bool = False) -> list[str]:
        items = (self.api("GET", f"{TASKS}/lists/@default/tasks",
                          {"showCompleted": str(include_done).lower(), "maxResults": 50}) or {}).get("items", [])
        out = []
        for t in items:
            due = f" (due {t['due'][:10]})" if t.get("due") else ""
            done = " [done]" if t.get("status") == "completed" else ""
            out.append(f"{t.get('title', '(untitled)')}{due}{done} [id {t['id']}]")
        return out

    def add_task(self, title: str, due: str | None = None, notes: str | None = None) -> str:
        body = {"title": title}
        if due:
            body["due"] = due[:10] + "T00:00:00.000Z"  # Google Tasks keeps only the date
        if notes:
            body["notes"] = notes
        t = self.api("POST", f"{TASKS}/lists/@default/tasks", body=body) or {}
        return f"ok: added '{title}' [id {t.get('id')}]"

    def _find_task(self, task: str) -> dict | None:
        items = (self.api("GET", f"{TASKS}/lists/@default/tasks", {"showCompleted": "false", "maxResults": 100})
                 or {}).get("items", [])
        return next((t for t in items if t["id"] == task), None) or next(
            (t for t in items if task.lower() in t.get("title", "").lower()), None)

    def complete_task(self, task: str) -> str:
        t = self._find_task(task)
        if not t:
            return f"error: no open task matching '{task}'"
        self.api("PATCH", f"{TASKS}/lists/@default/tasks/{t['id']}", body={"status": "completed"})
        return f"ok: '{t['title']}' done"

    def delete_task(self, task: str) -> str:
        t = self._find_task(task)
        if not t:
            return f"error: no open task matching '{task}'"
        self.api("DELETE", f"{TASKS}/lists/@default/tasks/{t['id']}")
        return f"ok: deleted '{t['title']}'"

    # ------------------------------------------------------------- contacts
    def contacts(self, query: str) -> list[dict]:
        # Google asks for one empty search first to warm its cache, otherwise results can be empty
        if not getattr(self, "_contacts_warm", False):
            self.api("GET", f"{PEOPLE}/people:searchContacts", {"query": "", "readMask": "names"})
            self._contacts_warm = True
        res = self.api("GET", f"{PEOPLE}/people:searchContacts",
                       {"query": query, "readMask": "names,phoneNumbers,emailAddresses", "pageSize": 5}) or {}
        out = []
        for r in res.get("results", []):
            p = r.get("person", {})
            out.append({"name": (p.get("names") or [{}])[0].get("displayName", "?"),
                        "phones": [n.get("canonicalForm") or n.get("value") for n in p.get("phoneNumbers", [])],
                        "emails": [e.get("value") for e in p.get("emailAddresses", [])]})
        return out


def _body_text(part: dict) -> str:
    """Plain text of a Gmail payload: text/plain if there is one, else HTML with tags stripped."""
    mime, data = part.get("mimeType", ""), part.get("body", {}).get("data")
    if mime == "text/plain" and data:
        return base64.urlsafe_b64decode(data + "==").decode(errors="replace").strip()
    subparts = part.get("parts", [])
    for p in subparts:
        if p.get("mimeType") == "text/plain":
            text = _body_text(p)
            if text:
                return text
    for p in subparts:
        text = _body_text(p)
        if text:
            return text
    if mime == "text/html" and data:
        raw = base64.urlsafe_b64decode(data + "==").decode(errors="replace")
        raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
        return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw))).strip()
    return ""


def register_google_tools(reg: ToolRegistry) -> bool:
    if not CREDENTIALS.exists():
        return False
    g = GoogleMore(json.loads(CREDENTIALS.read_text()))
    reg.ctx["google"] = g  # other tools (messages) look contacts up through it

    def safe(fn):
        try:
            return fn()
        except GoogleError as e:
            return f"error: {e}"

    @reg.register(
        "calendar",
        "The user's Google Calendar. 'list' shows events from 'start' (YYYY-MM-DD or YYYY-MM-DDTHH:MM, default "
        "now) for 'days' days; use it for 'what's on today/tomorrow/this week' or to find an event by 'query'. "
        "'create' adds an event: 'start' as YYYY-MM-DDTHH:MM local time worked out from the current time "
        "(e.g. 'tomorrow at 3' -> the right date at 15:00), with duration_minutes or end, or all_day. "
        "'delete' needs an event id from 'list'; confirm with the user before deleting. "
        "When speaking, say times naturally and never read out ids.",
        {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "create", "delete"]},
                "start": {"type": "string"},
                "days": {"type": "integer", "minimum": 1, "maximum": 31},
                "query": {"type": "string"},
                "title": {"type": "string"},
                "end": {"type": "string"},
                "duration_minutes": {"type": "integer", "minimum": 5},
                "location": {"type": "string"},
                "all_day": {"type": "boolean"},
                "event_id": {"type": "string"},
            },
            "required": ["action"],
        },
    )
    def calendar(ctx, action: str, start=None, days=1, query=None, title=None, end=None,
                 duration_minutes=60, location=None, all_day=False, event_id=None):
        if action == "list":
            return safe(lambda: g.events(start, days, query) or "no events")
        if action == "create":
            if not (title and start):
                return "error: create needs title and start"
            return safe(lambda: g.create_event(title, start, duration_minutes, end, location, all_day))
        if action == "delete":
            return safe(lambda: g.delete_event(event_id)) if event_id else "error: delete needs event_id"
        return f"error: unknown action {action}"

    @reg.register(
        "gmail",
        "The user's Gmail. 'search' takes a Gmail query ('is:unread newer_than:1d', 'from:anna', "
        "'subject:invoice'); summarise results briefly by sender and topic, never read ids aloud. 'read' opens one "
        "message by id. 'draft' saves an email as a draft. 'send' really sends it: only use it after reading the "
        "recipient, subject and text back to the user and hearing a clear yes; otherwise use 'draft'.",
        {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["search", "read", "draft", "send"]},
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                "message_id": {"type": "string"},
                "to": {"type": "string", "description": "email address"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["action"],
        },
    )
    def gmail(ctx, action: str, query="is:unread", limit=5, message_id=None, to=None, subject="", body=""):
        if action == "search":
            return safe(lambda: g.search_mail(query or "is:unread", limit) or "no matching emails")
        if action == "read":
            return safe(lambda: g.read_mail(message_id)) if message_id else "error: read needs message_id"
        if action in ("draft", "send"):
            if not (to and "@" in to):
                return "error: needs a recipient email address"
            return safe(lambda: g.compose(to, subject or "", body or "", send=action == "send"))
        return f"error: unknown action {action}"

    @reg.register(
        "tasks",
        "The user's Google Tasks: reminders, to-dos and the shopping list. 'list' shows open tasks, 'add' creates "
        "one (optional 'due' as YYYY-MM-DD), 'complete' and 'delete' take the task's id or a word from its title. "
        "For a reminder at a specific time today, use set_timer instead or as well.",
        {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "add", "complete", "delete"]},
                "title": {"type": "string"},
                "due": {"type": "string"},
                "notes": {"type": "string"},
                "task": {"type": "string", "description": "id or part of the title"},
                "include_done": {"type": "boolean"},
            },
            "required": ["action"],
        },
    )
    def tasks(ctx, action: str, title=None, due=None, notes=None, task=None, include_done=False):
        if action == "list":
            return safe(lambda: g.tasks(include_done) or "no open tasks")
        if action == "add":
            return safe(lambda: g.add_task(title, due, notes)) if title else "error: add needs a title"
        if action in ("complete", "delete"):
            if not task:
                return f"error: {action} needs a task"
            return safe(lambda: g.complete_task(task) if action == "complete" else g.delete_task(task))
        return f"error: unknown action {action}"

    @reg.register(
        "contacts",
        "Look up someone in the user's Google Contacts by name to get their phone number or email, "
        "for example before writing an email or sending a message.",
        {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    )
    def contacts(ctx, query: str):
        return safe(lambda: g.contacts(query) or f"no contact matching '{query}'")

    return True
