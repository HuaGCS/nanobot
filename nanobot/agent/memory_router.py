"""Routing layer for user memory reads and writes."""

from __future__ import annotations

from loguru import logger

from nanobot.agent.memory_backends.base import UserMemoryBackend
from nanobot.agent.memory_models import MemoryCommitRequest, MemoryScope, ResolvedMemoryContext


class MemoryRouter:
    """Coordinate memory access through the configured user-memory backend."""

    def __init__(
        self,
        user_backend: UserMemoryBackend,
        shadow_backends: list[UserMemoryBackend] | None = None,
    ):
        self.user_backend = user_backend
        self.shadow_backends = list(shadow_backends or [])

    def prepare_context(self, scope: MemoryScope) -> ResolvedMemoryContext:
        """Resolve the memory block that should enter the current prompt."""
        return self.user_backend.resolve_context(scope)

    async def commit_turn(self, request: MemoryCommitRequest) -> None:
        """Persist a completed turn through the active backend."""
        backends = [self.user_backend, *self.shadow_backends]
        for backend in backends:
            try:
                await backend.commit_turn(request)
            except Exception:
                logger.exception(
                    "Memory backend commit failed: {}", type(backend).__name__
                )

    async def flush_session(self, scope: MemoryScope) -> None:
        """Flush backend state for a session before scope-sensitive transitions."""
        backends = [self.user_backend, *self.shadow_backends]
        for backend in backends:
            try:
                await backend.flush_session(scope)
            except Exception:
                logger.exception(
                    "Memory backend flush failed: {}", type(backend).__name__
                )
