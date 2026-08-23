"""Isolated microphone capture and Shazam signature worker."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import selectors
import signal
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_MAX_AUDIO_BYTES = 400_000
_MAX_PROVIDER_REQUEST_BYTES = 262_144
_MAX_PROVIDER_RESPONSE_BYTES = 131_072
_PROVIDER_TIMEOUT_SECONDS = 10.0
_CAPTURE_TERM_TIMEOUT_SECONDS = 0.5
_CAPTURE_KILL_TIMEOUT_SECONDS = 0.5
_PROVIDER_QUERY = (
    "sync=true&webv3=true&sampling=true&connected=&shazamapiversion=v3"
    "&sharehub=true&hubv5minorversion=v5.1&hidelb=true&video=v3"
)
_PROVIDER_DEVICES = frozenset({"iphone", "android", "web"})
_FFMPEG_ENTRY_POINTS = {
    Path("/opt/homebrew/bin/ffmpeg"),
    Path("/usr/local/bin/ffmpeg"),
    Path("/opt/local/bin/ffmpeg"),
    Path("/usr/bin/ffmpeg"),
}


def _trusted_ffmpeg_executable(candidate: Path) -> Path | None:
    try:
        resolved = candidate.resolve(strict=True)
        metadata = resolved.stat()
    except OSError:
        return None
    location_allowed = resolved in _FFMPEG_ENTRY_POINTS
    if not location_allowed:
        for cellar in (
            Path("/opt/homebrew/Cellar/ffmpeg"),
            Path("/usr/local/Cellar/ffmpeg"),
        ):
            try:
                relative = resolved.relative_to(cellar)
            except ValueError:
                continue
            if len(relative.parts) == 3 and relative.parts[-2:] == ("bin", "ffmpeg"):
                location_allowed = True
                break
    if (
        not location_allowed
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid not in {0, os.getuid()}
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or not os.access(resolved, os.X_OK)
    ):
        return None
    return resolved


def _bounded_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    bounded = value[:limit]
    text = "".join(
        character for character in bounded if character >= " " or character == "\t"
    )
    return text.strip()


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def build_capture_command(ffmpeg_executable: Path, duration: float) -> list[str]:
    """Build a fixed, file-free AVFoundation capture command."""
    return [
        str(ffmpeg_executable),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "avfoundation",
        "-i",
        ":default",
        "-t",
        str(duration),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "libvorbis",
        "-q:a",
        "4",
        "-f",
        "ogg",
        "pipe:1",
    ]


def capture_audio(
    ffmpeg_executable: Path,
    duration: float,
    *,
    process_factory: Any = subprocess.Popen,
) -> bytes:
    try:
        process = process_factory(
            build_capture_command(ffmpeg_executable, duration),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            start_new_session=True,
        )
    except OSError as exc:
        raise RuntimeError("microphone capture failed") from exc

    if process.stdout is None:
        _terminate_capture_process_group(process)
        raise RuntimeError("microphone capture failed")

    previous_handlers: dict[int, Any] = {}

    def forward_termination(signum, _frame) -> None:
        _terminate_capture_process_group(process)
        raise SystemExit(128 + signum)

    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            previous_handlers[signum] = signal.signal(signum, forward_termination)
        except ValueError:
            pass

    selector = selectors.DefaultSelector()
    audio = bytearray()
    deadline = time.monotonic() + duration + 4.0
    try:
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_capture_process_group(process)
                raise RuntimeError("microphone capture failed")
            if not selector.select(remaining):
                _terminate_capture_process_group(process)
                raise RuntimeError("microphone capture failed")
            chunk = os.read(
                process.stdout.fileno(),
                min(65_536, _MAX_AUDIO_BYTES + 1 - len(audio)),
            )
            if not chunk:
                break
            audio.extend(chunk)
            if len(audio) > _MAX_AUDIO_BYTES:
                _terminate_capture_process_group(process)
                raise RuntimeError("audio sample exceeds the in-memory size limit")
        try:
            returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            _terminate_capture_process_group(process)
            raise RuntimeError("microphone capture failed") from exc
        if returncode != 0:
            raise RuntimeError("microphone capture failed")
        return bytes(audio)
    finally:
        selector.close()
        process.stdout.close()
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)


def _capture_process_group_exists(process_group: int | None) -> bool:
    if process_group is None:
        return False
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _capture_process_returncode(process) -> int | None:
    returncode = process.poll()
    return returncode if isinstance(returncode, int) else None


def _wait_for_capture_process_barrier(
    process,
    process_group: int | None,
    timeout: float,
) -> bool:
    deadline = time.monotonic() + timeout
    leader_exited = _capture_process_returncode(process) is not None
    if not leader_exited:
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
            leader_exited = True
        except subprocess.TimeoutExpired:
            leader_exited = False
    while _capture_process_group_exists(process_group):
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            return False
        time.sleep(min(0.01, remaining))
    return leader_exited


def _signal_capture_process_group(process, process_group: int | None, signum) -> None:
    try:
        if process_group is not None:
            os.killpg(process_group, signum)
        elif signum == signal.SIGTERM:
            process.terminate()
        else:
            process.kill()
    except OSError:
        pass


def _terminate_capture_process_group(process) -> None:
    process_id = getattr(process, "pid", None)
    process_group = process_id if isinstance(process_id, int) and process_id > 0 else None
    if (
        _capture_process_returncode(process) is not None
        and not _capture_process_group_exists(process_group)
    ):
        process.wait(timeout=0)
        return

    _signal_capture_process_group(process, process_group, signal.SIGTERM)
    if _wait_for_capture_process_barrier(
        process,
        process_group,
        _CAPTURE_TERM_TIMEOUT_SECONDS,
    ):
        return

    _signal_capture_process_group(process, process_group, signal.SIGKILL)
    if not _wait_for_capture_process_barrier(
        process,
        process_group,
        _CAPTURE_KILL_TIMEOUT_SECONDS,
    ):
        raise RuntimeError("microphone capture could not be stopped")


def sanitize_response(raw: object) -> dict[str, object]:
    """Project a Shazam response onto the fields needed by the parent."""
    if not isinstance(raw, dict):
        return {"matches": [], "track": {}}

    matches = []
    raw_matches = raw.get("matches")
    if isinstance(raw_matches, list):
        for item in raw_matches[:16]:
            if isinstance(item, dict):
                projected_match: dict[str, object] = {}
                identifier = _bounded_text(item.get("id"), 128)
                if identifier:
                    projected_match["id"] = identifier
                for key in ("offset", "timeskew"):
                    number = _finite_number(item.get(key))
                    if number is not None:
                        projected_match[key] = number
                matches.append(projected_match)

    projected_track: dict[str, object] = {}
    raw_track = raw.get("track")
    if isinstance(raw_track, dict):
        for key, limit in (("key", 128), ("title", 512), ("subtitle", 512)):
            value = _bounded_text(raw_track.get(key), limit)
            if value:
                projected_track[key] = value
        images = raw_track.get("images")
        if isinstance(images, dict):
            coverart = _bounded_text(images.get("coverart"), 2_048)
            if coverart:
                projected_track["images"] = {"coverart": coverart}
        sections = raw_track.get("sections")
        if isinstance(sections, list):
            for section in sections[:32]:
                if not isinstance(section, dict):
                    continue
                metadata = section.get("metadata")
                if not isinstance(metadata, list):
                    continue
                album = next(
                    (
                        field
                        for field in metadata[:64]
                        if isinstance(field, dict) and field.get("title") == "Album"
                    ),
                    None,
                )
                if album is not None:
                    album_title = _bounded_text(album.get("title"), 64)
                    album_text = _bounded_text(album.get("text"), 512)
                    projected_track["sections"] = [
                        {
                            "metadata": [
                                {
                                    "title": album_title,
                                    "text": album_text,
                                }
                            ]
                        }
                    ]
                    break

    return {"matches": matches, "track": projected_track}


class BoundedShazamHTTPClient:
    """Read one recognition response through a strict streaming budget."""

    def __init__(
        self,
        *,
        session_factory=None,
        max_response_bytes: int = _MAX_PROVIDER_RESPONSE_BYTES,
    ) -> None:
        if max_response_bytes < 1:
            raise ValueError("response budget must be positive")
        self._session_factory = session_factory
        self._max_response_bytes = max_response_bytes

    async def request(self, method: str, url: str, *args, **kwargs) -> dict[str, object]:
        if method.upper() != "POST" or args:
            raise RuntimeError("recognition request rejected")
        parsed = urlsplit(url)
        try:
            port = parsed.port
        except ValueError as exc:
            raise RuntimeError("recognition request rejected") from exc
        if (
            parsed.scheme != "https"
            or parsed.netloc not in {"amp.shazam.com", "amp.shazam.com:443"}
            or port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or parsed.query != _PROVIDER_QUERY
        ):
            raise RuntimeError("recognition request rejected")

        path_parts = parsed.path.split("/")
        if (
            len(path_parts) != 10
            or path_parts[:5] != ["", "discovery", "v5", "en-US", "GB"]
            or path_parts[5] not in _PROVIDER_DEVICES
            or path_parts[6:8] != ["-", "tag"]
        ):
            raise RuntimeError("recognition request rejected")
        dependency_identifiers = path_parts[8:10]
        try:
            parsed_identifiers = [
                uuid.UUID(identifier) for identifier in dependency_identifiers
            ]
        except ValueError as exc:
            raise RuntimeError("recognition request rejected") from exc
        if any(
            identifier.version != 4 or identifier.variant != uuid.RFC_4122
            for identifier in parsed_identifiers
        ):
            raise RuntimeError("recognition request rejected")
        canonical_identifiers = [str(identifier) for identifier in parsed_identifiers]
        request_url = (
            "https://amp.shazam.com/discovery/v5/en-US/GB/"
            f"{path_parts[5]}/-/tag/"
            f"{canonical_identifiers[0]}/{canonical_identifiers[1]}"
            f"?{_PROVIDER_QUERY}"
        )
        request_payload = kwargs.get("json")
        if not isinstance(request_payload, dict):
            raise RuntimeError("recognition request rejected")
        signature = request_payload.get("signature")
        if not isinstance(signature, dict):
            raise RuntimeError("recognition request rejected")
        signature_uri = signature.get("uri")
        if not isinstance(signature_uri, str):
            raise RuntimeError("recognition request rejected")
        if len(signature_uri) > _MAX_PROVIDER_REQUEST_BYTES:
            raise RuntimeError("recognition request exceeds the size limit")
        try:
            signature_uri.encode("ascii")
        except UnicodeEncodeError as exc:
            raise RuntimeError("recognition request rejected") from exc

        timezone = _bounded_text(request_payload.get("timezone"), 64)
        sample_ms = signature.get("samplems")
        timestamp = request_payload.get("timestamp")
        if (
            not timezone
            or isinstance(sample_ms, bool)
            or not isinstance(sample_ms, int)
            or not 0 <= sample_ms <= 20_000
            or isinstance(timestamp, bool)
            or not isinstance(timestamp, int)
            or not 0 <= timestamp <= 10**16
        ):
            raise RuntimeError("recognition request rejected")

        projected_request = {
            "timezone": timezone,
            "signature": {"uri": signature_uri, "samplems": sample_ms},
            "timestamp": timestamp,
            "context": {},
            "geolocation": {},
        }
        request_body = json.dumps(
            projected_request,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
        if len(request_body) > _MAX_PROVIDER_REQUEST_BYTES:
            raise RuntimeError("recognition request exceeds the size limit")

        if set(kwargs) - {"headers", "json", "proxy"}:
            raise RuntimeError("recognition request rejected")
        if kwargs.get("proxy") is not None:
            raise RuntimeError("recognition request rejected")
        raw_headers = kwargs.get("headers")
        if not isinstance(raw_headers, dict):
            raw_headers = {}
        headers = {
            name: text
            for name, limit in (
                ("Accept-Language", 64),
                ("X-Shazam-Platform", 32),
                ("X-Shazam-AppVersion", 32),
                ("User-Agent", 256),
            )
            if (text := _bounded_text(raw_headers.get(name), limit))
        }
        headers.update(
            {
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Content-Type": "application/json",
            }
        )
        session_factory = self._session_factory
        timeout: object = _PROVIDER_TIMEOUT_SECONDS
        if session_factory is None:
            from aiohttp import ClientSession, ClientTimeout

            session_factory = ClientSession
            timeout = ClientTimeout(total=_PROVIDER_TIMEOUT_SECONDS)

        async with session_factory(
            timeout=timeout,
            trust_env=False,
            auto_decompress=False,
        ) as session:
            async with session.post(
                request_url,
                allow_redirects=False,
                data=request_body,
                headers=headers,
            ) as response:
                if response.status != 200:
                    raise RuntimeError("recognition request failed")
                raw_length = response.headers.get("Content-Length")
                if raw_length is not None:
                    if (
                        not isinstance(raw_length, str)
                        or len(raw_length) > 20
                        or not raw_length.isdecimal()
                    ):
                        raise RuntimeError("recognition response is invalid")
                    if int(raw_length) > self._max_response_bytes:
                        raise RuntimeError("recognition response exceeds the size limit")
                body = bytearray()
                async for chunk in response.content.iter_chunked(16_384):
                    if not isinstance(chunk, bytes):
                        raise RuntimeError("recognition response is invalid")
                    if len(body) + len(chunk) > self._max_response_bytes:
                        raise RuntimeError("recognition response exceeds the size limit")
                    body.extend(chunk)

        try:
            payload = json.loads(body)
        except (RecursionError, UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError("recognition response is invalid") from exc
        return sanitize_response(payload)


async def recognize_audio(audio: bytes, *, shazam_factory=None) -> dict[str, object]:
    if shazam_factory is None:
        from shazamio import Shazam

        shazam_factory = Shazam
    http_client: Any = BoundedShazamHTTPClient()
    raw = await shazam_factory(http_client=http_client).recognize(audio)
    return sanitize_response(raw)


def main(
    argv: list[str] | None = None,
    *,
    capture=capture_audio,
    recognize=recognize_audio,
    output=sys.stdout,
) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ffmpeg", required=True, type=Path)
    parser.add_argument("--duration", required=True, type=float)
    options = parser.parse_args(argv)

    ffmpeg_executable = _trusted_ffmpeg_executable(options.ffmpeg)
    if ffmpeg_executable is None:
        raise RuntimeError("ffmpeg executable is not trusted")
    capture_started_at = time.time()
    audio = capture(ffmpeg_executable, options.duration)
    try:
        result = asyncio.run(recognize(audio))
    finally:
        audio = b""
    result["captureStartedAt"] = capture_started_at
    json.dump(result, output, separators=(",", ":"), ensure_ascii=False)
    output.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
