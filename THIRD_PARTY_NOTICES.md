# Related open-source work

This project was independently implemented for Hermes Desktop. No third-party
source code is bundled in this repository. The installer creates an isolated
runtime and downloads the pinned dependency listed below. The architecture and
compatibility research also benefited from these open-source projects:

- [shazamio/ShazamIO](https://github.com/shazamio/ShazamIO) `0.8.1`
  (MIT): installed into the profile-scoped recognition environment and used to
  generate Shazam signatures locally and query an unofficial Shazam-compatible
  endpoint. It is not an official Apple/Shazam API and carries no availability,
  price, or service-level guarantee.
- [`shazamio-core==1.1.2`](https://pypi.org/project/shazamio-core/1.1.2/)
  (MIT): the transitive compiled fingerprint engine downloaded by the isolated,
  hash-locked installer. The PyPI metadata license field is blank, but the exact
  1.1.2 wheel and source distribution both include an MIT license with
  `Copyright © 2024 dotX12`. This artifact evidence is the basis for the license
  classification; the wheel is downloaded at install time and is not bundled in
  this repository.

- [janestreetshiller/hermes-spotify-player](https://github.com/janestreetshiller/hermes-spotify-player)
  (MIT): the closest Hermes Desktop precedent, targeting Spotify with a scoped
  backend, dockable pane, and LRCLIB synchronization.
- [Takpap/apple-music-lyrics](https://github.com/Takpap/apple-music-lyrics)
  (MIT): demonstrated that Music.app's local URL cache can contain Apple Music
  syllable-timed TTML responses.
- [ateymoori/lyricglow](https://github.com/ateymoori/lyricglow)
  (MIT): an Apple Music/Spotify desktop overlay using AppleScript and LRCLIB.
- [ddddxxx/LyricsX](https://github.com/ddddxxx/LyricsX)
  (MPL-2.0): a mature macOS lyric display application.
- [juntaochi/amcli](https://github.com/juntaochi/amcli)
  (MIT): an Apple Music terminal controller with synchronized lyrics.

Apple Music is a trademark of Apple Inc. LRCLIB is an independent community
service. Lyrics remain the property of their respective rightsholders.
