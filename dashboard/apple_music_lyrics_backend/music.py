"""Read and control Music.app through a single, JSON-producing JXA call."""

from __future__ import annotations

import base64
import ctypes
import json
import math
import os
import re
import selectors
import subprocess
import threading
import time
import zlib
from dataclasses import dataclass
from typing import Callable

from .errors import ArtworkStaleIdentityError, ArtworkTransientError
from .models import ArtworkPayload, TrackInfo

NOW_PLAYING_SCRIPT = r'''
const music = Application("Music");
if (!music.running()) {
  JSON.stringify({ running: false, state: "not_running" });
} else {
  const state = String(music.playerState());
  if (state === "stopped") {
    JSON.stringify({ running: true, state: state });
  } else {
    const track = music.currentTrack();
    let lyrics = "";
    let persistentId = "";
    let databaseId = "";
    try { lyrics = String(track.lyrics() || ""); } catch (_) {}
    try { persistentId = String(track.persistentID() || ""); } catch (_) {}
    try { databaseId = String(track.databaseID() || ""); } catch (_) {}
    JSON.stringify({
      running: true,
      state: state,
      title: String(track.name() || ""),
      artist: String(track.artist() || ""),
      album: String(track.album() || ""),
      duration: Number(track.duration() || 0),
      position: Number(music.playerPosition() || 0),
      lyrics: lyrics,
      persistentId: persistentId,
      databaseId: databaseId
    });
  }
}
'''.strip()

ARTWORK_SCRIPT = r'''
const music = Application("Music");
if (!music.running()) {
  JSON.stringify({ running: false });
} else {
  const state = String(music.playerState());
  if (state === "stopped") {
    JSON.stringify({ running: true, state: state });
  } else {
    const track = music.currentTrack();
    let persistentId = "";
    let databaseId = "";
    try { persistentId = String(track.persistentID() || ""); } catch (_) {}
    try { databaseId = String(track.databaseID() || ""); } catch (_) {}
    const out = {
      running: true,
      state: state,
      title: String(track.name() || ""),
      artist: String(track.artist() || ""),
      album: String(track.album() || ""),
      duration: Number(track.duration() || 0),
      persistentId: persistentId,
      databaseId: databaseId,
      rawArtwork: "",
      artworkReadError: false,
      after: null
    };
    try {
      const artworks = track.artworks();
      if (artworks.length) {
        const raw = artworks[0].rawData();
        if (typeof raw === "string" && raw.length <= 4000016) out.rawArtwork = raw;
      }
    } catch (_) {
      out.artworkReadError = true;
    }
    try {
      const current = music.currentTrack();
      let currentPersistentId = "";
      let currentDatabaseId = "";
      try { currentPersistentId = String(current.persistentID() || ""); } catch (_) {}
      try { currentDatabaseId = String(current.databaseID() || ""); } catch (_) {}
      out.after = {
        running: true,
        state: String(music.playerState()),
        title: String(current.name() || ""),
        artist: String(current.artist() || ""),
        album: String(current.album() || ""),
        duration: Number(current.duration() || 0),
        persistentId: currentPersistentId,
        databaseId: currentDatabaseId
      };
    } catch (_) {}
    JSON.stringify(out);
  }
}
'''.strip()

ARTWORK_IDENTITY_SCRIPT = r'''
const music = Application("Music");
if (!music.running()) {
  JSON.stringify({ running: false });
} else {
  const state = String(music.playerState());
  if (state === "stopped") {
    JSON.stringify({ running: true, state: state });
  } else {
    const track = music.currentTrack();
    let persistentId = "";
    let databaseId = "";
    try { persistentId = String(track.persistentID() || ""); } catch (_) {}
    try { databaseId = String(track.databaseID() || ""); } catch (_) {}
    JSON.stringify({
      running: true,
      state: state,
      title: String(track.name() || ""),
      artist: String(track.artist() || ""),
      album: String(track.album() || ""),
      duration: Number(track.duration() || 0),
      persistentId: persistentId,
      databaseId: databaseId
    });
  }
}
'''.strip()

_RAW_ARTWORK = re.compile(r"^'tdta'\(\$([0-9A-Fa-f]+)\$\)$")
_MAX_ARTWORK_BYTES = 2_000_000
_MAX_ARTWORK_DIMENSION = 2048
_MAX_ARTWORK_PIXELS = 4_194_304
_MAX_JXA_STDOUT_BYTES = 4_100_000
_MAX_JXA_STDERR_BYTES = 65_536


def _safe_dimensions(width: int, height: int) -> bool:
    return (
        0 < width <= _MAX_ARTWORK_DIMENSION
        and 0 < height <= _MAX_ARTWORK_DIMENSION
        and width * height <= _MAX_ARTWORK_PIXELS
    )


def _imageio_decodes_jpeg(data: bytes, expected_dimensions: tuple[int, int]) -> bool:
    """Force an in-memory macOS ImageIO decode after structural JPEG validation."""
    try:
        core_foundation = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        image_io = ctypes.CDLL("/System/Library/Frameworks/ImageIO.framework/ImageIO")
        core_graphics = ctypes.CDLL(
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
        )
    except OSError:
        return False

    pointer = ctypes.c_void_p
    core_foundation.CFDataCreate.argtypes = [
        pointer,
        ctypes.POINTER(ctypes.c_ubyte),
        ctypes.c_long,
    ]
    core_foundation.CFDataCreate.restype = pointer
    core_foundation.CFDataGetLength.argtypes = [pointer]
    core_foundation.CFDataGetLength.restype = ctypes.c_long
    core_foundation.CFRelease.argtypes = [pointer]
    image_io.CGImageSourceCreateWithData.argtypes = [pointer, pointer]
    image_io.CGImageSourceCreateWithData.restype = pointer
    image_io.CGImageSourceCreateImageAtIndex.argtypes = [
        pointer,
        ctypes.c_size_t,
        pointer,
    ]
    image_io.CGImageSourceCreateImageAtIndex.restype = pointer
    core_graphics.CGImageGetWidth.argtypes = [pointer]
    core_graphics.CGImageGetWidth.restype = ctypes.c_size_t
    core_graphics.CGImageGetHeight.argtypes = [pointer]
    core_graphics.CGImageGetHeight.restype = ctypes.c_size_t
    core_graphics.CGImageGetBytesPerRow.argtypes = [pointer]
    core_graphics.CGImageGetBytesPerRow.restype = ctypes.c_size_t
    core_graphics.CGImageGetDataProvider.argtypes = [pointer]
    core_graphics.CGImageGetDataProvider.restype = pointer
    core_graphics.CGDataProviderCopyData.argtypes = [pointer]
    core_graphics.CGDataProviderCopyData.restype = pointer

    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    cf_data = source = image = decoded = None
    try:
        cf_data = core_foundation.CFDataCreate(None, buffer, len(data))
        if not cf_data:
            return False
        source = image_io.CGImageSourceCreateWithData(cf_data, None)
        if not source:
            return False
        image = image_io.CGImageSourceCreateImageAtIndex(source, 0, None)
        if not image:
            return False
        width = core_graphics.CGImageGetWidth(image)
        height = core_graphics.CGImageGetHeight(image)
        if (width, height) != expected_dimensions:
            return False
        bytes_per_row = core_graphics.CGImageGetBytesPerRow(image)
        decoded_limit = _MAX_ARTWORK_PIXELS * 8
        if bytes_per_row == 0 or bytes_per_row * height > decoded_limit:
            return False
        provider = core_graphics.CGImageGetDataProvider(image)
        if not provider:
            return False
        decoded = core_graphics.CGDataProviderCopyData(provider)
        if not decoded:
            return False
        decoded_length = core_foundation.CFDataGetLength(decoded)
        return 0 < decoded_length <= decoded_limit
    except (AttributeError, OverflowError, TypeError, ValueError):
        return False
    finally:
        for value in (decoded, image, source, cf_data):
            if value:
                core_foundation.CFRelease(value)


def _valid_jpeg(data: bytes) -> bool:
    if len(data) < 12 or not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
        return False

    start_of_frames = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
    offset = 2
    dimensions: tuple[int, int] | None = None
    saw_scan = False
    in_scan = False
    while offset < len(data):
        if in_scan and data[offset] != 0xFF:
            offset += 1
            continue
        if data[offset] != 0xFF:
            return False
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            return False
        marker = data[offset]
        offset += 1

        was_in_scan = in_scan
        if was_in_scan:
            if marker == 0x00 or marker in range(0xD0, 0xD8):
                continue
            in_scan = False
        elif marker == 0x00 or marker in range(0xD0, 0xD8):
            return False

        if marker == 0xD8:
            return False
        if marker == 0x01:
            in_scan = was_in_scan
            continue
        if marker == 0xD9:
            return (
                offset == len(data)
                and saw_scan
                and dimensions is not None
                and _safe_dimensions(dimensions[0], dimensions[1])
                and _imageio_decodes_jpeg(data, dimensions)
            )
        if offset + 2 > len(data):
            return False
        segment_length = int.from_bytes(data[offset : offset + 2], "big")
        if segment_length < 2 or offset + segment_length > len(data):
            return False
        if marker in start_of_frames:
            if segment_length < 11 or dimensions is not None or saw_scan:
                return False
            component_count = data[offset + 7]
            if component_count == 0 or segment_length != 8 + 3 * component_count:
                return False
            height = int.from_bytes(data[offset + 3 : offset + 5], "big")
            width = int.from_bytes(data[offset + 5 : offset + 7], "big")
            if not _safe_dimensions(width, height):
                return False
            dimensions = (width, height)
        if marker == 0xDA:
            if dimensions is None or segment_length < 8:
                return False
            component_count = data[offset + 2]
            if component_count == 0 or segment_length != 6 + 2 * component_count:
                return False
            saw_scan = True
            in_scan = True
        offset += segment_length
    return False


_PNG_DEPTHS_BY_COLOR_TYPE = {
    0: {1, 2, 4, 8, 16},
    2: {8, 16},
    3: {1, 2, 4, 8},
    4: {8, 16},
    6: {8, 16},
}
_PNG_CHANNELS_BY_COLOR_TYPE = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_PNG_FIXED_ANCILLARY_LENGTHS = {
    b"cHRM": 32,
    b"gAMA": 4,
    b"pHYs": 9,
    b"sRGB": 1,
    b"tIME": 7,
}


def _png_scanline_sizes(
    width: int,
    height: int,
    bits_per_pixel: int,
    interlace: int,
) -> list[int]:
    if interlace == 0:
        row_bytes = (width * bits_per_pixel + 7) // 8
        return [row_bytes] * height

    sizes: list[int] = []
    for x_start, y_start, x_step, y_step in (
        (0, 0, 8, 8),
        (4, 0, 8, 8),
        (0, 4, 4, 8),
        (2, 0, 4, 4),
        (0, 2, 2, 4),
        (1, 0, 2, 2),
        (0, 1, 1, 2),
    ):
        pass_width = max(0, (width - x_start + x_step - 1) // x_step)
        pass_height = max(0, (height - y_start + y_step - 1) // y_step)
        if pass_width and pass_height:
            row_bytes = (pass_width * bits_per_pixel + 7) // 8
            sizes.extend([row_bytes] * pass_height)
    return sizes


def _valid_png(data: bytes) -> bool:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    offset = 8
    dimensions: tuple[int, int] | None = None
    bit_depth = 0
    color_type = -1
    interlace = -1
    saw_iend = False
    saw_idat = False
    idat_finished = False
    saw_palette = False
    palette_entries = 0
    saw_transparency = False
    seen_ancillary: set[bytes] = set()
    idat = bytearray()
    first_chunk = True
    while offset + 12 <= len(data):
        length = int.from_bytes(data[offset : offset + 4], "big")
        chunk_type = data[offset + 4 : offset + 8]
        if len(chunk_type) != 4 or not all(
            65 <= character <= 90 or 97 <= character <= 122
            for character in chunk_type
        ):
            return False
        if chunk_type[2] & 0x20:
            return False
        chunk_end = offset + 12 + length
        if chunk_end > len(data):
            return False
        chunk_data = data[offset + 8 : offset + 8 + length]
        expected_crc = int.from_bytes(data[offset + 8 + length : chunk_end], "big")
        if zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF != expected_crc:
            return False
        if first_chunk:
            if chunk_type != b"IHDR" or length != 13:
                return False
            width = int.from_bytes(chunk_data[0:4], "big")
            height = int.from_bytes(chunk_data[4:8], "big")
            bit_depth = chunk_data[8]
            color_type = chunk_data[9]
            compression = chunk_data[10]
            filter_method = chunk_data[11]
            interlace = chunk_data[12]
            if (
                not _safe_dimensions(width, height)
                or bit_depth not in _PNG_DEPTHS_BY_COLOR_TYPE.get(color_type, set())
                or compression != 0
                or filter_method != 0
                or interlace not in {0, 1}
            ):
                return False
            dimensions = (width, height)
            first_chunk = False
        elif chunk_type == b"IHDR":
            return False
        elif chunk_type in {b"acTL", b"fcTL", b"fdAT"}:
            return False
        elif chunk_type == b"PLTE":
            if (
                saw_palette
                or saw_idat
                or color_type in {0, 4}
                or length == 0
                or length % 3 != 0
                or length > 768
                or (color_type == 3 and length // 3 > 2**bit_depth)
            ):
                return False
            saw_palette = True
            palette_entries = length // 3
        elif chunk_type == b"tRNS":
            if saw_transparency or saw_idat:
                return False
            if color_type == 0:
                valid_transparency = length == 2
            elif color_type == 2:
                valid_transparency = length == 6
            elif color_type == 3:
                valid_transparency = saw_palette and 0 < length <= palette_entries
            else:
                valid_transparency = False
            if not valid_transparency:
                return False
            saw_transparency = True
        elif chunk_type in _PNG_FIXED_ANCILLARY_LENGTHS:
            if (
                chunk_type in seen_ancillary
                or length != _PNG_FIXED_ANCILLARY_LENGTHS[chunk_type]
                or (saw_idat and chunk_type != b"tIME")
                or (chunk_type == b"gAMA" and int.from_bytes(chunk_data, "big") == 0)
                or (chunk_type == b"sRGB" and chunk_data[0] > 3)
                or (chunk_type == b"pHYs" and chunk_data[8] > 1)
            ):
                return False
            if chunk_type == b"tIME":
                year = int.from_bytes(chunk_data[:2], "big")
                month, day, hour, minute, second = chunk_data[2:]
                if not (
                    year > 0
                    and 1 <= month <= 12
                    and 1 <= day <= 31
                    and hour <= 23
                    and minute <= 59
                    and second <= 60
                ):
                    return False
            seen_ancillary.add(chunk_type)
            if saw_idat:
                idat_finished = True
        elif chunk_type == b"IDAT":
            if idat_finished or (color_type == 3 and not saw_palette):
                return False
            saw_idat = True
            idat.extend(chunk_data)
        elif chunk_type == b"IEND":
            if length != 0 or not saw_idat:
                return False
            saw_iend = True
            offset = chunk_end
            break
        else:
            # Reject unsupported ancillary chunks as well as unknown critical
            # chunks. In particular, this prevents compressed metadata from
            # expanding after the bounded image-data validation below.
            return False
        offset = chunk_end
    if not saw_iend or offset != len(data) or dimensions is None:
        return False

    channels = _PNG_CHANNELS_BY_COLOR_TYPE[color_type]
    scanline_sizes = _png_scanline_sizes(
        dimensions[0],
        dimensions[1],
        channels * bit_depth,
        interlace,
    )
    expected_bytes = sum(row_bytes + 1 for row_bytes in scanline_sizes)
    try:
        decompressor = zlib.decompressobj()
        decoded = decompressor.decompress(bytes(idat), expected_bytes + 1)
        if len(decoded) > expected_bytes or decompressor.unconsumed_tail:
            return False
    except zlib.error:
        return False
    if (
        len(decoded) != expected_bytes
        or not decompressor.eof
        or decompressor.unused_data
    ):
        return False

    cursor = 0
    for row_bytes in scanline_sizes:
        if decoded[cursor] > 4:
            return False
        cursor += row_bytes + 1
    return cursor == len(decoded)


_CONTROL_SCRIPTS = {
    "play_pause": 'const music = Application("Music"); music.playpause(); "ok";',
    "next": 'const music = Application("Music"); music.nextTrack(); "ok";',
    "previous": 'const music = Application("Music"); music.previousTrack(); "ok";',
}
_AUTOMATION_SETTINGS_URL = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation"
)


@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


def _run_jxa(script: str) -> ProcessResult:
    command = ["/usr/bin/osascript", "-l", "JavaScript", "-e", script]
    # The executable path and argument vector are fixed; no shell is involved.
    process = subprocess.Popen(  # nosec B603
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    stdout = bytearray()
    stderr = bytearray()
    selector.register(process.stdout, selectors.EVENT_READ, (stdout, _MAX_JXA_STDOUT_BYTES))
    selector.register(process.stderr, selectors.EVENT_READ, (stderr, _MAX_JXA_STDERR_BYTES))
    deadline = time.monotonic() + 5
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, 5)
            events = selector.select(remaining)
            if not events:
                raise subprocess.TimeoutExpired(command, 5)
            for key, _ in events:
                buffer, limit = key.data
                chunk = os.read(key.fd, min(65_536, limit - len(buffer) + 1))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                buffer.extend(chunk)
                if len(buffer) > limit:
                    raise subprocess.SubprocessError("osascript output exceeded its safety limit")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(command, 5)
        returncode = process.wait(timeout=remaining)
    except Exception:
        process.kill()
        process.wait(timeout=1)
        raise
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
    return ProcessResult(
        returncode,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


def _open_automation_settings() -> ProcessResult:
    completed = subprocess.run(
        ["/usr/bin/open", _AUTOMATION_SETTINGS_URL],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    return ProcessResult(completed.returncode, completed.stdout, completed.stderr)


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return 0.0
    try:
        number = float(value)
        return number if math.isfinite(number) else 0.0
    except (ValueError, OverflowError):
        return 0.0


class MusicClient:
    """Small injectable boundary around Music.app automation."""

    def __init__(
        self,
        runner: Callable[[str], ProcessResult] | None = None,
        clock: Callable[[], float] | None = None,
        settings_opener: Callable[[], ProcessResult] | None = None,
        max_artwork_bytes: int = _MAX_ARTWORK_BYTES,
    ) -> None:
        self._runner = runner or _run_jxa
        self._clock = clock or time.time
        self._settings_opener = settings_opener or _open_automation_settings
        self._max_artwork_bytes = max(0, int(max_artwork_bytes))
        self._cache_lock = threading.RLock()
        self._artwork_cache: ArtworkPayload | None = None
        self._artwork_miss_identity: str | None = None
        self._latest_sampled_identity: str | None = None

    def sample(self) -> TrackInfo:
        sampled_at = self._clock()
        try:
            result = self._runner(NOW_PLAYING_SCRIPT)
        except (OSError, subprocess.SubprocessError, TimeoutError):
            return self._remember_sample(
                TrackInfo(False, "error", sampled_at=sampled_at, error="music_unavailable")
            )

        if result.returncode != 0:
            detail = f"{result.stderr}\n{result.stdout}".casefold()
            error = (
                "automation_permission"
                if "-1743" in detail or "not authorized" in detail
                else "music_unavailable"
            )
            return self._remember_sample(
                TrackInfo(False, "error", sampled_at=sampled_at, error=error)
            )

        try:
            payload = json.loads(result.stdout.strip() or "{}")
        except (ValueError, RecursionError):
            payload = None
        if not isinstance(payload, dict):
            return self._remember_sample(
                TrackInfo(False, "error", sampled_at=sampled_at, error="invalid_music_response")
            )

        return self._remember_sample(
            TrackInfo(
                running=bool(payload.get("running")),
                state=str(payload.get("state") or "stopped"),
                title=str(payload.get("title") or ""),
                artist=str(payload.get("artist") or ""),
                album=str(payload.get("album") or ""),
                duration=max(0.0, _number(payload.get("duration"))),
                position=max(0.0, _number(payload.get("position"))),
                plain_lyrics=str(payload.get("lyrics") or "")[:1_000_000],
                sampled_at=sampled_at,
                persistent_id=str(payload.get("persistentId") or "")[:128],
                database_id=str(payload.get("databaseId") or "")[:128],
            )
        )

    def artwork_for(self, expected_identity: str) -> ArtworkPayload | None:
        with self._cache_lock:
            cached = self._artwork_cache
            cached_artwork = (
                cached
                if cached is not None and cached.identity == expected_identity
                else None
            )
            cached_miss = self._artwork_miss_identity == expected_identity
        if cached_artwork is not None or cached_miss:
            try:
                result = self._runner(ARTWORK_IDENTITY_SCRIPT)
            except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
                raise ArtworkTransientError(
                    "Music.app artwork identity check failed"
                ) from exc
            if result.returncode != 0:
                raise ArtworkTransientError("Music.app artwork identity check failed")
            try:
                current = json.loads(result.stdout.strip() or "{}")
            except (ValueError, RecursionError) as exc:
                raise ArtworkTransientError(
                    "Music.app returned an invalid artwork identity response"
                ) from exc
            if not isinstance(current, dict):
                raise ArtworkTransientError(
                    "Music.app returned an invalid artwork identity response"
                )
            current_track = TrackInfo(
                running=bool(current.get("running")),
                state=str(current.get("state") or "stopped"),
                title=str(current.get("title") or ""),
                artist=str(current.get("artist") or ""),
                album=str(current.get("album") or ""),
                duration=max(0.0, _number(current.get("duration"))),
                persistent_id=str(current.get("persistentId") or "")[:128],
                database_id=str(current.get("databaseId") or "")[:128],
            )
            if current_track.identity != expected_identity:
                with self._cache_lock:
                    if self._artwork_cache is cached_artwork:
                        self._artwork_cache = None
                    if self._artwork_miss_identity == expected_identity:
                        self._artwork_miss_identity = None
                raise ArtworkStaleIdentityError(
                    "Music.app changed tracks while validating cached artwork"
                )
            return cached_artwork
        try:
            result = self._runner(ARTWORK_SCRIPT)
        except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
            raise ArtworkTransientError("Music.app artwork extraction failed") from exc
        if result.returncode != 0:
            raise ArtworkTransientError("Music.app artwork extraction failed")
        try:
            payload = json.loads(result.stdout.strip() or "{}")
        except (ValueError, RecursionError) as exc:
            raise ArtworkTransientError(
                "Music.app returned an invalid artwork response"
            ) from exc
        if not isinstance(payload, dict):
            raise ArtworkTransientError("Music.app returned an invalid artwork response")
        track = TrackInfo(
            running=bool(payload.get("running")),
            state=str(payload.get("state") or "stopped"),
            title=str(payload.get("title") or ""),
            artist=str(payload.get("artist") or ""),
            album=str(payload.get("album") or ""),
            duration=max(0.0, _number(payload.get("duration"))),
            persistent_id=str(payload.get("persistentId") or "")[:128],
            database_id=str(payload.get("databaseId") or "")[:128],
        )
        if track.identity != expected_identity:
            raise ArtworkStaleIdentityError(
                "Music.app changed tracks before artwork extraction"
            )
        after = payload.get("after")
        if not isinstance(after, dict):
            raise ArtworkTransientError(
                "Music.app returned an incomplete artwork response"
            )
        after_track = TrackInfo(
            running=bool(after.get("running")),
            state=str(after.get("state") or "stopped"),
            title=str(after.get("title") or ""),
            artist=str(after.get("artist") or ""),
            album=str(after.get("album") or ""),
            duration=max(0.0, _number(after.get("duration"))),
            persistent_id=str(after.get("persistentId") or "")[:128],
            database_id=str(after.get("databaseId") or "")[:128],
        )
        if after_track.identity != track.identity:
            raise ArtworkStaleIdentityError(
                "Music.app changed tracks during artwork extraction"
            )
        artwork_read_error = payload.get("artworkReadError")
        if not isinstance(artwork_read_error, bool):
            raise ArtworkTransientError(
                "Music.app returned an incomplete artwork response"
            )
        if artwork_read_error:
            raise ArtworkTransientError("Music.app artwork extraction failed")
        match = _RAW_ARTWORK.fullmatch(str(payload.get("rawArtwork") or ""))
        if match is None:
            return self._remember_artwork_miss(track.identity)
        hex_data = match.group(1)
        if len(hex_data) > self._max_artwork_bytes * 2:
            return self._remember_artwork_miss(track.identity)
        try:
            data = bytes.fromhex(hex_data)
        except ValueError:
            return self._remember_artwork_miss(track.identity)
        if _valid_jpeg(data):
            mime = "image/jpeg"
        elif _valid_png(data):
            mime = "image/png"
        else:
            return self._remember_artwork_miss(track.identity)
        encoded = base64.b64encode(data).decode("ascii")
        artwork = ArtworkPayload(
            identity=track.identity,
            data_url=f"data:{mime};base64,{encoded}",
            mime=mime,
            byte_length=len(data),
        )
        with self._cache_lock:
            if self._latest_sampled_identity in (None, artwork.identity):
                self._artwork_cache = artwork
                self._artwork_miss_identity = None
        return artwork

    def clear_artwork_cache(self) -> None:
        with self._cache_lock:
            self._artwork_cache = None
            self._artwork_miss_identity = None

    def _remember_artwork_miss(self, identity: str) -> None:
        with self._cache_lock:
            if self._latest_sampled_identity in (None, identity):
                self._artwork_cache = None
                self._artwork_miss_identity = identity
        return None

    def _remember_sample(self, track: TrackInfo) -> TrackInfo:
        sampled_identity = track.identity if track.running and track.title else None
        with self._cache_lock:
            self._latest_sampled_identity = sampled_identity
            if self._artwork_cache is not None and (
                sampled_identity is None
                or self._artwork_cache.identity != sampled_identity
            ):
                self._artwork_cache = None
            if self._artwork_miss_identity is not None and (
                sampled_identity is None
                or self._artwork_miss_identity != sampled_identity
            ):
                self._artwork_miss_identity = None
        return track

    def control(self, action: str) -> None:
        try:
            script = _CONTROL_SCRIPTS[action]
        except KeyError as exc:
            raise ValueError(f"Unsupported Music action: {action}") from exc
        self._run_or_raise(script)

    def seek(self, position: float) -> None:
        if isinstance(position, bool) or not isinstance(position, (int, float)):
            raise ValueError("Seek position must be between 0 and 86400 seconds")
        try:
            value = float(position)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                "Seek position must be between 0 and 86400 seconds"
            ) from exc
        if not math.isfinite(value) or value < 0 or value > 86_400:
            raise ValueError("Seek position must be between 0 and 86400 seconds")
        script = (
            'const music = Application("Music"); '
            f"music.playerPosition = {value!r}; \"ok\";"
        )
        self._run_or_raise(script)

    def open_automation_settings(self) -> None:
        try:
            result = self._settings_opener()
        except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
            raise RuntimeError("Could not open Automation settings") from exc
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "Could not open Automation settings")

    def _run_or_raise(self, script: str) -> None:
        try:
            result = self._runner(script)
        except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
            raise RuntimeError("Music.app command failed") from exc
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "Music.app command failed")
