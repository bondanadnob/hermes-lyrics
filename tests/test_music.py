import json
import subprocess
import unittest

from dashboard.apple_music_lyrics_backend.music import MusicClient, ProcessResult


class MusicClientTests(unittest.TestCase):
    def test_maps_jxa_payload_without_exposing_lyrics_in_track_dict(self):
        payload = {
            "running": True,
            "state": "playing",
            "title": "Song",
            "artist": "Artist",
            "album": "Album",
            "duration": 201.5,
            "position": 42.25,
            "lyrics": "plain words",
        }
        calls = []

        def runner(script):
            calls.append(script)
            return ProcessResult(0, json.dumps(payload), "")

        track = MusicClient(runner=runner, clock=lambda: 1234.5).sample()

        self.assertTrue(calls)
        self.assertEqual(track.title, "Song")
        self.assertEqual(track.position, 42.25)
        self.assertEqual(track.sampled_at, 1234.5)
        self.assertEqual(track.plain_lyrics, "plain words")
        self.assertNotIn("plain_lyrics", track.to_dict())

    def test_returns_permission_error_for_denied_apple_events(self):
        client = MusicClient(
            runner=lambda _script: ProcessResult(
                1,
                "",
                "execution error: Not authorized to send Apple events. (-1743)",
            )
        )

        track = client.sample()

        self.assertEqual(track.error, "automation_permission")
        self.assertFalse(track.running)

    def test_opens_automation_settings_through_a_fixed_injected_opener(self):
        calls = []

        def opener():
            calls.append("open")
            return ProcessResult(0, "", "")

        client = MusicClient(
            runner=lambda _script: ProcessResult(0, "", ""),
            settings_opener=opener,
        )

        client.open_automation_settings()

        self.assertEqual(calls, ["open"])

    def test_control_wraps_runner_failures_as_runtime_errors(self):
        def fail(_script):
            raise subprocess.TimeoutExpired("osascript", 5)

        client = MusicClient(runner=fail)

        with self.assertRaisesRegex(RuntimeError, "Music.app command failed"):
            client.control("next")

    def test_control_accepts_only_known_actions_and_seek_is_bounded(self):
        scripts = []
        client = MusicClient(
            runner=lambda script: scripts.append(script) or ProcessResult(0, "ok", "")
        )

        client.control("next")
        client.seek(95.5)

        self.assertIn("nextTrack", scripts[0])
        self.assertIn("95.5", scripts[1])
        with self.assertRaises(ValueError):
            client.control("delete everything")
        with self.assertRaises(ValueError):
            client.seek(-1)


if __name__ == "__main__":
    unittest.main()
