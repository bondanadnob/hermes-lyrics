"""Scoped Hermes routes for Lyrics for Hermes.

Hermes hosts this router. The plugin does not bind a listener or authenticate callers;
use it only with Hermes's local trusted backend scope.
"""

from __future__ import annotations

import asyncio
import ipaddress
import math
import platform
import re
import sys
import threading
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response

if __package__:
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
    from apple_music_lyrics_backend.errors import (
        ArtworkBusyError,
        ArtworkStaleIdentityError,
        ArtworkTransientError,
    )
    from apple_music_lyrics_backend.lrclib import LRCLIBProvider
    from apple_music_lyrics_backend.music import MusicClient
    from apple_music_lyrics_backend.service import LyricsService

VERSION = "0.3.0"
PLUGIN_ID = "lyrics-for-hermes"
DISPLAY_NAME = "Lyrics for Hermes"
_NO_STORE = {"Cache-Control": "private, no-store"}


def _require_loopback(request: Request) -> None:
    """Fail closed; never trust Host or proxy forwarding headers."""
    client = request.client
    try:
        address = ipaddress.ip_address(client.host if client else "")
    except ValueError as exc:
        raise HTTPException(403, "local trusted backend required", headers=_NO_STORE) from exc
    if not address.is_loopback:
        raise HTTPException(403, "local trusted backend required", headers=_NO_STORE)


router = APIRouter(dependencies=[Depends(_require_loopback)])
_playback = MusicClient()
_service = LyricsService(music=_playback, providers=(LRCLIBProvider(),))
_artwork_admission = threading.Lock()
_deferred_artwork_tasks: set[asyncio.Task] = set()


def _shutdown_plugin() -> None:
    shutdown = getattr(_playback, "shutdown", None)
    if shutdown:
        shutdown()


router.on_shutdown.append(_shutdown_plugin)


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
    response.headers.update(_NO_STORE)
    try:
        return await asyncio.to_thread(_service.state)
    except Exception as exc:
        raise HTTPException(
            503,
            "playback state temporarily unavailable",
            headers={**_NO_STORE, "Retry-After": "1"},
        ) from exc


@router.get("/artwork")
async def get_artwork(response: Response, identity: str | None = None):
    response.headers.update(_NO_STORE)
    if identity is None or re.fullmatch(r"[0-9a-f]{24}", identity) is None:
        raise HTTPException(400, "invalid track identity", headers=_NO_STORE)
    try:
        artwork = await _run_artwork(identity)
    except ArtworkStaleIdentityError as exc:
        raise HTTPException(409, "track identity is no longer current", headers=_NO_STORE) from exc
    except (ArtworkBusyError, ArtworkTransientError) as exc:
        raise HTTPException(
            503,
            "artwork temporarily unavailable",
            headers={**_NO_STORE, "Retry-After": "1"},
        ) from exc
    except Exception as exc:
        raise HTTPException(
            503,
            "artwork temporarily unavailable",
            headers={**_NO_STORE, "Retry-After": "1"},
        ) from exc
    if artwork is None:
        raise HTTPException(404, "artwork unavailable", headers=_NO_STORE)
    return {"artwork": artwork}


async def _request_object(request: Request | dict) -> dict:
    if isinstance(request, dict):
        return request
    try:
        body = await request.json()
    except (TypeError, ValueError):
        raise HTTPException(422, "invalid request", headers=_NO_STORE) from None
    if not isinstance(body, dict):
        raise HTTPException(422, "invalid request", headers=_NO_STORE)
    return body


@router.post("/control")
async def post_control(request: Request, response: Response):
    response.headers.update(_NO_STORE)
    body = await _request_object(request)
    action = body.get("action")
    if not isinstance(action, str):
        raise HTTPException(400, "action must be a string", headers=_NO_STORE)
    try:
        await asyncio.to_thread(_service.control, action)
    except ValueError as exc:
        raise HTTPException(400, str(exc), headers=_NO_STORE) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc), headers=_NO_STORE) from exc
    return {"ok": True}


@router.post("/seek")
async def post_seek(request: Request, response: Response):
    response.headers.update(_NO_STORE)
    body = await _request_object(request)
    raw = body.get("position")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise HTTPException(400, "position must be a number", headers=_NO_STORE)
    try:
        position = float(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise HTTPException(400, "position must be a number", headers=_NO_STORE) from exc
    if not math.isfinite(position):
        raise HTTPException(400, "position must be finite", headers=_NO_STORE)
    try:
        await asyncio.to_thread(_service.seek, position)
    except ValueError as exc:
        raise HTTPException(400, str(exc), headers=_NO_STORE) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc), headers=_NO_STORE) from exc
    return {"ok": True}


@router.post("/refresh")
async def post_refresh(response: Response):
    response.headers.update(_NO_STORE)
    if not _artwork_admission.acquire(blocking=False):
        raise HTTPException(
            503,
            "refresh temporarily unavailable",
            headers={**_NO_STORE, "Retry-After": "1"},
        )
    try:
        _service.refresh()
    except ArtworkBusyError as exc:
        raise HTTPException(
            503,
            "refresh temporarily unavailable",
            headers={**_NO_STORE, "Retry-After": "1"},
        ) from exc
    finally:
        _artwork_admission.release()
    return {"ok": True}


@router.post("/permissions")
async def post_permissions(response: Response):
    response.headers.update(_NO_STORE)
    try:
        await asyncio.to_thread(_service.open_automation_settings)
    except RuntimeError as exc:
        raise HTTPException(502, str(exc), headers=_NO_STORE) from exc
    return {"ok": True}


@router.get("/health")
async def get_health(response: Response):
    response.headers.update(_NO_STORE)
    return {
        "ok": True,
        "version": VERSION,
        "platform": platform.system(),
        "musicAppSupported": platform.system() == "Darwin",
        "providers": ["Music.app", "LRCLIB"],
        "microphone": False,
        "privateProviders": False,
    }
