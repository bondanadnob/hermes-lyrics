import base64
import json
import subprocess
import unittest
import zlib
from typing import cast

from dashboard.apple_music_lyrics_backend.errors import (
    ArtworkStaleIdentityError,
    ArtworkTransientError,
)
from dashboard.apple_music_lyrics_backend.models import TrackInfo
from dashboard.apple_music_lyrics_backend.music import (
    ARTWORK_IDENTITY_SCRIPT,
    ARTWORK_SCRIPT,
    NOW_PLAYING_SCRIPT,
    MusicClient,
    ProcessResult,
    _run_jxa,
)

_JPEG_1X1 = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsK"
    "CwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRT/2wBDAQMEBAUEBQkFBQkUDQsNFBQUFBQU"
    "FBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBT/wAARCAABAAEDASIA"
    "AhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQA"
    "AAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3"
    "ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWm"
    "p6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEA"
    "AwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSEx"
    "BhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElK"
    "U1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3"
    "uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD8+qKK"
    "K+oPnD//2Q=="
)
_JPEG_SOF_MARKERS = {
    0xC0,
    0xC1,
    0xC2,
    0xC3,
    0xC5,
    0xC6,
    0xC7,
    0xC9,
    0xCA,
    0xCB,
    0xCD,
    0xCE,
    0xCF,
}


def _jpeg_sof_offset(image: bytes | bytearray) -> int:
    for offset in range(len(image) - 1):
        if image[offset] == 0xFF and image[offset + 1] in _JPEG_SOF_MARKERS:
            return offset
    raise AssertionError("JPEG fixture has no start-of-frame marker")


def _jpeg_image(width: int = 1, height: int = 1) -> bytes:
    image = bytearray(_JPEG_1X1)
    offset = _jpeg_sof_offset(image)
    image[offset + 5 : offset + 7] = height.to_bytes(2, "big")
    image[offset + 7 : offset + 9] = width.to_bytes(2, "big")
    return bytes(image)


def _jpeg_sof_segment(image: bytes) -> bytes:
    offset = _jpeg_sof_offset(image)
    length = int.from_bytes(image[offset + 2 : offset + 4], "big")
    return image[offset : offset + 2 + length]


def _broken_jpeg_scan() -> bytes:
    return (
        b"\xff\xd8\xff\xc0\x00\x11\x08\x00\x01\x00\x01"
        b"\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00"
        b"\xff\xda\x00\x0c\x03\x01\x00\x02\x00\x03\x00\x00\x3f\x00"
        b"\x00\xff\xd9"
    )


def _png_image(width: int = 1, height: int = 1, animated: bool = False) -> bytes:
    image = bytearray(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    ))
    image[16:20] = width.to_bytes(4, "big")
    image[20:24] = height.to_bytes(4, "big")
    image[29:33] = (zlib.crc32(image[12:29]) & 0xFFFFFFFF).to_bytes(4, "big")
    if animated:
        chunk_type = b"acTL"
        chunk_data = (1).to_bytes(4, "big") + (0).to_bytes(4, "big")
        chunk = (
            len(chunk_data).to_bytes(4, "big")
            + chunk_type
            + chunk_data
            + (zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF).to_bytes(4, "big")
        )
        image[33:33] = chunk
    return bytes(image)


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    return (
        len(data).to_bytes(4, "big")
        + chunk_type
        + data
        + (zlib.crc32(chunk_type + data) & 0xFFFFFFFF).to_bytes(4, "big")
    )


def _rgba_png(
    *,
    include_idat: bool = True,
    compression: int = 0,
    idat_data: bytes | None = None,
) -> bytes:
    ihdr = (
        (1).to_bytes(4, "big")
        + (1).to_bytes(4, "big")
        + bytes([8, 6, compression, 0, 0])
    )
    chunks = [_png_chunk(b"IHDR", ihdr)]
    if include_idat:
        compressed = (
            idat_data
            if idat_data is not None
            else zlib.compress(b"\x00\x00\x00\x00\xff")
        )
        chunks.append(_png_chunk(b"IDAT", compressed))
    chunks.append(_png_chunk(b"IEND", b""))
    return b"\x89PNG\r\n\x1a\n" + b"".join(chunks)


def _process_result(payload) -> ProcessResult:
    serialized = payload
    if isinstance(payload, dict) and "rawArtwork" in payload:
        serialized = dict(payload)
        serialized.setdefault("artworkReadError", False)
        if "after" not in serialized:
            serialized["after"] = {
                key: payload.get(key)
                for key in (
                    "running",
                    "state",
                    "title",
                    "artist",
                    "album",
                    "duration",
                    "persistentId",
                    "databaseId",
                )
            }
    return ProcessResult(0, json.dumps(serialized), "")


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
            "persistentId": "A1B2C3D4E5F60708",
            "databaseId": "123",
        }
        calls = []

        def runner(script):
            calls.append(script)
            return _process_result(payload)

        track = MusicClient(runner=runner, clock=lambda: 1234.5).sample()

        self.assertTrue(calls)
        self.assertEqual(track.title, "Song")
        self.assertEqual(track.position, 42.25)
        self.assertEqual(track.sampled_at, 1234.5)
        self.assertEqual(track.plain_lyrics, "plain words")
        self.assertEqual(track.persistent_id, "A1B2C3D4E5F60708")
        self.assertEqual(track.database_id, "123")
        self.assertRegex(track.identity, r"^[0-9a-f]{24}$")
        self.assertEqual(track.to_dict()["identity"], track.identity)
        self.assertNotIn("plain_lyrics", track.to_dict())
        self.assertNotIn("persistent_id", track.to_dict())
        self.assertNotIn("database_id", track.to_dict())

    def test_sample_sanitizes_integer_too_large_for_float(self):
        payload = {
            "running": True,
            "state": "playing",
            "title": "Song",
            "artist": "Artist",
            "album": "Album",
            "duration": 10**400,
            "position": 42,
        }
        client = MusicClient(runner=lambda _script: _process_result(payload))

        track = client.sample()

        self.assertEqual(track.duration, 0.0)
        self.assertEqual(track.position, 42.0)

    def test_sample_sanitizes_boolean_numeric_fields(self):
        payload = {
            "running": True,
            "state": "playing",
            "title": "Song",
            "artist": "Artist",
            "album": "Album",
            "duration": True,
            "position": False,
        }
        client = MusicClient(runner=lambda _script: _process_result(payload))

        track = client.sample()

        self.assertEqual(track.duration, 0.0)
        self.assertEqual(track.position, 0.0)

    def test_sample_json_integer_digit_limit_is_an_invalid_response(self):
        stdout = '{"running":true,"duration":' + "9" * 5000 + "}"
        client = MusicClient(
            runner=lambda _script: ProcessResult(0, stdout, ""),
            clock=lambda: 1234.5,
        )

        track = client.sample()

        self.assertFalse(track.running)
        self.assertEqual(track.error, "invalid_music_response")
        self.assertEqual(track.sampled_at, 1234.5)

    def test_sample_deep_json_is_an_invalid_response(self):
        stdout = "[" * 2000 + "0" + "]" * 2000
        client = MusicClient(
            runner=lambda _script: ProcessResult(0, stdout, ""),
            clock=lambda: 1234.5,
        )

        track = client.sample()

        self.assertFalse(track.running)
        self.assertEqual(track.error, "invalid_music_response")
        self.assertEqual(track.sampled_at, 1234.5)

    def test_music_persistent_id_distinguishes_metadata_equivalent_tracks(self):
        first = TrackInfo(
            True,
            "playing",
            "Song",
            "Artist",
            "Album",
            201.5,
            persistent_id="A1B2C3D4E5F60708",
        )
        second = TrackInfo(
            True,
            "playing",
            "Song",
            "Artist",
            "Album",
            201.5,
            persistent_id="F8E7D6C5B4A30201",
        )

        self.assertNotEqual(first.identity, second.identity)
        self.assertEqual(first.key, second.key)

    def test_metadata_identity_fallback_uses_millisecond_duration(self):
        first = TrackInfo(True, "playing", "Song", "Artist", "Album", 201.5)
        second = TrackInfo(True, "playing", "Song", "Artist", "Album", 201.9)

        self.assertEqual(first.key, second.key)
        self.assertNotEqual(first.identity, second.identity)

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

    def test_reads_current_music_artwork_as_an_ephemeral_data_url(self):
        image = _jpeg_image()
        payload = {
            "running": True,
            "state": "playing",
            "title": "Song",
            "artist": "Artist",
            "album": "Album",
            "duration": 201.5,
            "rawArtwork": f"'tdta'(${image.hex().upper()}$)",
        }
        client = MusicClient(
            runner=lambda _script: _process_result(payload)
        )
        track = TrackInfo(True, "playing", "Song", "Artist", "Album", 201.5)

        artwork = client.artwork_for(track.identity)

        self.assertIsNotNone(artwork)
        self.assertEqual(artwork.identity, track.identity)
        self.assertEqual(artwork.mime, "image/jpeg")
        self.assertEqual(artwork.byte_length, len(image))
        encoded = artwork.data_url.split(",", 1)[1]
        self.assertEqual(base64.b64decode(encoded), image)

    def test_accepts_png_artwork_from_music(self):
        image = _png_image()
        payload = {
            "running": True,
            "state": "playing",
            "title": "Song",
            "artist": "Artist",
            "album": "Album",
            "duration": 201.5,
            "rawArtwork": f"'tdta'(${image.hex().upper()}$)",
        }
        client = MusicClient(
            runner=lambda _script: _process_result(payload)
        )
        track = TrackInfo(True, "playing", "Song", "Artist", "Album", 201.5)

        artwork = client.artwork_for(track.identity)

        self.assertIsNotNone(artwork)
        assert artwork is not None
        self.assertEqual(artwork.mime, "image/png")
        self.assertTrue(artwork.data_url.startswith("data:image/png;base64,"))

    def test_rejects_artwork_with_unsafe_pixel_dimensions(self):
        image = _jpeg_image(width=2049, height=2049)
        payload = {
            "running": True,
            "state": "playing",
            "title": "Song",
            "artist": "Artist",
            "album": "Album",
            "duration": 201.5,
            "rawArtwork": f"'tdta'(${image.hex().upper()}$)",
        }
        client = MusicClient(
            runner=lambda _script: _process_result(payload)
        )
        track = TrackInfo(True, "playing", "Song", "Artist", "Album", 201.5)

        self.assertIsNone(client.artwork_for(track.identity))

    def test_rejects_animated_and_oversized_png_artwork(self):
        track = TrackInfo(True, "playing", "Song", "Artist", "Album", 201.5)

        def client_for(image):
            payload = {
                "running": True,
                "state": "playing",
                "title": "Song",
                "artist": "Artist",
                "album": "Album",
                "duration": 201.5,
                "rawArtwork": f"'tdta'(${image.hex().upper()}$)",
            }
            return MusicClient(
                runner=lambda _script: _process_result(payload)
            )

        self.assertIsNone(client_for(_png_image(animated=True)).artwork_for(track.identity))
        self.assertIsNone(
            client_for(_png_image(width=5000, height=5000)).artwork_for(track.identity)
        )

        base = _png_image()
        orphan_frame_control = base[:33] + _png_chunk(b"fcTL", b"\x00" * 26) + base[33:]
        orphan_frame_data = base[:33] + _png_chunk(b"fdAT", b"\x00" * 8) + base[33:]
        compressed_profile = (
            base[:33]
            + _png_chunk(b"iCCP", b"profile\x00\x00" + zlib.compress(b"x" * 2_000_001))
            + base[33:]
        )
        compressed_text = (
            base[:33]
            + _png_chunk(b"zTXt", b"comment\x00\x00" + zlib.compress(b"x" * 2_000_001))
            + base[33:]
        )
        self.assertIsNone(client_for(orphan_frame_control).artwork_for(track.identity))
        self.assertIsNone(client_for(orphan_frame_data).artwork_for(track.identity))
        self.assertIsNone(client_for(compressed_profile).artwork_for(track.identity))
        self.assertIsNone(client_for(compressed_text).artwork_for(track.identity))

    def test_rejects_malformed_jpeg_structure(self):
        image = b"\xff\xd8\xff\xe0not-a-complete-jpeg"
        payload = {
            "running": True,
            "state": "playing",
            "title": "Song",
            "artist": "Artist",
            "album": "Album",
            "duration": 201.5,
            "rawArtwork": f"'tdta'(${image.hex().upper()}$)",
        }
        client = MusicClient(
            runner=lambda _script: _process_result(payload)
        )
        track = TrackInfo(True, "playing", "Song", "Artist", "Album", 201.5)

        self.assertIsNone(client.artwork_for(track.identity))

        concatenated = _jpeg_image() + _jpeg_image()[2:]
        payload["rawArtwork"] = f"'tdta'(${concatenated.hex().upper()}$)"
        self.assertIsNone(client.artwork_for(track.identity))

        double_soi = b"\xff\xd8" + _jpeg_image()
        payload["rawArtwork"] = f"'tdta'(${double_soi.hex().upper()}$)"
        self.assertIsNone(client.artwork_for(track.identity))

        truncated_then_full = _jpeg_image()[:-2] + _jpeg_image()
        payload["rawArtwork"] = f"'tdta'(${truncated_then_full.hex().upper()}$)"
        self.assertIsNone(client.artwork_for(track.identity))

        unsafe_sof = _jpeg_sof_segment(_jpeg_image(width=2049, height=2049))
        duplicate_sof = b"\xff\xd8" + unsafe_sof + _jpeg_image()[2:]
        payload["rawArtwork"] = f"'tdta'(${duplicate_sof.hex().upper()}$)"
        self.assertIsNone(client.artwork_for(track.identity))

        post_scan_sof = _jpeg_image()[:-2] + unsafe_sof + b"\xff\xd9"
        payload["rawArtwork"] = f"'tdta'(${post_scan_sof.hex().upper()}$)"
        self.assertIsNone(client.artwork_for(track.identity))

    def test_rejects_undecodable_jpeg_scan(self):
        image = _broken_jpeg_scan()
        payload = {
            "running": True,
            "state": "playing",
            "title": "Song",
            "artist": "Artist",
            "album": "Album",
            "duration": 201.5,
            "rawArtwork": f"'tdta'(${image.hex().upper()}$)",
        }
        client = MusicClient(runner=lambda _script: _process_result(payload))
        track = TrackInfo(True, "playing", "Song", "Artist", "Album", 201.5)

        self.assertIsNone(client.artwork_for(track.identity))

    def test_rejects_png_without_decodable_static_image_data(self):
        track = TrackInfo(True, "playing", "Song", "Artist", "Album", 201.5)

        def artwork_for(image: bytes):
            payload = {
                "running": True,
                "state": "playing",
                "title": "Song",
                "artist": "Artist",
                "album": "Album",
                "duration": 201.5,
                "rawArtwork": f"'tdta'(${image.hex().upper()}$)",
            }
            return MusicClient(
                runner=lambda _script: _process_result(payload)
            ).artwork_for(track.identity)

        self.assertIsNone(artwork_for(_rgba_png(include_idat=False)))
        self.assertIsNone(artwork_for(_rgba_png(compression=1)))
        self.assertIsNone(artwork_for(_rgba_png(idat_data=b"not-zlib-data")))
        valid = _rgba_png()
        forbidden_transparency = valid[:33] + _png_chunk(b"tRNS", b"\x00") + valid[33:]
        self.assertIsNone(artwork_for(forbidden_transparency))

    def test_rejects_artwork_if_music_changes_tracks_during_the_request(self):
        image = _jpeg_image()
        payload = {
            "running": True,
            "state": "playing",
            "title": "Different Song",
            "artist": "Different Artist",
            "album": "Different Album",
            "duration": 190,
            "rawArtwork": f"'tdta'(${image.hex().upper()}$)",
        }
        client = MusicClient(
            runner=lambda _script: _process_result(payload)
        )
        requested = TrackInfo(True, "playing", "Song", "Artist", "Album", 201.5)

        with self.assertRaises(ArtworkStaleIdentityError):
            client.artwork_for(requested.identity)

    def test_rejects_artwork_if_music_changes_during_extraction(self):
        image = _jpeg_image()
        payload = {
            "running": True,
            "state": "playing",
            "title": "Song",
            "artist": "Artist",
            "album": "Album",
            "duration": 201.5,
            "persistentId": "A1B2C3D4E5F60708",
            "rawArtwork": f"'tdta'(${image.hex().upper()}$)",
            "after": {
                "running": True,
                "state": "playing",
                "title": "Song",
                "artist": "Artist",
                "album": "Album",
                "duration": 201.5,
                "persistentId": "F8E7D6C5B4A30201",
            },
        }
        client = MusicClient(runner=lambda _script: _process_result(payload))
        requested = TrackInfo(
            True,
            "playing",
            "Song",
            "Artist",
            "Album",
            201.5,
            persistent_id="A1B2C3D4E5F60708",
        )

        with self.assertRaises(ArtworkStaleIdentityError):
            client.artwork_for(requested.identity)

    def test_rejects_artwork_over_the_configured_memory_limit(self):
        image = _jpeg_image()
        payload = {
            "running": True,
            "state": "playing",
            "title": "Song",
            "artist": "Artist",
            "album": "Album",
            "duration": 201.5,
            "rawArtwork": f"'tdta'(${image.hex().upper()}$)",
        }
        client = MusicClient(
            runner=lambda _script: _process_result(payload),
            max_artwork_bytes=8,
        )
        track = TrackInfo(True, "playing", "Song", "Artist", "Album", 201.5)

        self.assertIsNone(client.artwork_for(track.identity))

    def test_default_artwork_limit_accepts_2000000_bytes_and_rejects_2000001(self):
        base = _jpeg_image()
        track = TrackInfo(True, "playing", "Song", "Artist", "Album", 201.5)

        def result_for(size):
            image = base[:-2] + (b"\x00" * (size - len(base))) + base[-2:]
            payload = {
                "running": True,
                "state": "playing",
                "title": "Song",
                "artist": "Artist",
                "album": "Album",
                "duration": 201.5,
                "rawArtwork": f"'tdta'(${image.hex().upper()}$)",
            }
            return _process_result(payload)

        accepted = MusicClient(runner=lambda _script: result_for(2_000_000))
        rejected = MusicClient(runner=lambda _script: result_for(2_000_001))

        artwork = accepted.artwork_for(track.identity)
        self.assertIsNotNone(artwork)
        assert artwork is not None
        self.assertEqual(artwork.byte_length, 2_000_000)
        self.assertIsNone(rejected.artwork_for(track.identity))

    def test_jxa_caps_artwork_before_serializing_it_to_stdout(self):
        self.assertIn("raw.length <= 4000016", ARTWORK_SCRIPT)

    def test_artwork_read_error_is_transient_and_not_negative_cached(self):
        current = {
            "running": True,
            "state": "playing",
            "title": "Song",
            "artist": "Artist",
            "album": "Album",
            "duration": 201.5,
            "persistentId": "A1B2C3D4E5F60708",
            "databaseId": "123",
        }
        failed_extraction = {
            **current,
            "rawArtwork": "",
            "artworkReadError": True,
            "after": current,
        }
        calls = []

        def runner(script):
            calls.append(script)
            payload = failed_extraction if script == ARTWORK_SCRIPT else current
            return _process_result(payload)

        client = MusicClient(runner=runner)
        track = TrackInfo(
            True,
            "playing",
            "Song",
            "Artist",
            "Album",
            201.5,
            persistent_id="A1B2C3D4E5F60708",
            database_id="123",
        )

        self.assertIn("artworkReadError: false", ARTWORK_SCRIPT)
        self.assertIn("out.artworkReadError = true", ARTWORK_SCRIPT)
        for _ in range(2):
            with self.assertRaises(ArtworkTransientError):
                client.artwork_for(track.identity)
        self.assertEqual(calls, [ARTWORK_SCRIPT, ARTWORK_SCRIPT])

    def test_jxa_runner_rejects_oversized_stdout_before_json_parsing(self):
        with self.assertRaises(subprocess.SubprocessError):
            _run_jxa("'x'.repeat(4200000)")

    def test_non_object_artwork_response_is_a_transient_failure(self):
        client = MusicClient(runner=lambda _script: ProcessResult(0, "[]", ""))

        with self.assertRaises(ArtworkTransientError):
            client.artwork_for("a" * 24)

    def test_artwork_json_integer_digit_limit_is_a_transient_failure(self):
        stdout = '{"duration":' + "9" * 5000 + "}"
        client = MusicClient(runner=lambda _script: ProcessResult(0, stdout, ""))

        with self.assertRaises(ArtworkTransientError):
            client.artwork_for("a" * 24)

    def test_artwork_deep_json_is_a_transient_failure(self):
        stdout = "[" * 2000 + "0" + "]" * 2000
        client = MusicClient(runner=lambda _script: ProcessResult(0, stdout, ""))

        with self.assertRaises(ArtworkTransientError):
            client.artwork_for("a" * 24)

    def test_native_artwork_timeout_is_a_transient_failure(self):
        def runner(_script):
            raise subprocess.TimeoutExpired("osascript", 5)

        with self.assertRaises(ArtworkTransientError):
            MusicClient(runner=runner).artwork_for("a" * 24)

    def test_caches_only_the_current_artwork_in_memory(self):
        image = _jpeg_image()
        payload = {
            "running": True,
            "state": "playing",
            "title": "Song",
            "artist": "Artist",
            "album": "Album",
            "duration": 201.5,
            "rawArtwork": f"'tdta'(${image.hex().upper()}$)",
        }
        calls = []

        def runner(script):
            calls.append("identity" if script == ARTWORK_IDENTITY_SCRIPT else "artwork")
            return _process_result(payload)

        client = MusicClient(runner=runner)
        track = TrackInfo(True, "playing", "Song", "Artist", "Album", 201.5)

        first = client.artwork_for(track.identity)
        second = client.artwork_for(track.identity)

        self.assertIs(first, second)
        self.assertEqual(calls, ["artwork", "identity"])

    def test_definitive_artwork_miss_is_cached_until_manual_refresh(self):
        current = {
            "running": True,
            "state": "playing",
            "title": "Song",
            "artist": "Artist",
            "album": "Album",
            "duration": 201.5,
        }
        extraction = {**current, "rawArtwork": None, "after": current}
        calls = []

        def runner(script):
            kind = "identity" if script == ARTWORK_IDENTITY_SCRIPT else "artwork"
            calls.append(kind)
            return _process_result(current if kind == "identity" else extraction)

        client = MusicClient(runner=runner)
        track = TrackInfo(True, "playing", "Song", "Artist", "Album", 201.5)

        self.assertIsNone(client.artwork_for(track.identity))
        self.assertIsNone(client.artwork_for(track.identity))
        self.assertEqual(calls, ["artwork", "identity"])

        client.clear_artwork_cache()
        self.assertIsNone(client.artwork_for(track.identity))
        self.assertEqual(calls, ["artwork", "identity", "artwork"])

    def test_cache_hit_revalidates_the_current_music_item(self):
        image = _jpeg_image()
        first_payload = {
            "running": True,
            "state": "playing",
            "title": "Song",
            "artist": "Artist",
            "album": "Album",
            "duration": 201.5,
            "persistentId": "A1B2C3D4E5F60708",
            "rawArtwork": f"'tdta'(${image.hex().upper()}$)",
        }
        changed_payload = {
            "running": True,
            "state": "playing",
            "title": "Song",
            "artist": "Artist",
            "album": "Album",
            "duration": 201.5,
            "persistentId": "F8E7D6C5B4A30201",
        }
        payloads = [first_payload, changed_payload]
        calls = []

        def runner(_script):
            calls.append("jxa")
            return _process_result(payloads.pop(0))

        client = MusicClient(runner=runner)
        track = TrackInfo(
            True,
            "playing",
            "Song",
            "Artist",
            "Album",
            201.5,
            persistent_id="A1B2C3D4E5F60708",
        )

        self.assertIsNotNone(client.artwork_for(track.identity))
        with self.assertRaises(ArtworkStaleIdentityError):
            client.artwork_for(track.identity)
        self.assertEqual(calls, ["jxa", "jxa"])
        self.assertIsNone(client._artwork_cache)

    def test_cached_artwork_identity_json_digit_limit_is_transient(self):
        image = _jpeg_image()
        first_payload = {
            "running": True,
            "state": "playing",
            "title": "Song",
            "artist": "Artist",
            "album": "Album",
            "duration": 201.5,
            "persistentId": "A1B2C3D4E5F60708",
            "rawArtwork": f"'tdta'(${image.hex().upper()}$)",
        }
        malformed_identity = '{"duration":' + "9" * 5000 + "}"
        calls = []

        def runner(script):
            calls.append(script)
            if script == ARTWORK_SCRIPT:
                return _process_result(first_payload)
            return ProcessResult(0, malformed_identity, "")

        client = MusicClient(runner=runner)
        track = TrackInfo(
            True,
            "playing",
            "Song",
            "Artist",
            "Album",
            201.5,
            persistent_id="A1B2C3D4E5F60708",
        )

        self.assertIsNotNone(client.artwork_for(track.identity))
        with self.assertRaises(ArtworkTransientError):
            client.artwork_for(track.identity)
        self.assertEqual(calls, [ARTWORK_SCRIPT, ARTWORK_IDENTITY_SCRIPT])

    def test_cached_artwork_identity_deep_json_is_transient(self):
        image = _jpeg_image()
        first_payload = {
            "running": True,
            "state": "playing",
            "title": "Song",
            "artist": "Artist",
            "album": "Album",
            "duration": 201.5,
            "persistentId": "A1B2C3D4E5F60708",
            "rawArtwork": f"'tdta'(${image.hex().upper()}$)",
        }
        malformed_identity = "[" * 2000 + "0" + "]" * 2000
        calls = []

        def runner(script):
            calls.append(script)
            if script == ARTWORK_SCRIPT:
                return _process_result(first_payload)
            return ProcessResult(0, malformed_identity, "")

        client = MusicClient(runner=runner)
        track = TrackInfo(
            True,
            "playing",
            "Song",
            "Artist",
            "Album",
            201.5,
            persistent_id="A1B2C3D4E5F60708",
        )

        cached = client.artwork_for(track.identity)
        self.assertIsNotNone(cached)
        with self.assertRaises(ArtworkTransientError):
            client.artwork_for(track.identity)
        self.assertIs(client._artwork_cache, cached)
        self.assertEqual(calls, [ARTWORK_SCRIPT, ARTWORK_IDENTITY_SCRIPT])

    def test_sampling_a_new_track_invalidates_the_one_entry_artwork_cache(self):
        image = _jpeg_image()
        first_payload = {
            "running": True,
            "state": "playing",
            "title": "Song",
            "artist": "Artist",
            "album": "Album",
            "duration": 201.5,
            "persistentId": "A1B2C3D4E5F60708",
            "rawArtwork": f"'tdta'(${image.hex().upper()}$)",
        }
        changed_payload = {
            "running": True,
            "state": "playing",
            "title": "Other",
            "artist": "Artist",
            "album": "Album",
            "duration": 201.5,
            "position": 0,
            "lyrics": "",
            "persistentId": "F8E7D6C5B4A30201",
        }
        calls = []

        def runner(script):
            calls.append(script)
            if script == NOW_PLAYING_SCRIPT:
                return _process_result(changed_payload)
            if script == ARTWORK_IDENTITY_SCRIPT:
                return _process_result(changed_payload)
            if len(calls) == 1:
                return _process_result(first_payload)
            return _process_result(changed_payload)

        client = MusicClient(runner=runner)
        first = TrackInfo(
            True,
            "playing",
            "Song",
            "Artist",
            "Album",
            201.5,
            persistent_id="A1B2C3D4E5F60708",
        )

        self.assertIsNotNone(client.artwork_for(first.identity))
        client.sample()
        with self.assertRaises(ArtworkStaleIdentityError):
            client.artwork_for(first.identity)
        self.assertEqual(calls, [ARTWORK_SCRIPT, NOW_PLAYING_SCRIPT, ARTWORK_SCRIPT])

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

    def test_seek_rejects_non_numeric_values_without_dispatching(self):
        scripts = []
        client = MusicClient(
            runner=lambda script: scripts.append(script) or ProcessResult(0, "ok", "")
        )

        for value in (True, False, "1", None, 10**400, float("inf"), float("nan")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    client.seek(cast(float, value))

        self.assertEqual(scripts, [])


if __name__ == "__main__":
    unittest.main()
