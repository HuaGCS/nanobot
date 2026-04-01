"""Tests for Star Office UI status integration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.star_office import (
    StarOfficeHook,
    StarOfficePushSettings,
    StarOfficeStatusTracker,
)


class _FakeResponse:
    def __init__(self, payload: dict[str, object], *, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict[str, object]:
        return self._payload


class _FakeAsyncClient:
    def __init__(self, responses: list[_FakeResponse], calls: list[dict[str, object]], *args, **kwargs) -> None:
        self._responses = responses
        self._calls = calls
        self.timeout = kwargs.get("timeout")
        self.follow_redirects = kwargs.get("follow_redirects")

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    async def post(self, url: str, *, json: dict[str, object]) -> _FakeResponse:
        self._calls.append({"url": url, "json": json, "timeout": self.timeout})
        assert self._responses
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_star_office_hook_tracks_research_then_returns_idle() -> None:
    provider = MagicMock()
    call_count = {"n": 0}

    async def chat_with_retry(**_kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return LLMResponse(
                content="let me look that up",
                tool_calls=[ToolCallRequest(id="call_1", name="web_search", arguments={"q": "nanobot"})],
            )
        return LLMResponse(content="done", tool_calls=[], usage={})

    provider.chat_with_retry = chat_with_retry
    tools = MagicMock()
    tools.get_definitions.return_value = []

    tracker = StarOfficeStatusTracker()
    observed = []

    async def execute(name: str, arguments: dict[str, str]) -> str:
        del name, arguments
        observed.append(tracker.snapshot())
        return "search result"

    tools.execute = AsyncMock(side_effect=execute)

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "Research nanobot"}],
        tools=tools,
        model="test-model",
        max_iterations=3,
        hook=StarOfficeHook(tracker),
    ))

    assert result.final_content == "done"
    assert len(observed) == 1
    assert observed[0].state == "researching"
    assert observed[0].active_runs == 1
    assert "Researching with web search" == observed[0].detail

    final_snapshot = tracker.snapshot()
    assert final_snapshot.state == "idle"
    assert final_snapshot.detail == "Ready"
    assert final_snapshot.active_runs == 0


@pytest.mark.asyncio
async def test_star_office_hook_marks_exec_failures_as_error() -> None:
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(
        content="running command",
        tool_calls=[ToolCallRequest(id="call_1", name="exec", arguments={"command": "false"})],
    ))
    tools = MagicMock()
    tools.get_definitions.return_value = []

    tracker = StarOfficeStatusTracker()
    observed = []

    async def execute(name: str, arguments: dict[str, str]) -> str:
        del name, arguments
        observed.append(tracker.snapshot())
        raise RuntimeError("command failed")

    tools.execute = AsyncMock(side_effect=execute)

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "Run diagnostics"}],
        tools=tools,
        model="test-model",
        max_iterations=1,
        hook=StarOfficeHook(tracker),
        fail_on_tool_error=True,
    ))

    assert result.stop_reason == "tool_error"
    assert observed[0].state == "executing"
    assert observed[0].detail == "Running exec"

    final_snapshot = tracker.snapshot()
    assert final_snapshot.state == "error"
    assert "command failed" in final_snapshot.detail
    assert final_snapshot.active_runs == 0


@pytest.mark.asyncio
async def test_star_office_hook_clears_active_run_on_max_iterations() -> None:
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(
        content="still working",
        tool_calls=[ToolCallRequest(id="call_1", name="list_dir", arguments={"path": "."})],
    ))
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(return_value="tool result")

    tracker = StarOfficeStatusTracker()
    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "Keep going"}],
        tools=tools,
        model="test-model",
        max_iterations=1,
        hook=StarOfficeHook(tracker),
    ))

    assert result.stop_reason == "max_iterations"

    final_snapshot = tracker.snapshot()
    assert final_snapshot.state == "error"
    assert "maximum number of tool call iterations" in final_snapshot.detail
    assert final_snapshot.active_runs == 0


@pytest.mark.asyncio
async def test_star_office_tracker_pushes_join_and_status_updates(monkeypatch: pytest.MonkeyPatch) -> None:
    from nanobot import star_office as star_office_module

    calls: list[dict[str, object]] = []
    responses = [
        _FakeResponse({"ok": True, "agentId": "agent-1"}),
        _FakeResponse({"ok": True}),
    ]

    def _client_factory(*args, **kwargs):
        return _FakeAsyncClient(responses, calls, *args, **kwargs)

    monkeypatch.setattr(star_office_module.httpx, "AsyncClient", _client_factory)

    tracker = StarOfficeStatusTracker(
        push_settings=StarOfficePushSettings(
            enabled=True,
            office_url="https://office.example.com",
            join_key="join-secret",
            agent_name="nanobot-dev",
            timeout=12.0,
        )
    )

    tracker.publish_current()
    await tracker.flush()

    assert calls == [
        {
            "url": "https://office.example.com/join-agent",
            "json": {
                "name": "nanobot-dev",
                "joinKey": "join-secret",
                "state": "idle",
                "detail": "Ready",
            },
            "timeout": 12.0,
        },
        {
            "url": "https://office.example.com/agent-push",
            "json": {
                "agentId": "agent-1",
                "joinKey": "join-secret",
                "state": "idle",
                "detail": "Ready",
            },
            "timeout": 12.0,
        },
    ]


@pytest.mark.asyncio
async def test_star_office_tracker_rejoins_after_push_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from nanobot import star_office as star_office_module

    calls: list[dict[str, object]] = []
    responses = [
        _FakeResponse({"ok": True, "agentId": "agent-1"}),
        _FakeResponse({"ok": False, "error": "stale agent"}, status_code=404),
        _FakeResponse({"ok": True, "agentId": "agent-2"}),
        _FakeResponse({"ok": True}),
    ]

    def _client_factory(*args, **kwargs):
        return _FakeAsyncClient(responses, calls, *args, **kwargs)

    monkeypatch.setattr(star_office_module.httpx, "AsyncClient", _client_factory)

    tracker = StarOfficeStatusTracker(
        push_settings=StarOfficePushSettings(
            enabled=True,
            office_url="https://office.example.com",
            join_key="join-secret",
            agent_name="nanobot-dev",
        )
    )

    tracker.publish_current()
    await tracker.flush()

    assert calls[-2:] == [
        {
            "url": "https://office.example.com/join-agent",
            "json": {
                "name": "nanobot-dev",
                "joinKey": "join-secret",
                "state": "idle",
                "detail": "Ready",
            },
            "timeout": 10.0,
        },
        {
            "url": "https://office.example.com/agent-push",
            "json": {
                "agentId": "agent-2",
                "joinKey": "join-secret",
                "state": "idle",
                "detail": "Ready",
            },
            "timeout": 10.0,
        },
    ]


@pytest.mark.asyncio
async def test_star_office_tracker_owner_mode_pushes_main_state(monkeypatch: pytest.MonkeyPatch) -> None:
    from nanobot import star_office as star_office_module

    calls: list[dict[str, object]] = []
    responses = [
        _FakeResponse({"status": "ok"}),
    ]

    def _client_factory(*args, **kwargs):
        return _FakeAsyncClient(responses, calls, *args, **kwargs)

    monkeypatch.setattr(star_office_module.httpx, "AsyncClient", _client_factory)

    tracker = StarOfficeStatusTracker(
        push_settings=StarOfficePushSettings(
            enabled=True,
            mode="owner",
            office_url="http://127.0.0.1:19000",
            timeout=9.0,
        )
    )

    tracker.publish_current()
    await tracker.flush()

    assert calls == [
        {
            "url": "http://127.0.0.1:19000/set_state",
            "json": {
                "state": "idle",
                "detail": "Ready",
            },
            "timeout": 9.0,
        }
    ]
