# Changelog

All notable changes to this project are documented here.

## [0.2.0] - 2026-08-23

### Added

- Manual Nearby recognition from an explicitly initiated eight-second microphone sample.
- Local, isolated Shazam-compatible fingerprinting with bounded provider requests and responses.
- Estimated synchronized lyrics for recognized tracks through the existing LRCLIB pipeline.
- Visible listening, stop, no-match, provider-error, and uncertain-backend states.
- Self-contained desktop runtime dependencies and CI coverage for artwork and retained-state failure behavior.
- Commit-pinned GitHub Actions and a hash-locked Python CI environment.

### Security and privacy

- Keeps captured Ogg Vorbis audio and generated signatures in memory and does not log or retain them.
- Stops and reaps the recorder process group on Stop, timeout, source change, and shutdown.
- Prevents Nearby tracks from controlling or seeking Music.app.
- Documents the unofficial Shazam-compatible provider, LRCLIB metadata request, and artwork CDN boundaries.

## [0.1.1]

- Added privacy-safe Music.app artwork handling with bounded local decoding and Apple-owned remote fallback.

## [0.1.0]

- Initial synchronized Apple Music lyrics plugin for Hermes Desktop.