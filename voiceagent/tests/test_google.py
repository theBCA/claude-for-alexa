"""Calendar and Gmail tool logic against a fake Google API."""
import base64
import datetime as dt
import time
import unittest
from zoneinfo import ZoneInfo

from voiceagent.tools.google import Google, _body_text


class FakeGoogle(Google):
    def __init__(self):
        super().__init__({"client_id": "c", "refresh_token": "r", "access_token": "a",
                          "expires_at": time.time() + 3600}, save=lambda c: None)
        self._tz = ZoneInfo("Europe/Berlin")
        self.calls = []

    def api(self, method, url, params=None, body=None):
        self.calls.append((method, url.rsplit("/v1/users/me", 1)[-1].rsplit("/v3", 1)[-1], params, body))
        if url.endswith("/events") and method == "GET":
            return {"items": [
                {"id": "e1", "summary": "Dentist", "location": "Mitte",
                 "start": {"dateTime": "2026-09-26T15:00:00+02:00"}},
                {"id": "e2", "summary": "Ceren's birthday", "start": {"date": "2026-09-27"}}]}
        if url.endswith("/events") and method == "POST":
            return {"id": "new1"}
        if url.endswith("/messages") and method == "GET":
            return {"messages": [{"id": "m1"}]}
        if "/messages/m1" in url:
            return {"snippet": "Your order &amp; invoice", "payload": {"headers": [
                {"name": "From", "value": '"Anna Schmidt" <anna@example.com>'},
                {"name": "Subject", "value": "Invoice"}, {"name": "Date", "value": "Fri, 25 Sep 2026 10:00"}]}}
        return None


class TestGoogle(unittest.TestCase):
    def test_list_events_formats_times_and_all_day(self):
        g = FakeGoogle()
        out = g.events("2026-09-26", days=2)
        self.assertEqual(out[0], "Sat 26 Sep 15:00: Dentist @ Mitte [id e1]")
        self.assertEqual(out[1], "Sun 27 Sep all day: Ceren's birthday [id e2]")
        params = g.calls[-1][2]
        self.assertEqual(params["timeMin"], "2026-09-26T00:00:00+02:00")
        self.assertEqual(params["timeMax"], "2026-09-28T00:00:00+02:00")

    def test_create_event_uses_calendar_timezone(self):
        g = FakeGoogle()
        self.assertEqual(g.create_event("Call mum", "2026-09-26T18:30", 30), "ok: created 'Call mum' [id new1]")
        body = g.calls[-1][3]
        self.assertEqual(body["start"]["dateTime"], "2026-09-26T18:30:00+02:00")
        self.assertEqual(body["end"]["dateTime"], "2026-09-26T19:00:00+02:00")

    def test_all_day_event(self):
        g = FakeGoogle()
        g.create_event("Holiday", "2026-10-03", all_day=True)
        self.assertEqual(g.calls[-1][3]["end"], {"date": "2026-10-04"})

    def test_search_mail_is_short_and_clean(self):
        out = FakeGoogle().search_mail("is:unread")
        self.assertEqual(out, ["From Anna Schmidt: Invoice (Fri, 25 Sep 2026) - Your order & invoice [id m1]"])

    def test_draft_vs_send(self):
        g = FakeGoogle()
        self.assertEqual(g.compose("a@b.de", "Hi", "Text", send=False), "ok: draft to a@b.de saved in Gmail")
        self.assertTrue(g.calls[-1][1].endswith("/drafts"))
        g.compose("a@b.de", "Hi", "Text", send=True)
        self.assertTrue(g.calls[-1][1].endswith("/messages/send"))
        raw = base64.urlsafe_b64decode(g.calls[-1][3]["raw"]).decode()
        self.assertIn("To: a@b.de", raw)

    def test_body_prefers_plain_text(self):
        enc = lambda s: base64.urlsafe_b64encode(s.encode()).decode()  # noqa: E731
        payload = {"mimeType": "multipart/alternative", "parts": [
            {"mimeType": "text/html", "body": {"data": enc("<p>Hello <b>there</b></p>")}},
            {"mimeType": "text/plain", "body": {"data": enc("Hello there")}}]}
        self.assertEqual(_body_text(payload), "Hello there")
        self.assertEqual(_body_text(payload["parts"][0]), "Hello there")


if __name__ == "__main__":
    unittest.main()
