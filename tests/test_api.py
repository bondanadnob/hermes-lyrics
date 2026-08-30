import asyncio
import concurrent.futures
import json
import runpy
import time
import tomllib
import unittest
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response


async def asgi_request(
    app,
    path,
    *,
    method="GET",
    query_string="",
    body=b"",
    client=("127.0.0.1", 1),
    headers=None,
    include_body=False,
):
    messages = []
    received = False

    async def receive():
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": query_string.encode("ascii"),
            "headers": headers or [],
            "client": client,
            "server": ("127.0.0.1", 80),
        },
        receive,
        send,
    )
    start = next(message for message in messages if message["type"] == "http.response.start")
    response_headers = {key.decode(): value.decode() for key, value in start["headers"]}
    if include_body:
        response_body = b"".join(
            message.get("body", b"")
            for message in messages
            if message["type"] == "http.response.body"
        )
        return start["status"], response_headers, response_body
    return start["status"], response_headers


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


class PluginAPITests(unittest.TestCase):
    def test_manifest_uses_the_current_dashboard_api_contract(self):
        manifest_path = Path(__file__).resolve().parents[1] / "dashboard" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["name"], "lyrics-for-hermes")
        self.assertEqual(manifest["label"], "Lyrics for Hermes")
        self.assertEqual(manifest["api"], "plugin_api.py")
        self.assertTrue(manifest["tab"]["hidden"])
        self.assertTrue((manifest_path.parent / manifest["entry"]).is_file())

    def test_router_exposes_only_retained_routes(self):
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
                "/health",
            },
        )

    def test_default_service_uses_music_app_and_lrclib(self):
        from dashboard import plugin_api
        from dashboard.apple_music_lyrics_backend.lrclib import LRCLIBProvider
        from dashboard.apple_music_lyrics_backend.music import MusicClient

        self.assertIsInstance(plugin_api._service.music, MusicClient)
        self.assertEqual(len(plugin_api._service.providers), 1)
        self.assertIsInstance(plugin_api._service.providers[0], LRCLIBProvider)

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

    def test_backend_has_no_removed_runtime_or_broad_exception_tuple(self):
        from dashboard import plugin_api

        source = Path(plugin_api.__file__).read_text(encoding="utf-8")
        for forbidden in ("ambient", "recognition", "shutil.which(\"ffmpeg\")"):
            self.assertNotIn(forbidden, source.casefold())
        self.assertNotRegex(source, r"except\s*\([^)]*\bException\b")

    def test_health_discloses_only_public_retained_providers(self):
        from dashboard import plugin_api

        response = Response()
        health = asyncio.run(plugin_api.get_health(response))
        self.assertEqual(health["version"], "0.3.1")
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertEqual(health["providers"], ["Music.app", "LRCLIB"])
        self.assertFalse(health["microphone"])
        self.assertFalse(health["privateProviders"])

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
            artwork = asyncio.run(plugin_api.get_artwork(artwork_response, identity=identity))
            app = FastAPI()
            app.include_router(plugin_api.router)
            control_status, _ = asyncio.run(
                asgi_request(
                    app,
                    "/control",
                    method="POST",
                    body=b'{"action":"next"}',
                    headers=[(b"content-type", b"application/json")],
                )
            )
            seek_status, _ = asyncio.run(
                asgi_request(
                    app,
                    "/seek",
                    method="POST",
                    body=b'{"position":22.5}',
                    headers=[(b"content-type", b"application/json")],
                )
            )
            refresh = asyncio.run(plugin_api.post_refresh(Response()))
            permissions = asyncio.run(plugin_api.post_permissions(Response()))
        finally:
            plugin_api._service = previous

        self.assertEqual(state, {"status": "ready"})
        self.assertEqual(artwork["artwork"]["identity"], identity)
        self.assertEqual(state_response.headers["cache-control"], "private, no-store")
        self.assertEqual(artwork_response.headers["cache-control"], "private, no-store")
        self.assertEqual([control_status, seek_status], [200, 200])
        self.assertEqual([refresh, permissions], [{"ok": True}] * 2)
        self.assertEqual(
            fake.calls,
            [
                ("artwork", identity),
                ("control", "next"),
                ("seek", 22.5),
                ("refresh", None),
                ("permissions", None),
            ],
        )

    def test_artwork_endpoint_rejects_unbounded_track_identities(self):
        from dashboard import plugin_api

        with self.assertRaises(HTTPException) as raised:
            asyncio.run(plugin_api.get_artwork(Response(), identity="not-a-track-identity"))
        self.assertEqual(raised.exception.status_code, 400)

    def test_seek_rejects_non_numeric_non_finite_and_huge_values(self):
        from dashboard import plugin_api

        previous = plugin_api._service
        fake = FakeService()
        plugin_api._service = fake
        try:
            for value, detail in (
                (True, "position must be a number"),
                (False, "position must be a number"),
                ("1", "position must be a number"),
                (10**400, "position must be a number"),
                (float("inf"), "position must be finite"),
            ):
                with self.subTest(value=value):
                    with self.assertRaises(HTTPException) as raised:
                        asyncio.run(plugin_api.post_seek({"position": value}, Response()))
                    self.assertEqual(raised.exception.status_code, 400)
                    self.assertEqual(raised.exception.detail, detail)
        finally:
            plugin_api._service = previous
        self.assertFalse(any(call[0] == "seek" for call in fake.calls))

    def test_unavailable_artwork_is_not_returned_as_cacheable_null(self):
        from dashboard import plugin_api

        previous = plugin_api._service
        fake = FakeService()
        fake.has_artwork = False
        plugin_api._service = fake
        try:
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(plugin_api.get_artwork(Response(), identity="a" * 24))
        finally:
            plugin_api._service = previous
        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(raised.exception.headers, {"Cache-Control": "private, no-store"})

    def test_fastapi_validation_and_no_store_headers(self):
        from dashboard import plugin_api

        previous = plugin_api._service
        plugin_api._service = FakeService()
        app = FastAPI()
        app.include_router(plugin_api.router)
        try:
            state_status, state_headers = asyncio.run(asgi_request(app, "/state"))
            artwork_status, artwork_headers = asyncio.run(
                asgi_request(app, "/artwork", query_string=f"identity={'a' * 24}")
            )
            invalid_status, invalid_headers = asyncio.run(
                asgi_request(app, "/artwork", query_string="identity=not-valid")
            )
            missing_status, missing_headers = asyncio.run(asgi_request(app, "/artwork"))
        finally:
            plugin_api._service = previous

        self.assertEqual([state_status, artwork_status, invalid_status, missing_status], [200, 200, 400, 400])
        for headers in (state_headers, artwork_headers, invalid_headers, missing_headers):
            self.assertEqual(headers["cache-control"], "private, no-store")

    def test_loopback_guard_rejects_remote_clients_and_ignores_forwarded_headers(self):
        from dashboard import plugin_api

        previous = plugin_api._service
        fake = FakeService()
        plugin_api._service = fake
        app = FastAPI()
        app.include_router(plugin_api.router)
        body = b'{"action":"next"}'
        try:
            status, headers = asyncio.run(
                asgi_request(
                    app,
                    "/control",
                    method="POST",
                    body=body,
                    client=("203.0.113.8", 44),
                    headers=[
                        (b"content-type", b"application/json"),
                        (b"host", b"127.0.0.1"),
                        (b"x-forwarded-for", b"127.0.0.1"),
                    ],
                )
            )
        finally:
            plugin_api._service = previous
        self.assertEqual(status, 403)
        self.assertEqual(headers["cache-control"], "private, no-store")
        self.assertEqual(fake.calls, [])

    def test_malformed_control_body_is_rejected_before_parsing_for_remote_clients(self):
        from dashboard import plugin_api

        previous = plugin_api._service
        fake = FakeService()
        plugin_api._service = fake
        app = FastAPI()
        app.include_router(plugin_api.router)
        try:
            status, headers, response_body = asyncio.run(
                asgi_request(
                    app,
                    "/control",
                    method="POST",
                    body=b'{"action":',
                    client=("203.0.113.8", 44),
                    headers=[(b"content-type", b"application/json")],
                    include_body=True,
                )
            )
        finally:
            plugin_api._service = previous

        self.assertEqual(status, 403)
        self.assertEqual(headers["cache-control"], "private, no-store")
        self.assertEqual(response_body, b'{"detail":"local trusted backend required"}')
        self.assertEqual(fake.calls, [])

    def test_local_malformed_or_non_object_body_has_generic_no_store_validation_error(self):
        from dashboard import plugin_api

        app = FastAPI()
        app.include_router(plugin_api.router)
        for body in (b'{"action":"secret value', b'[]'):
            with self.subTest(body=body):
                status, headers, response_body = asyncio.run(
                    asgi_request(
                        app,
                        "/control",
                        method="POST",
                        body=body,
                        headers=[(b"content-type", b"application/json")],
                        include_body=True,
                    )
                )
                self.assertEqual(status, 422)
                self.assertEqual(headers["cache-control"], "private, no-store")
                self.assertEqual(response_body, b'{"detail":"invalid request"}')
                self.assertNotIn(body, response_body)

    def test_state_failures_are_controlled_and_not_cacheable(self):
        from dashboard import plugin_api

        previous = plugin_api._service
        fake = FakeService()
        fake.state_error = RuntimeError("injected state failure")
        plugin_api._service = fake
        app = FastAPI()
        app.include_router(plugin_api.router)
        try:
            status, headers = asyncio.run(asgi_request(app, "/state"))
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
                calls = [plugin_api.get_artwork(Response(), identity="a" * 24) for _ in range(8)]
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

    def test_transient_and_unknown_artwork_failures_are_retryable(self):
        from dashboard import plugin_api
        from dashboard.apple_music_lyrics_backend.service import ArtworkBusyError

        for failure in (ArtworkBusyError("busy"), RuntimeError("unexpected")):
            with self.subTest(failure=type(failure).__name__):
                previous = plugin_api._service
                fake = FakeService()
                fake.artwork_error = failure
                plugin_api._service = fake
                app = FastAPI()
                app.include_router(plugin_api.router)
                try:
                    status, headers = asyncio.run(
                        asgi_request(app, "/artwork", query_string=f"identity={'a' * 24}")
                    )
                finally:
                    plugin_api._service = previous
                self.assertEqual(status, 503)
                self.assertEqual(headers["cache-control"], "private, no-store")
                self.assertEqual(headers["retry-after"], "1")

    def test_busy_refresh_fails_fast_and_releases_admission(self):
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

    def test_stale_artwork_identity_is_not_retryable(self):
        from dashboard import plugin_api
        from dashboard.apple_music_lyrics_backend.service import ArtworkStaleIdentityError

        previous = plugin_api._service
        fake = FakeService()
        fake.artwork_error = ArtworkStaleIdentityError("stale")
        plugin_api._service = fake
        app = FastAPI()
        app.include_router(plugin_api.router)
        try:
            status, headers = asyncio.run(
                asgi_request(app, "/artwork", query_string=f"identity={'a' * 24}")
            )
        finally:
            plugin_api._service = previous
        self.assertEqual(status, 409)
        self.assertEqual(headers["cache-control"], "private, no-store")
        self.assertNotIn("retry-after", headers)

    def test_release_version_and_public_metadata_are_consistent(self):
        from dashboard import plugin_api
        from dashboard.apple_music_lyrics_backend.lrclib import CLIENT_HEADER

        root = Path(__file__).resolve().parents[1]
        manifest = json.loads((root / "dashboard" / "manifest.json").read_text())
        project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
        plugin_lines = (root / "plugin.yaml").read_text().splitlines()
        plugin_version = next(
            line.split(":", 1)[1].strip()
            for line in plugin_lines
            if line.startswith("version:")
        )
        self.assertEqual(
            {manifest["version"], project["version"], plugin_version, plugin_api.VERSION},
            {"0.3.1"},
        )
        self.assertEqual(project["name"], "hermes-lyrics")
        self.assertTrue(CLIENT_HEADER.startswith("LyricsForHermes/0.3.1 "))
        self.assertIn("https://github.com/bondanadnob/hermes-lyrics", CLIENT_HEADER)

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
                "track": {"running": True, "state": "playing", "identity": "a" * 24},
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
        summary = runpy.run_path(str(root / "scripts" / "smoke_test.py"))[
            "privacy_safe_summary"
        ](
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
