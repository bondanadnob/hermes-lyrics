import threading
import time
import unittest

from dashboard.apple_music_lyrics_backend.models import (
    ArtworkPayload,
    LyricLine,
    LyricsDocument,
    TrackInfo,
)
from dashboard.apple_music_lyrics_backend.service import (
    ArtworkBusyError,
    ArtworkStaleIdentityError,
    LyricsService,
)


class FakeMusic:
    def __init__(self, track: TrackInfo, artwork: ArtworkPayload | None = None):
        self.track = track
        self.artwork = artwork
        self.actions = []
        self.artwork_clears = 0

    def sample(self) -> TrackInfo:
        return self.track

    def control(self, action: str) -> None:
        self.actions.append(action)

    def seek(self, position: float) -> None:
        self.actions.append(("seek", position))

    def artwork_for(self, expected_identity: str) -> ArtworkPayload | None:
        self.actions.append(("artwork", expected_identity))
        return self.artwork

    def clear_artwork_cache(self) -> None:
        self.artwork_clears += 1

    def open_automation_settings(self) -> None:
        return None


class Provider:
    def __init__(self, document: LyricsDocument | None = None):
        self.document = document
        self.calls = 0
        self.clears = 0

    def lyrics_for(self, track: TrackInfo) -> LyricsDocument | None:
        del track
        self.calls += 1
        return self.document

    def clear_cache(self) -> None:
        self.clears += 1


class ArtworkURLProvider:
    def __init__(self, url: str | None = None):
        self.url = url
        self.calls = 0
        self.clears = 0

    def artwork_url_for(self, track: TrackInfo) -> str | None:
        del track
        self.calls += 1
        return self.url

    def clear_cache(self) -> None:
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

    def test_music_plain_lyrics_beat_an_unsynchronized_provider_document(self):
        service = LyricsService(
            music=FakeMusic(
                TrackInfo(
                    True,
                    "playing",
                    "Song",
                    "Artist",
                    plain_lyrics="Music plain",
                )
            ),
            providers=(
                Provider(
                    LyricsDocument(
                        lines=(LyricLine(0, "LRCLIB plain"),),
                        plain_text="LRCLIB plain",
                        source="LRCLIB",
                        synced=False,
                    )
                ),
            ),
        )

        state = service.state()

        self.assertEqual(state["status"], "plain")
        self.assertEqual(state["lyrics"]["source"], "Music.app")

    def test_remote_apple_artwork_is_independent_of_the_selected_lyrics(self):
        remote = ArtworkURLProvider("https://is1-ssl.mzstatic.com/320x320.jpg")
        service = LyricsService(
            music=FakeMusic(TrackInfo(True, "playing", "Song", "Artist")),
            providers=(
                Provider(
                    LyricsDocument(
                        lines=(LyricLine(1, "Synced"),),
                        source="LRCLIB",
                        synced=True,
                    )
                ),
            ),
            artwork_provider=remote,
        )

        state = service.state()

        self.assertEqual(state["lyrics"]["source"], "LRCLIB")
        self.assertEqual(
            state["artwork"]["remote_url"],
            "https://is1-ssl.mzstatic.com/320x320.jpg",
        )

        no_lyrics_state = LyricsService(
            music=FakeMusic(TrackInfo(True, "playing", "Song", "Artist")),
            providers=(Provider(),),
            artwork_provider=remote,
        ).state()
        self.assertEqual(no_lyrics_state["status"], "not_found")
        self.assertEqual(
            no_lyrics_state["artwork"]["remote_url"],
            "https://is1-ssl.mzstatic.com/320x320.jpg",
        )

    def test_lrclib_lyrics_do_not_erase_independent_music_artwork(self):
        track = TrackInfo(True, "playing", "Song", "Artist", "Album", 201.5)
        artwork = ArtworkPayload(
            identity=track.identity,
            data_url="data:image/jpeg;base64,/9j/",
            mime="image/jpeg",
            byte_length=3,
        )
        music = FakeMusic(track, artwork=artwork)
        service = LyricsService(
            music=music,
            providers=(
                Provider(),
                Provider(
                    LyricsDocument(
                        lines=(LyricLine(1, "Synced"),),
                        source="LRCLIB",
                        synced=True,
                    )
                ),
            ),
        )

        state = service.state()
        artwork_state = service.artwork(state["track"]["identity"])

        self.assertEqual(state["lyrics"]["source"], "LRCLIB")
        self.assertIsNone(state["lyrics"]["artwork_url"])
        self.assertEqual(artwork_state["data_url"], artwork.data_url)

    def test_refresh_clears_service_and_provider_caches(self):
        provider = Provider(
            LyricsDocument(lines=(LyricLine(1, "line"),), source="LRCLIB", synced=True)
        )
        music = FakeMusic(TrackInfo(True, "playing", "Song", "Artist"))
        service = LyricsService(
            music=music,
            providers=(provider,),
        )
        service.state()

        service.refresh()
        service.state()

        self.assertEqual(provider.clears, 1)
        self.assertEqual(provider.calls, 2)
        self.assertEqual(music.artwork_clears, 1)

    def test_stale_artwork_identity_is_rejected_before_native_work(self):
        track = TrackInfo(True, "playing", "Song", "Artist")
        music = FakeMusic(track)
        service = LyricsService(music=music, providers=())
        service.state()

        with self.assertRaises(ArtworkStaleIdentityError):
            service.artwork("f" * 24)

        self.assertNotIn(("artwork", "f" * 24), music.actions)

    def test_concurrent_artwork_requests_fail_fast_without_a_native_backlog(self):
        track = TrackInfo(True, "playing", "Song", "Artist")
        entered = threading.Event()
        release = threading.Event()

        class BlockingArtworkMusic(FakeMusic):
            def artwork_for(self, expected_identity: str) -> ArtworkPayload | None:
                entered.set()
                release.wait(2)
                return super().artwork_for(expected_identity)

        music = BlockingArtworkMusic(track)
        service = LyricsService(music=music, providers=())
        service.state()
        first = threading.Thread(target=service.artwork, args=(track.identity,))
        first.start()
        self.assertTrue(entered.wait(1))
        started = time.monotonic()
        try:
            with self.assertRaises(ArtworkBusyError):
                service.artwork(track.identity)
            self.assertLess(time.monotonic() - started, 0.25)
        finally:
            release.set()
            first.join(2)

        self.assertEqual(
            [action for action in music.actions if action[0] == "artwork"],
            [("artwork", track.identity)],
        )

    def test_slow_lyrics_lookup_does_not_queue_a_stale_artwork_request(self):
        track = TrackInfo(True, "playing", "Song", "Artist")
        entered = threading.Event()
        release = threading.Event()

        class BlockingProvider(Provider):
            def lyrics_for(self, track: TrackInfo) -> LyricsDocument | None:
                entered.set()
                release.wait(2)
                return super().lyrics_for(track)

        service = LyricsService(
            music=FakeMusic(track),
            providers=(BlockingProvider(),),
        )
        state_thread = threading.Thread(target=service.state)
        state_thread.start()
        self.assertTrue(entered.wait(1))
        started = time.monotonic()
        try:
            with self.assertRaises(ArtworkStaleIdentityError):
                service.artwork("f" * 24)
            self.assertLess(time.monotonic() - started, 0.25)
        finally:
            release.set()
            state_thread.join(2)

    def test_track_change_during_service_artwork_call_is_rejected_after_native_work(self):
        first = TrackInfo(True, "playing", "First", "Artist", persistent_id="first")
        second = TrackInfo(True, "playing", "Second", "Artist", persistent_id="second")
        payload = ArtworkPayload(
            first.identity,
            "data:image/jpeg;base64,/9j/",
            "image/jpeg",
            3,
        )
        entered = threading.Event()
        release = threading.Event()
        errors = []

        class RacingMusic(FakeMusic):
            def artwork_for(self, expected_identity: str) -> ArtworkPayload | None:
                entered.set()
                release.wait(2)
                return super().artwork_for(expected_identity)

        music = RacingMusic(first, artwork=payload)
        service = LyricsService(music=music, providers=())
        service.state()

        def request_artwork():
            try:
                service.artwork(first.identity)
            except Exception as error:  # Assert the typed boundary after joining.
                errors.append(error)

        artwork_thread = threading.Thread(target=request_artwork)
        artwork_thread.start()
        self.assertTrue(entered.wait(1))
        music.track = second
        service.state()
        release.set()
        artwork_thread.join(2)

        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ArtworkStaleIdentityError)

    def test_refresh_fails_fast_instead_of_applying_delayed_cache_clears(self):
        entered = threading.Event()
        release = threading.Event()
        refresh_done = threading.Event()
        refresh_errors = []

        class BlockingProvider(Provider):
            def lyrics_for(self, track: TrackInfo) -> LyricsDocument | None:
                entered.set()
                release.wait(2)
                return super().lyrics_for(track)

        provider = BlockingProvider()
        service = LyricsService(
            music=FakeMusic(TrackInfo(True, "playing", "Song", "Artist")),
            providers=(provider,),
        )
        state_thread = threading.Thread(target=service.state)
        state_thread.start()
        self.assertTrue(entered.wait(1))

        def refresh():
            try:
                service.refresh()
            except Exception as error:  # Assert the typed boundary after joining.
                refresh_errors.append(error)
            finally:
                refresh_done.set()

        refresh_thread = threading.Thread(target=refresh)
        refresh_thread.start()
        finished_fast = refresh_done.wait(0.2)
        release.set()
        refresh_thread.join(2)
        state_thread.join(2)

        self.assertTrue(finished_fast)
        self.assertEqual(len(refresh_errors), 1)
        self.assertIsInstance(refresh_errors[0], ArtworkBusyError)
        self.assertEqual(provider.clears, 0)

    def test_slow_artwork_does_not_block_state_polling(self):
        track = TrackInfo(True, "playing", "Song", "Artist")
        entered = threading.Event()
        release = threading.Event()
        state_done = threading.Event()

        class BlockingArtworkMusic(FakeMusic):
            def artwork_for(self, expected_identity: str) -> ArtworkPayload | None:
                entered.set()
                release.wait(2)
                return super().artwork_for(expected_identity)

        service = LyricsService(
            music=BlockingArtworkMusic(track),
            providers=(
                Provider(
                    LyricsDocument(
                        lines=(LyricLine(1, "line"),),
                        source="Apple Music",
                        synced=True,
                    )
                ),
            ),
        )
        service.state()
        artwork_thread = threading.Thread(target=service.artwork, args=(track.identity,))
        state_thread = threading.Thread(
            target=lambda: (service.state(), state_done.set())
        )
        artwork_thread.start()
        self.assertTrue(entered.wait(1))
        state_thread.start()
        try:
            self.assertTrue(state_done.wait(0.5))
        finally:
            release.set()
            artwork_thread.join(2)
            state_thread.join(2)

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
