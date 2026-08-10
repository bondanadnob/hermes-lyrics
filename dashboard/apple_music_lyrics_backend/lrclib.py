"""LRCLIB lookup with conservative metadata matching and no API key."""

from __future__ import annotations

import json
import math
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .lyrics import parse_lrc
from .models import LyricLine, LyricsDocument, TrackInfo

BASE_URL = "https://lrclib.net/api"
CLIENT_HEADER = (
    "HermesAppleMusicLyrics/0.1.0 "
    "(https://github.com/bondanadnob/hermes-apple-music-lyrics)"
)


@dataclass(frozen=True, slots=True)
class HTTPResponse:
    status: int
    body: str


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_HTTP_OPENER = build_opener(_NoRedirectHandler())


def _fetch(url: str, headers: dict[str, str], timeout: float) -> HTTPResponse:
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return HTTPResponse(0, "")
    if (
        parsed.scheme != "https"
        or parsed.hostname != "lrclib.net"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        return HTTPResponse(0, "")

    request = Request(url, headers=headers, method="GET")
    try:
        with _HTTP_OPENER.open(request, timeout=timeout) as response:  # nosec B310
            body = response.read(2_000_001)
            if len(body) > 2_000_000:
                return HTTPResponse(413, "")
            return HTTPResponse(response.status, body.decode("utf-8", errors="replace"))
    except HTTPError as exc:
        return HTTPResponse(exc.code, "")
    except (URLError, TimeoutError, OSError):
        return HTTPResponse(0, "")


def _normalize(value: object) -> str:
    folded = unicodedata.normalize("NFKD", str(value or "")).casefold()
    return "".join(character for character in folded if character.isalnum())


def _similarity(left: object, right: object) -> float:
    lhs, rhs = _normalize(left), _normalize(right)
    if not lhs or not rhs:
        return 0.0
    return SequenceMatcher(None, lhs, rhs).ratio()


def _candidate_score(candidate: dict, track: TrackInfo) -> float:
    title_score = _similarity(candidate.get("trackName") or candidate.get("name"), track.title)
    artist_score = _similarity(candidate.get("artistName"), track.artist)
    album_score = _similarity(candidate.get("albumName"), track.album) if track.album else 0.0

    if title_score < 0.62:
        return -1.0
    if track.artist and artist_score < 0.42:
        return -1.0

    duration_score = 0.0
    try:
        candidate_duration = float(candidate.get("duration") or 0)
        if candidate_duration > 0 and track.duration > 0:
            delta = abs(candidate_duration - track.duration)
            if delta > 15:
                return -1.0
            duration_score = max(0.0, 1.0 - delta / 15.0)
    except (TypeError, ValueError):
        pass

    return title_score * 55 + artist_score * 30 + album_score * 5 + duration_score * 20


def _is_strong_synced_candidate(candidate: dict, track: TrackInfo, score: float) -> bool:
    if score < 90:
        return False
    candidate_title = candidate.get("trackName") or candidate.get("name")
    if _similarity(candidate_title, track.title) < 0.9:
        return False
    if track.artist and _similarity(candidate.get("artistName"), track.artist) < 0.7:
        return False
    if track.duration > 0:
        try:
            duration = float(candidate.get("duration") or 0)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(duration) or duration <= 0 or abs(duration - track.duration) > 3:
            return False
    return True


def _document_from_result(result: dict) -> LyricsDocument | None:
    synced = result.get("syncedLyrics")
    plain = str(result.get("plainLyrics") or "")
    if isinstance(synced, str) and synced.strip():
        lines = parse_lrc(synced)
        if lines:
            enriched: list[LyricLine] = []
            for index, line in enumerate(lines):
                end = lines[index + 1].time if index + 1 < len(lines) else None
                enriched.append(LyricLine(line.time, line.text, end=end))
            return LyricsDocument(
                lines=tuple(enriched),
                plain_text=plain or "\n".join(line.text for line in enriched),
                source="LRCLIB",
                synced=True,
                word_timing="line",
            )
    if plain:
        return LyricsDocument(
            lines=tuple(
                LyricLine(float(index), text)
                for index, text in enumerate(plain.splitlines())
            ),
            plain_text=plain,
            source="LRCLIB",
            synced=False,
            word_timing="none",
        )
    return None


class LRCLIBProvider:
    def __init__(
        self,
        fetcher: Callable[[str, dict[str, str], float], HTTPResponse] | None = None,
    ) -> None:
        self._fetcher = fetcher or _fetch
        self._cache: dict[str, LyricsDocument] = {}
        self._headers = {
            "Accept": "application/json",
            "User-Agent": CLIENT_HEADER,
            "Lrclib-Client": CLIENT_HEADER,
        }

    def clear_cache(self) -> None:
        self._cache.clear()

    def lyrics_for(self, track: TrackInfo) -> LyricsDocument | None:
        if not track.title or not track.artist:
            return None
        if track.key in self._cache:
            return self._cache[track.key]

        exact_document: LyricsDocument | None = None
        exact_score = -1.0
        exact = self._exact(track)
        if exact is not None:
            exact_score = _candidate_score(exact, track)
        if exact is not None and exact_score >= 0:
            exact_document = _document_from_result(exact)
            if exact_document is not None and exact_document.synced:
                self._cache[track.key] = exact_document
                return exact_document

        results = self._search(track)
        candidates: list[tuple[float, LyricsDocument]] = []
        strong_synced: list[tuple[float, LyricsDocument]] = []
        for result in results:
            if not isinstance(result, dict):
                continue
            score = _candidate_score(result, track)
            if score < 0:
                continue
            document = _document_from_result(result)
            if document is not None:
                candidates.append((score, document))
                if document.synced and _is_strong_synced_candidate(result, track, score):
                    strong_synced.append((score, document))

        if strong_synced:
            document = max(strong_synced, key=lambda item: item[0])[1]
        elif exact_document is not None:
            document = exact_document
        elif candidates:
            document = max(candidates, key=lambda item: item[0])[1]
        else:
            document = None

        if document is not None:
            self._cache[track.key] = document
        return document

    def _exact(self, track: TrackInfo) -> dict | None:
        params: dict[str, str | int] = {
            "track_name": track.title,
            "artist_name": track.artist,
        }
        if track.album:
            params["album_name"] = track.album
        if track.duration > 0:
            params["duration"] = round(track.duration)
        response = self._fetcher(
            f"{BASE_URL}/get?{urlencode(params)}",
            self._headers,
            5.0,
        )
        if response.status != 200:
            return None
        try:
            result = json.loads(response.body)
            return result if isinstance(result, dict) else None
        except json.JSONDecodeError:
            return None

    def _search(self, track: TrackInfo) -> list[dict]:
        query = " ".join(part for part in [track.title, track.artist] if part)
        response = self._fetcher(
            f"{BASE_URL}/search?{urlencode({'q': query})}",
            self._headers,
            5.0,
        )
        if response.status != 200:
            return []
        try:
            result = json.loads(response.body)
            return result if isinstance(result, list) else []
        except json.JSONDecodeError:
            return []
