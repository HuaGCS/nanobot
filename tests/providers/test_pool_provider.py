import asyncio

import pytest

from nanobot.providers.base import GenerationSettings, LLMProvider, LLMResponse
from nanobot.providers.pool_provider import ProviderPoolEntry, ProviderPoolProvider


class ScriptedProvider(LLMProvider):
    def __init__(self, responses, *, default_model: str = "test-model"):
        super().__init__()
        self._responses = list(responses)
        self._default_model = default_model
        self.calls = 0
        self.models: list[str | None] = []
        self.last_kwargs: dict = {}

    async def chat(self, *args, **kwargs) -> LLMResponse:
        self.calls += 1
        self.last_kwargs = kwargs
        self.models.append(kwargs.get("model"))
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    async def chat_stream(self, *args, on_content_delta=None, **kwargs) -> LLMResponse:
        self.calls += 1
        self.last_kwargs = kwargs
        self.models.append(kwargs.get("model"))
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if on_content_delta and response.content:
            midpoint = max(1, len(response.content) // 2)
            await on_content_delta(response.content[:midpoint])
            await on_content_delta(response.content[midpoint:])
        return response

    def get_default_model(self) -> str:
        return self._default_model


@pytest.mark.asyncio
async def test_pool_failover_uses_next_provider_after_error() -> None:
    first = ScriptedProvider([LLMResponse(content="401 unauthorized", finish_reason="error")])
    second = ScriptedProvider([LLMResponse(content="ok from second")])
    pool = ProviderPoolProvider(
        [
            ProviderPoolEntry(name="openrouter", provider=first),
            ProviderPoolEntry(name="deepseek", provider=second),
        ],
        strategy="failover",
        default_model="shared-model",
    )

    response = await pool.chat_with_retry(messages=[{"role": "user", "content": "hello"}])

    assert response.content == "ok from second"
    assert first.calls == 1
    assert second.calls == 1
    assert first.models == ["shared-model"]
    assert second.models == ["shared-model"]
    assert pool.api_key == second.api_key
    assert pool.api_base == second.api_base


@pytest.mark.asyncio
async def test_pool_round_robin_rotates_starting_provider() -> None:
    first = ScriptedProvider([LLMResponse(content="first turn"), LLMResponse(content="fallback turn")])
    second = ScriptedProvider([LLMResponse(content="second turn"), LLMResponse(content="fourth turn")])
    pool = ProviderPoolProvider(
        [
            ProviderPoolEntry(name="openrouter", provider=first),
            ProviderPoolEntry(name="deepseek", provider=second),
        ],
        strategy="round_robin",
        default_model="rr-model",
    )

    response_one = await pool.chat_with_retry(messages=[{"role": "user", "content": "1"}])
    response_two = await pool.chat_with_retry(messages=[{"role": "user", "content": "2"}])

    assert response_one.content == "first turn"
    assert response_two.content == "second turn"
    assert first.calls == 1
    assert second.calls == 1


@pytest.mark.asyncio
async def test_pool_round_robin_falls_through_after_selected_provider_error() -> None:
    first = ScriptedProvider([LLMResponse(content="401 unauthorized", finish_reason="error")])
    second = ScriptedProvider([LLMResponse(content="recovered")])
    pool = ProviderPoolProvider(
        [
            ProviderPoolEntry(name="openrouter", provider=first),
            ProviderPoolEntry(name="deepseek", provider=second),
        ],
        strategy="round_robin",
        default_model="rr-model",
    )

    response = await pool.chat_with_retry(messages=[{"role": "user", "content": "hello"}])

    assert response.content == "recovered"
    assert first.calls == 1
    assert second.calls == 1


@pytest.mark.asyncio
async def test_pool_stream_failover_discards_failed_partial_deltas() -> None:
    first = ScriptedProvider([LLMResponse(content="bad stream", finish_reason="error")])
    second = ScriptedProvider([LLMResponse(content="good stream")])
    pool = ProviderPoolProvider(
        [
            ProviderPoolEntry(name="openrouter", provider=first),
            ProviderPoolEntry(name="deepseek", provider=second),
        ],
        strategy="failover",
        default_model="stream-model",
    )
    deltas: list[str] = []

    response = await pool.chat_stream_with_retry(
        messages=[{"role": "user", "content": "hello"}],
        on_content_delta=lambda delta: _collect_delta(deltas, delta),
    )

    assert response.content == "good stream"
    assert "".join(deltas) == "good stream"


@pytest.mark.asyncio
async def test_pool_generation_settings_propagate_to_children() -> None:
    first = ScriptedProvider([LLMResponse(content="ok")])
    second = ScriptedProvider([LLMResponse(content="ok2")])
    pool = ProviderPoolProvider(
        [
            ProviderPoolEntry(name="openrouter", provider=first),
            ProviderPoolEntry(name="deepseek", provider=second),
        ],
        strategy="failover",
        default_model="shared-model",
    )
    pool.generation = GenerationSettings(temperature=0.2, max_tokens=321, reasoning_effort="high")

    await pool.chat_with_retry(messages=[{"role": "user", "content": "hello"}])

    assert first.last_kwargs["temperature"] == 0.2
    assert first.last_kwargs["max_tokens"] == 321
    assert first.last_kwargs["reasoning_effort"] == "high"
    assert second.generation.max_tokens == 321


async def _collect_delta(deltas: list[str], delta: str) -> None:
    deltas.append(delta)
    await asyncio.sleep(0)
