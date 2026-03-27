"""Tests for persona-level visible response filtering."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.providers.base import LLMResponse


def _make_loop(tmp_path: Path):
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = MagicMock(max_tokens=4096)

    with patch("nanobot.agent.loop.SubagentManager") as mock_sub_mgr:
        mock_sub_mgr.return_value.cancel_by_session = AsyncMock(return_value=0)
        loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path)

    loop.tools.get_definitions = MagicMock(return_value=[])
    return loop


def _write_manifest(tmp_path: Path, *, persona: str, tags: list[str]) -> None:
    manifest_dir = tmp_path / "personas" / persona / ".nanobot"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "st_manifest.json").write_text(
        json.dumps({"response_filter_tags": tags}),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_run_agent_loop_filters_visible_output_but_preserves_history_content(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    _write_manifest(tmp_path, persona="coder", tags=["inner"])
    loop.provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="Hi <inner>private</inner> there", tool_calls=[]),
    )

    final_content, _, messages = await loop._run_agent_loop([], persona="coder")

    assert final_content == "Hi there"
    assistant_messages = [message for message in messages if message.get("role") == "assistant"]
    assert assistant_messages[-1]["content"] == "Hi <inner>private</inner> there"


@pytest.mark.asyncio
async def test_streaming_hides_filtered_persona_tags(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    _write_manifest(tmp_path, persona="coder", tags=["inner"])

    async def chat_stream_with_retry(*, on_content_delta, **_kwargs):
        for chunk in ("Hi ", "<inner>pri", "vate</inner>", " there"):
            await on_content_delta(chunk)
        return LLMResponse(content="Hi <inner>private</inner> there", tool_calls=[])

    loop.provider.chat_stream_with_retry = chat_stream_with_retry
    streamed: list[str] = []

    async def on_stream(delta: str) -> None:
        streamed.append(delta)

    final_content, _, _ = await loop._run_agent_loop(
        [],
        persona="coder",
        on_stream=on_stream,
    )

    assert "".join(streamed) == "Hi there"
    assert final_content == "Hi there"
