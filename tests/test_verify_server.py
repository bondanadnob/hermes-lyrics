import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

VERIFIER = Path(__file__).resolve().parents[1] / "scripts" / "verify_server.py"
SPEC = importlib.util.spec_from_file_location("apple_music_lyrics_verify_server", VERIFIER)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("could not load verify_server.py")
verify_server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify_server)


class VerifyServerTests(unittest.TestCase):
    def test_main_accepts_the_current_plugin_router_contract(self):
        calls = []

        class Server:
            def __init__(self, address, handler):
                calls.append((address, handler))

            def serve_forever(self):
                calls.append("served")

        argv = ["verify_server.py", "--port", "0"]
        with (
            patch.object(verify_server, "ThreadingHTTPServer", Server),
            patch.object(verify_server, "router") as current_router,
            patch.object(sys, "argv", argv),
        ):
            from dashboard.plugin_api import router

            current_router.routes = router.routes
            result = verify_server.main()

        self.assertEqual(result, 0)
        self.assertEqual(calls[0], (("127.0.0.1", 0), verify_server.HealthHandler))
        self.assertEqual(calls[1], "served")


if __name__ == "__main__":
    unittest.main()
