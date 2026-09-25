"""Spotify tool against a fake Web API (the real one needs your account and Premium)."""
import time
import unittest
from unittest import mock

from voiceagent.tools import spotify as sp_mod
from voiceagent.tools.spotify import Spotify, SpotifyError

DEVICES = [{"id": "phone1", "name": "Galaxy S24", "type": "Smartphone", "is_active": False},
           {"id": "echo1", "name": "Echo Dot Max", "type": "Speaker", "is_active": True}]


class FakeSpotify(Spotify):
    def __init__(self, default_device=None, playing=None):
        super().__init__({"client_id": "c", "refresh_token": "r", "access_token": "a",
                          "expires_at": time.time() + 3600}, default_device, save=lambda c: None)
        self.calls, self.playing = [], playing

    def api(self, method, path, params=None, body=None):
        self.calls.append((method, path, params, body))
        if path == "/me/player/devices":
            return {"devices": DEVICES}
        if path == "/search":
            kind = params["type"]
            items = {"track": [{"uri": "spotify:track:1", "name": "Şımarık", "artists": [{"name": "Tarkan"}]}],
                     "artist": [{"uri": "spotify:artist:9", "name": "Rammstein"}]}.get(kind, [])
            return {f"{kind}s": {"items": items}}
        if path == "/me/player" and method == "GET":
            return self.playing
        return None


class TestSpotify(unittest.TestCase):
    def test_play_track_on_active_device(self):
        s = FakeSpotify()
        self.assertEqual(s.play("Şımarık Tarkan", "track"), "ok: playing Şımarık by Tarkan on Echo Dot Max")
        self.assertEqual(s.calls[-1], ("PUT", "/me/player/play", {"device_id": "echo1"},
                                       {"uris": ["spotify:track:1"]}))

    def test_artist_plays_as_context_on_named_device(self):
        s = FakeSpotify()
        s.play("Rammstein", "artist", device="galaxy")
        self.assertEqual(s.calls[-1][2:], ({"device_id": "phone1"}, {"context_uri": "spotify:artist:9"}))

    def test_default_device_and_unknown_device(self):
        s = FakeSpotify(default_device="Galaxy")
        self.assertEqual(s.pick_device(None)["id"], "phone1")
        with self.assertRaises(SpotifyError):
            s.pick_device("kitchen")

    def test_nothing_found(self):
        self.assertTrue(FakeSpotify().play("xyz", "album").startswith("error: nothing found"))

    def test_duck_and_restore(self):
        s = FakeSpotify(playing={"is_playing": True, "device": {"id": "echo1", "volume_percent": 70}})
        s.duck(20)
        self.assertEqual(s.calls[-1], ("PUT", "/me/player/volume", {"volume_percent": 20, "device_id": "echo1"}, None))
        s.unduck()
        self.assertEqual(s.calls[-1][2], {"volume_percent": 70, "device_id": "echo1"})
        n = len(s.calls)
        s.unduck()  # nothing left to restore
        self.assertEqual(len(s.calls), n)

    def test_no_duck_when_paused(self):
        s = FakeSpotify(playing={"is_playing": False, "device": {"id": "echo1", "volume_percent": 70}})
        s.duck(20)
        self.assertNotIn("/me/player/volume", [c[1] for c in s.calls])

    def test_expired_token_refreshes(self):
        saved = []
        s = Spotify({"client_id": "c", "refresh_token": "old", "access_token": "x", "expires_at": 0},
                    save=saved.append)
        with mock.patch.object(sp_mod, "_token_request",
                               return_value={"access_token": "new", "expires_in": 3600, "refresh_token": "r2"}) as tr:
            self.assertEqual(s._token(), "new")
            self.assertEqual(tr.call_args[0][0]["refresh_token"], "old")
        self.assertEqual(saved[-1]["refresh_token"], "r2")


if __name__ == "__main__":
    unittest.main()
