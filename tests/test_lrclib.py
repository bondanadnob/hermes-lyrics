import json
import unittest

from dashboard.apple_music_lyrics_backend.lrclib import (
    HTTPResponse,
    LRCLIBProvider,
    _fetch,
    _NoRedirectHandler,
)
from dashboard.apple_music_lyrics_backend.models import TrackInfo


class LRCLIBProviderTests(unittest.TestCase):
    def test_default_http_client_rejects_non_lrclib_urls_and_redirects(self):
        response = _fetch("data:application/json,%7B%7D", {}, 1.0)
        self.assertEqual(response.status, 0)
        lookalike = _fetch("https://lrclib.net.evil.test/api", {}, 1.0)
        self.assertEqual(lookalike.status, 0)
        redirect = _NoRedirectHandler().redirect_request(
            None,
            None,
            302,
            "Found",
            {},
            "https://example.test",
        )
        self.assertIsNone(redirect)

    def test_exact_lookup_returns_parsed_synced_lyrics(self):
        seen_urls = []

        def fetch(url, _headers, _timeout):
            seen_urls.append(url)
            return HTTPResponse(
                200,
                json.dumps(
                    {
                        "trackName": "Song",
                        "artistName": "Artist",
                        "albumName": "Album",
                        "duration": 201.4,
                        "syncedLyrics": "[00:01.00]one\n[00:03.00]three",
                        "plainLyrics": "one\nthree",
                    }
                ),
            )

        document = LRCLIBProvider(fetcher=fetch).lyrics_for(
            TrackInfo(True, "playing", "Song", "Artist", "Album", 201.5)
        )

        self.assertIsNotNone(document)
        self.assertTrue(document.synced)
        self.assertEqual(document.source, "LRCLIB")
        self.assertEqual([line.text for line in document.lines], ["one", "three"])
        self.assertIn("track_name=Song", seen_urls[0])
        self.assertIn("duration=202", seen_urls[0])

    def test_search_scores_metadata_instead_of_taking_first_result(self):
        wrong = {
            "trackName": "Song (Live)",
            "artistName": "Wrong Artist",
            "albumName": "Other",
            "duration": 260,
            "syncedLyrics": "[00:01.00]wrong",
        }
        right = {
            "trackName": "Song",
            "artistName": "Artist",
            "albumName": "Album",
            "duration": 201.2,
            "syncedLyrics": "[00:01.00]right",
            "id": 2,
        }

        def fetch(url, _headers, _timeout):
            if "/api/get?" in url:
                return HTTPResponse(404, "")
            return HTTPResponse(200, json.dumps([wrong, right]))

        document = LRCLIBProvider(fetcher=fetch).lyrics_for(
            TrackInfo(True, "playing", "Song", "Artist", "Album", 201.5)
        )

        self.assertEqual(document.lines[0].text, "right")

    def test_huge_exact_duration_is_isolated_and_search_continues(self):
        malformed_exact = {
            "trackName": "Song",
            "artistName": "Artist",
            "albumName": "Album",
            "duration": 10**400,
            "syncedLyrics": "[00:01.00]malformed",
        }
        valid_search = {
            "trackName": "Song",
            "artistName": "Artist",
            "albumName": "Album",
            "duration": 201.4,
            "syncedLyrics": "[00:01.00]valid",
        }

        def fetch(url, _headers, _timeout):
            result = malformed_exact if "/api/get?" in url else [valid_search]
            return HTTPResponse(200, json.dumps(result))

        document = LRCLIBProvider(fetcher=fetch).lyrics_for(
            TrackInfo(True, "playing", "Song", "Artist", "Album", 201.5)
        )

        self.assertIsNotNone(document)
        assert document is not None
        self.assertEqual(document.lines[0].text, "valid")

    def test_json_integer_digit_limit_in_exact_isolated_and_search_continues(self):
        malformed_exact = (
            '{"trackName":"Song","artistName":"Artist","albumName":"Album",'
            '"duration":'
            + "9" * 5000
            + ',"syncedLyrics":"[00:01.00]malformed"}'
        )
        valid_search = {
            "trackName": "Song",
            "artistName": "Artist",
            "albumName": "Album",
            "duration": 201.4,
            "syncedLyrics": "[00:01.00]valid",
        }

        def fetch(url, _headers, _timeout):
            if "/api/get?" in url:
                return HTTPResponse(200, malformed_exact)
            return HTTPResponse(200, json.dumps([valid_search]))

        document = LRCLIBProvider(fetcher=fetch).lyrics_for(
            TrackInfo(True, "playing", "Song", "Artist", "Album", 201.5)
        )

        self.assertIsNotNone(document)
        assert document is not None
        self.assertEqual(document.lines[0].text, "valid")

    def test_deep_json_in_exact_isolated_and_search_continues(self):
        malformed_exact = "[" * 2000 + "0" + "]" * 2000
        valid_search = {
            "trackName": "Song",
            "artistName": "Artist",
            "albumName": "Album",
            "duration": 201.4,
            "syncedLyrics": "[00:01.00]valid",
        }
        calls = []

        def fetch(url, _headers, _timeout):
            calls.append(url)
            if "/api/get?" in url:
                return HTTPResponse(200, malformed_exact)
            return HTTPResponse(200, json.dumps([valid_search]))

        document = LRCLIBProvider(fetcher=fetch).lyrics_for(
            TrackInfo(True, "playing", "Song", "Artist", "Album", 201.5)
        )

        self.assertIsNotNone(document)
        assert document is not None
        self.assertEqual(document.lines[0].text, "valid")
        self.assertEqual(len(calls), 2)

    def test_json_integer_digit_limit_in_search_returns_no_document(self):
        malformed_search = '[{"duration":' + "9" * 5000 + "}]"
        calls = []

        def fetch(url, _headers, _timeout):
            calls.append(url)
            if "/api/get?" in url:
                return HTTPResponse(404, "")
            return HTTPResponse(200, malformed_search)

        document = LRCLIBProvider(fetcher=fetch).lyrics_for(
            TrackInfo(True, "playing", "Song", "Artist", "Album", 201.5)
        )

        self.assertIsNone(document)
        self.assertEqual(len(calls), 2)

    def test_deep_json_in_search_returns_no_document(self):
        malformed_search = "[" * 2000 + "0" + "]" * 2000
        calls = []

        def fetch(url, _headers, _timeout):
            calls.append(url)
            if "/api/get?" in url:
                return HTTPResponse(404, "")
            return HTTPResponse(200, malformed_search)

        document = LRCLIBProvider(fetcher=fetch).lyrics_for(
            TrackInfo(True, "playing", "Song", "Artist", "Album", 201.5)
        )

        self.assertIsNone(document)
        self.assertEqual(len(calls), 2)

    def test_oversized_exact_timestamp_is_isolated_and_search_continues(self):
        malformed_exact = {
            "trackName": "Song",
            "artistName": "Artist",
            "albumName": "Album",
            "duration": 201.5,
            "syncedLyrics": f"[{'9' * 5000}:00.00]malformed",
        }
        valid_search = {
            "trackName": "Song",
            "artistName": "Artist",
            "albumName": "Album",
            "duration": 201.4,
            "syncedLyrics": "[00:01.00]valid",
        }

        def fetch(url, _headers, _timeout):
            result = malformed_exact if "/api/get?" in url else [valid_search]
            return HTTPResponse(200, json.dumps(result))

        document = LRCLIBProvider(fetcher=fetch).lyrics_for(
            TrackInfo(True, "playing", "Song", "Artist", "Album", 201.5)
        )

        self.assertIsNotNone(document)
        assert document is not None
        self.assertEqual(document.lines[0].text, "valid")

    def test_oversized_search_timestamp_is_isolated_and_search_continues(self):
        malformed_search = {
            "trackName": "Song",
            "artistName": "Artist",
            "albumName": "Album",
            "duration": 201.5,
            "syncedLyrics": f"[{'9' * 401}:00.00]malformed",
        }
        valid_search = {
            "trackName": "Song",
            "artistName": "Artist",
            "albumName": "Album",
            "duration": 201.4,
            "syncedLyrics": "[00:01.00]valid",
        }

        def fetch(url, _headers, _timeout):
            if "/api/get?" in url:
                return HTTPResponse(404, "")
            return HTTPResponse(200, json.dumps([malformed_search, valid_search]))

        document = LRCLIBProvider(fetcher=fetch).lyrics_for(
            TrackInfo(True, "playing", "Song", "Artist", "Album", 201.5)
        )

        self.assertIsNotNone(document)
        assert document is not None
        self.assertEqual(document.lines[0].text, "valid")

    def test_search_prefers_a_strong_synced_match_over_exact_plain_lyrics(self):
        exact_plain = {
            "trackName": "Song",
            "artistName": "Artist",
            "albumName": "Album",
            "duration": 201.5,
            "plainLyrics": "plain only",
        }
        search_synced = {
            "trackName": "Song",
            "artistName": "Artist",
            "albumName": "Album",
            "duration": 201.4,
            "syncedLyrics": "[00:01.00]synced",
        }

        def fetch(url, _headers, _timeout):
            if "/api/get?" in url:
                return HTTPResponse(200, json.dumps(exact_plain))
            return HTTPResponse(200, json.dumps([exact_plain, search_synced]))

        document = LRCLIBProvider(fetcher=fetch).lyrics_for(
            TrackInfo(True, "playing", "Song", "Artist", "Album", 201.5)
        )

        self.assertTrue(document.synced)
        self.assertEqual(document.lines[0].text, "synced")

    def test_plain_exact_match_does_not_override_music_app_plain_fallback(self):
        exact_plain = {
            "trackName": "Song",
            "artistName": "Artist",
            "albumName": "Album",
            "duration": 180,
            "plainLyrics": "Exact plain",
            "syncedLyrics": None,
        }
        weaker_synced = {
            "trackName": "Song",
            "artistName": "Artist",
            "albumName": "",
            "duration": 0,
            "plainLyrics": "Weaker",
            "syncedLyrics": "[00:01.00]Weaker",
        }
        responses = iter(
            [
                HTTPResponse(200, json.dumps(exact_plain)),
                HTTPResponse(200, json.dumps([weaker_synced])),
            ]
        )
        provider = LRCLIBProvider(fetcher=lambda *_args: next(responses))

        document = provider.lyrics_for(
            TrackInfo(True, "playing", "Song", "Artist", "Album", 180)
        )

        self.assertIsNone(document)

    def test_plain_lrclib_lyrics_are_not_an_unsynchronized_fallback(self):
        provider = LRCLIBProvider(
            fetcher=lambda *_args: HTTPResponse(
                200,
                json.dumps(
                    {
                        "trackName": "Song",
                        "artistName": "Artist",
                        "plainLyrics": "first\n\nsecond",
                    }
                ),
            )
        )

        document = provider.lyrics_for(TrackInfo(True, "playing", "Song", "Artist"))

        self.assertIsNone(document)

    def test_weak_exact_response_is_rejected_and_search_is_attempted(self):
        calls = []
        responses = iter(
            [
                HTTPResponse(
                    200,
                    json.dumps(
                        {
                            "trackName": "Song Live",
                            "artistName": "Artist Tribute",
                            "albumName": "Album",
                            "syncedLyrics": "[00:01.00]wrong version",
                        }
                    ),
                ),
                HTTPResponse(200, "[]"),
            ]
        )

        def fetcher(url, headers, timeout):
            del headers, timeout
            calls.append(url)
            return next(responses)

        document = LRCLIBProvider(fetcher=fetcher).lyrics_for(
            TrackInfo(True, "playing", "Song", "Artist", "Album", 200)
        )

        self.assertIsNone(document)
        self.assertTrue(any("/search?" in url for url in calls))


if __name__ == "__main__":
    unittest.main()
