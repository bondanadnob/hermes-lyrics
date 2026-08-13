"""Hermes backend routes for the Apple Music Lyrics desktop plugin."""

from __future__ import annotations

import asyncio
import math
import platform
import re
import sys
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response

if __package__:
    from .apple_music_lyrics_backend.apple_cache import AppleMusicCacheProvider
    from .apple_music_lyrics_backend.errors import (
        ArtworkBusyError,
        ArtworkStaleIdentityError,
        ArtworkTransientError,
    )
    from .apple_music_lyrics_backend.lrclib import LRCLIBProvider
    from .apple_music_lyrics_backend.music import MusicClient
    from .apple_music_lyrics_backend.service import LyricsService
else:
    dashboard_dir = str(Path(__file__).resolve().parent)
    if dashboard_dir not in sys.path:
        sys.path.insert(0, dashboard_dir)
    from apple_music_lyrics_backend.apple_cache import AppleMusicCacheProvider
    from apple_music_lyrics_backend.errors import (
        ArtworkBusyError,
        ArtworkStaleIdentityError,
        ArtworkTransientError,
    )
    from apple_music_lyrics_backend.lrclib import LRCLIBProvider
    from apple_music_lyrics_backend.music import MusicClient
    from apple_music_lyrics_backend.service import LyricsService


VERSION = "0.1.1"
router = APIRouter()
_NO_STORE = {"Cache-Control": "private, no-store"}
_apple_cache = AppleMusicCacheProvider()
_service = LyricsService(
    music=MusicClient(),
    providers=(_apple_cache, LRCLIBProvider()),
    artwork_provider=_apple_cache,
)
_artwork_admission = threading.Lock()
_deferred_artwork_tasks: set[asyncio.Task] = set()


def _release_deferred_artwork(task: asyncio.Task) -> None:
    _deferred_artwork_tasks.discard(task)
    try:
        task.exception()
    except asyncio.CancelledError:
        pass
    finally:
        _artwork_admission.release()


async def _run_artwork(identity: str):
    if not _artwork_admission.acquire(blocking=False):
        raise ArtworkBusyError("artwork request already admitted")
    deferred_release = False
    try:
        task = asyncio.create_task(asyncio.to_thread(_service.artwork, identity))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            deferred_release = True
            _deferred_artwork_tasks.add(task)
            task.add_done_callback(_release_deferred_artwork)
            raise
    finally:
        if not deferred_release:
            _artwork_admission.release()


@router.get("/state")
async def get_state(response: Response):
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return await asyncio.to_thread(_service.state)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="playback state temporarily unavailable",
            headers={**_NO_STORE, "Retry-After": "1"},
        ) from exc


@router.get("/artwork")
async def get_artwork(response: Response, identity: str | None = None):
    response.headers["Cache-Control"] = "private, no-store"
    if identity is None or re.fullmatch(r"[0-9a-f]{24}", identity) is None:
        raise HTTPException(
            status_code=400,
            detail="invalid track identity",
            headers=_NO_STORE,
        )
    try:
        artwork = await _run_artwork(identity)
    except ArtworkStaleIdentityError as exc:
        raise HTTPException(
            status_code=409,
            detail="track identity is no longer current",
            headers=_NO_STORE,
        ) from exc
    except (ArtworkBusyError, ArtworkTransientError) as exc:
        raise HTTPException(
            status_code=503,
            detail="artwork temporarily unavailable",
            headers={**_NO_STORE, "Retry-After": "1"},
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="artwork temporarily unavailable",
            headers={**_NO_STORE, "Retry-After": "1"},
        ) from exc
    if artwork is None:
        raise HTTPException(
            status_code=404,
            detail="artwork unavailable",
            headers=_NO_STORE,
        )
    return {"artwork": artwork}


@router.post("/control")
async def post_control(body: dict):
    action = body.get("action")
    if not isinstance(action, str):
        raise HTTPException(status_code=400, detail="action must be a string")
    try:
        await asyncio.to_thread(_service.control, action)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/seek")
async def post_seek(body: dict):
    raw_position = body.get("position")
    if isinstance(raw_position, bool) or not isinstance(raw_position, (int, float)):
        raise HTTPException(status_code=400, detail="position must be a number")
    try:
        position = float(raw_position)
    except (TypeError, ValueError, OverflowError) as exc:
        raise HTTPException(status_code=400, detail="position must be a number") from exc
    if not math.isfinite(position):
        raise HTTPException(status_code=400, detail="position must be finite")
    try:
        await asyncio.to_thread(_service.seek, position)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/refresh")
async def post_refresh(response: Response):
    response.headers["Cache-Control"] = "private, no-store"
    if not _artwork_admission.acquire(blocking=False):
        raise HTTPException(
            status_code=503,
            detail="refresh temporarily unavailable",
            headers={**_NO_STORE, "Retry-After": "1"},
        )
    try:
        try:
            _service.refresh()
        except ArtworkBusyError as exc:
            raise HTTPException(
                status_code=503,
                detail="refresh temporarily unavailable",
                headers={**_NO_STORE, "Retry-After": "1"},
            ) from exc
    finally:
        _artwork_admission.release()
    return {"ok": True}


@router.post("/permissions")
async def post_permissions():
    try:
        await asyncio.to_thread(_service.open_automation_settings)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": True}


@router.get("/health")
async def get_health():
    return {
        "ok": True,
        "version": VERSION,
        "platform": platform.system(),
        "musicAppSupported": platform.system() == "Darwin",
    }
