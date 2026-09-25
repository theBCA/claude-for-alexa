"""Roborock tool logic against a fake device (the real one needs your account)."""
import asyncio
import unittest
from types import SimpleNamespace

from voiceagent.tools.roborock import Vacuums


class FakeProps:
    def __init__(self):
        self.sent = []
        room = lambda seg, name: SimpleNamespace(segment_id=seg, name=name)  # noqa: E731
        self.rooms = SimpleNamespace(rooms=[room(16, "Salon"), room(17, "Mutfak"), room(18, "Yatak Odası")])
        self.status = SimpleNamespace(state_name="charging", battery=87, error_code_name="none")
        self.command = SimpleNamespace(send=self._send)

        async def refresh():
            pass
        self.rooms.refresh = refresh
        self.status.refresh = refresh

    async def start(self):
        pass

    async def _send(self, cmd, params=None):
        self.sent.append((cmd, params))


class TestVacuum(unittest.TestCase):
    def setUp(self):
        self.props = FakeProps()
        self.v = Vacuums({"email": "x", "user_data": {}})
        device = SimpleNamespace(name="Roborock S8", duid="d1", v1_properties=self.props)

        async def devices():
            return [device]
        self.v.devices = devices

    def run_(self, action, rooms=None):
        return self.v.call(self.v.run(action, rooms, None, 1))

    def test_status_lists_rooms(self):
        out = self.run_("status")
        self.assertIn("charging, battery 87%", out)
        self.assertIn("Salon, Mutfak, Yatak Odası", out)
        self.assertNotIn("error", out)

    def test_clean_rooms_matches_names(self):
        self.assertTrue(self.run_("clean_rooms", ["mutfak", "yatak"]).startswith("ok"))
        self.assertEqual(self.props.sent, [("app_segment_clean", [{"segments": [17, 18], "repeat": 1}])])

    def test_unknown_room_lists_known(self):
        out = self.run_("clean_rooms", ["garage"])
        self.assertIn("unknown room(s) garage", out)
        self.assertIn("Known rooms: Salon", out)
        self.assertEqual(self.props.sent, [])

    def test_dock(self):
        self.run_("dock")
        self.assertEqual(self.props.sent, [("app_charge", None)])


if __name__ == "__main__":
    unittest.main()
