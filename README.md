# Hermes Apple Music Lyrics

[![Tests](https://github.com/bondanadnob/hermes-apple-music-lyrics/actions/workflows/test.yml/badge.svg)](https://github.com/bondanadnob/hermes-apple-music-lyrics/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A dockable, theme-aware lyrics experience for [Hermes Agent](https://github.com/NousResearch/hermes-agent) on macOS.

It follows the track playing in Music.app, preserves Music.app album artwork regardless of the lyric provider, highlights the current lyric line, auto-scrolls, and supports word-level karaoke timing when Apple Music has cached syllable-timed lyrics locally.

## Features

- Dockable right-side Hermes pane **and** a full Lyrics page
- Current title, artist, album, Music.app artwork, playback state, and progress
- Provider-independent artwork, including Music.app covers alongside synchronized LRCLIB lyrics
- Smooth local progress interpolation between Music.app samples
- Synchronized current-line highlighting and automatic scrolling
- Exact word-level timing for compatible Apple Music TTML cache entries
- Click any timed line to seek Music.app
- Previous, play/pause, next, and progress-bar seek controls
- Status-bar lyric preview and command-palette entry
- Native Hermes theme variables with reduced-motion support
- No API keys and no Apple credentials

## Lyric fallback order

1. **Apple Music's local read-only cache** — exact TTML syllable/word timing when Music.app has already cached it. This is a private, best-effort cache format and may change in a macOS update.
2. **[LRCLIB](https://lrclib.net)** — synchronized line timing matched conservatively by title, artist, album, and duration.
3. **Music.app plain lyrics** — displayed without synchronization.
4. A clear no-lyrics state.

Playback metadata always comes from the local Music.app through JXA/Apple Events. Artwork is read from that same current track when available, with independently matched, validated Apple-owned `mzstatic.com` cache metadata as the only fallback. The desktop finishes the local artwork attempt before it renders that remote fallback. Lyric selection is independent, so falling back to LRCLIB does not remove the cover or change the playback source.

## Requirements

- macOS with Music.app
- Hermes Agent/Desktop `0.20.0` or newer, connected to its **local** backend
- Internet access for the LRCLIB fallback
- One-time macOS Automation permission for Hermes/Python to read Music.app

## Install

```bash
git clone https://github.com/bondanadnob/hermes-apple-music-lyrics.git
cd hermes-apple-music-lyrics
python3 scripts/install.py
```

Restart Hermes Desktop and its local server after installation. The first now-playing request may trigger a macOS Automation permission dialog.

The installer validates and stages both the backend and desktop payloads before replacing either existing installation. If a commit fails, both targets are rolled back together. An empty `HERMES_HOME` safely uses the default `~/.hermes` location.

For development, link the checkout rather than copying it:

```bash
python3 scripts/install.py --link --force
```

If you installed the backend first with `hermes plugins install`, install only the desktop half with:

```bash
python3 ~/.hermes/plugins/apple-music-lyrics/scripts/install.py --desktop-only
```

## Use

1. Start a track in Music.app.
2. Open **Lyrics** from the Hermes sidebar, command palette, or the docked pane.
3. For Apple Music's own word timing, open the Lyrics view once in Music.app so the local cache can populate, then press **Refresh lyrics and artwork** in Hermes.

## Privacy and lyric rights

- Music.app is queried locally.
- Its cache database and data directory are opened read-only through held descriptors with symlink following disabled; filesystem candidate enumeration, database blobs, and descriptor-level cache-file reads are bounded before text is materialized. Malformed records are isolated so they cannot hide later valid entries.
- When local timed lyrics are unavailable, only track metadata is sent to LRCLIB for matching.
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
      │                                  │
      └───────────────┐                  ├────────► /artwork (per track, in memory)
                      ▼                  │
          local Apple TTML cache         │
                      │                  │
                      ├──► LRCLIB fallback
                      └──► plain-lyrics fallback
                                  │
                                  ▼
                     Hermes plugin REST bridge (/state)
                                  │
                                  ▼
                      docked pane · full page · status bar
```

## License

MIT
