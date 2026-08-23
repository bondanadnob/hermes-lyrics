import asyncio
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any, Coroutine, cast
from unittest.mock import patch

from dashboard.apple_music_lyrics_backend import ambient, ambient_worker
from dashboard.apple_music_lyrics_backend.models import TrackInfo

_STUBBORN_WORKER_SOURCE = """
import pathlib
import signal
import subprocess
import sys
import time

marker = pathlib.Path(sys.argv[sys.argv.index('--ffmpeg') + 1])
child_source = '''
import pathlib
import signal
import sys
import time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
pathlib.Path(sys.argv[1]).write_text(str(__import__('os').getpid()), encoding='utf-8')
while True:
    time.sleep(1.0)
'''
subprocess.Popen(
    [sys.executable, '-I', '-c', child_source, str(marker)],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    shell=False,
)
while True:
    time.sleep(1.0)
"""


_EXACT_PROVIDER_URL = (
    "https://amp.shazam.com/discovery/v5/en-US/GB/web/-/tag/"
    "123e4567-e89b-42d3-a456-426614174000/"
    "123e4567-e89b-42d3-b456-426614174001?"
    f"{ambient_worker._PROVIDER_QUERY}"
)


def _process_group_exists_for_test(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_marker(marker: Path, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    return marker.exists()


def _cleanup_process_group(process_group_id: int) -> None:
    if not _process_group_exists_for_test(process_group_id):
        return
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 2.0
    while (
        _process_group_exists_for_test(process_group_id)
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)


def _stubborn_worker_client(
    root: Path,
    *,
    process_wrapper: Callable[[subprocess.Popen], Any] | None = None,
) -> tuple[ambient.AmbientWorkerClient, Path, list[subprocess.Popen]]:
    worker_script = root / "stubborn_worker.py"
    marker = root / "recorder-ready"
    worker_script.write_text(_STUBBORN_WORKER_SOURCE, encoding="utf-8")
    processes: list[subprocess.Popen] = []

    def process_factory(*args, **kwargs):
        process = subprocess.Popen(*args, **kwargs)
        processes.append(process)
        return process_wrapper(process) if process_wrapper is not None else process

    return (
        ambient.AmbientWorkerClient(
            python_executable=Path(sys.executable),
            worker_script=worker_script,
            ffmpeg_executable=marker,
            process_factory=process_factory,
            clock=time.time,
        ),
        marker,
        processes,
    )


class _TestWorkerOutput(io.BytesIO):
    def __init__(self, payload: bytes, before_read: Callable[[], None] | None = None):
        super().__init__(payload)
        self.before_read = before_read
        self.read_sizes: list[int | None] = []

    def read(self, size: int | None = -1) -> bytes:
        self.read_sizes.append(size)
        if self.before_read is not None:
            self.before_read()
        return super().read(size)


class _CompletedWorkerProcess:
    def __init__(
        self,
        payload: bytes,
        *,
        returncode: int = 0,
        before_read: Callable[[], None] | None = None,
    ) -> None:
        self.returncode = returncode
        self.stdout = _TestWorkerOutput(payload, before_read)

    def poll(self) -> int:
        return self.returncode

    def wait(self, timeout: float) -> int:
        return self.returncode


class AmbientRecognitionContractTests(unittest.TestCase):
    def test_ambient_recognition_module_exists(self):
        specification = importlib.util.find_spec(
            "dashboard.apple_music_lyrics_backend.ambient"
        )

        self.assertIsNotNone(specification)

    def test_ffmpeg_discovery_rejects_an_executable_outside_fixed_roots(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "ffmpeg"
            executable.write_text("binary", encoding="utf-8")
            executable.chmod(0o755)

            discovered = ambient.discover_ffmpeg_executable((executable,))

        self.assertEqual(discovered, Path("/nonexistent/hermes-ffmpeg"))

    def test_ffmpeg_metadata_rejects_a_group_writable_binary(self):
        import inspect
        import stat
        from types import SimpleNamespace

        validator = getattr(ambient, "_is_trusted_ffmpeg_stat", None)
        if validator is None:
            self.fail("ambient._is_trusted_ffmpeg_stat is required")
        metadata = SimpleNamespace(
            st_mode=stat.S_IFREG | 0o775,
            st_uid=os.getuid(),
        )

        self.assertFalse(validator(metadata))
        self.assertIn(
            "_is_trusted_ffmpeg_stat",
            inspect.getsource(ambient.discover_ffmpeg_executable),
        )

    def test_ffmpeg_metadata_accepts_an_owner_controlled_regular_binary(self):
        import stat
        from types import SimpleNamespace

        metadata = SimpleNamespace(
            st_mode=stat.S_IFREG | 0o755,
            st_uid=os.getuid(),
        )

        self.assertTrue(ambient._is_trusted_ffmpeg_stat(cast(Any, metadata)))

    def test_runtime_paths_are_profile_scoped_and_plugin_local(self):
        path_builder = cast(
            Callable[..., Any] | None,
            getattr(ambient, "build_runtime_paths", None),
        )
        if path_builder is None:
            self.fail("ambient.build_runtime_paths is required")

        paths = path_builder(
            plugin_root=Path("/plugins/apple-music-lyrics"),
            hermes_home=Path("/profiles/cafe"),
            ffmpeg_executable=Path("/opt/homebrew/bin/ffmpeg"),
        )

        self.assertEqual(
            paths.python_executable,
            Path(
                "/profiles/cafe/plugin-data/apple-music-lyrics/"
                "recognition-venv/bin/python"
            ),
        )
        self.assertEqual(
            paths.worker_script,
            Path(
                "/plugins/apple-music-lyrics/dashboard/"
                "apple_music_lyrics_backend/ambient_worker.py"
            ),
        )
        self.assertEqual(
            paths.ffmpeg_executable,
            Path("/opt/homebrew/bin/ffmpeg"),
        )

    def test_router_factory_never_starts_recognition_during_construction(self):
        factory = cast(
            Callable[..., Any] | None,
            getattr(ambient, "build_playback_router", None),
        )
        if factory is None:
            self.fail("ambient.build_playback_router is required")

        process_calls = []

        class MusicSource:
            def sample(self):
                return TrackInfo(running=False, state="stopped")

        paths = ambient.AmbientRuntimePaths(
            python_executable=Path("/private/recognition/bin/python"),
            worker_script=Path("/private/plugin/ambient_worker.py"),
            ffmpeg_executable=Path("/opt/homebrew/bin/ffmpeg"),
        )
        router = factory(
            MusicSource(),
            paths,
            process_factory=lambda *args, **kwargs: process_calls.append((args, kwargs)),
            clock=lambda: 1_000.0,
        )

        router.select_source("ambient")
        track = router.sample()

        self.assertEqual(track.state, "ambient_idle")
        self.assertEqual(process_calls, [])

    def test_ambient_worker_module_exists(self):
        specification = importlib.util.find_spec(
            "dashboard.apple_music_lyrics_backend.ambient_worker"
        )

        self.assertIsNotNone(specification)

    def test_worker_captures_bounded_ogg_vorbis_to_a_pipe(self):
        command_builder = cast(
            Callable[[Path, float], list[str]] | None,
            getattr(ambient_worker, "build_capture_command", None),
        )
        if command_builder is None:
            self.fail("ambient_worker.build_capture_command is required")

        command = command_builder(Path("/opt/homebrew/bin/ffmpeg"), 8.0)

        self.assertEqual(
            command,
            [
                "/opt/homebrew/bin/ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "avfoundation",
                "-i",
                ":default",
                "-t",
                "8.0",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "libvorbis",
                "-q:a",
                "4",
                "-f",
                "ogg",
                "pipe:1",
            ],
        )
        self.assertFalse(any(argument.endswith(".ogg") for argument in command))

    def test_worker_sanitizes_the_shazam_response_before_output(self):
        sanitizer = cast(
            Callable[[object], dict[str, object]],
            getattr(ambient_worker, "sanitize_response", None),
        )
        if sanitizer is None:
            self.fail("ambient_worker.sanitize_response is required")
        raw = {
            "matches": [
                {"id": "match", "offset": 42.5, "timeskew": 0.1, "extra": "drop"}
            ],
            "track": {
                "key": "track",
                "title": "Song",
                "subtitle": "Artist",
                "images": {
                    "coverart": "https://is1-ssl.mzstatic.com/art.jpg",
                    "background": "drop",
                },
                "sections": [
                    {
                        "type": "SONG",
                        "metadata": [
                            {"title": "Album", "text": "Album Name"},
                            {"title": "Label", "text": "drop"},
                        ],
                        "lyrics": ["never retain this"],
                    }
                ],
                "hub": {"actions": ["drop"]},
            },
            "tagid": "drop",
        }

        sanitized = sanitizer(raw)

        self.assertEqual(
            sanitized,
            {
                "matches": [{"id": "match", "offset": 42.5, "timeskew": 0.1}],
                "track": {
                    "key": "track",
                    "title": "Song",
                    "subtitle": "Artist",
                    "images": {
                        "coverart": "https://is1-ssl.mzstatic.com/art.jpg"
                    },
                    "sections": [
                        {
                            "metadata": [
                                {"title": "Album", "text": "Album Name"}
                            ]
                        }
                    ],
                },
            },
        )
        self.assertLess(len(json.dumps(sanitized)), 4096)

    def test_worker_bounds_provider_text_before_json_output(self):
        raw = {
            "matches": [{"id": "m" * 10_000, "offset": 1.0}],
            "track": {
                "key": "k" * 10_000,
                "title": "t" * 100_000,
                "subtitle": "a" * 100_000,
                "images": {"coverart": "https://example.test/" + "x" * 100_000},
                "sections": [
                    {
                        "metadata": [
                            {"title": "Album", "text": "b" * 100_000}
                        ]
                    }
                ],
            },
        }

        sanitized = ambient_worker.sanitize_response(raw)
        matches = cast(list[dict[str, Any]], sanitized["matches"])
        match = matches[0]
        track = cast(dict[str, Any], sanitized["track"])

        self.assertLessEqual(len(match["id"]), 128)
        self.assertLessEqual(len(track["key"]), 128)
        self.assertLessEqual(len(track["title"]), 512)
        self.assertLessEqual(len(track["subtitle"]), 512)
        self.assertLessEqual(len(track["images"]["coverart"]), 2_048)
        self.assertLessEqual(len(track["sections"][0]["metadata"][0]["text"]), 512)
        self.assertLess(len(json.dumps(sanitized)), 8_192)

    def test_worker_text_sanitizer_never_scans_past_its_output_budget(self):
        class GuardedText(str):
            def __iter__(self):
                for index, character in enumerate(super().__iter__()):
                    if index >= 512:
                        raise AssertionError("sanitizer traversed the unbounded tail")
                    yield character

        sanitized = ambient_worker.sanitize_response(
            {
                "matches": [{"id": "match", "offset": 1.0}],
                "track": {
                    "key": "track",
                    "title": GuardedText("x" * 10_000),
                    "subtitle": "Artist",
                },
            }
        )

        track = cast(dict[str, Any], sanitized["track"])
        self.assertEqual(track["title"], "x" * 512)

    def test_worker_skips_an_overflowing_match_offset_and_keeps_scanning(self):
        sanitized = ambient_worker.sanitize_response(
            {
                "matches": [
                    {"id": "bad", "offset": 10**4_000},
                    {"id": "good", "offset": 7.5},
                ],
                "track": {
                    "key": "track",
                    "title": "Song",
                    "subtitle": "Artist",
                },
            }
        )

        self.assertEqual(
            sanitized["matches"],
            [{"id": "bad"}, {"id": "good", "offset": 7.5}],
        )

    def test_shazam_http_client_stops_streaming_at_the_response_budget(self):
        pulled_chunks = []

        class Content:
            async def iter_chunked(self, _size):
                for chunk in (b'{"matches":[]}', b"overflow", b"must-not-be-read"):
                    pulled_chunks.append(chunk)
                    yield chunk

        class Response:
            status = 200
            headers = {}
            url = _EXACT_PROVIDER_URL
            content = Content()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def json(self):
                raise AssertionError("the unbounded response.json path was used")

        class Session:
            def __init__(self, **_options):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def post(self, _url, **_options):
                return Response()

        client_class = getattr(ambient_worker, "BoundedShazamHTTPClient", None)
        if client_class is None:
            self.fail("ambient_worker.BoundedShazamHTTPClient is required")
        client = client_class(session_factory=Session, max_response_bytes=16)

        with self.assertRaisesRegex(RuntimeError, "response exceeds"):
            asyncio.run(
                client.request(
                    "POST",
                    Response.url,
                    headers={"Accept": "*/*"},
                    json={
                        "timezone": "UTC",
                        "signature": {"uri": "sig", "samplems": 8_000},
                        "timestamp": 1_000,
                        "context": {},
                        "geolocation": {},
                    },
                )
            )

        self.assertEqual(pulled_chunks, [b'{"matches":[]}', b"overflow"])

    def test_shazam_http_client_rejects_a_non_shazam_destination_before_io(self):
        session_created = False

        class Content:
            async def iter_chunked(self, _size):
                yield b'{"matches":[],"track":{}}'

        class Response:
            status = 200
            headers = {}
            content = Content()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        class Session:
            def __init__(self, **_options):
                nonlocal session_created
                session_created = True

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def post(self, _url, **_options):
                return Response()

        client = ambient_worker.BoundedShazamHTTPClient(session_factory=Session)

        with self.assertRaisesRegex(RuntimeError, "request rejected"):
            asyncio.run(
                client.request(
                    "POST",
                    "https://example.test/discovery/v5/en-US/GB/web/-/tag/123E4567-E89B-42D3-A456-426614174000/123E4567-E89B-42D3-B456-426614174001",
                    json={
                        "timezone": "UTC",
                        "signature": {"uri": "sig", "samplems": 8_000},
                        "timestamp": 1_000,
                        "context": {},
                        "geolocation": {},
                    },
                )
            )

        self.assertFalse(session_created)

    def test_shazam_http_client_rejects_non_exact_routes_before_io(self):
        session_calls = []
        first_request_id = "123e4567-e89b-42d3-a456-426614174000"
        second_request_id = "123e4567-e89b-42d3-b456-426614174001"
        exact_path = (
            "/discovery/v5/en-US/GB/web/-/tag/"
            f"{first_request_id}/{second_request_id}"
        )
        query = ambient_worker._PROVIDER_QUERY

        class Session:
            def __init__(self, **_options):
                session_calls.append(True)

            async def __aenter__(self):
                raise RuntimeError("provider I/O reached")

            async def __aexit__(self, *_args):
                return False

        invalid_urls = {
            "empty query": f"https://amp.shazam.com{exact_path}",
            "extra query field": f"https://amp.shazam.com{exact_path}?{query}&extra=1",
            "reordered query": (
                f"https://amp.shazam.com{exact_path}?webv3=true&sync=true"
            ),
            "wrong locale": (
                f"https://amp.shazam.com{exact_path.replace('/en-US/', '/en/')}?{query}"
            ),
            "wrong country": (
                f"https://amp.shazam.com{exact_path.replace('/GB/', '/US/')}?{query}"
            ),
            "unknown device": (
                f"https://amp.shazam.com{exact_path.replace('/web/', '/ipad/')}?{query}"
            ),
            "extra prefix component": (
                "https://amp.shazam.com/discovery/v5/extra/en-US/GB/web/-/tag/"
                f"{first_request_id}/{second_request_id}?{query}"
            ),
            "dot segment": (
                "https://amp.shazam.com/discovery/v5/en-US/GB/web/../web/-/tag/"
                f"{first_request_id}/{second_request_id}?{query}"
            ),
            "encoded dot segment": (
                "https://amp.shazam.com/discovery/v5/en-US/GB/web/%2e%2e/web/-/tag/"
                f"{first_request_id}/{second_request_id}?{query}"
            ),
            "encoded path separator": (
                "https://amp.shazam.com/discovery/v5/en-US/GB/web/-/tag/"
                f"{first_request_id}%2fextra/{second_request_id}?{query}"
            ),
            "extra suffix component": (
                f"https://amp.shazam.com{exact_path}/extra?{query}"
            ),
            "double separator": (
                f"https://amp.shazam.com{exact_path.replace('/-/tag/', '/-//tag/')}?{query}"
            ),
        }
        payload = {
            "timezone": "UTC",
            "signature": {"uri": "signature-uri", "samplems": 8_000},
            "timestamp": 1_000,
            "context": {},
            "geolocation": {},
        }

        for name, url in invalid_urls.items():
            with self.subTest(name=name):
                session_calls.clear()
                client = ambient_worker.BoundedShazamHTTPClient(
                    session_factory=Session
                )
                with self.assertRaisesRegex(RuntimeError, "request rejected"):
                    asyncio.run(client.request("POST", url, json=payload))
                self.assertEqual(session_calls, [])

    def test_shazam_http_client_rejects_oversized_content_length_before_reading(self):
        body_pulled = False

        class Content:
            async def iter_chunked(self, _size):
                nonlocal body_pulled
                body_pulled = True
                yield b"{}"

        class Response:
            status = 200
            headers = {"Content-Length": "17"}
            content = Content()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        class Session:
            def __init__(self, **_options):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def post(self, _url, **_options):
                return Response()

        client = ambient_worker.BoundedShazamHTTPClient(
            session_factory=Session,
            max_response_bytes=16,
        )
        with self.assertRaisesRegex(RuntimeError, "response exceeds"):
            asyncio.run(
                client.request(
                    "POST",
                    _EXACT_PROVIDER_URL,
                    json={
                        "timezone": "UTC",
                        "signature": {"uri": "sig", "samplems": 8_000},
                        "timestamp": 1_000,
                        "context": {},
                        "geolocation": {},
                    },
                )
            )

        self.assertFalse(body_pulled)

    def test_shazam_http_client_rejects_an_oversized_signature_before_io(self):
        session_created = False

        class Content:
            async def iter_chunked(self, _size):
                yield b'{"matches":[],"track":{}}'

        class Response:
            status = 200
            headers = {}
            content = Content()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        class Session:
            def __init__(self, **_options):
                nonlocal session_created
                session_created = True

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def post(self, _url, **_options):
                return Response()

        client = ambient_worker.BoundedShazamHTTPClient(session_factory=Session)
        with self.assertRaisesRegex(RuntimeError, "request exceeds"):
            asyncio.run(
                client.request(
                    "POST",
                    _EXACT_PROVIDER_URL,
                    json={
                        "timezone": "UTC",
                        "signature": {"uri": "s" * 300_000, "samplems": 8_000},
                        "timestamp": 1_000,
                        "context": {},
                        "geolocation": {},
                    },
                )
            )

        self.assertFalse(session_created)

    def test_shazam_http_client_projects_request_and_response_fields(self):
        session_options = []
        post_calls = []
        response_body = json.dumps(
            {
                "matches": [{"id": "match", "offset": 3.0, "drop": "x"}],
                "track": {
                    "key": "track",
                    "title": "Song",
                    "subtitle": "Artist",
                    "hub": {"drop": True},
                },
                "drop": "response metadata",
            }
        ).encode("utf-8")

        class Content:
            async def iter_chunked(self, _size):
                yield response_body

        class Response:
            status = 200
            headers = {"Content-Length": str(len(response_body))}
            content = Content()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        class Session:
            def __init__(self, **options):
                session_options.append(options)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def post(self, url, **options):
                post_calls.append((url, options))
                return Response()

        client = ambient_worker.BoundedShazamHTTPClient(session_factory=Session)
        result = asyncio.run(
            client.request(
                "POST",
                _EXACT_PROVIDER_URL,
                headers={
                    "Accept": "*/*",
                    "Accept-Language": "en-US",
                    "X-Shazam-Platform": "IPHONE",
                    "X-Shazam-AppVersion": "14.1.0",
                    "User-Agent": "bounded-agent",
                    "Accept-Encoding": "gzip, deflate",
                    "Authorization": "must-not-leave",
                },
                proxy=None,
                json={
                    "timezone": "UTC",
                    "signature": {
                        "uri": "signature-uri",
                        "samplems": 0,
                        "drop": "signature-extra",
                    },
                    "timestamp": 1_000,
                    "context": {"drop": "context"},
                    "geolocation": {"drop": "location"},
                    "drop": "request metadata",
                },
            )
        )

        self.assertEqual(
            result,
            {
                "matches": [{"id": "match", "offset": 3.0}],
                "track": {"key": "track", "title": "Song", "subtitle": "Artist"},
            },
        )
        self.assertEqual(len(session_options), 1)
        self.assertFalse(session_options[0]["trust_env"])
        self.assertFalse(session_options[0]["auto_decompress"])
        self.assertEqual(len(post_calls), 1)
        _url, options = post_calls[0]
        self.assertFalse(options["allow_redirects"])
        self.assertNotIn("json", options)
        self.assertNotIn("proxy", options)
        self.assertNotIn("Authorization", options["headers"])
        self.assertEqual(options["headers"]["Accept-Encoding"], "identity")
        self.assertEqual(options["headers"]["Content-Type"], "application/json")
        self.assertEqual(
            json.loads(options["data"]),
            {
                "timezone": "UTC",
                "signature": {"uri": "signature-uri", "samplems": 0},
                "timestamp": 1_000,
                "context": {},
                "geolocation": {},
            },
        )

    def test_shazam_http_client_canonicalizes_valid_uppercase_uuid4_path_identifiers(self):
        posted_urls = []
        response_body = b'{"matches":[],"track":{}}'
        first_request_id = "123e4567-e89b-42d3-a456-426614174000"
        second_request_id = "123e4567-e89b-42d3-b456-426614174001"
        raw_first_request_id = first_request_id.upper()
        raw_second_request_id = second_request_id.upper()

        class Content:
            async def iter_chunked(self, _size):
                yield response_body

        class Response:
            status = 200
            headers = {"Content-Length": str(len(response_body))}
            content = Content()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        class Session:
            def __init__(self, **_options):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def post(self, url, **_options):
                posted_urls.append(url)
                return Response()

        client = ambient_worker.BoundedShazamHTTPClient(session_factory=Session)
        query = (
            "sync=true&webv3=true&sampling=true&connected=&shazamapiversion=v3"
            "&sharehub=true&hubv5minorversion=v5.1&hidelb=true&video=v3"
        )
        request_url = (
            "https://amp.shazam.com/discovery/v5/en-US/GB/android/-/tag/"
            f"{raw_first_request_id}/{raw_second_request_id}?{query}"
        )
        try:
            asyncio.run(
                client.request(
                    "POST",
                    request_url,
                    json={
                        "timezone": "UTC",
                        "signature": {"uri": "signature-uri", "samplems": 8_000},
                        "timestamp": 1_000,
                        "context": {},
                        "geolocation": {},
                    },
                    headers={},
                    proxy=None,
                )
            )
        except RuntimeError as exc:
            self.fail(f"the pinned Shazam query was rejected: {exc}")

        self.assertEqual(len(posted_urls), 1)
        from urllib.parse import urlsplit

        posted = urlsplit(posted_urls[0])
        self.assertEqual(
            posted.path.rsplit("/", 2)[-2:],
            [first_request_id, second_request_id],
        )
        self.assertEqual(posted.query, query)

    def test_shazam_http_client_rejects_non_uuid4_request_identifiers(self):
        session_created = False
        response_body = b'{"matches":[],"track":{}}'

        class Content:
            async def iter_chunked(self, _size):
                yield response_body

        class Response:
            status = 200
            headers = {"Content-Length": str(len(response_body))}
            content = Content()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        class Session:
            def __init__(self, **_options):
                nonlocal session_created
                session_created = True

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def post(self, _url, **_options):
                return Response()

        payload = {
            "timezone": "UTC",
            "signature": {"uri": "signature-uri", "samplems": 8_000},
            "timestamp": 1_000,
            "context": {},
            "geolocation": {},
        }
        invalid_first_identifiers = {
            "uuid version 1": "123e4567-e89b-12d3-a456-426614174000",
            "malformed token": "not-a-uuid",
        }
        for name, first_identifier in invalid_first_identifiers.items():
            with self.subTest(name=name):
                session_created = False
                client = ambient_worker.BoundedShazamHTTPClient(session_factory=Session)
                with self.assertRaisesRegex(RuntimeError, "request rejected"):
                    asyncio.run(
                        client.request(
                            "POST",
                            (
                                "https://amp.shazam.com/discovery/v5/en-US/GB/android/-/tag/"
                                f"{first_identifier}/"
                                "123e4567-e89b-42d3-b456-426614174001?"
                                f"{ambient_worker._PROVIDER_QUERY}"
                            ),
                            json=payload,
                            headers={},
                            proxy=None,
                        )
                    )
                self.assertFalse(session_created)

    def test_shazam_http_client_maps_deep_json_to_a_controlled_error(self):
        response_body = ("[" * 2_000 + "0" + "]" * 2_000).encode("ascii")

        class Content:
            async def iter_chunked(self, _size):
                yield response_body

        class Response:
            status = 200
            headers = {"Content-Length": str(len(response_body))}
            content = Content()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        class Session:
            def __init__(self, **_options):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def post(self, _url, **_options):
                return Response()

        client = ambient_worker.BoundedShazamHTTPClient(session_factory=Session)
        with self.assertRaisesRegex(RuntimeError, "response is invalid"):
            asyncio.run(
                client.request(
                    "POST",
                    _EXACT_PROVIDER_URL,
                    json={
                        "timezone": "UTC",
                        "signature": {"uri": "sig", "samplems": 8_000},
                        "timestamp": 1_000,
                        "context": {},
                        "geolocation": {},
                    },
                )
            )

    def test_worker_stops_streaming_audio_at_the_memory_limit_and_reaps_recorder(self):
        capture = cast(
            Callable[..., bytes] | None,
            getattr(ambient_worker, "capture_audio", None),
        )
        if capture is None:
            self.fail("ambient_worker.capture_audio is required")

        processes = []
        child_source = (
            "import os,pathlib,signal,sys,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8'); "
            "time.sleep(30)"
        )
        leader_source = """
import os
import pathlib
import subprocess
import sys
import time

marker = pathlib.Path(sys.argv[1])
subprocess.Popen(
    [sys.executable, "-I", "-c", sys.argv[2], str(marker)],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    shell=False,
)
deadline = time.monotonic() + 2
while not marker.exists() and time.monotonic() < deadline:
    time.sleep(0.01)
if not marker.exists():
    raise SystemExit(2)
os.write(1, b"x" * 5_000_000)
time.sleep(30)
"""

        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "child.pid"

            def process_factory(_arguments, **options):
                process = subprocess.Popen(
                    [sys.executable, "-I", "-c", leader_source, str(marker), child_source],
                    **options,
                )
                processes.append(process)
                return process

            try:
                with self.assertRaisesRegex(RuntimeError, "audio sample exceeds"):
                    capture(
                        Path("/opt/homebrew/bin/ffmpeg"),
                        8.0,
                        process_factory=process_factory,
                    )

                self.assertEqual(len(processes), 1)
                self.assertIsNotNone(processes[0].poll())
                child_pid = int(marker.read_text(encoding="utf-8"))
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)
            finally:
                if processes:
                    try:
                        os.killpg(processes[0].pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_worker_hides_raw_microphone_capture_errors(self):
        def process_factory(_arguments, **options):
            return subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    (
                        "import sys; "
                        "sys.stderr.write('private device name: permission denied'); "
                        "raise SystemExit(1)"
                    ),
                ],
                **options,
            )

        with self.assertRaisesRegex(RuntimeError, "microphone capture failed") as raised:
            ambient_worker.capture_audio(
                Path("/opt/homebrew/bin/ffmpeg"),
                8.0,
                process_factory=process_factory,
            )

        self.assertNotIn("private device name", str(raised.exception))

    def test_worker_fingerprints_audio_and_returns_only_sanitized_metadata(self):
        received_audio = []

        class FakeShazam:
            def __init__(self, *, http_client=None):
                self.http_client = http_client

            async def recognize(self, audio):
                received_audio.append(audio)
                return {
                    "matches": [{"id": "match", "offset": 5.0, "extra": "drop"}],
                    "track": {
                        "key": "track",
                        "title": "Song",
                        "subtitle": "Artist",
                        "hub": {"drop": True},
                    },
                }

        recognize = cast(
            Callable[..., Coroutine[Any, Any, dict[str, object]]] | None,
            getattr(ambient_worker, "recognize_audio", None),
        )
        if recognize is None:
            self.fail("ambient_worker.recognize_audio is required")

        result = asyncio.run(
            recognize(b"bounded ogg bytes", shazam_factory=FakeShazam)
        )

        self.assertEqual(received_audio, [b"bounded ogg bytes"])
        self.assertEqual(
            result,
            {
                "matches": [{"id": "match", "offset": 5.0}],
                "track": {"key": "track", "title": "Song", "subtitle": "Artist"},
            },
        )

    def test_worker_injects_the_bounded_http_client_into_shazamio(self):
        received_clients = []

        class FakeShazam:
            def __init__(self, *, http_client=None):
                received_clients.append(http_client)

            async def recognize(self, _audio):
                return {"matches": [], "track": {}}

        asyncio.run(
            ambient_worker.recognize_audio(
                b"bounded ogg bytes",
                shazam_factory=FakeShazam,
            )
        )

        self.assertEqual(len(received_clients), 1)
        self.assertIsInstance(
            received_clients[0],
            ambient_worker.BoundedShazamHTTPClient,
        )

    def test_worker_main_writes_compact_json_for_one_ephemeral_capture(self):
        captured = []
        output = io.StringIO()
        trusted_ffmpeg = Path("/trusted/test/ffmpeg")
        main = getattr(ambient_worker, "main", None)
        if not callable(main):
            self.fail("ambient_worker.main is required")

        def capture(ffmpeg, duration):
            captured.append((ffmpeg, duration))
            return b"ogg"

        async def recognize(audio):
            self.assertEqual(audio, b"ogg")
            return {"matches": [], "track": {}}

        with patch.object(
            ambient_worker,
            "_trusted_ffmpeg_executable",
            return_value=trusted_ffmpeg,
        ):
            exit_code = main(
                ["--ffmpeg", str(trusted_ffmpeg), "--duration", "8.0"],
                capture=capture,
                recognize=recognize,
                output=output,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            captured,
            [(trusted_ffmpeg, 8.0)],
        )
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["matches"], [])
        self.assertEqual(payload["track"], {})
        self.assertEqual(set(payload), {"matches", "track", "captureStartedAt"})
        self.assertLess(len(output.getvalue()), 256)

    def test_worker_main_reports_the_capture_start_time(self):
        output = io.StringIO()
        trusted_ffmpeg = Path("/trusted/test/ffmpeg")

        def capture(_ffmpeg, _duration):
            return b"RIFF"

        async def recognize(_audio):
            return {"matches": [], "track": {}}

        before = time.time()
        with patch.object(
            ambient_worker,
            "_trusted_ffmpeg_executable",
            return_value=trusted_ffmpeg,
        ):
            ambient_worker.main(
                ["--ffmpeg", str(trusted_ffmpeg), "--duration", "8.0"],
                capture=capture,
                recognize=recognize,
                output=output,
            )
        after = time.time()
        payload = json.loads(output.getvalue())

        self.assertIn("captureStartedAt", payload)
        self.assertGreaterEqual(payload["captureStartedAt"], before)
        self.assertLessEqual(payload["captureStartedAt"], after)

    def test_worker_main_rejects_an_untrusted_ffmpeg_before_capture(self):
        import tempfile

        captured = []

        def capture(ffmpeg, duration):
            captured.append((ffmpeg, duration))
            return b"RIFF"

        async def recognize(_audio):
            return {"matches": [], "track": {}}

        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "ffmpeg"
            executable.write_text("binary", encoding="utf-8")
            executable.chmod(0o755)
            with self.assertRaisesRegex(RuntimeError, "ffmpeg executable is not trusted"):
                ambient_worker.main(
                    ["--ffmpeg", str(executable), "--duration", "8.0"],
                    capture=capture,
                    recognize=recognize,
                    output=io.StringIO(),
                )

        self.assertEqual(captured, [])

    def test_parses_a_bounded_match_and_anchors_the_current_position(self):
        parser = cast(
            Callable[..., TrackInfo | None] | None,
            getattr(ambient, "parse_shazam_match", None),
        )
        if parser is None:
            self.fail("ambient.parse_shazam_match is required")
        payload = {
            "matches": [{"id": "match-123", "offset": 91.25, "timeskew": 0.0}],
            "track": {
                "key": "track-456",
                "title": "Example Song",
                "subtitle": "Example Artist",
                "isrc": "US-AAA-00-00001",
                "images": {
                    "coverart": "https://is1-ssl.mzstatic.com/image/thumb/example.jpg"
                },
                "sections": [
                    {
                        "type": "SONG",
                        "metadata": [
                            {"title": "Album", "text": "Example Album"},
                            {"title": "Label", "text": "Example Label"},
                        ],
                    }
                ],
            },
        }

        track = parser(payload, captured_at=1_000.0, received_at=1_010.0)

        if track is None:
            self.fail("a valid Shazam match must produce a track")
        self.assertEqual(track.title, "Example Song")
        self.assertEqual(track.artist, "Example Artist")
        self.assertEqual(track.album, "Example Album")
        self.assertEqual(track.position, 101.25)
        self.assertEqual(track.sampled_at, 1_010.0)
        self.assertEqual(track.source, "ambient")
        self.assertFalse(track.can_control)
        self.assertFalse(track.can_seek)
        self.assertEqual(track.persistent_id, "shazam:track-456")
        self.assertEqual(
            track.artwork_url,
            "https://is1-ssl.mzstatic.com/image/thumb/example.jpg",
        )

    def test_parent_text_sanitizer_never_scans_past_its_output_budget(self):
        class GuardedText(str):
            def __iter__(self):
                for index, character in enumerate(super().__iter__()):
                    if index >= 512:
                        raise AssertionError("parent traversed the unbounded tail")
                    yield character

        track = ambient.parse_shazam_match(
            {
                "matches": [{"id": "match", "offset": 1.0}],
                "track": {
                    "key": "track",
                    "title": GuardedText("x" * 10_000),
                    "subtitle": "Artist",
                },
            },
            captured_at=1_000.0,
            received_at=1_001.0,
        )

        if track is None:
            self.fail("a valid bounded response must produce a track")
        self.assertEqual(track.title, "x" * 512)

    def test_parent_skips_an_overflowing_match_offset_and_uses_a_later_match(self):
        track = ambient.parse_shazam_match(
            {
                "matches": [
                    {"id": "bad", "offset": 10**4_000},
                    {"id": "good", "offset": 7.5},
                ],
                "track": {
                    "key": "track",
                    "title": "Song",
                    "subtitle": "Artist",
                },
            },
            captured_at=1_000.0,
            received_at=1_002.0,
        )

        if track is None:
            self.fail("the valid later match must remain reachable")
        self.assertEqual(track.position, 9.5)

    def test_worker_client_executes_a_fixed_worker_without_a_shell(self):
        worker_client = getattr(ambient, "AmbientWorkerClient", None)
        if worker_client is None:
            self.fail("ambient.AmbientWorkerClient is required")
        payload = {
            "matches": [{"id": "match-123", "offset": 12.0}],
            "track": {
                "key": "track-456",
                "title": "Example Song",
                "subtitle": "Example Artist",
            },
        }
        calls = []

        def process_factory(arguments, **options):
            calls.append((arguments, options))
            return _CompletedWorkerProcess(json.dumps(payload).encode("utf-8"))

        moments = iter((1_000.0, 1_010.0))
        client = worker_client(
            python_executable=Path("/private/recognizer/bin/python"),
            worker_script=Path("/private/plugin/ambient_worker.py"),
            ffmpeg_executable=Path("/opt/homebrew/bin/ffmpeg"),
            process_factory=process_factory,
            clock=lambda: next(moments),
        )

        track = client.recognize()

        self.assertEqual(
            calls[0][0],
            [
                "/private/recognizer/bin/python",
                "-I",
                "/private/plugin/ambient_worker.py",
                "--ffmpeg",
                "/opt/homebrew/bin/ffmpeg",
                "--duration",
                "8.0",
            ],
        )
        self.assertFalse(calls[0][1].get("shell", False))
        self.assertIsNotNone(track)

    def test_worker_client_anchors_position_to_the_worker_capture_start(self):
        payload = {
            "matches": [{"id": "match", "offset": 12.0}],
            "track": {"key": "track", "title": "Song", "subtitle": "Artist"},
            "captureStartedAt": 1_005.0,
        }

        moments = iter((1_000.0, 1_010.0))
        client = ambient.AmbientWorkerClient(
            python_executable=Path("/runtime/bin/python"),
            worker_script=Path("/plugin/ambient_worker.py"),
            ffmpeg_executable=Path("/opt/homebrew/bin/ffmpeg"),
            process_factory=lambda *_args, **_options: _CompletedWorkerProcess(
                json.dumps(payload).encode("utf-8")
            ),
            clock=lambda: next(moments),
        )

        track = client.recognize()

        if track is None:
            self.fail("a valid match must produce a track")
        self.assertEqual(track.position, 17.0)

    def test_worker_client_rejects_capture_time_before_worker_launch(self):
        payload = {
            "matches": [{"id": "match", "offset": 12.0}],
            "track": {"key": "track", "title": "Song", "subtitle": "Artist"},
            "captureStartedAt": 999.0,
        }

        moments = iter((1_000.0, 1_010.0))
        client = ambient.AmbientWorkerClient(
            python_executable=Path("/runtime/bin/python"),
            worker_script=Path("/plugin/ambient_worker.py"),
            ffmpeg_executable=Path("/opt/homebrew/bin/ffmpeg"),
            process_factory=lambda *_args, **_options: _CompletedWorkerProcess(
                json.dumps(payload).encode("utf-8")
            ),
            clock=lambda: next(moments),
        )

        track = client.recognize()

        if track is None:
            self.fail("a valid match must produce a track")
        self.assertEqual(track.position, 22.0)

    def test_worker_client_rejects_capture_time_after_response(self):
        payload = {
            "matches": [{"id": "match", "offset": 12.0}],
            "track": {"key": "track", "title": "Song", "subtitle": "Artist"},
            "captureStartedAt": 1_011.0,
        }

        moments = iter((1_000.0, 1_010.0))
        client = ambient.AmbientWorkerClient(
            python_executable=Path("/runtime/bin/python"),
            worker_script=Path("/plugin/ambient_worker.py"),
            ffmpeg_executable=Path("/opt/homebrew/bin/ffmpeg"),
            process_factory=lambda *_args, **_options: _CompletedWorkerProcess(
                json.dumps(payload).encode("utf-8")
            ),
            clock=lambda: next(moments),
        )

        track = client.recognize()

        if track is None:
            self.fail("a valid match must produce a track")
        self.assertEqual(track.position, 22.0)

    def test_worker_starts_a_private_process_group_for_recorder_cancellation(self):
        calls = []
        payload = {
            "matches": [{"id": "match", "offset": 1.0}],
            "track": {
                "key": "track",
                "title": "Song",
                "subtitle": "Artist",
            },
        }

        def process_factory(arguments, **options):
            calls.append((arguments, options))
            return _CompletedWorkerProcess(json.dumps(payload).encode("utf-8"))

        client = ambient.AmbientWorkerClient(
            python_executable=Path("/runtime/bin/python"),
            worker_script=Path("/plugin/ambient_worker.py"),
            ffmpeg_executable=Path("/opt/homebrew/bin/ffmpeg"),
            process_factory=process_factory,
            clock=lambda: 1_000.0,
        )
        client.recognize()

        self.assertTrue(calls[0][1].get("start_new_session"))

    def test_worker_client_rejects_a_second_active_recognition(self):
        first_started = threading.Event()
        release_first = threading.Event()
        process_calls = []

        def block_first_read():
            first_started.set()
            release_first.wait(1.0)

        def process_factory(*args, **kwargs):
            process_calls.append((args, kwargs))
            return _CompletedWorkerProcess(
                b'{"matches":[],"track":{}}',
                before_read=block_first_read if len(process_calls) == 1 else None,
            )

        client = ambient.AmbientWorkerClient(
            python_executable=Path("/runtime/bin/python"),
            worker_script=Path("/plugin/ambient_worker.py"),
            ffmpeg_executable=Path("/opt/homebrew/bin/ffmpeg"),
            process_factory=process_factory,
            clock=lambda: 1_000.0,
        )
        first = threading.Thread(target=client.recognize)
        first.start()
        self.assertTrue(first_started.wait(1.0))

        try:
            with self.assertRaisesRegex(RuntimeError, "already active"):
                client.recognize()
        finally:
            release_first.set()
            first.join(1.0)

        self.assertEqual(len(process_calls), 1)
        self.assertFalse(first.is_alive())

    def test_worker_discards_dependency_diagnostics(self):
        calls = []

        def process_factory(arguments, **options):
            calls.append((arguments, options))
            return _CompletedWorkerProcess(b'{"matches":[],"track":{}}')

        client = ambient.AmbientWorkerClient(
            python_executable=Path("/runtime/bin/python"),
            worker_script=Path("/plugin/ambient_worker.py"),
            ffmpeg_executable=Path("/opt/homebrew/bin/ffmpeg"),
            process_factory=process_factory,
            clock=lambda: 1_000.0,
        )
        client.recognize()

        self.assertIs(calls[0][1].get("stderr"), subprocess.DEVNULL)

    def test_worker_client_does_not_forward_parent_secrets(self):
        worker_client = getattr(ambient, "AmbientWorkerClient", None)
        if worker_client is None:
            self.fail("ambient.AmbientWorkerClient is required")
        calls = []
        payload = {
            "matches": [{"id": "match", "offset": 0.0}],
            "track": {
                "key": "track",
                "title": "Song",
                "subtitle": "Artist",
            },
        }

        def process_factory(arguments, **options):
            calls.append((arguments, options))
            return _CompletedWorkerProcess(json.dumps(payload).encode("utf-8"))

        moments = iter((1_000.0, 1_001.0))
        client = worker_client(
            python_executable=Path("/private/recognizer/bin/python"),
            worker_script=Path("/private/plugin/ambient_worker.py"),
            ffmpeg_executable=Path("/opt/homebrew/bin/ffmpeg"),
            process_factory=process_factory,
            clock=lambda: next(moments),
        )
        parent_environment = {
            "HOME": "/Users/example",
            "TMPDIR": "/private/tmp/example",
            "LANG": "en_US.UTF-8",
            "HERMES_API_KEY": "must-not-leak",
            "OPENAI_API_KEY": "must-not-leak",
        }

        with patch.dict(os.environ, parent_environment, clear=True):
            client.recognize()

        child_environment = calls[0][1].get("env")
        self.assertIsNotNone(child_environment)
        self.assertNotIn("HERMES_API_KEY", child_environment)
        self.assertNotIn("OPENAI_API_KEY", child_environment)
        self.assertEqual(child_environment["HOME"], "/Users/example")
        self.assertIn("/opt/homebrew/bin", child_environment["PATH"])

    def test_ambient_source_is_idle_until_listen_is_requested(self):
        source_class = getattr(ambient, "AmbientSource", None)
        if source_class is None:
            self.fail("ambient.AmbientSource is required")

        class Recognizer:
            calls = 0

            def recognize(self, cancel_event=None):
                self.calls += 1
                return None

        recognizer = Recognizer()
        source = source_class(recognizer)

        track = source.sample()

        self.assertEqual(recognizer.calls, 0)
        self.assertEqual(track.source, "ambient")
        self.assertEqual(track.state, "ambient_idle")
        self.assertFalse(track.running)
        self.assertFalse(track.can_control)
        self.assertFalse(track.can_seek)

    def test_listen_enters_a_visible_background_listening_state(self):
        source_class = getattr(ambient, "AmbientSource", None)
        if source_class is None:
            self.fail("ambient.AmbientSource is required")
        recognition_started = threading.Event()
        release_recognition = threading.Event()

        class Recognizer:
            def recognize(self, cancel_event=None):
                recognition_started.set()
                release_recognition.wait(1.0)
                return None

        source = source_class(Recognizer(), clock=lambda: 1_000.0)
        listen = getattr(source, "listen", None)
        if not callable(listen):
            self.fail("AmbientSource.listen is required")

        listen()
        self.assertTrue(recognition_started.wait(1.0))
        track = source.sample()
        release_recognition.set()

        self.assertEqual(track.state, "listening")
        self.assertTrue(track.running)
        self.assertFalse(track.can_control)
        self.assertFalse(track.can_seek)

    def test_repeated_listen_while_active_stays_single_flight(self):
        created_threads = []

        class Recognizer:
            def recognize(self, cancel_event=None):
                return None

            def cancel(self):
                return None

        class DeferredThread:
            def __init__(self, *, target, daemon):
                self.target = target
                self.daemon = daemon
                created_threads.append(self)

            def start(self):
                return None

        source = ambient.AmbientSource(Recognizer())
        with patch.object(ambient.threading, "Thread", DeferredThread):
            source.listen()
            source.listen()

        self.assertEqual(len(created_threads), 1)

    def test_thread_start_failure_rolls_back_to_a_stoppable_idle_state(self):
        cancel_calls = []

        class Recognizer:
            def recognize(self, cancel_event=None):
                raise AssertionError("recognition must not run when thread start fails")

            def cancel(self):
                cancel_calls.append(True)

        class StartFailureThread:
            def __init__(self, *, target, daemon):
                self.target = target
                self.daemon = daemon

            def start(self):
                raise RuntimeError("private thread diagnostics")

            def join(self, timeout=None):
                raise AssertionError("a never-started thread must not be joined")

            def is_alive(self):
                raise AssertionError("a never-started thread must not be queried")

        source = ambient.AmbientSource(Recognizer(), clock=lambda: 1_000.0)
        with patch.object(ambient.threading, "Thread", StartFailureThread):
            with self.assertRaisesRegex(
                RuntimeError,
                "recognition operation could not start",
            ) as raised:
                source.listen()

        self.assertNotIn("private", str(raised.exception))
        track = source.sample()
        self.assertEqual(track.state, "ambient_idle")
        self.assertFalse(track.running)
        source.stop()
        self.assertEqual(source.sample().state, "ambient_idle")
        self.assertEqual(cancel_calls, [True])

    def test_completed_recognition_becomes_the_active_ambient_track(self):
        recognized = TrackInfo(
            running=True,
            state="playing",
            title="Matched Song",
            artist="Matched Artist",
            position=48.5,
            sampled_at=1_010.0,
            persistent_id="shazam:matched",
            source="ambient",
            can_control=False,
            can_seek=False,
        )

        class Recognizer:
            def recognize(self, cancel_event=None):
                return recognized

        source = ambient.AmbientSource(Recognizer(), clock=lambda: 1_010.0)

        source.listen()
        deadline = time.monotonic() + 1.0
        track = source.sample()
        while not track.title and time.monotonic() < deadline:
            time.sleep(0.001)
            track = source.sample()

        self.assertEqual(track.title, "Matched Song")
        self.assertEqual(track.artist, "Matched Artist")
        self.assertEqual(track.position, 48.5)
        self.assertEqual(track.persistent_id, "shazam:matched")
        self.assertEqual(track.state, "playing")

    def test_completed_recognition_without_a_match_reports_no_match(self):
        class Recognizer:
            def recognize(self, cancel_event=None):
                return None

        source = ambient.AmbientSource(Recognizer(), clock=lambda: 1_000.0)

        source.listen()
        deadline = time.monotonic() + 1.0
        track = source.sample()
        while track.state == "listening" and time.monotonic() < deadline:
            time.sleep(0.001)
            track = source.sample()

        self.assertEqual(track.state, "no_match")
        self.assertFalse(track.running)
        self.assertEqual(track.title, "")

    def test_selecting_ambient_source_does_not_start_listening(self):
        router_class = getattr(ambient, "PlaybackRouter", None)
        if router_class is None:
            self.fail("ambient.PlaybackRouter is required")

        class MusicSource:
            def sample(self):
                return TrackInfo(
                    running=True,
                    state="playing",
                    title="Music.app Song",
                    source="music_app",
                )

        class NearbySource:
            listen_calls = 0

            def sample(self):
                return TrackInfo(
                    running=False,
                    state="ambient_idle",
                    source="ambient",
                    can_control=False,
                    can_seek=False,
                )

            def listen(self):
                self.listen_calls += 1

        nearby = NearbySource()
        router = router_class(MusicSource(), nearby)

        router.select_source("ambient")
        track = router.sample()

        self.assertEqual(nearby.listen_calls, 0)
        self.assertEqual(track.source, "ambient")
        self.assertEqual(track.state, "ambient_idle")

    def test_switching_back_to_music_app_stops_ambient_listening(self):
        class MusicSource:
            def sample(self):
                return TrackInfo(running=False, state="idle")

        class NearbySource:
            stop_calls = 0

            def sample(self):
                return TrackInfo(
                    running=True,
                    state="listening",
                    source="ambient",
                    can_control=False,
                    can_seek=False,
                )

            def stop(self):
                self.stop_calls += 1

        nearby = NearbySource()
        router = ambient.PlaybackRouter(MusicSource(), nearby)
        router.select_source("ambient")
        router.select_source("music_app")

        self.assertEqual(nearby.stop_calls, 1)

    def test_reselecting_music_app_still_stops_the_nearby_source(self):
        class NearbySource:
            stop_calls = 0

            def stop(self):
                self.stop_calls += 1

        nearby = NearbySource()
        router = ambient.PlaybackRouter(object(), nearby)

        router.select_source("music_app")

        self.assertEqual(nearby.stop_calls, 1)

    def test_explicit_ambient_listen_reaches_the_nearby_source(self):
        class NearbySource:
            calls = 0

            def listen(self):
                self.calls += 1

        nearby = NearbySource()
        router = ambient.PlaybackRouter(object(), nearby)
        listen = getattr(router, "listen_ambient", None)
        if not callable(listen):
            self.fail("PlaybackRouter.listen_ambient is required")

        router.select_source("ambient")
        listen()

        self.assertEqual(nearby.calls, 1)

    def test_source_switch_waits_for_an_admitted_music_control(self):
        control_entered = threading.Event()
        release_control = threading.Event()
        switch_returned = threading.Event()

        class MusicSource:
            def control(self, action):
                self.action = action
                control_entered.set()
                release_control.wait(1.0)

        class NearbySource:
            def stop(self):
                return None

        music = MusicSource()
        router = ambient.PlaybackRouter(music, NearbySource())
        controller = threading.Thread(target=lambda: router.control("next"))
        controller.start()
        self.assertTrue(control_entered.wait(1.0))

        switcher = threading.Thread(
            target=lambda: (
                router.select_source("ambient"),
                switch_returned.set(),
            )
        )
        switcher.start()
        self.assertFalse(switch_returned.wait(0.05))

        release_control.set()
        controller.join(1.0)
        switcher.join(1.0)
        self.assertFalse(controller.is_alive())
        self.assertFalse(switcher.is_alive())
        self.assertTrue(switch_returned.is_set())
        self.assertEqual(music.action, "next")
        with self.assertRaisesRegex(ValueError, "cannot be controlled"):
            router.control("next")

    def test_source_switch_waits_for_an_admitted_music_seek(self):
        seek_entered = threading.Event()
        release_seek = threading.Event()
        switch_returned = threading.Event()

        class MusicSource:
            def seek(self, position):
                self.position = position
                seek_entered.set()
                release_seek.wait(1.0)

        class NearbySource:
            def stop(self):
                return None

        music = MusicSource()
        router = ambient.PlaybackRouter(music, NearbySource())
        seeker = threading.Thread(target=lambda: router.seek(42.5))
        seeker.start()
        self.assertTrue(seek_entered.wait(1.0))

        switcher = threading.Thread(
            target=lambda: (
                router.select_source("ambient"),
                switch_returned.set(),
            )
        )
        switcher.start()
        self.assertFalse(switch_returned.wait(0.05))

        release_seek.set()
        seeker.join(1.0)
        switcher.join(1.0)
        self.assertFalse(seeker.is_alive())
        self.assertFalse(switcher.is_alive())
        self.assertTrue(switch_returned.is_set())
        self.assertEqual(music.position, 42.5)
        with self.assertRaisesRegex(ValueError, "cannot be sought"):
            router.seek(43.0)

    def test_source_switch_waits_for_an_admitted_music_sample(self):
        sample_entered = threading.Event()
        release_sample = threading.Event()
        switch_returned = threading.Event()
        sample_results = []

        class MusicSource:
            def sample(self):
                sample_entered.set()
                release_sample.wait(1.0)
                return "music-sample"

        class NearbySource:
            def sample(self):
                return "ambient-sample"

            def stop(self):
                return None

        router = ambient.PlaybackRouter(MusicSource(), NearbySource())
        sampler = threading.Thread(target=lambda: sample_results.append(router.sample()))
        sampler.start()
        self.assertTrue(sample_entered.wait(1.0))

        switcher = threading.Thread(
            target=lambda: (
                router.select_source("ambient"),
                switch_returned.set(),
            )
        )
        switcher.start()
        self.assertFalse(switch_returned.wait(0.05))

        release_sample.set()
        sampler.join(1.0)
        switcher.join(1.0)
        self.assertFalse(sampler.is_alive())
        self.assertFalse(switcher.is_alive())
        self.assertEqual(sample_results, ["music-sample"])
        self.assertEqual(router.sample(), "ambient-sample")

    def test_ambient_listen_is_rejected_while_music_app_is_selected(self):
        class NearbySource:
            calls = 0

            def listen(self):
                self.calls += 1

        nearby = NearbySource()
        router = ambient.PlaybackRouter(object(), nearby)

        with self.assertRaisesRegex(ValueError, "select Nearby"):
            router.listen_ambient()

        self.assertEqual(nearby.calls, 0)

    def test_source_switch_waits_for_an_admitted_listen_call(self):
        listen_entered = threading.Event()
        release_listen = threading.Event()
        switch_returned = threading.Event()

        class NearbySource:
            def listen(self):
                listen_entered.set()
                release_listen.wait(1.0)

            def stop(self):
                return None

        router = ambient.PlaybackRouter(object(), NearbySource())
        router.select_source("ambient")
        listener = threading.Thread(target=router.listen_ambient)
        listener.start()
        self.assertTrue(listen_entered.wait(1.0))

        def switch_source():
            router.select_source("music_app")
            switch_returned.set()

        switcher = threading.Thread(target=switch_source)
        switcher.start()
        self.assertFalse(switch_returned.wait(0.05))
        release_listen.set()
        listener.join(1.0)
        switcher.join(1.0)

        self.assertTrue(switch_returned.is_set())
        self.assertFalse(listener.is_alive())
        self.assertFalse(switcher.is_alive())

    def test_explicit_ambient_stop_reaches_the_nearby_source(self):
        class NearbySource:
            calls = 0

            def stop(self):
                self.calls += 1

        nearby = NearbySource()
        router = ambient.PlaybackRouter(object(), nearby)
        stop = getattr(router, "stop_ambient", None)
        if not callable(stop):
            self.fail("PlaybackRouter.stop_ambient is required")

        stop()

        self.assertEqual(nearby.calls, 1)

    def test_router_shutdown_stops_the_nearby_source(self):
        class NearbySource:
            calls = 0

            def stop(self):
                self.calls += 1

        nearby = NearbySource()
        router = ambient.PlaybackRouter(object(), nearby)
        shutdown = getattr(router, "shutdown", None)
        if not callable(shutdown):
            self.fail("PlaybackRouter.shutdown is required")

        shutdown()

        self.assertEqual(nearby.calls, 1)

    def test_router_preserves_the_music_app_boundary_in_music_mode(self):
        calls = []
        artwork = object()

        class MusicSource:
            def control(self, action):
                calls.append(("control", action))

            def seek(self, position):
                calls.append(("seek", position))

            def artwork_for(self, identity):
                calls.append(("artwork", identity))
                return artwork

            def clear_artwork_cache(self):
                calls.append(("clear", None))

            def open_automation_settings(self):
                calls.append(("permissions", None))

        router = ambient.PlaybackRouter(MusicSource(), object())
        for method_name in (
            "control",
            "seek",
            "artwork_for",
            "clear_artwork_cache",
            "open_automation_settings",
        ):
            if not callable(getattr(router, method_name, None)):
                self.fail(f"PlaybackRouter.{method_name} is required")

        router.control("next")
        router.seek(12.5)
        returned_artwork = router.artwork_for("identity")
        router.clear_artwork_cache()
        router.open_automation_settings()

        self.assertIs(returned_artwork, artwork)
        self.assertEqual(
            calls,
            [
                ("control", "next"),
                ("seek", 12.5),
                ("artwork", "identity"),
                ("clear", None),
                ("permissions", None),
            ],
        )

    def test_ambient_mode_rejects_music_app_controls(self):
        class MusicSource:
            def control(self, action):
                self.action = action

        router = ambient.PlaybackRouter(MusicSource(), object())
        router.select_source("ambient")

        with self.assertRaisesRegex(ValueError, "ambient audio"):
            router.control("next")

    def test_ambient_mode_rejects_music_app_seeking(self):
        class MusicSource:
            def seek(self, position):
                self.position = position

        router = ambient.PlaybackRouter(MusicSource(), object())
        router.select_source("ambient")

        with self.assertRaisesRegex(ValueError, "ambient audio"):
            router.seek(12.5)

    def test_stop_cancels_recognition_and_returns_to_idle(self):
        class Recognizer:
            cancel_calls = 0

            def recognize(self, cancel_event=None):
                return None

            def cancel(self):
                self.cancel_calls += 1

        recognizer = Recognizer()
        source = ambient.AmbientSource(recognizer, clock=lambda: 1_000.0)
        stop = getattr(source, "stop", None)
        if not callable(stop):
            self.fail("AmbientSource.stop is required")

        stop()
        track = source.sample()

        self.assertEqual(recognizer.cancel_calls, 1)
        self.assertEqual(track.state, "ambient_idle")
        self.assertFalse(track.running)

    def test_result_arriving_after_stop_is_discarded(self):
        recognized = TrackInfo(
            running=True,
            state="playing",
            title="Late Song",
            artist="Late Artist",
            source="ambient",
            can_control=False,
            can_seek=False,
        )
        created_threads = []

        class Recognizer:
            def recognize(self, cancel_event=None):
                return recognized

            def cancel(self):
                return None

        class DeferredThread:
            def __init__(self, *, target, daemon):
                self.target = target
                self.daemon = daemon
                self.finished = False
                created_threads.append(self)

            def start(self):
                return None

            def join(self, timeout=None):
                self.target()
                self.finished = True

            def is_alive(self):
                return not self.finished

        source = ambient.AmbientSource(Recognizer(), clock=lambda: 1_000.0)
        with patch.object(ambient.threading, "Thread", DeferredThread):
            source.listen()
        source.stop()

        track = source.sample()

        self.assertEqual(track.state, "ambient_idle")
        self.assertEqual(track.title, "")

    def test_stop_before_worker_spawn_prevents_process_creation(self):
        recognition_entered = threading.Event()
        release_recognition = threading.Event()
        cancel_called = threading.Event()
        recognition_finished = threading.Event()
        process_calls = []

        client = ambient.AmbientWorkerClient(
            python_executable=Path("/private/recognizer/bin/python"),
            worker_script=Path("/private/plugin/ambient_worker.py"),
            ffmpeg_executable=Path("/opt/homebrew/bin/ffmpeg"),
            process_factory=lambda *args, **kwargs: (
                process_calls.append((args, kwargs))
                or _CompletedWorkerProcess(b'{"matches":[],"track":{}}')
            ),
            clock=lambda: 1_000.0,
        )

        class PausedRecognizer:
            def recognize(self, cancel_event=None):
                recognition_entered.set()
                release_recognition.wait(1.0)
                try:
                    if cancel_event is None:
                        return client.recognize()
                    recognize = cast(Callable[..., TrackInfo | None], client.recognize)
                    return recognize(cancel_event)
                finally:
                    recognition_finished.set()

            def cancel(self):
                cancel_called.set()
                client.cancel()

        source = ambient.AmbientSource(PausedRecognizer(), clock=lambda: 1_000.0)
        source.listen()
        self.assertTrue(recognition_entered.wait(1.0))

        stopper = threading.Thread(target=source.stop)
        stopper.start()
        self.assertTrue(cancel_called.wait(1.0))
        release_recognition.set()
        self.assertTrue(recognition_finished.wait(1.0))
        stopper.join(1.0)

        self.assertFalse(stopper.is_alive())
        self.assertEqual(process_calls, [])
        self.assertEqual(source.sample().state, "ambient_idle")

    def test_stop_waits_for_the_recognition_thread_to_finish(self):
        recognition_started = threading.Event()
        release_recognition = threading.Event()
        recognition_finished = threading.Event()
        cancel_called = threading.Event()
        stop_returned = threading.Event()

        class SlowRecognizer:
            def recognize(self, cancel_event=None):
                recognition_started.set()
                release_recognition.wait(1.0)
                recognition_finished.set()
                return None

            def cancel(self):
                cancel_called.set()

        source = ambient.AmbientSource(SlowRecognizer(), clock=lambda: 1_000.0)
        source.listen()
        self.assertTrue(recognition_started.wait(1.0))

        def stop_source():
            source.stop()
            stop_returned.set()

        stopper = threading.Thread(target=stop_source)
        stopper.start()
        self.assertTrue(cancel_called.wait(1.0))
        self.assertFalse(stop_returned.wait(0.05))
        release_recognition.set()
        self.assertTrue(stop_returned.wait(1.0))
        stopper.join(1.0)

        self.assertTrue(recognition_finished.is_set())
        self.assertFalse(stopper.is_alive())
        self.assertEqual(source.sample().state, "ambient_idle")

    def test_rapid_stop_then_listen_never_overlaps_recognition(self):
        first_started = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()
        cancel_called = threading.Event()
        state_lock = threading.Lock()
        calls = 0
        active = 0
        max_active = 0

        class SerialRecognizer:
            def recognize(self, cancel_event=None):
                nonlocal calls, active, max_active
                with state_lock:
                    calls += 1
                    operation = calls
                    active += 1
                    max_active = max(max_active, active)
                try:
                    if operation == 1:
                        first_started.set()
                        release_first.wait(1.0)
                    else:
                        second_started.set()
                    return None
                finally:
                    with state_lock:
                        active -= 1

            def cancel(self):
                cancel_called.set()

        source = ambient.AmbientSource(SerialRecognizer(), clock=lambda: 1_000.0)
        source.listen()
        self.assertTrue(first_started.wait(1.0))

        stopper = threading.Thread(target=source.stop)
        stopper.start()
        self.assertTrue(cancel_called.wait(1.0))
        source.listen()
        self.assertFalse(second_started.wait(0.05))

        release_first.set()
        stopper.join(1.0)
        self.assertFalse(stopper.is_alive())
        source.listen()
        self.assertTrue(second_started.wait(1.0))

        deadline = time.monotonic() + 1.0
        while source.sample().state == "listening" and time.monotonic() < deadline:
            time.sleep(0.001)

        self.assertEqual(calls, 2)
        self.assertEqual(max_active, 1)
        self.assertEqual(source.sample().state, "no_match")

    def test_worker_cancel_terminates_the_active_process(self):
        process_started = threading.Event()
        process_released = threading.Event()
        process_terminated = threading.Event()
        payload = {
            "matches": [{"id": "match", "offset": 0.0}],
            "track": {"key": "track", "title": "Song", "subtitle": "Artist"},
        }

        class ActiveProcess:
            returncode = None

            def __init__(self):
                def block_read():
                    process_started.set()
                    process_released.wait(1.0)

                self.stdout = _TestWorkerOutput(
                    json.dumps(payload).encode("utf-8"),
                    block_read,
                )

            def poll(self):
                return self.returncode

            def terminate(self):
                self.returncode = 0
                process_terminated.set()
                process_released.set()

            def wait(self, timeout):
                if not process_released.wait(timeout):
                    raise subprocess.TimeoutExpired("ambient_worker", timeout)
                return self.returncode

        moments = iter((1_000.0, 1_001.0))
        client = ambient.AmbientWorkerClient(
            python_executable=Path("/private/recognizer/bin/python"),
            worker_script=Path("/private/plugin/ambient_worker.py"),
            ffmpeg_executable=Path("/opt/homebrew/bin/ffmpeg"),
            process_factory=lambda *args, **kwargs: ActiveProcess(),
            clock=lambda: next(moments),
        )
        cancel = getattr(client, "cancel", None)
        if not callable(cancel):
            self.fail("AmbientWorkerClient.cancel is required")
        worker = threading.Thread(target=client.recognize)
        worker.start()
        self.assertTrue(process_started.wait(1.0))

        cancel()
        worker.join(1.0)

        self.assertTrue(process_terminated.is_set())
        self.assertFalse(worker.is_alive())

    def test_worker_cancel_signals_the_entire_process_group(self):
        process_started = threading.Event()
        process_released = threading.Event()
        payload = {
            "matches": [{"id": "match", "offset": 0.0}],
            "track": {"key": "track", "title": "Song", "subtitle": "Artist"},
        }

        class GroupProcess:
            pid = 4_242
            returncode = None

            def __init__(self):
                def block_read():
                    process_started.set()
                    process_released.wait(1.0)

                self.stdout = _TestWorkerOutput(
                    json.dumps(payload).encode("utf-8"),
                    block_read,
                )

            def poll(self):
                return self.returncode

            def wait(self, timeout):
                if not process_released.wait(timeout):
                    raise subprocess.TimeoutExpired("ambient_worker", timeout)
                self.returncode = 0
                return self.returncode

        moments = iter((1_000.0, 1_001.0))
        client = ambient.AmbientWorkerClient(
            python_executable=Path("/private/recognizer/bin/python"),
            worker_script=Path("/private/plugin/ambient_worker.py"),
            ffmpeg_executable=Path("/opt/homebrew/bin/ffmpeg"),
            process_factory=lambda *args, **kwargs: GroupProcess(),
            clock=lambda: next(moments),
        )

        group_alive = True

        def release_group(process_id, signal_number):
            nonlocal group_alive
            if signal_number == 0:
                if group_alive:
                    return None
                raise ProcessLookupError
            if signal_number == signal.SIGTERM:
                group_alive = False
                process_released.set()
            return None

        with patch.object(ambient.os, "killpg", side_effect=release_group) as kill_group:
            worker = threading.Thread(target=client.recognize)
            worker.start()
            self.assertTrue(process_started.wait(1.0))
            client.cancel()
            worker.join(1.0)

        self.assertIn((4_242, signal.SIGTERM), [call.args for call in kill_group.call_args_list])
        self.assertNotIn((4_242, signal.SIGKILL), [call.args for call in kill_group.call_args_list])
        self.assertFalse(worker.is_alive())

    def test_worker_cancel_kills_and_reaps_a_stubborn_process_group(self):
        process_started = threading.Event()
        process_released = threading.Event()
        signals = []

        class StubbornProcess:
            pid = 6_262
            returncode = None

            def __init__(self):
                self.wait_calls = []

                def block_read():
                    process_started.set()
                    process_released.wait(2.0)

                self.stdout = _TestWorkerOutput(
                    b'{"matches":[],"track":{}}',
                    block_read,
                )

            def poll(self):
                return self.returncode

            def wait(self, timeout):
                self.wait_calls.append(timeout)
                if len(self.wait_calls) == 1:
                    raise subprocess.TimeoutExpired("ambient_worker", timeout)
                self.returncode = -signal.SIGKILL
                return self.returncode

        process = StubbornProcess()
        client = ambient.AmbientWorkerClient(
            python_executable=Path("/private/recognizer/bin/python"),
            worker_script=Path("/private/plugin/ambient_worker.py"),
            ffmpeg_executable=Path("/opt/homebrew/bin/ffmpeg"),
            process_factory=lambda *args, **kwargs: process,
            clock=lambda: 1_000.0,
        )

        group_alive = True

        def signal_group(process_id, signal_number):
            nonlocal group_alive
            if signal_number == 0:
                if group_alive:
                    return None
                raise ProcessLookupError
            signals.append((process_id, signal_number))
            if signal_number == signal.SIGKILL:
                group_alive = False
                process.returncode = -signal.SIGKILL
                process_released.set()
            return None

        def run_recognition():
            try:
                client.recognize()
            except RuntimeError:
                pass

        with patch.object(ambient.os, "killpg", side_effect=signal_group):
            worker = threading.Thread(target=run_recognition)
            worker.start()
            self.assertTrue(process_started.wait(1.0))
            client.cancel()
            worker.join(1.0)

        self.assertEqual(
            signals,
            [(6_262, signal.SIGTERM), (6_262, signal.SIGKILL)],
        )
        self.assertEqual(len(process.wait_calls), 2)
        self.assertFalse(worker.is_alive())

    def test_worker_cancel_waits_for_a_stubborn_descendant_group_to_disappear(self):
        import sys
        import tempfile

        worker_source = """
import pathlib
import signal
import subprocess
import sys
import time

marker = pathlib.Path(sys.argv[sys.argv.index('--ffmpeg') + 1])
child_source = '''
import pathlib
import signal
import sys
import time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
pathlib.Path(sys.argv[1]).write_text(str(__import__('os').getpid()), encoding='utf-8')
while True:
    time.sleep(1.0)
'''
subprocess.Popen(
    [sys.executable, '-I', '-c', child_source, str(marker)],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    shell=False,
)
while True:
    time.sleep(1.0)
"""

        def process_group_exists(process_group_id: int) -> bool:
            try:
                os.killpg(process_group_id, 0)
            except ProcessLookupError:
                return False
            except PermissionError:
                return True
            return True

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worker_script = root / "stubborn_worker.py"
            marker = root / "recorder-ready"
            worker_script.write_text(worker_source, encoding="utf-8")
            processes = []

            def process_factory(*args, **kwargs):
                process = subprocess.Popen(*args, **kwargs)
                processes.append(process)
                return process

            client = ambient.AmbientWorkerClient(
                python_executable=Path(sys.executable),
                worker_script=worker_script,
                ffmpeg_executable=marker,
                process_factory=process_factory,
                clock=time.time,
            )

            def run_recognition():
                try:
                    client.recognize()
                except RuntimeError:
                    pass

            worker = threading.Thread(target=run_recognition)
            worker.start()
            deadline = time.monotonic() + 2.0
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists(), "stubborn recorder surrogate did not start")
            process_group_id = processes[0].pid

            try:
                client.cancel()
                worker.join(2.0)

                self.assertFalse(worker.is_alive())
                self.assertFalse(process_group_exists(process_group_id))
            finally:
                if process_group_exists(process_group_id):
                    try:
                        os.killpg(process_group_id, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    cleanup_deadline = time.monotonic() + 2.0
                    while (
                        process_group_exists(process_group_id)
                        and time.monotonic() < cleanup_deadline
                    ):
                        time.sleep(0.01)

    def test_explicit_stop_waits_for_a_stubborn_descendant_group(self):
        with tempfile.TemporaryDirectory() as temporary:
            client, marker, processes = _stubborn_worker_client(Path(temporary))
            source = ambient.AmbientSource(client, clock=time.time)
            source.listen()
            self.assertTrue(_wait_for_marker(marker))
            process_group_id = processes[0].pid

            try:
                with patch.object(ambient, "_PROCESS_TERM_TIMEOUT", 0.1):
                    source.stop()

                self.assertEqual(source.sample().state, "ambient_idle")
                self.assertFalse(_process_group_exists_for_test(process_group_id))
            finally:
                _cleanup_process_group(process_group_id)

    def test_source_switch_waits_for_a_stubborn_descendant_group(self):
        with tempfile.TemporaryDirectory() as temporary:
            client, marker, processes = _stubborn_worker_client(Path(temporary))
            source = ambient.AmbientSource(client, clock=time.time)
            router = ambient.PlaybackRouter(object(), source)
            router.select_source("ambient")
            router.listen_ambient()
            self.assertTrue(_wait_for_marker(marker))
            process_group_id = processes[0].pid

            try:
                with patch.object(ambient, "_PROCESS_TERM_TIMEOUT", 0.1):
                    router.select_source("music_app")

                self.assertEqual(source.sample().state, "ambient_idle")
                self.assertFalse(_process_group_exists_for_test(process_group_id))
            finally:
                _cleanup_process_group(process_group_id)

    def test_shutdown_waits_for_a_stubborn_descendant_group(self):
        with tempfile.TemporaryDirectory() as temporary:
            client, marker, processes = _stubborn_worker_client(Path(temporary))
            source = ambient.AmbientSource(client, clock=time.time)
            router = ambient.PlaybackRouter(object(), source)
            router.select_source("ambient")
            router.listen_ambient()
            self.assertTrue(_wait_for_marker(marker))
            process_group_id = processes[0].pid

            try:
                with patch.object(ambient, "_PROCESS_TERM_TIMEOUT", 0.1):
                    router.shutdown()

                self.assertEqual(source.sample().state, "ambient_idle")
                self.assertFalse(_process_group_exists_for_test(process_group_id))
            finally:
                _cleanup_process_group(process_group_id)

    def test_worker_timeout_waits_for_a_stubborn_descendant_group(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "recorder-ready"

            class ShortTimeoutProcess:
                def __init__(self, process):
                    self._process = process
                    self.stdout = process.stdout

                @property
                def pid(self):
                    return self._process.pid

                @property
                def returncode(self):
                    return self._process.returncode

                def poll(self):
                    return self._process.poll()

                def wait(self, timeout):
                    return self._process.wait(timeout=timeout)

            client, actual_marker, processes = _stubborn_worker_client(
                root,
                process_wrapper=ShortTimeoutProcess,
            )
            self.assertEqual(actual_marker, marker)

            try:
                with (
                    patch.object(ambient, "_PROCESS_TERM_TIMEOUT", 0.1),
                    patch.object(ambient, "_WORKER_TIMEOUT_SECONDS", 0.2),
                    self.assertRaisesRegex(RuntimeError, "timed out"),
                ):
                    client.recognize()

                self.assertTrue(marker.exists())
                self.assertFalse(
                    _process_group_exists_for_test(processes[0].pid)
                )
            finally:
                if processes:
                    _cleanup_process_group(processes[0].pid)

    def test_worker_timeout_signals_the_entire_process_group(self):
        class HungProcess:
            pid = 5_151
            returncode = None

            def __init__(self):
                self.wait_calls = []
                self.stdout = _TestWorkerOutput(
                    b"",
                    lambda: time.sleep(0.1),
                )

            def poll(self):
                return self.returncode

            def wait(self, timeout):
                self.wait_calls.append(timeout)
                self.returncode = -signal.SIGTERM
                return self.returncode

        process = HungProcess()
        client = ambient.AmbientWorkerClient(
            python_executable=Path("/private/recognizer/bin/python"),
            worker_script=Path("/private/plugin/ambient_worker.py"),
            ffmpeg_executable=Path("/opt/homebrew/bin/ffmpeg"),
            process_factory=lambda *args, **kwargs: process,
            clock=lambda: 1_000.0,
        )

        group_alive = True

        def signal_group(process_id, signal_number):
            nonlocal group_alive
            if signal_number == 0:
                if group_alive:
                    return None
                raise ProcessLookupError
            if signal_number == signal.SIGTERM:
                group_alive = False
            return None

        with patch.object(
            ambient.os,
            "killpg",
            side_effect=signal_group,
        ) as kill_group:
            with (
                patch.object(ambient, "_WORKER_TIMEOUT_SECONDS", 0.01),
                self.assertRaisesRegex(RuntimeError, "timed out"),
            ):
                client.recognize()

        self.assertIn(
            (5_151, signal.SIGTERM),
            [call.args for call in kill_group.call_args_list],
        )
        self.assertEqual(len(process.wait_calls), 1)
        self.assertGreater(process.wait_calls[0], 0)
        self.assertLessEqual(process.wait_calls[0], 1.0)

    def test_worker_output_overflow_is_bounded_before_materialization(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worker_script = root / "unbounded_writer.py"
            worker_script.write_text(
                """
import os
import sys
while True:
    os.write(sys.stdout.fileno(), b'x' * 4096)
""",
                encoding="utf-8",
            )
            processes = []

            class WriterProcess:
                def __init__(self, process):
                    self._process = process
                    self.stdout = process.stdout
                    self.communicate_calls = 0

                @property
                def pid(self):
                    return self._process.pid

                @property
                def returncode(self):
                    return self._process.returncode

                def communicate(self, timeout):
                    self.communicate_calls += 1
                    return self._process.communicate(timeout=0.25)

                def poll(self):
                    return self._process.poll()

                def wait(self, timeout):
                    return self._process.wait(timeout=timeout)

            def process_factory(*args, **kwargs):
                process = WriterProcess(subprocess.Popen(*args, **kwargs))
                processes.append(process)
                return process

            client = ambient.AmbientWorkerClient(
                python_executable=Path(sys.executable),
                worker_script=worker_script,
                ffmpeg_executable=root / "unused-ffmpeg",
                process_factory=process_factory,
                clock=time.time,
            )

            started_at = time.monotonic()
            try:
                with self.assertRaisesRegex(RuntimeError, "output exceeds"):
                    client.recognize()

                self.assertLess(time.monotonic() - started_at, 2.0)
                self.assertEqual(processes[0].communicate_calls, 0)
                self.assertFalse(
                    _process_group_exists_for_test(processes[0].pid)
                )
                self.assertTrue(processes[0].stdout.closed)
            finally:
                if processes:
                    _cleanup_process_group(processes[0].pid)

    def test_worker_client_rejects_oversized_worker_output(self):
        payload = {
            "matches": [{"id": "match", "offset": 0.0}],
            "track": {
                "key": "track",
                "title": "x" * 17_000,
                "subtitle": "Artist",
            },
        }

        client = ambient.AmbientWorkerClient(
            python_executable=Path("/private/recognizer/bin/python"),
            worker_script=Path("/private/plugin/ambient_worker.py"),
            ffmpeg_executable=Path("/opt/homebrew/bin/ffmpeg"),
            process_factory=lambda *args, **kwargs: _CompletedWorkerProcess(
                json.dumps(payload).encode("utf-8")
            ),
            clock=lambda: 1_000.0,
        )

        with self.assertRaisesRegex(RuntimeError, "worker output exceeds"):
            client.recognize()

    def test_worker_client_hides_nonzero_exit_details(self):
        client = ambient.AmbientWorkerClient(
            python_executable=Path("/private/recognizer/bin/python"),
            worker_script=Path("/private/plugin/ambient_worker.py"),
            ffmpeg_executable=Path("/opt/homebrew/bin/ffmpeg"),
            process_factory=lambda *args, **kwargs: _CompletedWorkerProcess(
                b'{"matches":[],"track":{}}',
                returncode=1,
            ),
            clock=lambda: 1_000.0,
        )

        with self.assertRaisesRegex(RuntimeError, "recognition worker failed") as raised:
            client.recognize()

        self.assertNotIn("private device details", str(raised.exception))

    def test_source_converts_worker_exceptions_to_a_safe_error_state(self):
        created_threads = []

        class Recognizer:
            def recognize(self, cancel_event=None):
                raise RuntimeError("private provider diagnostics")

            def cancel(self):
                return None

        class DeferredThread:
            def __init__(self, *, target, daemon):
                self.target = target
                created_threads.append(self)

            def start(self):
                return None

        source = ambient.AmbientSource(Recognizer(), clock=lambda: 1_000.0)
        with patch.object(ambient.threading, "Thread", DeferredThread):
            source.listen()
        try:
            created_threads[0].target()
        except RuntimeError:
            pass

        track = source.sample()

        self.assertEqual(track.state, "recognition_error")
        self.assertEqual(track.error, "ambient_recognition")
        self.assertNotIn("private", track.error or "")


if __name__ == "__main__":
    unittest.main()
