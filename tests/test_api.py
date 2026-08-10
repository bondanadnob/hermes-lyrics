import asyncio
import json
import unittest
from pathlib import Path


class FakeService:
    def __init__(self):
        self.calls = []

    def state(self):
        return {"status": "ready"}

    def control(self, action):
        self.calls.append(("control", action))
        if action == "bad":
            raise ValueError("bad action")

    def seek(self, position):
        self.calls.append(("seek", position))

    def refresh(self):
        self.calls.append(("refresh", None))

    def open_automation_settings(self):
        self.calls.append(("permissions", None))


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
            {"/state", "/control", "/seek", "/refresh", "/permissions", "/health"},
        )

    def test_endpoint_functions_delegate_to_service(self):
        from dashboard import plugin_api

        previous = plugin_api._service
        fake = FakeService()
        plugin_api._service = fake
        try:
            state = asyncio.run(plugin_api.get_state())
            control = asyncio.run(plugin_api.post_control({"action": "next"}))
            seek = asyncio.run(plugin_api.post_seek({"position": 22.5}))
            refresh = asyncio.run(plugin_api.post_refresh())
            permissions = asyncio.run(plugin_api.post_permissions())
        finally:
            plugin_api._service = previous

        self.assertEqual(state, {"status": "ready"})
        self.assertEqual(control, {"ok": True})
        self.assertEqual(seek, {"ok": True})
        self.assertEqual(refresh, {"ok": True})
        self.assertEqual(permissions, {"ok": True})
        self.assertEqual(
            fake.calls,
            [
                ("control", "next"),
                ("seek", 22.5),
                ("refresh", None),
                ("permissions", None),
            ],
        )


if __name__ == "__main__":
    unittest.main()
