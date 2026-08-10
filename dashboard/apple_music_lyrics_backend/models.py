"""Serializable domain models for now-playing and synchronized lyrics."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class LyricWord:
    text: str
    start: float
    end: float


@dataclass(frozen=True, slots=True)
class LyricLine:
    time: float
    text: str
    end: float | None = None
    words: tuple[LyricWord, ...] = ()
    translation: str | None = None
    transliteration: str | None = None


@dataclass(frozen=True, slots=True)
class LyricsDocument:
    lines: tuple[LyricLine, ...] = ()
    plain_text: str = ""
    source: str = "none"
    synced: bool = False
    word_timing: str = "none"
    artwork_url: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TrackInfo:
    running: bool
    state: str
    title: str = ""
    artist: str = ""
    album: str = ""
    duration: float = 0.0
    position: float = 0.0
    plain_lyrics: str = ""
    sampled_at: float = 0.0
    error: str | None = None

    @property
    def key(self) -> str:
        return "\u241f".join(
            [
                self.title.casefold().strip(),
                self.artist.casefold().strip(),
                self.album.casefold().strip(),
                str(round(self.duration)),
            ]
        )

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("plain_lyrics", None)
        return data
