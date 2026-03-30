"""Abstract interfaces for user memory backends."""

from __future__ import annotations

from abc import ABC, abstractmethod

from nanobot.agent.memory_models import MemoryCommitRequest, MemoryScope, ResolvedMemoryContext


class UserMemoryBackend(ABC):
    """Backend abstraction for user-scoped long-term memory."""

    @abstractmethod
    def resolve_context(self, scope: MemoryScope) -> ResolvedMemoryContext:
        """Return the memory block that should be injected into the prompt."""

    async def commit_turn(self, request: MemoryCommitRequest) -> None:
        """Persist a completed turn to the backend."""
        return None

    async def flush_session(self, scope: MemoryScope) -> None:
        """Flush any buffered memory writes for the given scope."""
        return None
