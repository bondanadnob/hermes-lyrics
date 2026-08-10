"""Read and control Music.app through a single, JSON-producing JXA call."""

from __future__ import annotations

import json
import math
import subprocess
import time
from dataclasses import dataclass
from typing import Callable

from .models import TrackInfo

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
    try { lyrics = String(track.lyrics() || ""); } catch (_) {}
    JSON.stringify({
      running: true,
      state: state,
      title: String(track.name() || ""),
      artist: String(track.artist() || ""),
      album: String(track.album() || ""),
      duration: Number(track.duration() || 0),
      position: Number(music.playerPosition() || 0),
      lyrics: lyrics
    });
  }
}
'''.strip()


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
    completed = subprocess.run(
        ["/usr/bin/osascript", "-l", "JavaScript", "-e", script],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    return ProcessResult(completed.returncode, completed.stdout, completed.stderr)


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
    try:
        number = float(value)
        return number if math.isfinite(number) else 0.0
    except (TypeError, ValueError):
        return 0.0


class MusicClient:
    """Small injectable boundary around Music.app automation."""

    def __init__(
        self,
        runner: Callable[[str], ProcessResult] | None = None,
        clock: Callable[[], float] | None = None,
        settings_opener: Callable[[], ProcessResult] | None = None,
    ) -> None:
        self._runner = runner or _run_jxa
        self._clock = clock or time.time
        self._settings_opener = settings_opener or _open_automation_settings

    def sample(self) -> TrackInfo:
        sampled_at = self._clock()
        try:
            result = self._runner(NOW_PLAYING_SCRIPT)
        except (OSError, subprocess.SubprocessError, TimeoutError):
            return TrackInfo(False, "error", sampled_at=sampled_at, error="music_unavailable")

        if result.returncode != 0:
            detail = f"{result.stderr}\n{result.stdout}".casefold()
            error = (
                "automation_permission"
                if "-1743" in detail or "not authorized" in detail
                else "music_unavailable"
            )
            return TrackInfo(False, "error", sampled_at=sampled_at, error=error)

        try:
            payload = json.loads(result.stdout.strip() or "{}")
        except json.JSONDecodeError:
            return TrackInfo(False, "error", sampled_at=sampled_at, error="invalid_music_response")

        return TrackInfo(
            running=bool(payload.get("running")),
            state=str(payload.get("state") or "stopped"),
            title=str(payload.get("title") or ""),
            artist=str(payload.get("artist") or ""),
            album=str(payload.get("album") or ""),
            duration=max(0.0, _number(payload.get("duration"))),
            position=max(0.0, _number(payload.get("position"))),
            plain_lyrics=str(payload.get("lyrics") or "")[:1_000_000],
            sampled_at=sampled_at,
        )

    def control(self, action: str) -> None:
        try:
            script = _CONTROL_SCRIPTS[action]
        except KeyError as exc:
            raise ValueError(f"Unsupported Music action: {action}") from exc
        self._run_or_raise(script)

    def seek(self, position: float) -> None:
        value = _number(position)
        if value < 0 or value > 86_400 or value != float(position):
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
