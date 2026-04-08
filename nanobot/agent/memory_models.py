"""Shared models for memory routing and backend integration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class MemoryScope:
    """Resolved scope for one memory read/write operation."""

    workspace: Path
    session_key: str
    channel: str
    chat_id: str
    sender_id: str | None
    persona: str
    language: str
    query: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedMemoryContext:
    """Prepared memory block ready for prompt injection."""

    block: str = ""
    source: str = "file"


@dataclass(frozen=True, slots=True)
class MemoryCommitRequest:
    """Normalized turn payload for future memory backend writes."""

    scope: MemoryScope
    inbound_content: Any | None = None
    outbound_content: str | None = None
    persisted_messages: tuple[dict[str, Any], ...] = ()
