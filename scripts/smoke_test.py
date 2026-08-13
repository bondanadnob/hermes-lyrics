#!/usr/bin/env python3
"""Exercise the real Music.app and lyric providers without printing lyric text."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dashboard.apple_music_lyrics_backend.apple_cache import (  # noqa: E402
    AppleMusicCacheProvider,
)
from dashboard.apple_music_lyrics_backend.lrclib import LRCLIBProvider  # noqa: E402
from dashboard.apple_music_lyrics_backend.music import MusicClient  # noqa: E402
from dashboard.apple_music_lyrics_backend.service import LyricsService  # noqa: E402


def privacy_safe_summary(state: dict) -> dict:
    track = state.get("track") or {}
    lyrics = state.get("lyrics") or {}
    artwork = state.get("artwork") or {}
    identity = str(track.get("identity") or "")
    status = state.get("status")
    return {
        "backend": {
            "ready": status == "ready",
            "permission_required": status == "permission_required",
            "error": status == "error",
        },
        "playback": {
            "music_running": bool(track.get("running")),
            "playing": track.get("state") == "playing",
            "private_identity_valid": re.fullmatch(r"[0-9a-f]{24}", identity)
            is not None,
        },
        "remote_artwork_fallback_available": bool(artwork.get("remote_url")),
        "lyrics": {
            "available": status == "ready",
            "synced": bool(lyrics.get("synced")),
            "word_timed": lyrics.get("word_timing") not in {None, "none"},
        },
        "lyric_text_emitted": False,
        "track_metadata_emitted": False,
    }


def main() -> int:
    apple_cache = AppleMusicCacheProvider()
    state = LyricsService(
        music=MusicClient(),
        providers=(apple_cache, LRCLIBProvider()),
        artwork_provider=apple_cache,
    ).state()
    print(json.dumps(privacy_safe_summary(state), indent=2, ensure_ascii=False))
    return 1 if state.get("status") in {"error", "permission_required"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
