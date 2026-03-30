"""Provider-pool wrapper for failover and round-robin routing."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from loguru import logger

from nanobot.providers.base import GenerationSettings, LLMProvider, LLMResponse


@dataclass(slots=True)
class ProviderPoolEntry:
    """Concrete provider instance plus optional model override."""

    name: str
    provider: LLMProvider
    model: str | None = None


class ProviderPoolProvider(LLMProvider):
    """Route requests across multiple providers."""

    def __init__(
        self,
        entries: list[ProviderPoolEntry],
        *,
        strategy: str = "failover",
        default_model: str | None = None,
    ) -> None:
        if not entries:
            raise ValueError("provider pool requires at least one target")
        self.entries = entries
        self.strategy = strategy
        self._default_model = default_model or entries[0].provider.get_default_model()
        self._selection_lock = asyncio.Lock()
        self._round_robin_index = 0
        self._generation = GenerationSettings()
        self._last_successful_entry = entries[0]
        super().__init__(api_key=entries[0].provider.api_key, api_base=entries[0].provider.api_base)
        self.default_model = self._default_model
        self.generation = entries[0].provider.generation

    @property
    def generation(self) -> GenerationSettings:
        return self._generation

    @generation.setter
    def generation(self, value: GenerationSettings) -> None:
        self._generation = value
        for entry in getattr(self, "entries", []):
            entry.provider.generation = value

    @property
    def default_model(self) -> str:
        return self._default_model

    @default_model.setter
    def default_model(self, value: str) -> None:
        self._default_model = value
        for entry in getattr(self, "entries", []):
            if entry.model is None and hasattr(entry.provider, "default_model"):
                entry.provider.default_model = value

    async def _ordered_entries(self) -> list[ProviderPoolEntry]:
        if self.strategy != "round_robin" or len(self.entries) <= 1:
            return list(self.entries)
        async with self._selection_lock:
            start = self._round_robin_index % len(self.entries)
            self._round_robin_index = (self._round_robin_index + 1) % len(self.entries)
        return [*self.entries[start:], *self.entries[:start]]

    def _resolve_model(self, entry: ProviderPoolEntry, requested_model: str | None) -> str:
        return entry.model or requested_model or self.default_model

    def _mark_success(self, entry: ProviderPoolEntry) -> None:
        self._last_successful_entry = entry
        self.api_key = entry.provider.api_key
        self.api_base = entry.provider.api_base

    async def _run_attempt(
        self,
        entry: ProviderPoolEntry,
        *,
        streaming: bool,
        use_retry: bool,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
        **kwargs: Any,
    ) -> tuple[LLMResponse, list[str]]:
        method_name = {
            (False, False): "chat",
            (False, True): "chat_with_retry",
            (True, False): "chat_stream",
            (True, True): "chat_stream_with_retry",
        }[(streaming, use_retry)]
        method = getattr(entry.provider, method_name)
        deltas: list[str] = []
        call_kwargs = dict(kwargs)

        if streaming:
            if on_content_delta is None or len(self.entries) <= 1:
                call_kwargs["on_content_delta"] = on_content_delta
            else:
                async def _buffer(delta: str) -> None:
                    deltas.append(delta)

                call_kwargs["on_content_delta"] = _buffer

        try:
            response = await method(**call_kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            response = LLMResponse(content=f"Error calling LLM: {exc}", finish_reason="error")
        return response, deltas

    async def _dispatch(
        self,
        *,
        streaming: bool,
        use_retry: bool,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        last_response: LLMResponse | None = None
        ordered = await self._ordered_entries()

        for index, entry in enumerate(ordered, start=1):
            resolved_model = self._resolve_model(entry, kwargs.get("model"))
            response, deltas = await self._run_attempt(
                entry,
                streaming=streaming,
                use_retry=use_retry,
                on_content_delta=on_content_delta,
                **{**kwargs, "model": resolved_model},
            )
            if response.finish_reason != "error":
                self._mark_success(entry)
                if streaming and on_content_delta and deltas:
                    for delta in deltas:
                        await on_content_delta(delta)
                return response

            last_response = response
            if index < len(ordered):
                logger.warning(
                    "Provider pool {} failed on {} (model: {}), trying next target: {}",
                    self.strategy,
                    entry.name,
                    resolved_model,
                    (response.content or "")[:160],
                )

        return last_response or LLMResponse(
            content="Error calling LLM: provider pool is empty",
            finish_reason="error",
        )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        return await self._dispatch(
            streaming=False,
            use_retry=False,
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            tool_choice=tool_choice,
        )

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        return await self._dispatch(
            streaming=True,
            use_retry=False,
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            tool_choice=tool_choice,
            on_content_delta=on_content_delta,
        )

    async def chat_with_retry(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: object = LLMProvider._SENTINEL,
        temperature: object = LLMProvider._SENTINEL,
        reasoning_effort: object = LLMProvider._SENTINEL,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        if max_tokens is self._SENTINEL:
            max_tokens = self.generation.max_tokens
        if temperature is self._SENTINEL:
            temperature = self.generation.temperature
        if reasoning_effort is self._SENTINEL:
            reasoning_effort = self.generation.reasoning_effort
        return await self._dispatch(
            streaming=False,
            use_retry=True,
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            tool_choice=tool_choice,
        )

    async def chat_stream_with_retry(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: object = LLMProvider._SENTINEL,
        temperature: object = LLMProvider._SENTINEL,
        reasoning_effort: object = LLMProvider._SENTINEL,
        tool_choice: str | dict[str, Any] | None = None,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        if max_tokens is self._SENTINEL:
            max_tokens = self.generation.max_tokens
        if temperature is self._SENTINEL:
            temperature = self.generation.temperature
        if reasoning_effort is self._SENTINEL:
            reasoning_effort = self.generation.reasoning_effort
        return await self._dispatch(
            streaming=True,
            use_retry=True,
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            tool_choice=tool_choice,
            on_content_delta=on_content_delta,
        )

    def get_default_model(self) -> str:
        return self.default_model
