"""Orchestrate Music.app sampling and ordered lyric-provider fallbacks."""

from __future__ import annotations

import threading
import time
from typing import Iterable, Protocol

from .models import LyricLine, LyricsDocument, TrackInfo


class MusicBoundary(Protocol):
    def sample(self) -> TrackInfo: ...

    def control(self, action: str) -> None: ...

    def seek(self, position: float) -> None: ...


class LyricsProvider(Protocol):
    def lyrics_for(self, track: TrackInfo) -> LyricsDocument | None: ...

    def clear_cache(self) -> None: ...


class LyricsService:
    def __init__(
        self,
        music: MusicBoundary,
        providers: Iterable[LyricsProvider],
        monotonic_clock=time.monotonic,
    ) -> None:
        self.music = music
        self.providers = tuple(providers)
        self._clock = monotonic_clock
        self._lock = threading.RLock()
        self._documents: dict[str, LyricsDocument] = {}
        self._miss_until: dict[str, float] = {}

    def state(self) -> dict:
        with self._lock:
            track = self.music.sample()
            base = {
                "protocolVersion": 1,
                "track": track.to_dict(),
                "lyrics": LyricsDocument().to_dict(),
            }
            if track.error == "automation_permission":
                return {**base, "status": "permission_required"}
            if track.error:
                return {**base, "status": "error"}
            if not track.running or track.state in {"not_running", "stopped"} or not track.title:
                return {**base, "status": "idle"}

            document = self._document_for(track)
            if document is None:
                return {**base, "status": "not_found"}
            return {
                **base,
                "status": "ready" if document.synced else "plain",
                "lyrics": document.to_dict(),
            }

    def control(self, action: str) -> None:
        self.music.control(action)

    def seek(self, position: float) -> None:
        self.music.seek(position)

    def open_automation_settings(self) -> None:
        self.music.open_automation_settings()

    def refresh(self) -> None:
        with self._lock:
            self._documents.clear()
            self._miss_until.clear()
            for provider in self.providers:
                provider.clear_cache()

    def _document_for(self, track: TrackInfo) -> LyricsDocument | None:
        cached = self._documents.get(track.key)
        if cached is not None:
            return cached
        if self._miss_until.get(track.key, 0.0) > self._clock():
            return None

        for provider in self.providers:
            try:
                document = provider.lyrics_for(track)
            except Exception:
                document = None
            if document is not None and document.lines:
                self._documents[track.key] = document
                return document

        if track.plain_lyrics:
            document = LyricsDocument(
                lines=tuple(
                    LyricLine(float(index), text)
                    for index, text in enumerate(track.plain_lyrics.splitlines())
                ),
                plain_text=track.plain_lyrics,
                source="Music.app",
                synced=False,
                word_timing="none",
            )
            self._documents[track.key] = document
            return document

        self._miss_until[track.key] = self._clock() + 30.0
        return None
