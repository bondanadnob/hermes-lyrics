#!/usr/bin/env python3
"""Exercise the real Music.app and lyric providers without printing lyric text."""

from __future__ import annotations

import json
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


def main() -> int:
    state = LyricsService(
        music=MusicClient(),
        providers=(AppleMusicCacheProvider(), LRCLIBProvider()),
    ).state()
    track = state.get("track") or {}
    lyrics = state.get("lyrics") or {}
    summary = {
        "status": state.get("status"),
        "track": {
            "title": track.get("title"),
            "artist": track.get("artist"),
            "state": track.get("state"),
            "duration": track.get("duration"),
        },
        "lyrics": {
            "source": lyrics.get("source"),
            "synced": lyrics.get("synced"),
            "word_timing": lyrics.get("word_timing"),
            "line_count": len(lyrics.get("lines") or []),
        },
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 1 if state.get("status") in {"error", "permission_required"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
