# Lyrics for Hermes

[![Tests](https://github.com/bondanadnob/hermes-lyrics/actions/workflows/test.yml/badge.svg)](https://github.com/bondanadnob/hermes-lyrics/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Lyrics for Hermes is an independent, unofficial Hermes Desktop plugin for use with Music.app. It provides a dockable lyrics pane and full page with current-track metadata, validated artwork, synchronized LRCLIB lyrics, plain Music.app lyric fallback, line highlighting, automatic scrolling, seeking, and playback controls.

This project is independent and is not affiliated with, authorized, sponsored, or endorsed by Apple Inc.

## Requirements

- macOS with Music.app
- Hermes Desktop connected to its local backend
- Python 3.11 or newer for installation and development
- Internet access when LRCLIB lookup is needed
- One-time macOS Automation permission for Hermes or Python to read and control Music.app

No API key or separate media-processing runtime is required. The plugin does not use the microphone or ship audio-recognition code.

## Install

```bash
git clone https://github.com/bondanadnob/hermes-lyrics.git
cd hermes-lyrics
python3 scripts/install.py
```

The installer resolves the active Hermes profile (or honors `HERMES_HOME`), rejects source/target overlap, stages the complete unified payload, and swaps it into `plugins/lyrics-for-hermes` transactionally. Use `--force` to replace an existing canonical installation or `--link --force` for local development. Use `--no-enable` to leave Hermes backend enablement unchanged.

Restart Hermes Desktop and its local server after installation. Open **Lyrics for Hermes**, grant Automation permission if prompted, and play a track in Music.app.

The supported distribution is this Git repository and its GitHub source archives. No PyPI/wheel release is published or supported: Hermes requires the unified root payload (`plugin.yaml`, dashboard manifest/assets, and desktop plugin), which the source installer preserves.

## Migration from the legacy ID

The canonical plugin ID is `lyrics-for-hermes`. During installation, the legacy `apple-music-lyrics` key is quarantined from all three former profile locations—`plugins`, `desktop-plugins`, and `plugin-data`—inside the same filesystem transaction. A failed swap restores every quarantined legacy path. Only after filesystem success does the installer ask Hermes to disable `apple-music-lyrics` and enable `lyrics-for-hermes` for the verified active profile.

## Privacy, security, and lyric rights

Music.app metadata, controls, and artwork are accessed locally through Apple Events. Structural, byte-size, and pixel-dimension validation applies to local artwork data before display. The remote fallback is an HTTPS `mzstatic.com` URL that is allowlisted and length-limited; it is rendered by the browser and is not structurally decoded by the plugin. Lyrics and artwork are held in memory and are not written to a plugin database.

LRCLIB receives title, artist, album, and duration only when a synchronized lyric lookup is needed. Availability and results are not guaranteed. The repository contains no song lyrics or artwork. Display is for personal viewing only; do not export or redistribute content beyond applicable rights and service terms. Report takedown or rights concerns through the repository issue tracker.

Hermes plugin routes are unauthenticated and intended only for a loopback-bound Hermes backend; remote dashboards are intentionally unsupported. Every route independently rejects clients outside IPv4 `127.0.0.0/8` and IPv6 `::1`; Host and forwarding headers are not trusted. State, artwork, and mutations use `Cache-Control: private, no-store` where applicable.

## Development and verification

The Python and desktop test dependencies are exact-version locked. `SBOM.json` is deterministic CycloneDX evidence derived from every component in `requirements-ci.lock` and `package-lock.json`.

```bash
python -m pip install --require-hashes --only-binary=:all: -r requirements-ci.lock
python -m pip install --no-deps --no-build-isolation -e .
python -m unittest discover -s tests -v
ruff check .
npm ci --ignore-scripts
npm audit --omit=optional --audit-level=high
npm test
python scripts/generate_sbom.py --check
```

The smoke scripts emit only status booleans, not personal track metadata or lyric text. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for attribution.

## License

MIT
