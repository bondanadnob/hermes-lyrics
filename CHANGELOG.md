# Changelog

All notable changes to this project are documented here.

## [0.3.1] - 2026-08-30

### Security

- Copy-mode installation now rejects every symlink in the source tree before replacing an existing plugin, preventing external files from being dereferenced into the installed payload.
- Copy-mode installation now excludes alternate virtualenv names, marker-based virtualenvs, Hermes metadata, and common Python development caches.

## [0.3.0] - 2026-08-30

### Removed

- Removed the unsupported private Apple lyric-cache provider and its TTML parser.
- Removed all microphone, ambient-recognition, unofficial Shazam-compatible endpoint, FFmpeg, and isolated recognition-runtime code.
- The unreleased TTML spacing hotfix is superseded by removal of that unsupported provider.

### Changed

- Rebranded the public project and plugin as **Lyrics for Hermes** with the new `lyrics-for-hermes` plugin ID; `apple-music-lyrics` remains only as a legacy migration key.
- Added a loopback-only route guard because Hermes plugin routes are otherwise unauthenticated.
- Added bounded, nonblocking LRCLIB `Retry-After` cooldown handling.
- Updated vulnerable Python build/runtime tool pins and regenerated the SHA-256 lock.
- Added deterministic CycloneDX dependency and license evidence.
- Added transactional, profile-scoped migration and expanded privacy, rights, permission, and non-affiliation disclosures.

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
