"""Tests for the Mem0-backed user-memory backend."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from nanobot.agent.memory_backends.mem0_backend import Mem0UserMemoryBackend
from nanobot.agent.memory_models import MemoryCommitRequest, MemoryScope
from nanobot.config.schema import Config


class _FakeAsyncMemoryClient:
    def __init__(self) -> None:
        self.search_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.add_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.get_all_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def search(self, *args, **kwargs):
        self.search_calls.append((args, kwargs))
        return {
            "results": [
                {"memory": "Prefers green tea"},
                {"memory": "Lives in Shanghai"},
            ]
        }

    async def add(self, *args, **kwargs):
        self.add_calls.append((args, kwargs))
        return {"status": "ok"}

    async def get_all(self, *args, **kwargs):
        self.get_all_calls.append((args, kwargs))
        return {"results": [{"memory": "Known fallback memory"}]}


class _FakeAsyncMemory:
    last_config = None
    last_client: _FakeAsyncMemoryClient | None = None

    def __init__(self) -> None:
        self._client = _FakeAsyncMemoryClient()
        type(self).last_client = self._client

    @classmethod
    def from_config(cls, config):
        cls.last_config = config
        cls.last_client = _FakeAsyncMemoryClient()
        return cls.last_client

    async def search(self, *args, **kwargs):
        return await self._client.search(*args, **kwargs)

    async def add(self, *args, **kwargs):
        return await self._client.add(*args, **kwargs)

    async def get_all(self, *args, **kwargs):
        return await self._client.get_all(*args, **kwargs)


def _make_scope(tmp_path: Path, *, query: str | None = None) -> MemoryScope:
    return MemoryScope(
        workspace=tmp_path / "workspace",
        session_key="cli:direct",
        channel="cli",
        chat_id="direct",
        sender_id="user-123",
        persona="coder",
        language="zh",
        query=query,
    )


@pytest.mark.asyncio
async def test_mem0_backend_resolves_query_context_from_search(tmp_path: Path) -> None:
    config = Config.model_validate(
        {
            "memory": {
                "user": {
                    "backend": "mem0",
                    "mem0": {
                        "llm": {
                            "provider": "openai",
                            "apiKey": "llm-key",
                            "url": "https://api.openai.example/v1",
                            "model": "gpt-4.1-mini",
                        },
                        "embedder": {
                            "provider": "openai",
                            "apiKey": "embed-key",
                            "url": "https://embed.example/v1",
                            "model": "text-embedding-3-small",
                        },
                        "vectorStore": {
                            "provider": "qdrant",
                            "url": "https://qdrant.example.com:6333",
                            "config": {"collection_name": "nanobot_user_memory"},
                        },
                    },
                }
            }
        }
    )

    with patch(
        "nanobot.agent.memory_backends.mem0_backend.import_module",
        return_value=SimpleNamespace(AsyncMemory=_FakeAsyncMemory),
    ):
        backend = Mem0UserMemoryBackend(config.memory.user.mem0)
        resolved = await backend.resolve_context(_make_scope(tmp_path, query="tea"))

    assert resolved.source == "mem0"
    assert "Prefers green tea" in resolved.block
    assert "Lives in Shanghai" in resolved.block
    assert _FakeAsyncMemory.last_client is not None
    assert _FakeAsyncMemory.last_client.search_calls == [
        (
            ("tea",),
            {
                "user_id": "user-123",
                "agent_id": "coder",
                "limit": 5,
            },
        )
    ]
    assert _FakeAsyncMemory.last_config == {
        "llm": {
            "provider": "openai",
            "config": {
                "api_key": "llm-key",
                "model": "gpt-4.1-mini",
                "url": "https://api.openai.example/v1",
                "base_url": "https://api.openai.example/v1",
                "openai_base_url": "https://api.openai.example/v1",
            },
        },
        "embedder": {
            "provider": "openai",
            "config": {
                "api_key": "embed-key",
                "model": "text-embedding-3-small",
                "url": "https://embed.example/v1",
                "base_url": "https://embed.example/v1",
                "openai_base_url": "https://embed.example/v1",
            },
        },
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "collection_name": "nanobot_user_memory",
                "url": "https://qdrant.example.com:6333",
                "host": "qdrant.example.com",
                "port": 6333,
            },
        },
    }


@pytest.mark.asyncio
async def test_mem0_backend_commits_turn_messages_with_metadata(tmp_path: Path) -> None:
    config = Config.model_validate(
        {
            "memory": {
                "user": {
                    "backend": "mem0",
                    "mem0": {
                        "metadata": {
                            "tenant": "paid",
                            "env": "prod",
                        }
                    },
                }
            }
        }
    )

    with patch(
        "nanobot.agent.memory_backends.mem0_backend.import_module",
        return_value=SimpleNamespace(AsyncMemory=_FakeAsyncMemory),
    ):
        backend = Mem0UserMemoryBackend(config.memory.user.mem0)
        request = MemoryCommitRequest(
            scope=_make_scope(tmp_path, query="hello"),
            inbound_content="Remember I like green tea.",
            outbound_content="I'll keep that in mind.",
        )
        await backend.commit_turn(request)

    assert _FakeAsyncMemory.last_client is not None
    assert _FakeAsyncMemory.last_client.add_calls == [
        (
            (
                [
                    {"role": "user", "content": "Remember I like green tea."},
                    {"role": "assistant", "content": "I'll keep that in mind."},
                ],
            ),
            {
                "user_id": "user-123",
                "agent_id": "coder",
                "metadata": {
                    "tenant": "paid",
                    "env": "prod",
                    "session_key": "cli:direct",
                    "channel": "cli",
                    "chat_id": "direct",
                    "persona": "coder",
                    "language": "zh",
                    "sender_id": "user-123",
                },
            },
        )
    ]


def test_mem0_backend_raises_clear_error_when_dependency_missing() -> None:
    config = Config().memory.user.mem0

    with patch(
        "nanobot.agent.memory_backends.mem0_backend.import_module",
        side_effect=ImportError("missing mem0"),
    ):
        with pytest.raises(RuntimeError, match="pip install nanobot-ai\\[mem0\\]"):
            Mem0UserMemoryBackend(config)
