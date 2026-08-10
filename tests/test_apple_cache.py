import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dashboard.apple_music_lyrics_backend import apple_cache as apple_cache_module
from dashboard.apple_music_lyrics_backend.apple_cache import AppleMusicCacheProvider
from dashboard.apple_music_lyrics_backend.models import TrackInfo


class AppleMusicCacheProviderTests(unittest.TestCase):
    def test_reads_matching_word_timed_ttml_from_read_only_cache_database(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "fsCachedData").mkdir()
            database = root / "Cache.db"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE cfurl_cache_response (
                    entry_ID INTEGER PRIMARY KEY,
                    request_key TEXT,
                    time_stamp INTEGER
                );
                CREATE TABLE cfurl_cache_receiver_data (
                    entry_ID INTEGER,
                    isDataOnFS INTEGER,
                    receiver_data BLOB
                );
                """
            )
            ttml = '<tt><body><p begin="1s" end="3s"><span begin="1s" end="2s">hello</span></p></body></tt>'
            payload = {
                "data": [
                    {
                        "attributes": {
                            "name": "Song",
                            "artistName": "Artist",
                            "albumName": "Album",
                            "durationInMillis": 201500,
                            "artwork": {"url": "https://is1-ssl.mzstatic.com/{w}x{h}.{f}"},
                        },
                        "relationships": {
                            "syllable-lyrics": {
                                "data": [
                                    {
                                        "attributes": {
                                            "ttmlLocalizations": {"en-US": ttml}
                                        }
                                    }
                                ]
                            }
                        },
                    }
                ]
            }
            connection.execute(
                "INSERT INTO cfurl_cache_response VALUES (1, ?, 10)",
                ("https://amp-api.music.apple.com/v1/catalog/us/songs/1/syllable-lyrics",),
            )
            connection.execute(
                "INSERT INTO cfurl_cache_receiver_data VALUES (1, 0, ?)",
                (json.dumps(payload).encode("utf-8"),),
            )
            connection.commit()
            connection.close()

            document = AppleMusicCacheProvider(cache_root=root).lyrics_for(
                TrackInfo(True, "playing", "Song", "Artist", "Album", 201.5)
            )

            self.assertIsNotNone(document)
            self.assertEqual(document.source, "Apple Music")
            self.assertEqual(document.word_timing, "exact")
            self.assertEqual(document.lines[0].words[0].text, "hello")
            self.assertEqual(
                document.artwork_url,
                "https://is1-ssl.mzstatic.com/320x320.jpg",
            )

    def test_total_cache_payload_scan_is_bounded(self):
        provider = AppleMusicCacheProvider(cache_root=Path("/definitely/missing"))
        provider._database_rows = lambda: [(0, b"1234"), (0, b"{}")]

        with patch.object(apple_cache_module, "_MAX_SCAN_BYTES", 5):
            self.assertEqual(list(provider._candidate_payloads()), ["1234"])

    def test_database_file_reference_does_not_follow_symlink_outside_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "cache"
            data_directory = root / "fsCachedData"
            data_directory.mkdir(parents=True)
            outside = Path(directory) / "outside-data"
            outside.write_text("secret", encoding="utf-8")
            filename = "01234567-89ab-cdef-0123-456789abcdef"
            (data_directory / filename).symlink_to(outside)

            provider = AppleMusicCacheProvider(cache_root=root)
            provider._database_rows = lambda: [(1, filename.encode("utf-8"))]

            self.assertEqual(list(provider._candidate_payloads()), [])

    def test_recent_cache_scan_does_not_follow_symlinks_outside_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "cache"
            data_directory = root / "fsCachedData"
            data_directory.mkdir(parents=True)
            outside = Path(directory) / "outside-data"
            outside.write_text("secret", encoding="utf-8")
            (data_directory / "0123456789abcdef").symlink_to(outside)

            provider = AppleMusicCacheProvider(cache_root=root)

            self.assertEqual(list(provider._candidate_payloads()), [])

    def test_corrupt_inline_cache_row_does_not_hide_later_candidates(self):
        provider = AppleMusicCacheProvider(cache_root=Path("/definitely/missing"))
        provider._database_rows = lambda: [(0, b"\xff"), (0, b'{"ok": true}')]

        self.assertEqual(list(provider._candidate_payloads()), ['{"ok": true}'])

    def test_untimed_cache_ttml_does_not_preempt_synchronized_fallbacks(self):
        provider = AppleMusicCacheProvider(cache_root=Path("/definitely/missing"))
        payload = {
            "data": [
                {
                    "attributes": {"name": "Song", "artistName": "Artist"},
                    "relationships": {
                        "syllable-lyrics": {
                            "data": [
                                {
                                    "attributes": {
                                        "ttmlLocalizations": "<tt><body><p>plain</p></body></tt>"
                                    }
                                }
                            ]
                        }
                    },
                }
            ]
        }
        provider._candidate_payloads = lambda: iter([json.dumps(payload)])

        document = provider.lyrics_for(TrackInfo(True, "playing", "Song", "Artist"))

        self.assertIsNone(document)

    def test_rejects_non_apple_artwork_hosts(self):
        self.assertIsNone(
            AppleMusicCacheProvider._artwork_url(
                "https://tracker.example/{w}x{h}.{f}"
            )
        )

    def test_rejects_a_same_title_with_wrong_artist_and_duration(self):
        provider = AppleMusicCacheProvider(cache_root=Path("/definitely/missing"))
        candidate = {
            "name": "Song",
            "artistName": "Wrong",
            "albumName": "Album",
            "durationInMillis": 400000,
        }

        self.assertIsNone(
            provider.match_score(
                candidate,
                TrackInfo(True, "playing", "Song", "Artist", "Album", 201.5),
            )
        )


if __name__ == "__main__":
    unittest.main()
