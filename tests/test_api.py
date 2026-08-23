import asyncio
import concurrent.futures
import json
import runpy
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, HTTPException, Response


async def asgi_get(app, path, query_string=""):
    messages = []
    received = False

    async def receive():
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": query_string.encode("ascii"),
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 80),
        },
        receive,
        send,
    )
    start = next(message for message in messages if message["type"] == "http.response.start")
    return start["status"], {key.decode(): value.decode() for key, value in start["headers"]}


class FakeService:
    def __init__(self):
        self.calls = []
        self.has_artwork = True
        self.artwork_error: Exception | None = None
        self.state_error: Exception | None = None
        self.refresh_error: Exception | None = None

    def state(self):
        if self.state_error is not None:
            raise self.state_error
        return {"status": "ready"}

    def artwork(self, identity):
        self.calls.append(("artwork", identity))
        if self.artwork_error is not None:
            raise self.artwork_error
        if not self.has_artwork:
            return None
        return {"identity": identity, "data_url": "data:image/jpeg;base64,/9j/"}

    def control(self, action):
        self.calls.append(("control", action))
        if action == "bad":
            raise ValueError("bad action")

    def seek(self, position):
        self.calls.append(("seek", position))

    def refresh(self):
        self.calls.append(("refresh", None))
        if self.refresh_error is not None:
            raise self.refresh_error

    def open_automation_settings(self):
        self.calls.append(("permissions", None))

    def select_source(self, source):
        self.calls.append(("source", source))

    def listen_ambient(self):
        self.calls.append(("ambient_listen", None))

    def stop_ambient(self):
        self.calls.append(("ambient_stop", None))


class PluginAPITests(unittest.TestCase):
    def test_manifest_uses_the_current_dashboard_api_contract(self):
        manifest_path = Path(__file__).resolve().parents[1] / "dashboard" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["name"], "apple-music-lyrics")
        self.assertEqual(manifest["api"], "plugin_api.py")
        self.assertTrue(manifest["tab"]["hidden"])
        entry = manifest_path.parent / manifest["entry"]
        self.assertTrue(entry.is_file(), entry)

    def test_router_exposes_state_controls_seek_refresh_permissions_and_health(self):
        from dashboard import plugin_api

        paths = {route.path for route in plugin_api.router.routes}

        self.assertEqual(
            paths,
            {
                "/state",
                "/artwork",
                "/control",
                "/seek",
                "/refresh",
                "/permissions",
                "/source",
                "/ambient/listen",
                "/ambient/stop",
                "/health",
            },
        )

    def test_default_service_routes_music_through_the_ambient_playback_router(self):
        from dashboard import plugin_api
        from dashboard.apple_music_lyrics_backend.ambient import PlaybackRouter

        self.assertIsInstance(plugin_api._service.music, PlaybackRouter)

    def test_router_registers_backend_shutdown_cleanup(self):
        from dashboard import plugin_api

        calls = []

        class Playback:
            def shutdown(self):
                calls.append("shutdown")

        handlers = [
            handler
            for handler in plugin_api.router.on_shutdown
            if getattr(handler, "__name__", "") == "_shutdown_plugin"
        ]
        self.assertEqual(len(handlers), 1)

        previous = plugin_api._playback
        plugin_api._playback = Playback()
        try:
            handlers[0]()
        finally:
            plugin_api._playback = previous

        self.assertEqual(calls, ["shutdown"])

    def test_backend_does_not_select_ffmpeg_from_inherited_path(self):
        from dashboard import plugin_api

        source = Path(plugin_api.__file__).read_text(encoding="utf-8")
        self.assertNotIn('shutil.which("ffmpeg")', source)

    def test_health_discloses_experimental_ambient_provider_and_readiness(self):
        from dashboard import plugin_api
        from dashboard.apple_music_lyrics_backend.ambient import AmbientRuntimePaths

        previous_paths = plugin_api._ambient_paths
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            python = root / "python"
            worker = root / "ambient_worker.py"
            ffmpeg = root / "ffmpeg"
            for executable in (python, ffmpeg):
                executable.write_text("", encoding="utf-8")
                executable.chmod(0o755)
            worker.write_text("", encoding="utf-8")
            plugin_api._ambient_paths = AmbientRuntimePaths(
                python_executable=python,
                worker_script=worker,
                ffmpeg_executable=ffmpeg,
            )
            try:
                with patch.object(plugin_api.platform, "system", return_value="Darwin"):
                    health = asyncio.run(plugin_api.get_health())
            finally:
                plugin_api._ambient_paths = previous_paths

        self.assertIn("ambientRecognition", health)
        recognition = health["ambientRecognition"]
        self.assertTrue(recognition["available"])
        self.assertTrue(recognition["experimental"])
        self.assertEqual(recognition["provider"], "ShazamIO")
        self.assertEqual(
            recognition["networkPayload"],
            "audio_fingerprint_and_protocol_metadata",
        )
        self.assertEqual(recognition["lyricsLookup"], "track_metadata_to_lrclib")
        self.assertEqual(recognition["sampleSeconds"], 8)

    def test_endpoint_functions_delegate_to_service(self):
        from dashboard import plugin_api

        previous = plugin_api._service
        fake = FakeService()
        plugin_api._service = fake
        try:
            state_response = Response()
            artwork_response = Response()
            state = asyncio.run(plugin_api.get_state(state_response))
            identity = "a" * 24
            artwork = asyncio.run(
                plugin_api.get_artwork(artwork_response, identity=identity)
            )
            control = asyncio.run(plugin_api.post_control({"action": "next"}))
            seek = asyncio.run(plugin_api.post_seek({"position": 22.5}))
            refresh = asyncio.run(plugin_api.post_refresh(Response()))
            permissions = asyncio.run(plugin_api.post_permissions())
            source = asyncio.run(plugin_api.post_source({"source": "ambient"}))
            listen = asyncio.run(plugin_api.post_ambient_listen())
            stop = asyncio.run(plugin_api.post_ambient_stop())
        finally:
            plugin_api._service = previous

        self.assertEqual(state, {"status": "ready"})
        self.assertEqual(artwork["artwork"]["identity"], identity)
        self.assertEqual(state_response.headers["cache-control"], "private, no-store")
        self.assertEqual(artwork_response.headers["cache-control"], "private, no-store")
        self.assertEqual(control, {"ok": True})
        self.assertEqual(seek, {"ok": True})
        self.assertEqual(refresh, {"ok": True})
        self.assertEqual(permissions, {"ok": True})
        self.assertEqual(source, {"ok": True})
        self.assertEqual(listen, {"ok": True})
        self.assertEqual(stop, {"ok": True})
        self.assertEqual(
            fake.calls,
            [
                ("artwork", identity),
                ("control", "next"),
                ("seek", 22.5),
                ("refresh", None),
                ("permissions", None),
                ("source", "ambient"),
                ("ambient_listen", None),
                ("ambient_stop", None),
            ],
        )

    def test_artwork_endpoint_rejects_unbounded_track_identities(self):
        from dashboard import plugin_api

        with self.assertRaises(HTTPException) as raised:
            asyncio.run(
                plugin_api.get_artwork(Response(), identity="not-a-track-identity")
            )

        self.assertEqual(raised.exception.status_code, 400)

    def test_seek_rejects_integers_too_large_for_a_float(self):
        from dashboard import plugin_api

        previous = plugin_api._service
        fake = FakeService()
        plugin_api._service = fake
        try:
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(plugin_api.post_seek({"position": 10**400}))
        finally:
            plugin_api._service = previous

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.detail, "position must be a number")
        self.assertNotIn(("seek", 10**400), fake.calls)

    def test_source_rejects_unknown_or_non_string_values(self):
        from dashboard import plugin_api

        previous = plugin_api._service
        fake = FakeService()
        plugin_api._service = fake
        try:
            for value in (None, True, "nearby"):
                with self.subTest(value=value):
                    with self.assertRaises(HTTPException) as raised:
                        asyncio.run(plugin_api.post_source({"source": value}))
                    self.assertEqual(raised.exception.status_code, 400)
        finally:
            plugin_api._service = previous

        self.assertFalse(any(call[0] == "source" for call in fake.calls))

    def test_source_switch_maps_a_stop_failure_to_service_unavailable(self):
        from dashboard import plugin_api

        class SourceFailureService(FakeService):
            def select_source(self, source):
                raise RuntimeError("private process diagnostics")

        previous = plugin_api._service
        plugin_api._service = SourceFailureService()
        try:
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(plugin_api.post_source({"source": "music_app"}))
        finally:
            plugin_api._service = previous

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.detail, "unable to switch source safely")
        self.assertNotIn("private", raised.exception.detail)

    def test_ambient_listen_maps_wrong_source_to_a_conflict(self):
        from dashboard import plugin_api

        class WrongSourceService(FakeService):
            def listen_ambient(self):
                raise ValueError("select Nearby before listening")

        previous = plugin_api._service
        plugin_api._service = WrongSourceService()
        try:
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(plugin_api.post_ambient_listen())
        finally:
            plugin_api._service = previous

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail, "select Nearby before listening")

    def test_ambient_listen_maps_startup_failure_to_service_unavailable(self):
        from dashboard import plugin_api

        class ListenFailureService(FakeService):
            def listen_ambient(self):
                raise RuntimeError("private thread diagnostics")

        previous = plugin_api._service
        plugin_api._service = ListenFailureService()
        try:
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(plugin_api.post_ambient_listen())
        finally:
            plugin_api._service = previous

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(
            raised.exception.detail,
            "recognition could not start",
        )
        headers = raised.exception.headers or {}
        self.assertEqual(headers.get("Cache-Control"), "private, no-store")
        self.assertEqual(headers.get("Retry-After"), "1")
        self.assertNotIn("private", raised.exception.detail)

    def test_ambient_stop_maps_an_incomplete_barrier_to_service_unavailable(self):
        from dashboard import plugin_api

        class StopFailureService(FakeService):
            def stop_ambient(self):
                raise RuntimeError("private process diagnostics")

        previous = plugin_api._service
        plugin_api._service = StopFailureService()
        try:
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(plugin_api.post_ambient_stop())
        finally:
            plugin_api._service = previous

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.detail, "recognition is still stopping")
        self.assertNotIn("private", raised.exception.detail)

    def test_seek_rejects_json_booleans(self):
        from dashboard import plugin_api

        previous = plugin_api._service
        fake = FakeService()
        plugin_api._service = fake
        try:
            for position in (True, False):
                with self.subTest(position=position):
                    with self.assertRaises(HTTPException) as raised:
                        asyncio.run(plugin_api.post_seek({"position": position}))
                    self.assertEqual(raised.exception.status_code, 400)
                    self.assertEqual(
                        raised.exception.detail,
                        "position must be a number",
                    )
        finally:
            plugin_api._service = previous

        self.assertFalse(any(call[0] == "seek" for call in fake.calls))

    def test_unavailable_artwork_is_not_returned_as_a_cacheable_null(self):
        from dashboard import plugin_api

        previous = plugin_api._service
        fake = FakeService()
        fake.has_artwork = False
        plugin_api._service = fake
        try:
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(
                    plugin_api.get_artwork(Response(), identity="a" * 24)
                )
        finally:
            plugin_api._service = previous

        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(
            raised.exception.headers,
            {"Cache-Control": "private, no-store"},
        )

    def test_fastapi_request_validation_and_no_store_headers(self):
        from dashboard import plugin_api

        previous = plugin_api._service
        plugin_api._service = FakeService()
        app = FastAPI()
        app.include_router(plugin_api.router)
        try:
            state_status, state_headers = asyncio.run(asgi_get(app, "/state"))
            artwork_status, artwork_headers = asyncio.run(
                asgi_get(app, "/artwork", f"identity={'a' * 24}")
            )
            invalid_status, invalid_headers = asyncio.run(
                asgi_get(app, "/artwork", "identity=not-valid")
            )
            missing_status, missing_headers = asyncio.run(asgi_get(app, "/artwork"))
        finally:
            plugin_api._service = previous

        self.assertEqual(state_status, 200)
        self.assertEqual(artwork_status, 200)
        self.assertEqual(invalid_status, 400)
        self.assertEqual(missing_status, 400)
        self.assertEqual(state_headers["cache-control"], "private, no-store")
        self.assertEqual(artwork_headers["cache-control"], "private, no-store")
        self.assertEqual(invalid_headers["cache-control"], "private, no-store")
        self.assertEqual(missing_headers["cache-control"], "private, no-store")

    def test_state_failures_are_controlled_and_not_cacheable(self):
        from dashboard import plugin_api

        previous = plugin_api._service
        fake = FakeService()
        fake.state_error = RuntimeError("injected state failure")
        plugin_api._service = fake
        app = FastAPI()
        app.include_router(plugin_api.router)
        try:
            status, headers = asyncio.run(asgi_get(app, "/state"))
        finally:
            plugin_api._service = previous

        self.assertEqual(status, 503)
        self.assertEqual(headers["cache-control"], "private, no-store")
        self.assertEqual(headers["retry-after"], "1")

    def test_artwork_admission_happens_before_the_shared_executor_queue(self):
        from dashboard import plugin_api

        class SlowService(FakeService):
            def artwork(self, identity):
                self.calls.append(("artwork", identity))
                time.sleep(0.03)
                return {"identity": identity, "data_url": "data:image/jpeg;base64,/9j/"}

        async def exercise():
            loop = asyncio.get_running_loop()
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            loop.set_default_executor(executor)
            try:
                calls = [
                    plugin_api.get_artwork(Response(), identity="a" * 24)
                    for _ in range(8)
                ]
                return await asyncio.gather(*calls, return_exceptions=True)
            finally:
                executor.shutdown(wait=True)

        previous = plugin_api._service
        fake = SlowService()
        plugin_api._service = fake
        try:
            outcomes = asyncio.run(exercise())
        finally:
            plugin_api._service = previous

        successes = [outcome for outcome in outcomes if isinstance(outcome, dict)]
        failures = [outcome for outcome in outcomes if isinstance(outcome, HTTPException)]
        self.assertEqual(len(successes), 1)
        self.assertEqual([failure.status_code for failure in failures], [503] * 7)
        self.assertEqual(fake.calls, [("artwork", "a" * 24)])

    def test_transient_artwork_failure_is_retryable_and_not_cacheable(self):
        from dashboard import plugin_api
        from dashboard.apple_music_lyrics_backend.service import ArtworkBusyError

        previous = plugin_api._service
        fake = FakeService()
        fake.artwork_error = ArtworkBusyError("artwork extraction already in progress")
        plugin_api._service = fake
        app = FastAPI()
        app.include_router(plugin_api.router)
        try:
            status, headers = asyncio.run(
                asgi_get(app, "/artwork", f"identity={'a' * 24}")
            )
        finally:
            plugin_api._service = previous

        self.assertEqual(status, 503)
        self.assertEqual(headers["cache-control"], "private, no-store")
        self.assertEqual(headers["retry-after"], "1")

    def test_busy_refresh_fails_fast_and_is_not_cacheable(self):
        from dashboard import plugin_api
        from dashboard.apple_music_lyrics_backend.service import ArtworkBusyError

        previous = plugin_api._service
        fake = FakeService()
        fake.refresh_error = ArtworkBusyError("refresh busy")
        plugin_api._service = fake
        try:
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(plugin_api.post_refresh(Response()))
        finally:
            plugin_api._service = previous

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(
            raised.exception.headers,
            {"Cache-Control": "private, no-store", "Retry-After": "1"},
        )
        self.assertFalse(plugin_api._artwork_admission.locked())

    def test_refresh_does_not_race_an_admitted_artwork_request(self):
        from dashboard import plugin_api

        previous = plugin_api._service
        fake = FakeService()
        plugin_api._service = fake
        self.assertTrue(plugin_api._artwork_admission.acquire(blocking=False))
        try:
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(plugin_api.post_refresh(Response()))
        finally:
            plugin_api._artwork_admission.release()
            plugin_api._service = previous

        self.assertEqual(raised.exception.status_code, 503)
        self.assertNotIn(("refresh", None), fake.calls)

    def test_stale_artwork_identity_is_not_cacheable_or_retryable(self):
        from dashboard import plugin_api
        from dashboard.apple_music_lyrics_backend.service import (
            ArtworkStaleIdentityError,
        )

        previous = plugin_api._service
        fake = FakeService()
        fake.artwork_error = ArtworkStaleIdentityError("stale")
        plugin_api._service = fake
        app = FastAPI()
        app.include_router(plugin_api.router)
        try:
            status, headers = asyncio.run(
                asgi_get(app, "/artwork", f"identity={'a' * 24}")
            )
        finally:
            plugin_api._service = previous

        self.assertEqual(status, 409)
        self.assertEqual(headers["cache-control"], "private, no-store")
        self.assertNotIn("retry-after", headers)

    def test_release_version_is_consistent(self):
        from dashboard import plugin_api
        from dashboard.apple_music_lyrics_backend.lrclib import CLIENT_HEADER

        root = Path(__file__).resolve().parents[1]
        manifest_version = json.loads(
            (root / "dashboard" / "manifest.json").read_text(encoding="utf-8")
        )["version"]
        project_version = tomllib.loads(
            (root / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]["version"]
        plugin_version = next(
            line.split(":", 1)[1].strip()
            for line in (root / "plugin.yaml").read_text(encoding="utf-8").splitlines()
            if line.startswith("version:")
        )

        self.assertEqual(
            {manifest_version, project_version, plugin_version, plugin_api.VERSION},
            {"0.2.0"},
        )
        self.assertTrue(
            CLIENT_HEADER.startswith(f"HermesAppleMusicLyrics/{manifest_version} ")
        )

    def test_real_smoke_script_does_not_emit_personal_track_metadata(self):
        root = Path(__file__).resolve().parents[1]
        script = root / "scripts" / "smoke_test.py"
        source = script.read_text(encoding="utf-8")

        for field in ("title", "artist", "album", "duration"):
            self.assertNotIn(f'track.get("{field}")', source)
        self.assertNotIn('lyrics.get("source")', source)
        self.assertNotIn('lyrics.get("lines")', source)
        self.assertIn('"lyric_text_emitted": False', source)

        summary = runpy.run_path(str(script))["privacy_safe_summary"](
            {
                "status": "ready",
                "track": {
                    "running": True,
                    "state": "playing",
                    "identity": "a" * 24,
                },
                "lyrics": {"synced": True, "word_timing": "line"},
                "artwork": {"remote_url": "https://is1-ssl.mzstatic.com/cover.jpg"},
            }
        )

        def leaves(value):
            if isinstance(value, dict):
                for child in value.values():
                    yield from leaves(child)
            else:
                yield value

        self.assertTrue(all(isinstance(value, (bool, int)) for value in leaves(summary)))

    def test_smoke_summary_does_not_report_empty_not_found_lyrics(self):
        root = Path(__file__).resolve().parents[1]
        script = root / "scripts" / "smoke_test.py"
        summary = runpy.run_path(str(script))["privacy_safe_summary"](
            {
                "status": "not_found",
                "track": {"running": True, "state": "playing"},
                "lyrics": {
                    "lines": [],
                    "plain_text": "",
                    "source": None,
                    "synced": False,
                    "word_timing": "none",
                },
                "artwork": {"remote_url": None},
            }
        )

        self.assertFalse(summary["lyrics"]["available"])


if __name__ == "__main__":
    unittest.main()
