"""Helpers for structured PROFILE.md / INSIGHTS.md bullet metadata."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from datetime import date

_BULLET_RE = re.compile(r"^\s*[-*]\s+")
_LEGACY_VERIFY_RE = re.compile(r"\(verify(?: [^)]+)?\)", re.IGNORECASE)
_METADATA_COMMENT_RE = re.compile(r"<!--\s*nanobot-meta:\s*(?P<body>.*?)\s*-->")
_CONFIDENCE_VALUES = {"low", "medium", "high"}


@dataclass(slots=True, frozen=True)
class MemoryFactMetadata:
    """Parsed metadata attached to one PROFILE/INSIGHTS bullet."""

    confidence: str | None = None
    last_verified: str | None = None
    legacy_verify: bool = False

    @property
    def has_structured_metadata(self) -> bool:
        return bool(self.confidence or self.last_verified)


@dataclass(slots=True, frozen=True)
class MemoryMetadataSummary:
    """Compact structured-metadata summary for one Markdown layer."""

    tagged_bullets: int = 0
    high_confidence: int = 0
    medium_confidence: int = 0
    low_confidence: int = 0
    with_last_verified: int = 0
    legacy_verify_markers: int = 0


def parse_memory_fact_metadata(line: str) -> MemoryFactMetadata | None:
    """Parse one Markdown bullet for structured/legacy verification markers."""
    if not _BULLET_RE.match(line):
        return None

    legacy_verify = bool(_LEGACY_VERIFY_RE.search(line))
    confidence: str | None = None
    last_verified: str | None = None

    if match := _METADATA_COMMENT_RE.search(line):
        body = match.group("body")
        try:
            tokens = shlex.split(body)
        except ValueError:
            tokens = body.split()

        for token in tokens:
            if "=" not in token:
                continue
            key, value = token.split("=", 1)
            normalized_key = key.strip().lower().replace("-", "_")
            normalized_value = value.strip().strip(",")

            if normalized_key == "confidence":
                candidate = normalized_value.lower()
                if candidate in _CONFIDENCE_VALUES:
                    confidence = candidate
            elif normalized_key == "last_verified":
                try:
                    last_verified = date.fromisoformat(normalized_value).isoformat()
                except ValueError:
                    continue

    if not confidence and not last_verified and not legacy_verify:
        return None

    return MemoryFactMetadata(
        confidence=confidence,
        last_verified=last_verified,
        legacy_verify=legacy_verify,
    )


def summarize_memory_metadata(markdown: str) -> MemoryMetadataSummary:
    """Summarize structured metadata usage inside one Markdown file."""
    tagged_bullets = 0
    high_confidence = 0
    medium_confidence = 0
    low_confidence = 0
    with_last_verified = 0
    legacy_verify_markers = 0

    for line in markdown.splitlines():
        metadata = parse_memory_fact_metadata(line)
        if metadata is None:
            continue
        if metadata.has_structured_metadata:
            tagged_bullets += 1
        if metadata.confidence == "high":
            high_confidence += 1
        elif metadata.confidence == "medium":
            medium_confidence += 1
        elif metadata.confidence == "low":
            low_confidence += 1
        if metadata.last_verified:
            with_last_verified += 1
        if metadata.legacy_verify:
            legacy_verify_markers += 1

    return MemoryMetadataSummary(
        tagged_bullets=tagged_bullets,
        high_confidence=high_confidence,
        medium_confidence=medium_confidence,
        low_confidence=low_confidence,
        with_last_verified=with_last_verified,
        legacy_verify_markers=legacy_verify_markers,
    )


def format_memory_metadata_summary(markdown: str) -> str:
    """Render a stable plain-text summary for Dream prompt context."""
    summary = summarize_memory_metadata(markdown)
    return "\n".join(
        [
            f"- Tagged bullets: {summary.tagged_bullets}",
            (
                "- Confidence counts: "
                f"high={summary.high_confidence}, "
                f"medium={summary.medium_confidence}, "
                f"low={summary.low_confidence}"
            ),
            f"- Bullets with last_verified: {summary.with_last_verified}",
            f"- Legacy (verify) markers: {summary.legacy_verify_markers}",
        ]
    )
