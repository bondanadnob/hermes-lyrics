"""Public-distribution hardening contracts for version 0.3.0."""

import asyncio
import unittest


class HardeningContracts(unittest.TestCase):
    def test_lrclib_429_starts_monotonic_cooldown_and_skips_search(self):
        from dashboard.apple_music_lyrics_backend.lrclib import HTTPResponse, LRCLIBProvider
        from dashboard.apple_music_lyrics_backend.models import TrackInfo

        calls = []
        now = [10.0]

        def fetch(url, headers, timeout):
            calls.append(url)
            return HTTPResponse(429, "", {"Retry-After": "7"})

        provider = LRCLIBProvider(fetcher=fetch, monotonic_clock=lambda: now[0])
        track = TrackInfo(
            running=True, state="playing", title="Song", artist="Artist", persistent_id="a" * 24
        )
        self.assertIsNone(provider.lyrics_for(track))
        self.assertEqual(len(calls), 1, "429 exact lookup must suppress fallback search")
        self.assertIsNone(provider.lyrics_for(track))
        self.assertEqual(len(calls), 1, "provider cooldown must suppress immediate retries")
        now[0] = 17.0
        self.assertIsNone(provider.lyrics_for(track))
        self.assertEqual(len(calls), 2)

    def test_routes_exclude_microphone_and_health_discloses_public_providers(self):
        from dashboard import plugin_api

        paths = {route.path for route in plugin_api.router.routes}
        self.assertNotIn("/source", paths)
        self.assertFalse(any("ambient" in path or "microphone" in path for path in paths))
        from fastapi import Response

        response = Response()
        health = asyncio.run(plugin_api.get_health(response))
        self.assertEqual(health["providers"], ["Music.app", "LRCLIB"])
        self.assertFalse(health["microphone"])
        self.assertFalse(health["privateProviders"])
        self.assertEqual(response.headers["cache-control"], "private, no-store")

    def test_router_rejects_non_loopback_before_control_mutation(self):
        from fastapi import FastAPI

        from dashboard import plugin_api

        calls = []
        previous = plugin_api._service

        class Service:
            def control(self, action):
                calls.append(action)

        plugin_api._service = Service()
        app = FastAPI()
        app.include_router(plugin_api.router)
        messages = []

        async def receive():
            return {"type": "http.request", "body": b'{"action":"next"}', "more_body": False}

        async def send(message):
            messages.append(message)

        try:
            asyncio.run(
                app(
                    {
                        "type": "http",
                        "asgi": {"version": "3.0"},
                        "http_version": "1.1",
                        "method": "POST",
                        "scheme": "http",
                        "path": "/control",
                        "raw_path": b"/control",
                        "query_string": b"",
                        "headers": [(b"content-type", b"application/json")],
                        "client": ("203.0.113.1", 12),
                        "server": ("127.0.0.1", 80),
                    },
                    receive,
                    send,
                )
            )
        finally:
            plugin_api._service = previous
        self.assertEqual(
            next(m for m in messages if m["type"] == "http.response.start")["status"], 403
        )
        self.assertEqual(calls, [])

    def test_release_display_name_and_canonical_id_are_stable(self):
        from dashboard import plugin_api

        self.assertEqual(plugin_api.VERSION, "0.3.0")
        self.assertEqual(plugin_api.PLUGIN_ID, "lyrics-for-hermes")
        self.assertEqual(plugin_api.DISPLAY_NAME, "Lyrics for Hermes")


if __name__ == "__main__":
    unittest.main()
