import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import cast
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
            self.assertIsNone(document.artwork_url)
            self.assertEqual(
                AppleMusicCacheProvider(cache_root=root).artwork_url_for(
                    TrackInfo(True, "playing", "Song", "Artist", "Album", 201.5)
                ),
                "https://is1-ssl.mzstatic.com/320x320.jpg",
            )

    def test_total_cache_payload_scan_is_bounded(self):
        provider = AppleMusicCacheProvider(cache_root=Path("/definitely/missing"))
        provider._database_rows = lambda: [(0, b"1234"), (0, b"{}")]

        with patch.object(apple_cache_module, "_MAX_SCAN_BYTES", 5):
            self.assertEqual(list(provider._candidate_payloads()), ["1234"])

    def test_oversized_sqlite_blobs_are_filtered_before_python_materialization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "fsCachedData").mkdir()
            connection = sqlite3.connect(root / "Cache.db")
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
            connection.execute(
                "INSERT INTO cfurl_cache_response VALUES (1, ?, 1)",
                ("https://amp-api.music.apple.com/syllable-lyrics/1",),
            )
            connection.execute(
                "INSERT INTO cfurl_cache_receiver_data VALUES (1, 0, ?)",
                (b"x" * 1_000_000,),
            )
            connection.commit()
            connection.close()

            provider = AppleMusicCacheProvider(cache_root=root)
            with patch.object(apple_cache_module, "_MAX_SCAN_BYTES", 1):
                self.assertEqual(list(provider._database_rows()), [])

    def test_non_blob_sqlite_row_does_not_hide_a_later_valid_blob(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "fsCachedData").mkdir()
            connection = sqlite3.connect(root / "Cache.db")
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
            request_key = "https://amp-api.music.apple.com/syllable-lyrics/1"
            connection.executemany(
                "INSERT INTO cfurl_cache_response VALUES (?, ?, ?)",
                [(1, request_key, 2), (2, request_key, 1)],
            )
            connection.execute(
                "INSERT INTO cfurl_cache_receiver_data VALUES (1, 0, -1)"
            )
            connection.execute(
                "INSERT INTO cfurl_cache_receiver_data VALUES (2, 0, ?)",
                (b'{"ok": true}',),
            )
            connection.commit()
            connection.close()

            provider = AppleMusicCacheProvider(cache_root=root)

            self.assertEqual(list(provider._candidate_payloads()), ['{"ok": true}'])

    def test_cache_database_symlink_outside_root_is_not_opened(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "cache"
            root.mkdir()
            (root / "fsCachedData").mkdir()
            outside_database = Path(directory) / "outside.db"
            connection = sqlite3.connect(outside_database)
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
                INSERT INTO cfurl_cache_response VALUES (
                    1,
                    'https://amp-api.music.apple.com/syllable-lyrics/1',
                    1
                );
                """
            )
            connection.execute(
                "INSERT INTO cfurl_cache_receiver_data VALUES (1, 0, ?)",
                (b'{"outside": true}',),
            )
            connection.commit()
            connection.close()
            (root / "Cache.db").symlink_to(outside_database)

            provider = AppleMusicCacheProvider(cache_root=root)

            self.assertEqual(list(provider._database_rows()), [])

    def test_cache_file_growth_is_bounded_during_the_read(self):
        class GrowingPath:
            def __init__(self, path):
                self.path = path

            def __fspath__(self):
                return str(self.path)

            def stat(self):
                return type("Stat", (), {"st_size": 1})()

            def read_text(self, **_kwargs):
                return "unbounded-old-path"

        provider = AppleMusicCacheProvider(cache_root=Path("/definitely/missing"))
        with tempfile.TemporaryDirectory() as directory:
            actual = Path(directory) / "growing"
            actual.write_bytes(b"12345")
            path = cast(Path, GrowingPath(actual))
            with patch.object(apple_cache_module, "_MAX_RESPONSE_BYTES", 4):
                self.assertIsNone(provider._read_cache_file(path, 4))

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

    def test_reads_a_bounded_database_file_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "cache"
            data_directory = root / "fsCachedData"
            data_directory.mkdir(parents=True)
            filename = "01234567-89ab-cdef-0123-456789abcdef"
            (data_directory / filename).write_text('{"ok": true}', encoding="utf-8")

            provider = AppleMusicCacheProvider(cache_root=root)

            def database_rows():
                yield 1, filename.encode("utf-8")

            provider._database_rows = database_rows

            self.assertEqual(list(provider._candidate_payloads()), ['{"ok": true}'])

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

    def test_cache_data_directory_symlink_outside_root_is_not_opened(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "cache"
            root.mkdir()
            outside = Path(directory) / "outside-data"
            outside.mkdir()
            filename = "01234567-89ab-cdef-0123-456789abcdef"
            (outside / filename).write_text('{"outside": true}', encoding="utf-8")
            (root / "fsCachedData").symlink_to(outside, target_is_directory=True)

            provider = AppleMusicCacheProvider(cache_root=root)

            def database_rows():
                yield 1, filename.encode("utf-8")

            provider._database_rows = database_rows

            self.assertEqual(list(provider._candidate_payloads()), [])

    def test_recent_cache_enumeration_uses_lazy_bounded_scandir(self):
        counters = {"yielded": 0, "stat": 0}

        class Entry:
            def __init__(self, index):
                self.name = f"{index:036x}"[-36:]
                self.path = f"/fake-cache/{self.name}"

            def is_file(self, *, follow_symlinks=True):
                self.assert_no_follow(follow_symlinks)
                return True

            def stat(self, *, follow_symlinks=True):
                self.assert_no_follow(follow_symlinks)
                counters["stat"] += 1
                return type("Stat", (), {"st_mtime": 5000 - counters["stat"]})()

            @staticmethod
            def assert_no_follow(follow_symlinks):
                if follow_symlinks:
                    raise AssertionError("directory entry checks must not follow symlinks")

        class Scandir:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __iter__(self):
                return self

            def __next__(self):
                if counters["yielded"] >= 5000:
                    raise StopIteration
                index = counters["yielded"]
                counters["yielded"] += 1
                return Entry(index)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "fsCachedData").mkdir()
            provider = AppleMusicCacheProvider(cache_root=root)

            def database_rows():
                yield from ()

            provider._database_rows = database_rows
            with (
                patch.object(
                    os, "listdir", side_effect=AssertionError("eager enumeration")
                ),
                patch.object(os, "scandir", return_value=Scandir()) as scandir,
                patch.object(provider, "_read_cache_file", return_value=None),
            ):
                self.assertEqual(list(provider._candidate_payloads()), [])

            scandir.assert_called_once()
            self.assertIsInstance(scandir.call_args.args[0], int)
        self.assertLessEqual(counters["yielded"], apple_cache_module._MAX_CANDIDATES)
        self.assertLessEqual(counters["stat"], apple_cache_module._MAX_CANDIDATES)

    def test_corrupt_inline_cache_row_does_not_hide_later_candidates(self):
        provider = AppleMusicCacheProvider(cache_root=Path("/definitely/missing"))
        provider._database_rows = lambda: [(0, b"\xff"), (0, b'{"ok": true}')]

        self.assertEqual(list(provider._candidate_payloads()), ['{"ok": true}'])

    def test_json_integer_digit_limit_does_not_hide_later_lyrics_candidate(self):
        malformed = (
            '{"data":[{"attributes":{"durationInMillis":'
            + "9" * 5000
            + "}}]}"
        )
        ttml = '<tt><body><p begin="1s" end="2s">valid</p></body></tt>'
        valid = json.dumps(
            {
                "data": [
                    {
                        "attributes": {
                            "name": "Song",
                            "artistName": "Artist",
                            "albumName": "Album",
                            "durationInMillis": 201500,
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
        )
        provider = AppleMusicCacheProvider(cache_root=Path("/definitely/missing"))

        def candidate_payloads():
            yield malformed
            yield valid

        provider._candidate_payloads = candidate_payloads

        document = provider.lyrics_for(
            TrackInfo(True, "playing", "Song", "Artist", "Album", 201.5)
        )

        self.assertIsNotNone(document)
        assert document is not None
        self.assertEqual(document.lines[0].text, "valid")

    def test_deep_json_does_not_hide_later_lyrics_candidate(self):
        malformed = "[" * 2000 + "0" + "]" * 2000
        ttml = '<tt><body><p begin="1s" end="2s">valid</p></body></tt>'
        valid = json.dumps(
            {
                "data": [
                    {
                        "attributes": {
                            "name": "Song",
                            "artistName": "Artist",
                            "albumName": "Album",
                            "durationInMillis": 201500,
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
        )
        provider = AppleMusicCacheProvider(cache_root=Path("/definitely/missing"))

        def candidate_payloads():
            yield malformed
            yield valid

        provider._candidate_payloads = candidate_payloads

        document = provider.lyrics_for(
            TrackInfo(True, "playing", "Song", "Artist", "Album", 201.5)
        )

        self.assertIsNotNone(document)
        assert document is not None
        self.assertEqual(document.lines[0].text, "valid")

    def test_json_integer_digit_limit_does_not_hide_later_artwork_candidate(self):
        malformed = (
            '{"data":[{"attributes":{"durationInMillis":'
            + "9" * 5000
            + "}}]}"
        )
        valid = json.dumps(
            {
                "data": [
                    {
                        "attributes": {
                            "name": "Song",
                            "artistName": "Artist",
                            "albumName": "Album",
                            "durationInMillis": 201500,
                            "artwork": {
                                "url": "https://is1-ssl.mzstatic.com/320x320.jpg"
                            },
                        }
                    }
                ]
            }
        )
        provider = AppleMusicCacheProvider(cache_root=Path("/definitely/missing"))

        def candidate_payloads():
            yield malformed
            yield valid

        provider._candidate_payloads = candidate_payloads

        artwork_url = provider.artwork_url_for(
            TrackInfo(True, "playing", "Song", "Artist", "Album", 201.5)
        )

        self.assertEqual(
            artwork_url,
            "https://is1-ssl.mzstatic.com/320x320.jpg",
        )

    def test_deep_json_does_not_hide_later_artwork_candidate(self):
        malformed = "[" * 2000 + "0" + "]" * 2000
        valid = json.dumps(
            {
                "data": [
                    {
                        "attributes": {
                            "name": "Song",
                            "artistName": "Artist",
                            "albumName": "Album",
                            "durationInMillis": 201500,
                            "artwork": {
                                "url": "https://is1-ssl.mzstatic.com/320x320.jpg"
                            },
                        }
                    }
                ]
            }
        )
        provider = AppleMusicCacheProvider(cache_root=Path("/definitely/missing"))

        def candidate_payloads():
            yield malformed
            yield valid

        provider._candidate_payloads = candidate_payloads

        artwork_url = provider.artwork_url_for(
            TrackInfo(True, "playing", "Song", "Artist", "Album", 201.5)
        )

        self.assertEqual(
            artwork_url,
            "https://is1-ssl.mzstatic.com/320x320.jpg",
        )

    def test_invalid_cache_file_bytes_still_consume_the_scan_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_directory = root / "fsCachedData"
            data_directory.mkdir()
            later_valid = data_directory / "00000000-0000-0000-0000-000000000001"
            invalid = data_directory / "00000000-0000-0000-0000-000000000002"
            later_valid.write_bytes(b"{}")
            invalid.write_bytes(b"\xff" * 5)
            invalid.touch()

            provider = AppleMusicCacheProvider(cache_root=root)
            with patch.object(apple_cache_module, "_MAX_SCAN_BYTES", 5):
                self.assertEqual(list(provider._candidate_payloads()), [])

    def test_malformed_song_records_do_not_hide_a_later_valid_song(self):
        ttml = '<tt><body><p begin="1s" end="2s">valid</p></body></tt>'
        valid = {
            "attributes": {
                "name": "Song",
                "artistName": "Artist",
                "albumName": "Album",
                "durationInMillis": 201500,
            },
            "relationships": {
                "syllable-lyrics": {
                    "data": [{"attributes": {"ttmlLocalizations": {"en-US": ttml}}}]
                }
            },
        }
        malformed_records = (
            {
                "attributes": {
                    "name": "Song",
                    "artistName": "Artist",
                    "albumName": "Album",
                    "durationInMillis": 10**400,
                },
                "relationships": valid["relationships"],
            },
            {
                "attributes": {
                    "name": "Song",
                    "artistName": "Artist",
                    "albumName": "Album",
                    "durationInMillis": 201500,
                },
                "relationships": "not-an-object",
            },
        )

        for malformed in malformed_records:
            with self.subTest(malformed=malformed):
                provider = AppleMusicCacheProvider(cache_root=Path("/definitely/missing"))

                def candidate_payloads():
                    yield json.dumps({"data": [malformed, valid]})

                provider._candidate_payloads = candidate_payloads

                document = provider.lyrics_for(
                    TrackInfo(True, "playing", "Song", "Artist", "Album", 201.5)
                )

                self.assertIsNotNone(document)
                assert document is not None
                self.assertEqual(document.lines[0].text, "valid")

    def test_ttml_json_integer_digit_limit_is_rejected(self):
        malformed = '{"en-US":' + "9" * 5000 + "}"

        self.assertIsNone(AppleMusicCacheProvider._choose_ttml(malformed))

    def test_ttml_deep_json_is_rejected(self):
        malformed = "[" * 2000 + "0" + "]" * 2000

        self.assertIsNone(AppleMusicCacheProvider._choose_ttml(malformed))

    def test_malformed_high_score_ttml_does_not_hide_a_valid_candidate(self):
        malformed = {
            "attributes": {
                "name": "Song",
                "artistName": "Artist",
                "albumName": "Album",
                "durationInMillis": 201500,
            },
            "relationships": {
                "syllable-lyrics": {
                    "data": [
                        {
                            "attributes": {
                                "ttmlLocalizations": "<tt><body><p begin=\"1s\""
                            }
                        }
                    ]
                }
            },
        }
        valid = {
            "attributes": {
                "name": "Song",
                "artistName": "Artist",
                "durationInMillis": 201500,
            },
            "relationships": {
                "syllable-lyrics": {
                    "data": [
                        {
                            "attributes": {
                                "ttmlLocalizations": (
                                    '<tt><body><p begin="1s" end="2s">valid</p>'
                                    "</body></tt>"
                                )
                            }
                        }
                    ]
                }
            },
        }
        provider = AppleMusicCacheProvider(cache_root=Path("/definitely/missing"))

        def candidate_payloads():
            yield json.dumps({"data": [malformed, valid]})

        provider._candidate_payloads = candidate_payloads

        document = provider.lyrics_for(
            TrackInfo(True, "playing", "Song", "Artist", "Album", 201.5)
        )

        self.assertIsNotNone(document)
        assert document is not None
        self.assertEqual(document.lines[0].text, "valid")

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

    def test_artwork_fallback_does_not_require_accepted_apple_lyrics(self):
        provider = AppleMusicCacheProvider(cache_root=Path("/definitely/missing"))
        payload = {
            "data": [
                {
                    "attributes": {
                        "name": "Song",
                        "artistName": "Artist",
                        "artwork": {
                            "url": "https://is1-ssl.mzstatic.com/{w}x{h}.{f}"
                        },
                    },
                    "relationships": {},
                }
            ]
        }
        provider._candidate_payloads = lambda: iter([json.dumps(payload)])
        track = TrackInfo(True, "playing", "Song", "Artist")

        self.assertIsNone(provider.lyrics_for(track))
        self.assertEqual(
            provider.artwork_url_for(track),
            "https://is1-ssl.mzstatic.com/320x320.jpg",
        )

        next_track = TrackInfo(
            True,
            "playing",
            "Song",
            "Artist",
            persistent_id="A1B2C3D4E5F60708",
        )
        self.assertEqual(
            provider.artwork_url_for(next_track),
            "https://is1-ssl.mzstatic.com/320x320.jpg",
        )
        self.assertEqual(list(provider._artwork_cache), [next_track.identity])

    def test_rejects_non_apple_artwork_hosts(self):
        self.assertIsNone(
            AppleMusicCacheProvider._artwork_url(
                "https://tracker.example/{w}x{h}.{f}"
            )
        )
        self.assertIsNone(
            AppleMusicCacheProvider._artwork_url(
                "https://user@is1-ssl.mzstatic.com/{w}x{h}.{f}"
            )
        )
        self.assertIsNone(
            AppleMusicCacheProvider._artwork_url(
                "https://is1-ssl.mzstatic.com:444/{w}x{h}.{f}"
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

        wrong_album = {
            "name": "Song",
            "artistName": "Artist",
            "albumName": "Unrelated Compilation",
            "durationInMillis": 201500,
        }
        self.assertIsNone(
            provider.match_score(
                wrong_album,
                TrackInfo(True, "playing", "Song", "Artist", "Album", 201.5),
            )
        )


if __name__ == "__main__":
    unittest.main()
