import unittest

from dashboard.apple_music_lyrics_backend.models import LyricLine, LyricsDocument, TrackInfo
from dashboard.apple_music_lyrics_backend.service import LyricsService


class FakeMusic:
    def __init__(self, track):
        self.track = track
        self.actions = []

    def sample(self):
        return self.track

    def control(self, action):
        self.actions.append(action)

    def seek(self, position):
        self.actions.append(("seek", position))


class Provider:
    def __init__(self, document=None):
        self.document = document
        self.calls = 0
        self.clears = 0

    def lyrics_for(self, _track):
        self.calls += 1
        return self.document

    def clear_cache(self):
        self.clears += 1


class LyricsServiceTests(unittest.TestCase):
    def test_prefers_apple_cache_and_does_not_call_network_fallback(self):
        apple_document = LyricsDocument(
            lines=(LyricLine(1, "Apple"),), source="Apple Music", synced=True
        )
        apple = Provider(apple_document)
        network = Provider(
            LyricsDocument(lines=(LyricLine(1, "Network"),), source="LRCLIB", synced=True)
        )
        service = LyricsService(
            music=FakeMusic(TrackInfo(True, "playing", "Song", "Artist")),
            providers=(apple, network),
        )

        state = service.state()

        self.assertEqual(state["status"], "ready")
        self.assertEqual(state["lyrics"]["source"], "Apple Music")
        self.assertEqual(apple.calls, 1)
        self.assertEqual(network.calls, 0)

    def test_uses_music_plain_lyrics_when_synced_sources_miss(self):
        service = LyricsService(
            music=FakeMusic(
                TrackInfo(
                    True,
                    "playing",
                    "Song",
                    "Artist",
                    plain_lyrics="first\nsecond",
                )
            ),
            providers=(Provider(), Provider()),
        )

        state = service.state()

        self.assertEqual(state["status"], "plain")
        self.assertEqual(state["lyrics"]["source"], "Music.app")
        self.assertFalse(state["lyrics"]["synced"])

    def test_refresh_clears_service_and_provider_caches(self):
        provider = Provider(
            LyricsDocument(lines=(LyricLine(1, "line"),), source="LRCLIB", synced=True)
        )
        service = LyricsService(
            music=FakeMusic(TrackInfo(True, "playing", "Song", "Artist")),
            providers=(provider,),
        )
        service.state()

        service.refresh()
        service.state()

        self.assertEqual(provider.clears, 1)
        self.assertEqual(provider.calls, 2)

    def test_reports_idle_and_permission_states_without_provider_calls(self):
        provider = Provider()
        idle = LyricsService(
            music=FakeMusic(TrackInfo(False, "not_running")), providers=(provider,)
        ).state()
        denied = LyricsService(
            music=FakeMusic(
                TrackInfo(False, "error", error="automation_permission")
            ),
            providers=(provider,),
        ).state()

        self.assertEqual(idle["status"], "idle")
        self.assertEqual(denied["status"], "permission_required")
        self.assertEqual(provider.calls, 0)


if __name__ == "__main__":
    unittest.main()
