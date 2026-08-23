"""Orchestrate Music.app sampling and ordered lyric-provider fallbacks."""

from __future__ import annotations

import threading
import time
from typing import Iterable, Protocol
from urllib.parse import urlparse

from .errors import ArtworkBusyError, ArtworkStaleIdentityError
from .models import ArtworkPayload, LyricLine, LyricsDocument, TrackInfo


class MusicBoundary(Protocol):
    def sample(self) -> TrackInfo: ...

    def control(self, action: str) -> None: ...

    def seek(self, position: float) -> None: ...

    def artwork_for(self, expected_identity: str) -> ArtworkPayload | None: ...

    def clear_artwork_cache(self) -> None: ...

    def open_automation_settings(self) -> None: ...


class LyricsProvider(Protocol):
    def lyrics_for(self, track: TrackInfo) -> LyricsDocument | None: ...

    def clear_cache(self) -> None: ...


class ArtworkURLProvider(Protocol):
    def artwork_url_for(self, track: TrackInfo) -> str | None: ...

    def clear_cache(self) -> None: ...


def _safe_remote_artwork_url(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 2048
        or any(character in value for character in "\r\n\t\\")
    ):
        return None
    try:
        parsed = urlparse(value)
        hostname = (parsed.hostname or "").casefold()
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() == "https"
        and parsed.username is None
        and parsed.password is None
        and port in (None, 443)
        and (hostname == "mzstatic.com" or hostname.endswith(".mzstatic.com"))
    ):
        return value
    return None


class LyricsService:
    def __init__(
        self,
        music: MusicBoundary,
        providers: Iterable[LyricsProvider],
        artwork_provider: ArtworkURLProvider | None = None,
        monotonic_clock=time.monotonic,
    ) -> None:
        self.music = music
        self.providers = tuple(providers)
        self.artwork_provider = artwork_provider
        self._clock = monotonic_clock
        self._lock = threading.RLock()
        self._identity_lock = threading.Lock()
        self._artwork_lock = threading.Lock()
        self._documents: dict[str, LyricsDocument] = {}
        self._miss_until: dict[str, float] = {}
        self._active_track_identity: str | None = None
        self._latest_track_identity: str | None = None

    def state(self) -> dict:
        with self._lock:
            track = self.music.sample()
            base = {
                "protocolVersion": 1,
                "track": track.to_dict(),
                "lyrics": LyricsDocument().to_dict(),
                "artwork": {"remote_url": None},
            }
            if track.error == "automation_permission":
                self._select_track(None)
                self._set_latest_identity(None)
                return {**base, "status": "permission_required"}
            if track.source == "ambient" and track.state == "recognition_error":
                self._select_track(None)
                self._set_latest_identity(None)
                return {**base, "status": "recognition_error"}
            if track.error:
                self._select_track(None)
                self._set_latest_identity(None)
                return {**base, "status": "error"}
            if track.source == "ambient" and track.state == "ambient_idle":
                self._select_track(None)
                self._set_latest_identity(None)
                return {**base, "status": "ambient_idle"}
            if track.source == "ambient" and track.state == "listening":
                self._select_track(None)
                self._set_latest_identity(None)
                return {**base, "status": "listening"}
            if track.source == "ambient" and track.state == "no_match":
                self._select_track(None)
                self._set_latest_identity(None)
                return {**base, "status": "no_match"}
            if not track.running or track.state in {"not_running", "stopped"} or not track.title:
                self._select_track(None)
                self._set_latest_identity(None)
                return {**base, "status": "idle"}

            self._select_track(track.identity)
            self._set_latest_identity(track.identity)
            document = self._document_for(track)
            base["artwork"]["remote_url"] = self._remote_artwork_for(track)
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

    def artwork(self, expected_identity: str) -> dict | None:
        with self._identity_lock:
            if self._latest_track_identity != expected_identity:
                raise ArtworkStaleIdentityError("requested track is no longer current")
        if not self._artwork_lock.acquire(blocking=False):
            raise ArtworkBusyError("artwork extraction already in progress")
        try:
            with self._identity_lock:
                if self._latest_track_identity != expected_identity:
                    raise ArtworkStaleIdentityError("requested track is no longer current")
            artwork = self.music.artwork_for(expected_identity)
            with self._identity_lock:
                if self._latest_track_identity != expected_identity:
                    raise ArtworkStaleIdentityError(
                        "track changed before artwork could be returned"
                    )
                if artwork is not None and artwork.identity != expected_identity:
                    raise ArtworkStaleIdentityError(
                        "native artwork identity does not match the requested track"
                    )
                return artwork.to_dict() if artwork is not None else None
        finally:
            self._artwork_lock.release()

    def open_automation_settings(self) -> None:
        self.music.open_automation_settings()

    def select_source(self, source: str) -> None:
        with self._lock:
            getattr(self.music, "select_source")(source)
            self._select_track(None)
            self._set_latest_identity(None)

    def listen_ambient(self) -> None:
        with self._lock:
            getattr(self.music, "listen_ambient")()
            self._select_track(None)
            self._set_latest_identity(None)

    def stop_ambient(self) -> None:
        with self._lock:
            getattr(self.music, "stop_ambient")()
            self._select_track(None)
            self._set_latest_identity(None)

    def refresh(self) -> None:
        if not self._lock.acquire(blocking=False):
            raise ArtworkBusyError("lyrics refresh already in progress")
        artwork_locked = False
        try:
            if not self._artwork_lock.acquire(blocking=False):
                raise ArtworkBusyError("artwork extraction already in progress")
            artwork_locked = True
            self._documents.clear()
            self._miss_until.clear()
            self._clear_provider_caches()
            self.music.clear_artwork_cache()
        finally:
            if artwork_locked:
                self._artwork_lock.release()
            self._lock.release()

    def _document_for(self, track: TrackInfo) -> LyricsDocument | None:
        cached = self._documents.get(track.identity)
        if cached is not None:
            return cached
        if self._miss_until.get(track.identity, 0.0) > self._clock():
            return None

        for provider in self.providers:
            try:
                document = provider.lyrics_for(track)
            except Exception:
                document = None
            if document is not None and document.lines and document.synced:
                self._documents[track.identity] = document
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
            self._documents[track.identity] = document
            return document

        self._miss_until[track.identity] = self._clock() + 30.0
        return None

    def _remote_artwork_for(self, track: TrackInfo) -> str | None:
        recognized_artwork = _safe_remote_artwork_url(track.artwork_url)
        if recognized_artwork is not None:
            return recognized_artwork
        if self.artwork_provider is None:
            return None
        try:
            return _safe_remote_artwork_url(
                self.artwork_provider.artwork_url_for(track)
            )
        except Exception:
            return None

    def _set_latest_identity(self, identity: str | None) -> None:
        with self._identity_lock:
            self._latest_track_identity = identity

    def _select_track(self, identity: str | None) -> None:
        if identity == self._active_track_identity:
            return
        if identity is None:
            self._clear_provider_caches()
        self._documents.clear()
        self._miss_until.clear()
        self._active_track_identity = identity

    def _clear_provider_caches(self) -> None:
        seen: set[int] = set()
        for provider in (*self.providers, self.artwork_provider):
            if provider is None or id(provider) in seen:
                continue
            seen.add(id(provider))
            provider.clear_cache()
