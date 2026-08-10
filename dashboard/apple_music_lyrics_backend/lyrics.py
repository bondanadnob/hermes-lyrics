"""Timestamped lyric parsing shared by the backend and tests."""

from __future__ import annotations

import re
from bisect import bisect_right

from .models import LyricLine

_LEADING_TIMESTAMPS = re.compile(r"^\s*((?:\[\d+:\d+(?:[.:]\d+)?\]\s*)+)(.*)$")
_TIMESTAMP = re.compile(r"\[(\d+):(\d+)(?:[.:](\d+))?\]")
_OFFSET = re.compile(r"^\s*\[offset:([+-]?\d+)\]\s*$", re.IGNORECASE)


def _fraction_seconds(value: str | None) -> float:
    if not value:
        return 0.0
    return int(value) / (10 ** len(value))


def parse_lrc(content: str | None) -> list[LyricLine]:
    """Parse common LRC variants into time-sorted lyric lines.

    Metadata is ignored, empty timestamped lines are retained as real gaps,
    multiple timestamps on one line are expanded, and a global millisecond
    ``offset`` is applied to every timestamp.
    """

    if not content:
        return []

    offset_seconds = 0.0
    for raw_line in content.splitlines():
        offset_match = _OFFSET.match(raw_line)
        if offset_match:
            offset_seconds = int(offset_match.group(1)) / 1000.0

    lines: list[LyricLine] = []
    for raw_line in content.splitlines():
        match = _LEADING_TIMESTAMPS.match(raw_line)
        if not match:
            continue
        stamps, body = match.groups()
        text = body.strip()
        for stamp in _TIMESTAMP.finditer(stamps):
            minutes = int(stamp.group(1))
            seconds = int(stamp.group(2))
            fraction = _fraction_seconds(stamp.group(3))
            timestamp = max(0.0, minutes * 60 + seconds + fraction + offset_seconds)
            lines.append(LyricLine(time=timestamp, text=text))

    lines.sort(key=lambda line: line.time)
    return lines


def find_current_line(lines: list[LyricLine], position: float) -> int:
    """Return the index active at ``position``, or ``-1`` before line one."""

    return bisect_right([line.time for line in lines], position) - 1
