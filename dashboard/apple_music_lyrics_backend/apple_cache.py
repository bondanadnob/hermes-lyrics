"""Read Apple Music's existing local syllable-lyrics cache, read-only."""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import replace
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote, urlparse

from .models import LyricsDocument, TrackInfo
from .ttml import parse_ttml

_CACHE_FILENAME = re.compile(r"^[0-9A-Fa-f-]{36}$")
_MAX_CANDIDATES = 160
_MAX_RESPONSE_BYTES = 8_000_000
_MAX_SCAN_BYTES = 64_000_000


def _normalize(value: object) -> str:
    folded = unicodedata.normalize("NFKD", str(value or "")).casefold()
    return "".join(character for character in folded if character.isalnum())


def _ratio(left: object, right: object) -> float:
    lhs, rhs = _normalize(left), _normalize(right)
    if not lhs or not rhs:
        return 0.0
    return SequenceMatcher(None, lhs, rhs).ratio()


class AppleMusicCacheProvider:
    """Best-effort provider for Music.app's private local cache format."""

    def __init__(self, cache_root: Path | None = None) -> None:
        self.cache_root = cache_root or Path.home() / "Library/Caches/com.apple.Music"
        self.database = self.cache_root / "Cache.db"
        self.data_directory = self.cache_root / "fsCachedData"
        self._cache: dict[str, LyricsDocument] = {}

    def clear_cache(self) -> None:
        self._cache.clear()

    def match_score(self, candidate: dict, track: TrackInfo) -> float | None:
        title = _ratio(candidate.get("name"), track.title)
        artist = _ratio(candidate.get("artistName"), track.artist)
        album = _ratio(candidate.get("albumName"), track.album) if track.album else 0.0
        if title < 0.75 or (track.artist and artist < 0.55):
            return None

        duration_score = 0.0
        try:
            duration = float(candidate.get("durationInMillis") or 0) / 1000.0
            if duration > 0 and track.duration > 0:
                delta = abs(duration - track.duration)
                if delta > 12:
                    return None
                duration_score = max(0.0, 1.0 - delta / 12.0)
        except (TypeError, ValueError):
            pass
        return title * 100 + artist * 45 + album * 15 + duration_score * 40

    def lyrics_for(self, track: TrackInfo) -> LyricsDocument | None:
        if not track.title or not track.artist:
            return None
        if track.key in self._cache:
            return self._cache[track.key]

        best_song: dict | None = None
        best_score = -1.0
        for payload in self._candidate_payloads():
            try:
                response = json.loads(payload)
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
                continue
            data = response.get("data") if isinstance(response, dict) else None
            if not isinstance(data, list):
                continue
            for song in data:
                if not isinstance(song, dict):
                    continue
                attributes = song.get("attributes")
                if not isinstance(attributes, dict):
                    continue
                score = self.match_score(attributes, track)
                if score is None or score <= best_score:
                    continue
                if self._ttml_value(song) is None:
                    continue
                best_song, best_score = song, score
            if best_score >= 195:
                break

        if best_song is None:
            return None
        ttml = self._choose_ttml(self._ttml_value(best_song))
        if not ttml:
            return None
        try:
            document = parse_ttml(ttml)
        except ValueError:
            return None
        if not document.lines or not document.synced:
            return None

        attributes = best_song.get("attributes") or {}
        artwork = attributes.get("artwork") or {}
        artwork_url = self._artwork_url(artwork.get("url")) if isinstance(artwork, dict) else None
        document = replace(document, artwork_url=artwork_url)
        self._cache[track.key] = document
        return document

    def _candidate_payloads(self):
        seen_files: set[Path] = set()
        remaining_bytes = _MAX_SCAN_BYTES
        for is_on_fs, receiver_data in self._database_rows():
            if remaining_bytes <= 0:
                return
            data = bytes(receiver_data or b"")
            if is_on_fs:
                try:
                    filename = data.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                if not _CACHE_FILENAME.fullmatch(filename):
                    continue
                path = self.data_directory / filename
                try:
                    resolved = path.resolve()
                    resolved.relative_to(self.data_directory.resolve())
                    size = resolved.stat().st_size
                except (OSError, ValueError):
                    continue
                if not 0 < size <= min(_MAX_RESPONSE_BYTES, remaining_bytes):
                    continue
                remaining_bytes -= size
                payload = self._read_cache_file(resolved)
                if payload is not None:
                    seen_files.add(resolved)
                    yield payload
            elif 0 < len(data) <= min(_MAX_RESPONSE_BYTES, remaining_bytes):
                remaining_bytes -= len(data)
                try:
                    yield data.decode("utf-8", errors="strict")
                except UnicodeDecodeError:
                    continue

        try:
            recent = sorted(
                (path for path in self.data_directory.iterdir() if path.is_file()),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )[:_MAX_CANDIDATES]
        except OSError:
            recent = []
        for path in recent:
            if remaining_bytes <= 0:
                return
            try:
                resolved = path.resolve()
                resolved.relative_to(self.data_directory.resolve())
                size = resolved.stat().st_size
            except (OSError, ValueError):
                continue
            if resolved in seen_files:
                continue
            if not 0 < size <= min(_MAX_RESPONSE_BYTES, remaining_bytes):
                continue
            remaining_bytes -= size
            payload = self._read_cache_file(resolved)
            if payload is not None:
                yield payload

    def _database_rows(self) -> list[tuple[int, bytes]]:
        if not self.database.is_file():
            return []
        uri = f"file:{quote(str(self.database.resolve()))}?mode=ro"
        query = """
            SELECT d.isDataOnFS, d.receiver_data
            FROM cfurl_cache_response AS r
            JOIN cfurl_cache_receiver_data AS d USING(entry_ID)
            WHERE r.request_key LIKE '%syllable-lyrics%'
            ORDER BY r.time_stamp DESC
            LIMIT ?
        """
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=1)
            try:
                return list(connection.execute(query, (_MAX_CANDIDATES,)))
            finally:
                connection.close()
        except sqlite3.Error:
            return []

    def _read_cache_file(self, path: Path) -> str | None:
        try:
            if path.stat().st_size > _MAX_RESPONSE_BYTES:
                return None
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None

    @staticmethod
    def _ttml_value(song: dict):
        relationships = song.get("relationships") or {}
        relation = relationships.get("syllable-lyrics") or {}
        data = relation.get("data") or []
        if not isinstance(data, list) or not data:
            return None
        attributes = data[0].get("attributes") if isinstance(data[0], dict) else None
        return attributes.get("ttmlLocalizations") if isinstance(attributes, dict) else None

    @staticmethod
    def _choose_ttml(value) -> str | None:
        if isinstance(value, str):
            raw = value.strip()
            if raw.startswith("<"):
                return raw
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                return None
        if not isinstance(value, dict):
            return None
        for key in ("en-US", "en", "und"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip().startswith("<"):
                return candidate.strip()
        for candidate in value.values():
            if isinstance(candidate, str) and candidate.strip().startswith("<"):
                return candidate.strip()
        return None

    @staticmethod
    def _artwork_url(value: object) -> str | None:
        if not isinstance(value, str) or not value.startswith("https://"):
            return None
        hostname = (urlparse(value).hostname or "").casefold()
        if hostname != "mzstatic.com" and not hostname.endswith(".mzstatic.com"):
            return None
        return (
            value.replace("{w}", "320")
            .replace("{h}", "320")
            .replace("{c}", "bb")
            .replace("{f}", "jpg")
        )
