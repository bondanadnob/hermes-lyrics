"""Privacy-bounded ambient music recognition for the lyrics plugin."""

from __future__ import annotations

import json
import math
import os
import signal
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import TrackInfo

_MAX_TEXT = 512
_MAX_IDENTIFIER = 128
_MAX_URL = 2048
_MAX_WORKER_OUTPUT_BYTES = 16_384
_MAX_TRACK_SECONDS = 86_400.0
_MAX_RESULT_LATENCY = 30.0
_PROCESS_TERM_TIMEOUT = 1.0
_PROCESS_KILL_TIMEOUT = 1.0
_SOURCE_STOP_TIMEOUT = 3.0
_WORKER_TIMEOUT_SECONDS = 25.0
_FFMPEG_CANDIDATES = (
    Path("/opt/homebrew/bin/ffmpeg"),
    Path("/usr/local/bin/ffmpeg"),
    Path("/opt/local/bin/ffmpeg"),
    Path("/usr/bin/ffmpeg"),
)
_FFMPEG_UNAVAILABLE = Path("/nonexistent/hermes-ffmpeg")


@dataclass(frozen=True)
class AmbientRuntimePaths:
    python_executable: Path
    worker_script: Path
    ffmpeg_executable: Path


def _is_trusted_ffmpeg_location(path: Path) -> bool:
    if path in _FFMPEG_CANDIDATES:
        return True
    for cellar in (
        Path("/opt/homebrew/Cellar/ffmpeg"),
        Path("/usr/local/Cellar/ffmpeg"),
    ):
        try:
            relative = path.relative_to(cellar)
        except ValueError:
            continue
        if len(relative.parts) == 3 and relative.parts[-2:] == ("bin", "ffmpeg"):
            return True
    return False


def _is_trusted_ffmpeg_stat(metadata: os.stat_result) -> bool:
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid in {0, os.getuid()}
        and metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH) == 0
    )


def discover_ffmpeg_executable(
    candidates: tuple[Path, ...] = _FFMPEG_CANDIDATES,
) -> Path:
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
            metadata = resolved.stat()
        except OSError:
            continue
        if (
            _is_trusted_ffmpeg_location(resolved)
            and _is_trusted_ffmpeg_stat(metadata)
            and os.access(resolved, os.X_OK)
        ):
            return resolved
    return _FFMPEG_UNAVAILABLE


def build_runtime_paths(
    *,
    plugin_root: Path,
    hermes_home: Path,
    ffmpeg_executable: Path,
) -> AmbientRuntimePaths:
    return AmbientRuntimePaths(
        python_executable=(
            hermes_home
            / "plugin-data"
            / "apple-music-lyrics"
            / "recognition-venv"
            / "bin"
            / "python"
        ),
        worker_script=(
            plugin_root
            / "dashboard"
            / "apple_music_lyrics_backend"
            / "ambient_worker.py"
        ),
        ffmpeg_executable=ffmpeg_executable,
    )


def _bounded_text(value: object, limit: int = _MAX_TEXT) -> str:
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


def _album_from_sections(value: object) -> str:
    if not isinstance(value, list):
        return ""
    for section in value[:32]:
        if not isinstance(section, dict):
            continue
        metadata = section.get("metadata")
        if not isinstance(metadata, list):
            continue
        for field in metadata[:64]:
            if not isinstance(field, dict):
                continue
            if _bounded_text(field.get("title"), 64).casefold() == "album":
                return _bounded_text(field.get("text"))
    return ""


def parse_shazam_match(
    payload: object,
    *,
    captured_at: float,
    received_at: float,
) -> TrackInfo | None:
    """Convert an untrusted Shazam response into a bounded ambient track."""
    if not isinstance(payload, dict):
        return None
    captured = _finite_number(captured_at)
    received = _finite_number(received_at)
    if captured is None or received is None:
        return None

    raw_matches = payload.get("matches")
    raw_track = payload.get("track")
    if not isinstance(raw_matches, list) or not isinstance(raw_track, dict):
        return None

    match: dict[str, Any] | None = None
    offset: float | None = None
    for candidate in raw_matches[:16]:
        if not isinstance(candidate, dict):
            continue
        candidate_offset = _finite_number(candidate.get("offset"))
        if (
            candidate_offset is not None
            and 0.0 <= candidate_offset <= _MAX_TRACK_SECONDS
        ):
            match = candidate
            offset = candidate_offset
            break
    if match is None or offset is None:
        return None

    title = _bounded_text(raw_track.get("title"))
    artist = _bounded_text(raw_track.get("subtitle"))
    if not title or not artist:
        return None

    track_identifier = _bounded_text(raw_track.get("key"), _MAX_IDENTIFIER)
    if not track_identifier:
        track_identifier = _bounded_text(match.get("id"), _MAX_IDENTIFIER)
    if not track_identifier:
        return None

    images = raw_track.get("images")
    artwork_url = (
        _bounded_text(images.get("coverart"), _MAX_URL)
        if isinstance(images, dict)
        else ""
    )
    elapsed = min(_MAX_RESULT_LATENCY, max(0.0, received - captured))

    return TrackInfo(
        running=True,
        state="playing",
        title=title,
        artist=artist,
        album=_album_from_sections(raw_track.get("sections")),
        position=min(_MAX_TRACK_SECONDS, offset + elapsed),
        sampled_at=received,
        persistent_id=f"shazam:{track_identifier}",
        source="ambient",
        can_control=False,
        can_seek=False,
        artwork_url=artwork_url or None,
    )


def _worker_environment(ffmpeg_executable: Path) -> dict[str, str]:
    environment = {
        "PATH": f"{ffmpeg_executable.parent}:/usr/bin:/bin",
    }
    for name in ("HOME", "TMPDIR", "LANG"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


def _process_group_id(process: Any) -> int | None:
    process_id = getattr(process, "pid", None)
    return process_id if isinstance(process_id, int) and process_id > 0 else None


def _signal_process(process: Any, signal_number: signal.Signals) -> None:
    try:
        process_group_id = _process_group_id(process)
        if process_group_id is not None:
            os.killpg(process_group_id, signal_number)
        elif signal_number == signal.SIGTERM:
            process.terminate()
        else:
            process.kill()
    except OSError:
        pass


def _process_group_exists(process_group_id: int | None) -> bool:
    if process_group_id is None:
        return False
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _process_returncode(process: Any) -> int | None:
    poll = getattr(process, "poll", None)
    if callable(poll):
        returncode = poll()
        return returncode if isinstance(returncode, int) else None
    returncode = getattr(process, "returncode", None)
    return returncode if isinstance(returncode, int) else None


def _wait_for_process_barrier(
    process: Any,
    process_group_id: int | None,
    timeout: float,
) -> bool:
    deadline = time.monotonic() + timeout
    leader_exited = _process_returncode(process) is not None
    if not leader_exited:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining)
            leader_exited = True
        except subprocess.TimeoutExpired:
            leader_exited = False

    while _process_group_exists(process_group_id):
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            return False
        time.sleep(min(0.01, remaining))
    return leader_exited


def _terminate_process(process: Any) -> None:
    process_group_id = _process_group_id(process)
    if (
        _process_returncode(process) is not None
        and not _process_group_exists(process_group_id)
    ):
        wait = getattr(process, "wait", None)
        if callable(wait):
            wait(timeout=0)
        return

    _signal_process(process, signal.SIGTERM)
    if _wait_for_process_barrier(
        process,
        process_group_id,
        _PROCESS_TERM_TIMEOUT,
    ):
        return

    _signal_process(process, signal.SIGKILL)
    if not _wait_for_process_barrier(
        process,
        process_group_id,
        _PROCESS_KILL_TIMEOUT,
    ):
        raise RuntimeError("recognition worker process group did not terminate")


def _read_bounded_worker_output(process: Any, timeout: float) -> bytes:
    stream = getattr(process, "stdout", None)
    read = getattr(stream, "read", None)
    if stream is None or not callable(read):
        raise RuntimeError("recognition worker output stream is unavailable")

    result: dict[str, object] = {}

    def read_once() -> None:
        try:
            result["stdout"] = read(_MAX_WORKER_OUTPUT_BYTES + 1)
        except (OSError, ValueError) as exc:
            result["error"] = exc

    reader = threading.Thread(target=read_once, daemon=True)
    reader.start()
    try:
        reader.join(timeout)
        if reader.is_alive():
            _terminate_process(process)
            reader.join(_PROCESS_KILL_TIMEOUT)
            if reader.is_alive():
                raise RuntimeError("recognition worker output reader did not stop")
            raise RuntimeError("recognition worker timed out")

        stdout = result.get("stdout")
        if result.get("error") is not None or not isinstance(stdout, bytes):
            _terminate_process(process)
            raise RuntimeError("recognition worker failed")
        if len(stdout) > _MAX_WORKER_OUTPUT_BYTES:
            _terminate_process(process)
            raise RuntimeError("worker output exceeds the size limit")

        _terminate_process(process)
        return stdout
    finally:
        if not reader.is_alive():
            try:
                stream.close()
            except (OSError, ValueError):
                pass


class AmbientWorkerClient:
    """Invoke the isolated recognition worker through a fixed argv."""

    def __init__(
        self,
        *,
        python_executable: Path,
        worker_script: Path,
        ffmpeg_executable: Path,
        process_factory: Any = subprocess.Popen,
        clock=time.time,
    ) -> None:
        self.python_executable = python_executable
        self.worker_script = worker_script
        self.ffmpeg_executable = ffmpeg_executable
        self._process_factory = process_factory
        self._clock = clock
        self._process_lock = threading.Lock()
        self._process: Any | None = None

    def recognize(self, cancel_event: threading.Event | None = None) -> TrackInfo | None:
        worker_started_at = self._clock()
        with self._process_lock:
            if cancel_event is not None and cancel_event.is_set():
                return None
            if self._process is not None:
                raise RuntimeError("recognition worker already active")
            process = self._process_factory(
                [
                    str(self.python_executable),
                    "-I",
                    str(self.worker_script),
                    "--ffmpeg",
                    str(self.ffmpeg_executable),
                    "--duration",
                    "8.0",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                shell=False,
                start_new_session=True,
                env=_worker_environment(self.ffmpeg_executable),
            )
            self._process = process
        try:
            stdout = _read_bounded_worker_output(
                process,
                _WORKER_TIMEOUT_SECONDS,
            )
            received_at = self._clock()
            if process.returncode != 0:
                raise RuntimeError("recognition worker failed")
            payload = json.loads(stdout.decode("utf-8"))
            capture_started_at = _finite_number(payload.get("captureStartedAt"))
            return parse_shazam_match(
                payload,
                captured_at=(
                    capture_started_at
                    if (
                        capture_started_at is not None
                        and worker_started_at <= capture_started_at <= received_at
                    )
                    else worker_started_at
                ),
                received_at=received_at,
            )
        finally:
            with self._process_lock:
                if self._process is process:
                    self._process = None

    def cancel(self) -> None:
        with self._process_lock:
            process = self._process
        if process is not None:
            _terminate_process(process)


class AmbientSource:
    """Manual-only ambient playback source."""

    def __init__(self, recognizer, clock=time.time) -> None:
        self._recognizer = recognizer
        self._clock = clock
        self._lock = threading.Lock()
        self._state = "ambient_idle"
        self._track: TrackInfo | None = None
        self._generation = 0
        self._cancel_event: threading.Event | None = None
        self._thread: threading.Thread | None = None

    def listen(self) -> None:
        with self._lock:
            if self._state == "listening" or self._thread is not None:
                return
            self._state = "listening"
            self._track = None
            self._generation += 1
            generation = self._generation
            cancel_event = threading.Event()
            self._cancel_event = cancel_event
            thread = threading.Thread(
                target=lambda: self._recognize(generation, cancel_event),
                daemon=True,
            )
            self._thread = thread
            try:
                thread.start()
            except Exception as exc:
                cancel_event.set()
                self._generation += 1
                if self._thread is thread:
                    self._thread = None
                    self._cancel_event = None
                    self._state = "ambient_idle"
                    self._track = None
                raise RuntimeError("recognition operation could not start") from exc

    def stop(self) -> None:
        with self._lock:
            self._generation += 1
            cancel_event = self._cancel_event
            thread = self._thread
            if cancel_event is not None:
                cancel_event.set()
            self._track = None
        self._recognizer.cancel()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=_SOURCE_STOP_TIMEOUT)
            if thread.is_alive():
                raise RuntimeError("recognition operation did not stop")
        with self._lock:
            if self._thread is thread:
                self._thread = None
                self._cancel_event = None
            self._state = "ambient_idle"
            self._track = None

    def _recognize(self, generation: int, cancel_event: threading.Event) -> None:
        try:
            try:
                track = self._recognizer.recognize(cancel_event)
            except Exception:
                with self._lock:
                    if generation == self._generation:
                        self._track = None
                        self._state = "recognition_error"
                return
            with self._lock:
                if generation != self._generation:
                    return
                self._track = track
                self._state = track.state if track is not None else "no_match"
        finally:
            with self._lock:
                if self._cancel_event is cancel_event:
                    self._thread = None
                    self._cancel_event = None

    def sample(self) -> TrackInfo:
        with self._lock:
            state = self._state
            track = self._track
        if track is not None:
            return track
        return TrackInfo(
            running=state == "listening",
            state=state,
            sampled_at=self._clock(),
            source="ambient",
            can_control=False,
            can_seek=False,
            error="ambient_recognition" if state == "recognition_error" else None,
        )


class PlaybackRouter:
    """Route playback reads without coupling Music.app to ambient recognition."""

    def __init__(self, music_source, ambient_source) -> None:
        self._music_source = music_source
        self._ambient_source = ambient_source
        self._source = "music_app"
        self._lock = threading.RLock()

    def select_source(self, source: str) -> None:
        if source not in {"music_app", "ambient"}:
            raise ValueError("source must be music_app or ambient")
        with self._lock:
            if source == "music_app":
                self._ambient_source.stop()
            self._source = source

    def sample(self) -> TrackInfo:
        with self._lock:
            if self._source == "ambient":
                return self._ambient_source.sample()
            return self._music_source.sample()

    def listen_ambient(self) -> None:
        with self._lock:
            if self._source != "ambient":
                raise ValueError("select Nearby before listening")
            self._ambient_source.listen()

    def stop_ambient(self) -> None:
        with self._lock:
            self._ambient_source.stop()

    def shutdown(self) -> None:
        with self._lock:
            self._ambient_source.stop()

    def control(self, action: str) -> None:
        with self._lock:
            if self._source == "ambient":
                raise ValueError("ambient audio cannot be controlled")
            self._music_source.control(action)

    def seek(self, position: float) -> None:
        with self._lock:
            if self._source == "ambient":
                raise ValueError("ambient audio cannot be sought")
            self._music_source.seek(position)

    def artwork_for(self, expected_identity: str):
        return self._music_source.artwork_for(expected_identity)

    def clear_artwork_cache(self) -> None:
        self._music_source.clear_artwork_cache()

    def open_automation_settings(self) -> None:
        self._music_source.open_automation_settings()


def build_playback_router(
    music_source,
    paths: AmbientRuntimePaths,
    *,
    process_factory: Any = subprocess.Popen,
    clock=time.time,
) -> PlaybackRouter:
    recognizer = AmbientWorkerClient(
        python_executable=paths.python_executable,
        worker_script=paths.worker_script,
        ffmpeg_executable=paths.ffmpeg_executable,
        process_factory=process_factory,
        clock=clock,
    )
    return PlaybackRouter(
        music_source,
        AmbientSource(recognizer, clock=clock),
    )
