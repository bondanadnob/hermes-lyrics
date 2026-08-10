#!/usr/bin/env python3
"""Exercise the public LRCLIB API with a known track, without emitting lyric text."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dashboard.apple_music_lyrics_backend.lrclib import LRCLIBProvider  # noqa: E402
from dashboard.apple_music_lyrics_backend.models import TrackInfo  # noqa: E402


def main() -> int:
    track = TrackInfo(
        running=True,
        state="playing",
        title="Blinding Lights",
        artist="The Weeknd",
        album="After Hours",
        duration=200,
        position=0,
        sampled_at=0,
    )
    document = LRCLIBProvider().lyrics_for(track)
    summary = {
        "provider": "lrclib",
        "found": document is not None,
        "synced": document.synced if document else None,
        "line_count": len(document.lines) if document else 0,
        "lyric_text_emitted": False,
    }
    print(json.dumps(summary, indent=2))
    return 0 if document and document.synced and document.lines else 1


if __name__ == "__main__":
    raise SystemExit(main())
