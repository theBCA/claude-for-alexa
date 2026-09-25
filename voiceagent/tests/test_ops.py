"""Backup and restore round-trip, with the credential-filtering rules."""
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from voiceagent import ops


class TestOps(unittest.TestCase):
    def test_include_filters_claude_noise(self):
        keep = [Path("spotify.json"), Path("lepro_client_key.pem"),
                Path("claude-personal/.claude.json"), Path("google.json")]
        drop = [Path("memory.db"), Path("brain.log"), Path("claude-cli.log"),
                Path("claude-personal/sessions/1.json"), Path("claude-personal/projects/x.jsonl"),
                Path("claude-personal/backups/.claude.json.backup.1")]
        for p in keep:
            self.assertTrue(ops._include(p), p)
        for p in drop:
            self.assertFalse(ops._include(p), p)

    def test_backup_then_restore(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            home, project, other = d / "home", d / "project", d / "other"
            for p in (home / "claude-personal" / "sessions", project, other):
                p.mkdir(parents=True)
            (home / "spotify.json").write_text('{"refresh_token":"r"}')
            (home / "memory.db").write_text("sqlite")                       # skipped
            (home / "claude-personal" / ".claude.json").write_text("{}")
            (home / "claude-personal" / "sessions" / "1.json").write_text("noise")  # skipped
            (project / "config.yaml").write_text("server:\n  token: abc\n")
            (project / ".env").write_text("LEPRO_PASSWORD=secret\n")
            bundle = d / "b.tgz"
            with mock.patch.object(ops, "HOME", home), mock.patch.object(ops, "PROJECT", project):
                ops.backup(str(bundle))
            names = tarfile.open(bundle).getnames()
            self.assertIn("home/spotify.json", names)
            self.assertIn("config/.env", names)
            self.assertNotIn("home/memory.db", names)
            self.assertNotIn("home/claude-personal/sessions/1.json", names)
            # restore into fresh roots
            with mock.patch.object(ops, "HOME", other), mock.patch.object(ops, "PROJECT", d / "restored"):
                ops.restore(str(bundle))
                self.assertEqual((other / "spotify.json").read_text(), '{"refresh_token":"r"}')
                self.assertEqual((d / "restored" / ".env").read_text(), "LEPRO_PASSWORD=secret\n")
                self.assertEqual(oct((other / "spotify.json").stat().st_mode)[-3:], "600")


if __name__ == "__main__":
    unittest.main()
