"""Hermes backend routes for the Apple Music Lyrics desktop plugin."""

from __future__ import annotations

import asyncio
import math
import platform
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException

if __package__:
    from .apple_music_lyrics_backend.apple_cache import AppleMusicCacheProvider
    from .apple_music_lyrics_backend.lrclib import LRCLIBProvider
    from .apple_music_lyrics_backend.music import MusicClient
    from .apple_music_lyrics_backend.service import LyricsService
else:
    dashboard_dir = str(Path(__file__).resolve().parent)
    if dashboard_dir not in sys.path:
        sys.path.insert(0, dashboard_dir)
    from apple_music_lyrics_backend.apple_cache import AppleMusicCacheProvider
    from apple_music_lyrics_backend.lrclib import LRCLIBProvider
    from apple_music_lyrics_backend.music import MusicClient
    from apple_music_lyrics_backend.service import LyricsService


VERSION = "0.1.0"
router = APIRouter()
_service = LyricsService(
    music=MusicClient(),
    providers=(AppleMusicCacheProvider(), LRCLIBProvider()),
)


@router.get("/state")
async def get_state():
    return await asyncio.to_thread(_service.state)


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
    try:
        position = float(body.get("position"))
    except (TypeError, ValueError) as exc:
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
async def post_refresh():
    await asyncio.to_thread(_service.refresh)
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
