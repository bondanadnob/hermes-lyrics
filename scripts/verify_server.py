#!/usr/bin/env python3
"""Boot a deterministic local health server for ``hermes verify``."""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dashboard.plugin_api import router

EXPECTED_ROUTES = {
    "/control",
    "/health",
    "/permissions",
    "/refresh",
    "/seek",
    "/state",
}


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        if self.path != "/health":
            self.send_error(404)
            return
        body = json.dumps(
            {
                "ok": True,
                "plugin": "apple-music-lyrics",
                "routes": sorted(EXPECTED_ROUTES),
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=54782)
    args = parser.parse_args()

    mounted = {route.path for route in router.routes}
    if mounted != EXPECTED_ROUTES:
        raise RuntimeError(f"unexpected plugin routes: {sorted(mounted)}")

    server = ThreadingHTTPServer(("127.0.0.1", args.port), HealthHandler)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
