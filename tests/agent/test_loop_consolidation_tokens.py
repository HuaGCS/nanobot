import asyncio
import copy
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import nanobot.agent.memory as memory_module
from nanobot.agent.loop import AgentLoop
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMResponse


def _make_loop(tmp_path, *, estimated_tokens: int, context_window_tokens: int) -> AgentLoop:
    from nanobot.providers.base import GenerationSettings
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=0)
    provider.estimate_prompt_tokens.return_value = (estimated_tokens, "test-counter")
    _response = LLMResponse(content="ok", tool_calls=[])
    provider.chat_with_retry = AsyncMock(return_value=_response)
    provider.chat_stream_with_retry = AsyncMock(return_value=_response)

    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        context_window_tokens=context_window_tokens,
    )
    loop.tools.get_definitions = MagicMock(return_value=[])
    loop.memory_consolidator._SAFETY_BUFFER = 0
    return loop


@pytest.mark.asyncio
async def test_prompt_below_threshold_does_not_consolidate(tmp_path) -> None:
    loop = _make_loop(tmp_path, estimated_tokens=100, context_window_tokens=200)
    loop.memory_consolidator.consolidate_messages = AsyncMock(return_value=True)  # type: ignore[method-assign]

    await loop.process_direct("hello", session_key="cli:test")

    loop.memory_consolidator.consolidate_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_prompt_above_threshold_triggers_consolidation(tmp_path, monkeypatch) -> None:
    loop = _make_loop(tmp_path, estimated_tokens=1000, context_window_tokens=200)
    loop.memory_consolidator.consolidate_messages = AsyncMock(return_value=True)  # type: ignore[method-assign]
    session = loop.sessions.get_or_create("cli:test")
    session.messages = [
        {"role": "user", "content": "u1", "timestamp": "2026-01-01T00:00:00"},
        {"role": "assistant", "content": "a1", "timestamp": "2026-01-01T00:00:01"},
        {"role": "user", "content": "u2", "timestamp": "2026-01-01T00:00:02"},
    ]
    loop.sessions.save(session)
    monkeypatch.setattr(memory_module, "estimate_message_tokens", lambda _message: 500)

    await loop.process_direct("hello", session_key="cli:test")

    assert loop.memory_consolidator.consolidate_messages.await_count >= 1


@pytest.mark.asyncio
async def test_prompt_above_threshold_archives_until_next_user_boundary(tmp_path, monkeypatch) -> None:
    loop = _make_loop(tmp_path, estimated_tokens=1000, context_window_tokens=200)
    loop.memory_consolidator.consolidate_messages = AsyncMock(return_value=True)  # type: ignore[method-assign]

    session = loop.sessions.get_or_create("cli:test")
    session.messages = [
        {"role": "user", "content": "u1", "timestamp": "2026-01-01T00:00:00"},
        {"role": "assistant", "content": "a1", "timestamp": "2026-01-01T00:00:01"},
        {"role": "user", "content": "u2", "timestamp": "2026-01-01T00:00:02"},
        {"role": "assistant", "content": "a2", "timestamp": "2026-01-01T00:00:03"},
        {"role": "user", "content": "u3", "timestamp": "2026-01-01T00:00:04"},
    ]
    loop.sessions.save(session)

    token_map = {"u1": 120, "a1": 120, "u2": 120, "a2": 120, "u3": 120}
    monkeypatch.setattr(memory_module, "estimate_message_tokens", lambda message: token_map[message["content"]])

    await loop.memory_consolidator.maybe_consolidate_by_tokens(session)

    archived_chunk = loop.memory_consolidator.consolidate_messages.await_args.args[0]
    assert [message["content"] for message in archived_chunk] == ["u1", "a1", "u2", "a2"]
    assert session.last_consolidated == 4


@pytest.mark.asyncio
async def test_consolidation_loops_until_target_met(tmp_path, monkeypatch) -> None:
    """Verify maybe_consolidate_by_tokens keeps looping until under threshold."""
    loop = _make_loop(tmp_path, estimated_tokens=0, context_window_tokens=200)
    loop.memory_consolidator.consolidate_messages = AsyncMock(return_value=True)  # type: ignore[method-assign]

    session = loop.sessions.get_or_create("cli:test")
    session.messages = [
        {"role": "user", "content": "u1", "timestamp": "2026-01-01T00:00:00"},
        {"role": "assistant", "content": "a1", "timestamp": "2026-01-01T00:00:01"},
        {"role": "user", "content": "u2", "timestamp": "2026-01-01T00:00:02"},
        {"role": "assistant", "content": "a2", "timestamp": "2026-01-01T00:00:03"},
        {"role": "user", "content": "u3", "timestamp": "2026-01-01T00:00:04"},
        {"role": "assistant", "content": "a3", "timestamp": "2026-01-01T00:00:05"},
        {"role": "user", "content": "u4", "timestamp": "2026-01-01T00:00:06"},
    ]
    loop.sessions.save(session)

    call_count = [0]
    def mock_estimate(_session):
        call_count[0] += 1
        if call_count[0] == 1:
            return (500, "test")
        if call_count[0] == 2:
            return (300, "test")
        return (80, "test")

    loop.memory_consolidator.estimate_session_prompt_tokens = mock_estimate  # type: ignore[method-assign]
    monkeypatch.setattr(memory_module, "estimate_message_tokens", lambda _m: 100)

    await loop.memory_consolidator.maybe_consolidate_by_tokens(session)

    assert loop.memory_consolidator.consolidate_messages.await_count == 2
    assert session.last_consolidated == 6


@pytest.mark.asyncio
async def test_consolidation_continues_below_trigger_until_half_target(tmp_path, monkeypatch) -> None:
    """Once triggered, consolidation should continue until it drops below half threshold."""
    loop = _make_loop(tmp_path, estimated_tokens=0, context_window_tokens=200)
    loop.memory_consolidator.consolidate_messages = AsyncMock(return_value=True)  # type: ignore[method-assign]

    session = loop.sessions.get_or_create("cli:test")
    session.messages = [
        {"role": "user", "content": "u1", "timestamp": "2026-01-01T00:00:00"},
        {"role": "assistant", "content": "a1", "timestamp": "2026-01-01T00:00:01"},
        {"role": "user", "content": "u2", "timestamp": "2026-01-01T00:00:02"},
        {"role": "assistant", "content": "a2", "timestamp": "2026-01-01T00:00:03"},
        {"role": "user", "content": "u3", "timestamp": "2026-01-01T00:00:04"},
        {"role": "assistant", "content": "a3", "timestamp": "2026-01-01T00:00:05"},
        {"role": "user", "content": "u4", "timestamp": "2026-01-01T00:00:06"},
    ]
    loop.sessions.save(session)

    call_count = [0]

    def mock_estimate(_session):
        call_count[0] += 1
        if call_count[0] == 1:
            return (500, "test")
        if call_count[0] == 2:
            return (150, "test")
        return (80, "test")

    loop.memory_consolidator.estimate_session_prompt_tokens = mock_estimate  # type: ignore[method-assign]
    monkeypatch.setattr(memory_module, "estimate_message_tokens", lambda _m: 100)

    await loop.memory_consolidator.maybe_consolidate_by_tokens(session)

    assert loop.memory_consolidator.consolidate_messages.await_count == 2
    assert session.last_consolidated == 6


@pytest.mark.asyncio
async def test_preflight_consolidation_before_llm_call(tmp_path, monkeypatch) -> None:
    """Verify preflight consolidation runs before the LLM call in process_direct."""
    order: list[str] = []

    loop = _make_loop(tmp_path, estimated_tokens=0, context_window_tokens=200)

    async def track_consolidate(messages):
        order.append("consolidate")
        return True
    loop.memory_consolidator.consolidate_messages = track_consolidate  # type: ignore[method-assign]

    async def track_llm(*args, **kwargs):
        order.append("llm")
        return LLMResponse(content="ok", tool_calls=[])
    loop.provider.chat_with_retry = track_llm
    loop.provider.chat_stream_with_retry = track_llm

    session = loop.sessions.get_or_create("cli:test")
    session.messages = [
        {"role": "user", "content": "u1", "timestamp": "2026-01-01T00:00:00"},
        {"role": "assistant", "content": "a1", "timestamp": "2026-01-01T00:00:01"},
        {"role": "user", "content": "u2", "timestamp": "2026-01-01T00:00:02"},
    ]
    loop.sessions.save(session)
    monkeypatch.setattr(memory_module, "estimate_message_tokens", lambda _m: 500)

    call_count = [0]
    def mock_estimate(_session):
        call_count[0] += 1
        return (1000 if call_count[0] <= 1 else 80, "test")
    loop.memory_consolidator.estimate_session_prompt_tokens = mock_estimate  # type: ignore[method-assign]

    await loop.process_direct("hello", session_key="cli:test")

    assert "consolidate" in order
    assert "llm" in order
    assert order.index("consolidate") < order.index("llm")


@pytest.mark.asyncio
async def test_slow_preflight_consolidation_continues_in_background(tmp_path, monkeypatch) -> None:
    order: list[str] = []

    loop = _make_loop(tmp_path, estimated_tokens=0, context_window_tokens=200)
    monkeypatch.setattr(loop, "_PREFLIGHT_CONSOLIDATION_BUDGET_SECONDS", 0.01)

    release = asyncio.Event()

    async def slow_consolidation(_session):
        order.append("consolidate-start")
        await release.wait()
        order.append("consolidate-end")

    async def track_llm(*args, **kwargs):
        order.append("llm")
        return LLMResponse(content="ok", tool_calls=[])

    loop.memory_consolidator.maybe_consolidate_by_tokens = slow_consolidation  # type: ignore[method-assign]
    loop.provider.chat_with_retry = track_llm

    await loop.process_direct("hello", session_key="cli:test")

    assert "consolidate-start" in order
    assert "llm" in order
    assert "consolidate-end" not in order

    release.set()
    await loop.close_mcp()

    assert "consolidate-end" in order


@pytest.mark.asyncio
async def test_large_tool_results_are_compacted_before_next_llm_call(tmp_path, monkeypatch) -> None:
    from nanobot.providers.base import GenerationSettings, ToolCallRequest

    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "MEMORY.md").write_text("persisted memory", encoding="utf-8")

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=512)

    def estimate(messages, tools, model):
        payload = json.dumps({"messages": messages, "tools": tools, "model": model}, ensure_ascii=False)
        return (len(payload) // 2, "test-counter")

    provider.estimate_prompt_tokens = estimate

    captured_second_call: list[dict] = []
    call_count = {"n": 0}

    async def scripted_chat_with_retry(*, messages, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return LLMResponse(
                content="use tool",
                tool_calls=[ToolCallRequest(id="call_1", name="list_dir", arguments={"path": "."})],
            )
        captured_second_call[:] = messages
        return LLMResponse(content="done", tool_calls=[])

    provider.chat_with_retry = scripted_chat_with_retry

    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        context_window_tokens=20_000,
    )

    async def fake_execute(_self, _name, _arguments):
        return "x" * 40_000

    monkeypatch.setattr("nanobot.agent.tools.registry.ToolRegistry.execute", fake_execute)

    response = await loop.process_direct("remember and continue", session_key="cli:test")

    assert response is not None
    assert response.content == "done"
    assert captured_second_call[0]["role"] == "system"
    assert "persisted memory" in captured_second_call[0]["content"]

    tool_messages = [msg for msg in captured_second_call if msg.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert isinstance(tool_messages[0]["content"], str)
    assert len(tool_messages[0]["content"]) < 40_000
    assert "truncated" in tool_messages[0]["content"] or "omitted" in tool_messages[0]["content"]

    estimated, _ = provider.estimate_prompt_tokens(
        captured_second_call,
        loop.tools.get_definitions(),
        loop.model,
    )
    assert estimated <= loop._prompt_budget_tokens()


@pytest.mark.asyncio
async def test_multi_step_tool_turn_keeps_memory_and_recent_context(tmp_path, monkeypatch) -> None:
    from nanobot.providers.base import GenerationSettings, ToolCallRequest

    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "MEMORY.md").write_text(
        "- Project codename: Atlas\n- Always prefer long-term memory facts\n",
        encoding="utf-8",
    )

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=512)

    def estimate(messages, tools, model):
        payload = json.dumps(
            {"messages": messages, "tools": tools, "model": model},
            ensure_ascii=False,
        )
        return (len(payload) // 2, "test-counter")

    provider.estimate_prompt_tokens = estimate

    captured_calls: list[list[dict]] = []
    call_count = {"n": 0}

    async def scripted_chat_with_retry(*, messages, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return LLMResponse(
                content="first tool",
                tool_calls=[ToolCallRequest(id="call_1", name="search_docs", arguments={"query": "atlas"})],
            )
        if call_count["n"] == 2:
            captured_calls.append(copy.deepcopy(messages))
            return LLMResponse(
                content="second tool",
                tool_calls=[ToolCallRequest(id="call_2", name="scan_repo", arguments={"path": "."})],
            )
        captured_calls.append(copy.deepcopy(messages))
        return LLMResponse(content="done", tool_calls=[])

    provider.chat_with_retry = scripted_chat_with_retry

    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        context_window_tokens=26_000,
    )

    async def fake_execute(_self, name, _arguments):
        if name == "search_docs":
            return "TOOL_A:" + ("a" * 30_000)
        if name == "scan_repo":
            return "TOOL_B:" + ("b" * 30_000)
        return "unexpected"

    monkeypatch.setattr("nanobot.agent.tools.registry.ToolRegistry.execute", fake_execute)

    response = await loop.process_direct("Use the remembered Atlas facts and inspect the repo", session_key="cli:test")

    assert response is not None
    assert response.content == "done"
    assert len(captured_calls) == 2

    second_call, third_call = captured_calls
    assert second_call[0]["role"] == "system"
    assert "Project codename: Atlas" in second_call[0]["content"]
    assert "Project codename: Atlas" in third_call[0]["content"]

    second_tool_messages = [msg for msg in second_call if msg.get("role") == "tool"]
    third_tool_messages = [msg for msg in third_call if msg.get("role") == "tool"]
    assert len(second_tool_messages) == 1
    assert len(third_tool_messages) == 2

    older_tool = next(msg for msg in third_tool_messages if msg.get("name") == "search_docs")
    newer_tool = next(msg for msg in third_tool_messages if msg.get("name") == "scan_repo")
    assert isinstance(older_tool["content"], str)
    assert isinstance(newer_tool["content"], str)
    assert older_tool["content"].startswith("TOOL_A:")
    assert newer_tool["content"].startswith("TOOL_B:")
    assert len(second_tool_messages[0]["content"]) > 25_000
    assert len(older_tool["content"]) < len(second_tool_messages[0]["content"])
    assert len(older_tool["content"]) < len(newer_tool["content"])

    assistant_tool_turns = [
        msg for msg in third_call
        if msg.get("role") == "assistant" and msg.get("tool_calls")
    ]
    assert [turn["tool_calls"][0]["function"]["name"] for turn in assistant_tool_turns] == [
        "search_docs",
        "scan_repo",
    ]

    estimated, _ = provider.estimate_prompt_tokens(
        third_call,
        loop.tools.get_definitions(),
        loop.model,
    )
    assert estimated <= loop._prompt_budget_tokens()
