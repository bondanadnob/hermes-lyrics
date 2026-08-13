"""Read Apple Music's existing local syllable-lyrics cache, read-only."""

from __future__ import annotations

import itertools
import json
import math
import os
import re
import sqlite3
import stat
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlparse

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
        self._cache: dict[str, LyricsDocument | None] = {}
        self._artwork_cache: dict[str, str | None] = {}
        self._active_identity: str | None = None

    def clear_cache(self) -> None:
        self._cache.clear()
        self._artwork_cache.clear()
        self._active_identity = None

    def match_score(self, candidate: dict, track: TrackInfo) -> float | None:
        title = _ratio(candidate.get("name"), track.title)
        artist = _ratio(candidate.get("artistName"), track.artist)
        album = _ratio(candidate.get("albumName"), track.album) if track.album else 0.0
        if title < 0.75 or (track.artist and artist < 0.55):
            return None
        if track.album and candidate.get("albumName") and album < 0.55:
            return None

        duration_score = 0.0
        raw_duration = candidate.get("durationInMillis")
        try:
            if isinstance(raw_duration, bool) or not isinstance(
                raw_duration, (int, float, str, type(None))
            ):
                return None
            duration = float(raw_duration or 0) / 1000.0
            if not math.isfinite(duration) or duration < 0:
                return None
            if duration > 0 and track.duration > 0:
                delta = abs(duration - track.duration)
                if delta > 12:
                    return None
                duration_score = max(0.0, 1.0 - delta / 12.0)
        except (TypeError, ValueError, OverflowError):
            return None
        return title * 100 + artist * 45 + album * 15 + duration_score * 40

    def lyrics_for(self, track: TrackInfo) -> LyricsDocument | None:
        if not track.title or not track.artist:
            return None
        self._select_identity(track.identity)
        if track.identity in self._cache:
            return self._cache[track.identity]

        best_document: LyricsDocument | None = None
        best_score = -1.0
        best_artwork_url: str | None = None
        best_artwork_score = -1.0
        for payload in self._candidate_payloads():
            try:
                response = json.loads(payload)
            except (ValueError, UnicodeDecodeError, TypeError, RecursionError):
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
                if score is None:
                    continue
                artwork = attributes.get("artwork") or {}
                artwork_url = (
                    self._artwork_url(artwork.get("url"))
                    if isinstance(artwork, dict)
                    else None
                )
                if artwork_url is not None and score > best_artwork_score:
                    best_artwork_url, best_artwork_score = artwork_url, score

                ttml = self._choose_ttml(self._ttml_value(song))
                if ttml is None:
                    continue
                try:
                    document = parse_ttml(ttml)
                except ValueError:
                    continue
                if (
                    document.lines
                    and document.synced
                    and score > best_score
                ):
                    best_document, best_score = document, score
            if best_score >= 195 and best_artwork_score >= 195:
                break

        self._artwork_cache[track.identity] = best_artwork_url
        self._cache[track.identity] = best_document
        return best_document

    def artwork_url_for(self, track: TrackInfo) -> str | None:
        if not track.title or not track.artist:
            return None
        self._select_identity(track.identity)
        if track.identity in self._artwork_cache:
            return self._artwork_cache[track.identity]

        best_url: str | None = None
        best_score = -1.0
        for payload in self._candidate_payloads():
            try:
                response = json.loads(payload)
            except (ValueError, UnicodeDecodeError, TypeError, RecursionError):
                continue
            data = response.get("data") if isinstance(response, dict) else None
            if not isinstance(data, list):
                continue
            for song in data:
                attributes = song.get("attributes") if isinstance(song, dict) else None
                if not isinstance(attributes, dict):
                    continue
                score = self.match_score(attributes, track)
                artwork = attributes.get("artwork") or {}
                url = (
                    self._artwork_url(artwork.get("url"))
                    if isinstance(artwork, dict)
                    else None
                )
                if score is not None and url is not None and score > best_score:
                    best_url, best_score = url, score
            if best_score >= 195:
                break
        self._artwork_cache[track.identity] = best_url
        return best_url

    def _select_identity(self, identity: str) -> None:
        if identity == self._active_identity:
            return
        self._cache.clear()
        self._artwork_cache.clear()
        self._active_identity = identity

    def _candidate_payloads(self):
        seen_files: set[str] = set()
        remaining_bytes = _MAX_SCAN_BYTES
        data_descriptor = self._open_data_directory()
        try:
            for is_on_fs, receiver_data in self._database_rows():
                if remaining_bytes <= 0:
                    return
                data = (
                    receiver_data
                    if isinstance(receiver_data, bytes)
                    else bytes(receiver_data or b"")
                )
                if not data:
                    continue
                if len(data) > remaining_bytes:
                    return
                remaining_bytes -= len(data)
                if is_on_fs:
                    if data_descriptor is None:
                        continue
                    try:
                        filename = data.decode("utf-8")
                    except UnicodeDecodeError:
                        continue
                    if not _CACHE_FILENAME.fullmatch(filename):
                        continue
                    result = self._read_cache_file(
                        filename,
                        remaining_bytes,
                        directory_descriptor=data_descriptor,
                    )
                    if result is not None:
                        payload, consumed = result
                        remaining_bytes -= consumed
                        seen_files.add(filename)
                        if payload is not None:
                            yield payload
                elif len(data) <= _MAX_RESPONSE_BYTES:
                    try:
                        yield data.decode("utf-8", errors="strict")
                    except UnicodeDecodeError:
                        continue

            if data_descriptor is None:
                return
            try:
                with os.scandir(data_descriptor) as entries:
                    recent = sorted(
                        (
                            (entry.stat(follow_symlinks=False).st_mtime, entry.name)
                            for entry in itertools.islice(entries, _MAX_CANDIDATES)
                            if entry.is_file(follow_symlinks=False)
                            and _CACHE_FILENAME.fullmatch(entry.name)
                        ),
                        reverse=True,
                    )
            except OSError:
                recent = []
            for _mtime, filename in recent:
                if remaining_bytes <= 0:
                    return
                if filename in seen_files:
                    continue
                result = self._read_cache_file(
                    filename,
                    remaining_bytes,
                    directory_descriptor=data_descriptor,
                )
                if result is not None:
                    payload, consumed = result
                    remaining_bytes -= consumed
                    if payload is not None:
                        yield payload
        finally:
            if data_descriptor is not None:
                os.close(data_descriptor)

    def _open_data_directory(self) -> int | None:
        root_descriptor = None
        data_descriptor = None
        try:
            directory_flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            root_descriptor = os.open(self.cache_root, directory_flags)
            data_descriptor = os.open(
                "fsCachedData",
                directory_flags,
                dir_fd=root_descriptor,
            )
            if not stat.S_ISDIR(os.fstat(data_descriptor).st_mode):
                os.close(data_descriptor)
                return None
            return data_descriptor
        except OSError:
            if data_descriptor is not None:
                os.close(data_descriptor)
            return None
        finally:
            if root_descriptor is not None:
                os.close(root_descriptor)

    def _database_rows(self):
        query = """
            SELECT d.isDataOnFS, d.receiver_data
            FROM cfurl_cache_response AS r
            JOIN cfurl_cache_receiver_data AS d USING(entry_ID)
            WHERE r.request_key LIKE '%syllable-lyrics%'
              AND typeof(d.receiver_data) = 'blob'
              AND length(d.receiver_data) BETWEEN 1 AND ?
            ORDER BY r.time_stamp DESC
            LIMIT ?
        """
        root_descriptor = None
        database_descriptor = None
        connection = None
        try:
            directory_flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            root_descriptor = os.open(self.cache_root, directory_flags)
            database_descriptor = os.open(
                "Cache.db",
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=root_descriptor,
            )
            metadata = os.fstat(database_descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                return
            uri = f"file:/dev/fd/{database_descriptor}?mode=ro&immutable=1"
            connection = sqlite3.connect(uri, uri=True, timeout=1)
            row_limit = min(_MAX_RESPONSE_BYTES, _MAX_SCAN_BYTES)
            for row in connection.execute(
                query,
                (row_limit, _MAX_CANDIDATES),
            ):
                yield row
        except (OSError, sqlite3.Error):
            return
        finally:
            if connection is not None:
                connection.close()
            if database_descriptor is not None:
                os.close(database_descriptor)
            if root_descriptor is not None:
                os.close(root_descriptor)

    def _read_cache_file(
        self,
        path: Path | str,
        remaining_bytes: int,
        *,
        directory_descriptor: int | None = None,
    ) -> tuple[str | None, int] | None:
        limit = min(_MAX_RESPONSE_BYTES, max(0, int(remaining_bytes)))
        if limit == 0:
            return None
        descriptor = None
        data = bytearray()
        try:
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags, dir_fd=directory_descriptor)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= limit:
                return None
            while len(data) <= limit:
                chunk = os.read(descriptor, min(65_536, limit + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if not data:
                return None
            if len(data) > limit:
                return None, len(data)
            try:
                return data.decode("utf-8", errors="strict"), len(data)
            except UnicodeError:
                return None, len(data)
        except (OSError, ValueError):
            return (None, len(data)) if data else None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _ttml_value(song: dict):
        relationships = song.get("relationships")
        if not isinstance(relationships, dict):
            return None
        relation = relationships.get("syllable-lyrics")
        if not isinstance(relation, dict):
            return None
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
            except (ValueError, RecursionError):
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
        if (
            not isinstance(value, str)
            or not value.startswith("https://")
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
            parsed.scheme.casefold() != "https"
            or parsed.username is not None
            or parsed.password is not None
            or port not in (None, 443)
            or (
                hostname != "mzstatic.com"
                and not hostname.endswith(".mzstatic.com")
            )
        ):
            return None
        resolved = (
            value.replace("{w}", "320")
            .replace("{h}", "320")
            .replace("{c}", "bb")
            .replace("{f}", "jpg")
        )
        return resolved if len(resolved) <= 2048 else None
