"""Serializable domain models for now-playing and synchronized lyrics."""

from __future__ import annotations

import hashlib
import math
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
class ArtworkPayload:
    identity: str
    data_url: str
    mime: str
    byte_length: int
    source: str = "Music.app"

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
    persistent_id: str = ""
    database_id: str = ""
    source: str = "music_app"
    can_control: bool = True
    can_seek: bool = True
    artwork_url: str | None = None

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

    @property
    def identity(self) -> str:
        if self.persistent_id.strip():
            material = f"persistent:{self.persistent_id.casefold().strip()}"
        elif self.database_id.strip():
            material = f"database:{self.database_id.casefold().strip()}"
        else:
            duration = self.duration if math.isfinite(self.duration) else 0.0
            metadata = "\u241f".join(
                [
                    self.title.casefold().strip(),
                    self.artist.casefold().strip(),
                    self.album.casefold().strip(),
                    str(round(duration * 1000)),
                ]
            )
            material = f"metadata:{metadata}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("plain_lyrics", None)
        data.pop("persistent_id", None)
        data.pop("database_id", None)
        data["identity"] = self.identity
        return data
