"""Tests for the stage-2 memory routing abstraction."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.agent.memory_backends.file_backend import FileUserMemoryBackend
from nanobot.agent.memory_models import MemoryCommitRequest, MemoryScope, ResolvedMemoryContext
from nanobot.agent.memory_router import MemoryRouter
from nanobot.bus.events import InboundMessage
from nanobot.config.schema import Config


def _make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    return workspace


def test_system_prompt_prefers_injected_memory_context(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    (workspace / "memory").mkdir()
    (workspace / "memory" / "MEMORY.md").write_text("root memory", encoding="utf-8")

    builder = ContextBuilder(workspace)
    prompt = builder.build_system_prompt(memory_context="## Long-term Memory\ninjected memory")

    assert "injected memory" in prompt
    assert "root memory" not in prompt


def test_file_user_memory_backend_reads_persona_scope(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    (workspace / "memory").mkdir()
    (workspace / "memory" / "MEMORY.md").write_text("root memory", encoding="utf-8")

    persona_dir = workspace / "personas" / "coder" / "memory"
    persona_dir.mkdir(parents=True)
    (persona_dir / "MEMORY.md").write_text("coder memory", encoding="utf-8")

    backend = FileUserMemoryBackend()
    scope = MemoryScope(
        workspace=workspace,
        session_key="cli:direct",
        channel="cli",
        chat_id="direct",
        sender_id="user",
        persona="coder",
        language="en",
    )

    resolved = asyncio.run(backend.resolve_context(scope))

    assert resolved.source == "file"
    assert "coder memory" in resolved.block
    assert "root memory" not in resolved.block


@pytest.mark.asyncio
async def test_loop_uses_memory_router_for_prompt_context(tmp_path: Path) -> None:
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus
    from nanobot.providers.base import GenerationSettings

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=1024)
    provider.chat_with_retry = AsyncMock(
        return_value=SimpleNamespace(
            has_tool_calls=False,
            content="ok",
            finish_reason="stop",
            reasoning_content=None,
            thinking_blocks=None,
            tool_calls=[],
            usage=None,
        )
    )
    provider.chat_stream_with_retry = provider.chat_with_retry

    with patch("nanobot.agent.loop.SubagentManager"):
        loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path)

    loop.memory_router.prepare_context = AsyncMock(
        return_value=ResolvedMemoryContext(
            block="## Long-term Memory\nrouter memory",
            source="test",
        )
    )

    response = await loop._process_message(
        InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="hello")
    )

    assert response is not None
    assert response.content == "ok"

    prompt_messages = provider.chat_with_retry.await_args.kwargs["messages"]
    assert "router memory" in prompt_messages[0]["content"]


@pytest.mark.asyncio
async def test_memory_router_fans_out_shadow_writes() -> None:
    primary = MagicMock()
    primary.resolve_context = AsyncMock(
        return_value=ResolvedMemoryContext(block="primary", source="file")
    )
    primary.commit_turn = AsyncMock()
    primary.flush_session = AsyncMock()

    shadow = MagicMock()
    shadow.commit_turn = AsyncMock()
    shadow.flush_session = AsyncMock()

    router = MemoryRouter(user_backend=primary, shadow_backends=[shadow])
    request = MemoryCommitRequest(
        scope=MemoryScope(
            workspace=Path("/tmp/workspace"),
            session_key="cli:direct",
            channel="cli",
            chat_id="direct",
            sender_id="user",
            persona="default",
            language="en",
        )
    )

    await router.commit_turn(request)
    await router.flush_session(request.scope)

    primary.commit_turn.assert_awaited_once_with(request)
    shadow.commit_turn.assert_awaited_once_with(request)
    primary.flush_session.assert_awaited_once_with(request.scope)
    shadow.flush_session.assert_awaited_once_with(request.scope)


@pytest.mark.asyncio
async def test_memory_router_falls_back_to_file_when_primary_returns_empty() -> None:
    primary = MagicMock()
    primary.resolve_context = AsyncMock(
        return_value=ResolvedMemoryContext(block="", source="mem0")
    )

    fallback = MagicMock()
    fallback.resolve_context = AsyncMock(
        return_value=ResolvedMemoryContext(block="## Long-term Memory\nfile memory", source="file")
    )

    router = MemoryRouter(user_backend=primary, fallback_backend=fallback)
    scope = MemoryScope(
        workspace=Path("/tmp/workspace"),
        session_key="cli:direct",
        channel="cli",
        chat_id="direct",
        sender_id="user",
        persona="default",
        language="en",
    )

    resolved = await router.prepare_context(scope)

    assert resolved.source == "file"
    assert "file memory" in resolved.block
    primary.resolve_context.assert_awaited_once_with(scope)
    fallback.resolve_context.assert_awaited_once_with(scope)


@pytest.mark.asyncio
async def test_memory_router_falls_back_to_file_when_primary_raises() -> None:
    primary = MagicMock()
    primary.resolve_context = AsyncMock(side_effect=RuntimeError("mem0 unavailable"))

    fallback = MagicMock()
    fallback.resolve_context = AsyncMock(
        return_value=ResolvedMemoryContext(block="## Long-term Memory\nfile memory", source="file")
    )

    router = MemoryRouter(user_backend=primary, fallback_backend=fallback)
    scope = MemoryScope(
        workspace=Path("/tmp/workspace"),
        session_key="cli:direct",
        channel="cli",
        chat_id="direct",
        sender_id="user",
        persona="default",
        language="en",
    )

    resolved = await router.prepare_context(scope)

    assert resolved.source == "file"
    assert "file memory" in resolved.block
    primary.resolve_context.assert_awaited_once_with(scope)
    fallback.resolve_context.assert_awaited_once_with(scope)


@pytest.mark.asyncio
async def test_reload_runtime_config_enables_mem0_shadow_backend_when_requested(
    tmp_path: Path,
) -> None:
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus
    from nanobot.providers.base import GenerationSettings

    class FakeMem0Backend(FileUserMemoryBackend):
        def __init__(self, config) -> None:
            self.config = config

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=1024)

    with (
        patch("nanobot.agent.loop.SubagentManager"),
        patch("nanobot.agent.loop.Mem0UserMemoryBackend", FakeMem0Backend),
    ):
        loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path)
        assert loop.memory_config.user.shadow_write_mem0 is False
        assert loop.memory_router.shadow_backends == []

        config = Config.model_validate(
            {
                "memory": {
                    "user": {
                        "shadowWriteMem0": True,
                        "mem0": {
                            "llm": {
                                "provider": "ollama",
                                "model": "qwen3:8b",
                                "url": "http://127.0.0.1:11434",
                            },
                            "embedder": {
                                "provider": "openai",
                                "apiKey": "embed-key",
                                "url": "https://embed.example.com/v1",
                                "model": "text-embedding-3-small",
                            },
                            "vectorStore": {
                                "provider": "qdrant",
                                "url": "https://qdrant.example.com",
                                "headers": {"api-key": "qdrant-key"},
                            },
                        },
                    }
                }
            }
        )

        await loop.reload_runtime_config(config)

    assert loop.memory_config.user.shadow_write_mem0 is True
    assert len(loop.memory_router.shadow_backends) == 1
    assert isinstance(loop.memory_router.shadow_backends[0], FakeMem0Backend)


@pytest.mark.asyncio
async def test_reload_runtime_config_switches_primary_backend_to_mem0(tmp_path: Path) -> None:
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus
    from nanobot.providers.base import GenerationSettings

    class FakeMem0Backend(FileUserMemoryBackend):
        def __init__(self, config) -> None:
            self.config = config

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=1024)

    with (
        patch("nanobot.agent.loop.SubagentManager"),
        patch("nanobot.agent.loop.Mem0UserMemoryBackend", FakeMem0Backend),
    ):
        loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path)

        config = Config.model_validate(
            {
                "memory": {
                    "user": {
                        "backend": "mem0",
                        "mem0": {
                            "llm": {
                                "provider": "openai",
                                "apiKey": "llm-key",
                                "model": "gpt-4.1-mini",
                            }
                        },
                    }
                }
            }
        )

        await loop.reload_runtime_config(config)

    assert isinstance(loop.memory_router.user_backend, FakeMem0Backend)
    assert isinstance(loop.memory_router.fallback_backend, FileUserMemoryBackend)
    assert loop.memory_router.shadow_backends == []
