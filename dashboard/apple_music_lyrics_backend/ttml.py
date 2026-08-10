"""Secure, dependency-free parser for Apple Music-style TTML lyrics."""

from __future__ import annotations

import math
import re

# ElementTree is safe here because TTML size is capped and DTD/entities are rejected.
import xml.etree.ElementTree as ET  # nosec B405

from .models import LyricLine, LyricsDocument, LyricWord

_DANGEROUS_XML = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)
_WHITESPACE = re.compile(r"\s+")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":")[-1]


def parse_time(value: str | None) -> float | None:
    if value is None:
        return None
    raw = value.strip()
    try:
        if raw.endswith("ms"):
            result = float(raw[:-2]) / 1000.0
        elif raw.endswith("s"):
            result = float(raw[:-1])
        else:
            parts = raw.split(":")
            if not 1 <= len(parts) <= 3:
                return None
            numbers = [float(part) for part in parts]
            result = sum(
                number * (60**offset) for offset, number in enumerate(reversed(numbers))
            )
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _normalized(text: str) -> str:
    return _WHITESPACE.sub(" ", text)


def _element_text(element: ET.Element) -> str:
    return _normalized("".join(element.itertext()))


def parse_ttml(content: str) -> LyricsDocument:
    """Parse line- and word-timed TTML without resolving external entities."""

    if not content or _DANGEROUS_XML.search(content):
        raise ValueError("Unsafe or empty TTML document")
    try:
        # Safe here because content size is capped and DTD/entities are rejected above.
        root = ET.fromstring(content)  # nosec B314
    except ET.ParseError as exc:
        raise ValueError(f"Invalid TTML document: {exc}") from exc

    timed_lines: list[LyricLine] = []
    plain_lines: list[str] = []
    has_timed_spans = False
    for paragraph in (element for element in root.iter() if _local_name(element.tag) == "p"):
        line_start = parse_time(paragraph.attrib.get("begin"))
        line_end = parse_time(paragraph.attrib.get("end"))
        words: list[LyricWord] = []

        for span in (element for element in paragraph.iter() if _local_name(element.tag) == "span"):
            start = parse_time(span.attrib.get("begin"))
            if start is None:
                continue
            end = parse_time(span.attrib.get("end"))
            if end is None:
                end = line_end if line_end is not None else start + 0.1
            end = max(end, start + 0.01)
            text = _element_text(span)
            if not text.strip():
                continue
            words.append(LyricWord(text=text, start=start, end=end))

        if words:
            words[0] = LyricWord(words[0].text.lstrip(), words[0].start, words[0].end)
            words[-1] = LyricWord(words[-1].text.rstrip(), words[-1].start, words[-1].end)
            words = [word for word in words if word.text]

        if words:
            text = "".join(word.text for word in words)
            start = line_start if line_start is not None else words[0].start
            has_timed_spans = True
        else:
            text = _element_text(paragraph).strip()
            if not text:
                continue
            start = line_start

        plain_lines.append(text)
        if start is None:
            continue
        safe_end = max(line_end, start + 0.01) if line_end is not None else None
        timed_lines.append(
            LyricLine(
                time=max(0.0, start),
                text=text,
                end=safe_end,
                words=tuple(words),
            )
        )

    timed_lines.sort(key=lambda line: line.time)
    if timed_lines:
        lines = timed_lines
        synced = True
    else:
        lines = [LyricLine(float(index), text) for index, text in enumerate(plain_lines)]
        synced = False
    return LyricsDocument(
        lines=tuple(lines),
        plain_text="\n".join(plain_lines),
        source="Apple Music",
        synced=synced,
        word_timing="exact" if has_timed_spans else "none",
    )
