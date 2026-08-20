# Hermes Apple Music Lyrics

[![Tests](https://github.com/bondanadnob/hermes-apple-music-lyrics/actions/workflows/test.yml/badge.svg)](https://github.com/bondanadnob/hermes-apple-music-lyrics/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A dockable, theme-aware lyrics experience for [Hermes Agent](https://github.com/NousResearch/hermes-agent) on macOS.

It follows the track playing in Music.app or, on explicit request, recognizes nearby music from an eight-second microphone sample. It preserves artwork, highlights the current lyric line, auto-scrolls, and supports word-level karaoke timing when Apple Music has cached syllable-timed lyrics locally.

## Features

- Dockable right-side Hermes pane **and** a full Lyrics page
- Current title, artist, album, Music.app artwork, playback state, and progress
- Provider-independent artwork, including Music.app covers alongside synchronized LRCLIB lyrics
- Smooth local progress interpolation between Music.app samples
- Synchronized current-line highlighting and automatic scrolling
- Exact word-level timing for compatible Apple Music TTML cache entries
- Click any timed line to seek Music.app
- Previous, play/pause, next, and progress-bar seek controls for Music.app
- Manual **Nearby** mode with visible microphone state, Stop control, Shazam-compatible recognition, and estimated lyric synchronization
- Nearby mode never exposes playback or seek controls for external audio
- Status-bar lyric preview and command-palette entry
- Native Hermes theme variables with reduced-motion support
- No API keys and no Apple credentials

## Lyric fallback order

1. **Apple Music's local read-only cache** — exact TTML syllable/word timing when Music.app has already cached it. This is a private, best-effort cache format and may change in a macOS update.
2. **[LRCLIB](https://lrclib.net)** — synchronized line timing matched conservatively by title, artist, album, and duration.
3. **Music.app plain lyrics** — displayed without synchronization.
4. A clear no-lyrics state.

In **Music.app** mode, playback metadata comes from the local app through JXA/Apple Events. Artwork is read from that same current track when available, with independently matched, validated Apple-owned `mzstatic.com` metadata as the only fallback.

In **Nearby** mode, a Shazam-compatible match supplies title, artist, album, artwork, and an estimated playback offset. The same LRCLIB pipeline then resolves synchronized lyrics. Ambient audio cannot be paused or sought, so Music.app transport and lyric-seek controls are hidden.

## Requirements

- macOS with Music.app
- Hermes Agent/Desktop `0.20.0` or newer, connected to its **local** backend
- FFmpeg with AVFoundation and `libvorbis` support (`brew install ffmpeg`) for optional Nearby recognition
- Internet access for LRCLIB and, when Nearby is used, the unofficial Shazam-compatible endpoint
- One-time macOS Automation permission for Hermes/Python to read Music.app
- One-time macOS microphone permission when Nearby listening is first requested

## Install

```bash
git clone https://github.com/bondanadnob/hermes-apple-music-lyrics.git
cd hermes-apple-music-lyrics
python3 scripts/install.py
```

For a named Hermes profile, target the profile reported by the CLI explicitly:

```bash
HERMES_HOME="$(dirname "$(hermes config path)")" python3 scripts/install.py
```

Restart Hermes Desktop and its local server after installation. The first now-playing request may trigger a macOS Automation permission dialog.

The installer validates a fully pinned, SHA-256-hashed dependency lock and builds a profile-scoped recognition environment at `plugin-data/apple-music-lyrics/recognition-venv` without modifying Hermes's own Python environment. It stages one unified plugin payload at `plugins/apple-music-lyrics`—including the desktop entry at `plugins/apple-music-lyrics/desktop/plugin.js`—and the recognition runtime before replacing any existing target. This two-target commit uses backup/rollback semantics. A legacy standalone copy at `desktop-plugins/apple-music-lyrics` is quarantined in the same transaction, restored if commit fails, and removed only after both authoritative targets commit.

For development, link the checkout rather than copying it:

```bash
python3 scripts/install.py --link --force
```

Use this installer as the authoritative install path; do not create a second standalone desktop copy after `hermes plugins install`.

## Use

### Music.app

1. Start a track in Music.app.
2. Open **Lyrics** from the Hermes sidebar, command palette, or docked pane.
3. For Apple Music's own word timing, open Lyrics once in Music.app so the local cache can populate, then press **Refresh lyrics and artwork** in Hermes.

### Nearby music

1. Open **Lyrics** and choose **Nearby**. Merely selecting it does not activate the microphone.
2. Read the on-screen provider disclosure, then press **Listen for 8 seconds**.
3. Hermes shows **Microphone active** while capturing; press **Stop** at any time to terminate both the worker and FFmpeg recorder.
4. A match is routed through LRCLIB and displayed from the estimated Shazam offset. The private response offset is treated as the reference-track time matching the start of the captured query, mirroring Apple's documented [ShazamKit `matchOffset`](https://developer.apple.com/documentation/shazamkit/shmatchedmediaitem/matchoffset) meaning; elapsed time since the worker's actual capture start is then added. The private endpoint is unofficial and may not preserve that semantic, and timing can drift because external audio cannot be paused, queried continuously, or sought.

## Privacy and lyric rights

- Music.app is queried locally.
- Nearby listening is manual only. Selecting Nearby, opening the pane, polling state, or restarting Hermes never activates the microphone.
- A single capture is limited to eight seconds of mono 16 kHz audio, encoded as Ogg Vorbis in process memory solely for compatibility with the pinned local fingerprint engine; the plugin creates no recording or fingerprint files. Stop, source switching, and timeout signal the worker and FFmpeg process group.
- In the pinned ShazamIO implementation, the audio signature is generated locally. Shazam infrastructure receives the fingerprint—not the in-memory Ogg audio—plus bounded protocol/request metadata: sample duration, timestamp, fixed timezone and locale/platform fields, generic client headers, two random UUID4 request identifiers, and the connection IP address. The identifiers generated by the pinned dependency are admitted only after canonical version-4 validation.
- ShazamIO requires no account, API key, or stated per-request fee today, but it is unofficial, has no SLA, may be rate-limited or changed without notice, and is not guaranteed to remain available or free.
- The worker receives an allowlisted environment without Hermes API keys/tokens. Raw audio, signatures, full Shazam responses, and dependency diagnostics are neither logged nor retained by the plugin. Process isolation is not a macOS security sandbox.
- Its cache database and data directory are opened read-only through held descriptors with symlink following disabled; filesystem candidate enumeration, database blobs, and descriptor-level cache-file reads are bounded before text is materialized. Malformed records are isolated so they cannot hide later valid entries.
- After a Nearby match, LRCLIB receives track metadata (title, artist, album, and duration when available) for lyrics matching. The eight-second audio and fingerprint are not sent to LRCLIB.
- Lyrics are held in process memory and are not written to a plugin database.
- Music.app artwork is read locally, limited to structurally validated static JPEG or PNG images of at most 2 MB and 2048 pixels per side, and held only in process/render memory. JPEG scan data must also decode successfully through macOS ImageIO entirely in memory. PNG image data is decompressed under an exact scanline bound; APNG chunks, compressed ancillary metadata, and unsupported chunks are rejected.
- Remote artwork is accepted only from Apple-controlled `mzstatic.com` HTTPS hosts; local artwork uses a bounded image data URL.
- Track artwork requests use a truncated hash of Music.app's persistent track identifier (with a local database/metadata fallback), so titles, artists, and albums are not placed in request URLs.
- State and every controlled artwork response are marked `private, no-store`; inactive artwork query data is released quickly. Transient native artwork failures are retried a bounded number of times. A definitive local-artwork miss is kept only in memory for the current track identity and is retried only after manual refresh or a track change. Neither fetched lyrics nor artwork is persisted by the plugin. A validated remote Apple image may still be cached independently by Chromium, Apple, or its CDN.
- This repository contains no song lyrics.

Lyrics remain the property of their respective rightsholders. Apple Music is a trademark of Apple Inc.

## Test

The backend and parser suite uses synthetic lyrics only:

```bash
python3 -m venv .venv
.venv/bin/python -m ensurepip --upgrade
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/ruff check .
.venv/bin/python -m unittest discover -s tests -v
node --check desktop/plugin.js
node --check dashboard/dist/index.js
node tests/desktop_artwork_runtime.mjs
```

To exercise the real Music.app and providers without printing lyric text:

```bash
.venv/bin/python scripts/smoke_test.py
.venv/bin/python scripts/lrclib_smoke_test.py
```

The first command exercises the real Music.app integration while emitting only status booleans—no current track metadata or lyric text. The second verifies that LRCLIB returns synchronized data for a known public test track without printing lyric text.

## Prior art research

I did not find an existing Hermes Agent plugin for Apple Music lyrics. Useful adjacent open-source projects do exist:

- [janestreetshiller/hermes-spotify-player](https://github.com/janestreetshiller/hermes-spotify-player) — the closest Hermes precedent, with synced LRCLIB lyrics for Spotify rather than Apple Music.
- [Takpap/apple-music-lyrics](https://github.com/Takpap/apple-music-lyrics) — native macOS karaoke lyrics from Music.app's local TTML cache.
- [ateymoori/lyricglow](https://github.com/ateymoori/lyricglow) — Apple Music/Spotify desktop lyric overlay using AppleScript and LRCLIB.
- [ddddxxx/LyricsX](https://github.com/ddddxxx/LyricsX) — mature macOS desktop/menu-bar lyrics app.
- [juntaochi/amcli](https://github.com/juntaochi/amcli) — terminal Apple Music controller with synchronized lyrics.
- [jaychempan/coding-with-beat](https://github.com/jaychempan/coding-with-beat) — terminal/IDE-oriented Apple Music lyrics.

See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for attribution details.

## Architecture

```text
Music.app ── JXA / Apple Events ──► playback metadata + bounded artwork
     │                                    │
     └── local Apple TTML cache ──────────┤
                                          ├──► LRCLIB lyrics pipeline
Manual Nearby button                      │
     │                                    │
     └── 8 s Ogg pipe ──► local fingerprint ──► unofficial Shazam endpoint
                                          │              │
                                          │        match + offset
                                          └──────────────┘
                                                         │
                                                         ▼
                                         scoped REST (/state + actions)
                                                         │
                                                         ▼
                                         pane · page · status bar
```

## License

MIT
