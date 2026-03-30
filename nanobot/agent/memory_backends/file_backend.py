"""File-backed user memory backend."""

from __future__ import annotations

from nanobot.agent.memory import MemoryStore
from nanobot.agent.memory_backends.base import UserMemoryBackend
from nanobot.agent.memory_models import MemoryScope, ResolvedMemoryContext
from nanobot.agent.personas import persona_workspace


class FileUserMemoryBackend(UserMemoryBackend):
    """Read user memory from the existing persona-scoped markdown files."""

    def resolve_context(self, scope: MemoryScope) -> ResolvedMemoryContext:
        store = MemoryStore(persona_workspace(scope.workspace, scope.persona))
        return ResolvedMemoryContext(block=store.get_memory_context(), source="file")
