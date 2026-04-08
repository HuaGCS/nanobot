"""Mem0-backed user memory integration."""

from __future__ import annotations

import asyncio
import inspect
from importlib import import_module
from typing import Any
from urllib.parse import urlsplit

from nanobot.agent.memory_backends.base import UserMemoryBackend
from nanobot.agent.memory_models import MemoryCommitRequest, MemoryScope, ResolvedMemoryContext


class Mem0UserMemoryBackend(UserMemoryBackend):
    """Resolve prompt memory and shadow writes through Mem0 OSS."""

    _SEARCH_TOP_K = 5
    _MAX_CONTEXT_CHARS = 4_000
    _MAX_ITEM_CHARS = 500

    def __init__(self, config) -> None:
        self._config = config
        self._client = self._build_client()

    async def resolve_context(self, scope: MemoryScope) -> ResolvedMemoryContext:
        query = (scope.query or "").strip()
        user_id, agent_id = self._entity_scope(scope)

        if query:
            raw = await self._call_client(
                "search",
                query,
                user_id=user_id,
                agent_id=agent_id,
                limit=self._SEARCH_TOP_K,
            )
        else:
            raw = await self._call_client("get_all", user_id=user_id, agent_id=agent_id)

        memories = self._extract_memories(raw)
        return ResolvedMemoryContext(
            block=self._format_context(memories),
            source="mem0",
        )

    async def commit_turn(self, request: MemoryCommitRequest) -> None:
        messages = self._build_turn_messages(request)
        if not messages:
            return

        scope = request.scope
        user_id, agent_id = self._entity_scope(scope)
        metadata = dict(self._config.metadata or {})
        metadata.update(
            {
                "session_key": scope.session_key,
                "channel": scope.channel,
                "chat_id": scope.chat_id,
                "persona": scope.persona,
                "language": scope.language,
            }
        )
        if scope.sender_id:
            metadata["sender_id"] = scope.sender_id

        await self._call_client(
            "add",
            messages,
            user_id=user_id,
            agent_id=agent_id,
            metadata=metadata,
        )

    def _build_client(self) -> Any:
        try:
            module = import_module("mem0")
        except ImportError as exc:  # pragma: no cover - exercised in runtime envs
            raise RuntimeError(
                "mem0ai is not installed. Run: pip install nanobot-ai[mem0]"
            ) from exc

        async_cls = getattr(module, "AsyncMemory", None)
        config_payload = self._build_mem0_config_payload()

        if async_cls is not None:
            if config_payload:
                return self._build_client_from_config(async_cls, config_payload)
            return async_cls()

        memory_cls = getattr(module, "Memory", None)
        if memory_cls is None:  # pragma: no cover - defensive
            raise RuntimeError("mem0 package does not expose AsyncMemory or Memory")
        if config_payload:
            return self._build_client_from_config(memory_cls, config_payload)
        return memory_cls()

    def _build_client_from_config(self, client_cls: Any, config_payload: dict[str, Any]) -> Any:
        from_config = getattr(client_cls, "from_config", None)
        if from_config is None:  # pragma: no cover - defensive
            raise RuntimeError(f"{client_cls.__name__} does not expose from_config()")
        try:
            return from_config(config_dict=config_payload)
        except TypeError:
            return from_config(config_payload)

    async def _call_client(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        method = getattr(self._client, method_name)
        if inspect.iscoroutinefunction(method):
            return await method(*args, **kwargs)
        return await asyncio.to_thread(method, *args, **kwargs)

    def _build_mem0_config_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for section_name in ("llm", "embedder", "vector_store"):
            section = self._build_provider_section(section_name, getattr(self._config, section_name))
            if section:
                payload[section_name] = section
        return payload

    def _build_provider_section(self, section_name: str, provider_config) -> dict[str, Any]:
        config = dict(provider_config.config or {})
        if provider_config.api_key:
            config.setdefault("api_key", provider_config.api_key)
        if provider_config.model:
            config.setdefault("model", provider_config.model)
        if provider_config.headers:
            config.setdefault("headers", dict(provider_config.headers))
        if provider_config.url:
            config.setdefault("url", provider_config.url)
            if section_name in {"llm", "embedder"}:
                config.setdefault("base_url", provider_config.url)
                if provider_config.provider == "openai":
                    config.setdefault("openai_base_url", provider_config.url)
                elif provider_config.provider == "ollama":
                    config.setdefault("ollama_base_url", provider_config.url)
            elif section_name == "vector_store":
                parsed = urlsplit(provider_config.url)
                if parsed.hostname:
                    config.setdefault("host", parsed.hostname)
                if parsed.port:
                    config.setdefault("port", parsed.port)

        if not provider_config.provider and not config:
            return {}

        section: dict[str, Any] = {"config": config}
        if provider_config.provider:
            section["provider"] = provider_config.provider
        return section

    def _entity_scope(self, scope: MemoryScope) -> tuple[str, str]:
        return scope.sender_id or scope.session_key, scope.persona

    def _build_turn_messages(self, request: MemoryCommitRequest) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []

        inbound = self._stringify_content(request.inbound_content)
        if inbound:
            messages.append({"role": "user", "content": inbound})

        outbound = self._stringify_content(request.outbound_content)
        if outbound:
            messages.append({"role": "assistant", "content": outbound})

        if messages:
            return messages

        for message in request.persisted_messages:
            role = str(message.get("role", "")).strip()
            if role not in {"user", "assistant"}:
                continue
            content = self._stringify_content(message.get("content"))
            if content:
                messages.append({"role": role, "content": content})
        return messages

    def _stringify_content(self, content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str) and text.strip():
                        parts.append(text.strip())
                elif isinstance(block, str) and block.strip():
                    parts.append(block.strip())
            return "\n".join(parts).strip()
        return ""

    def _extract_memories(self, raw: Any) -> list[str]:
        if isinstance(raw, dict):
            items = raw.get("results")
            if items is None:
                items = raw.get("memories")
        else:
            items = raw

        if not isinstance(items, list):
            return []

        memories: list[str] = []
        seen: set[str] = set()
        for item in items:
            text = self._extract_memory_text(item)
            if not text:
                continue
            normalized = " ".join(text.split())
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            memories.append(normalized)
            if len(memories) >= self._SEARCH_TOP_K:
                break
        return memories

    def _extract_memory_text(self, item: Any) -> str:
        if isinstance(item, str):
            return item.strip()
        if not isinstance(item, dict):
            return ""
        for key in ("memory", "text", "content"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _format_context(self, memories: list[str]) -> str:
        if not memories:
            return ""

        lines = ["## Long-term Memory"]
        total_chars = len(lines[0])
        for memory in memories:
            if len(memory) > self._MAX_ITEM_CHARS:
                memory = memory[: self._MAX_ITEM_CHARS - 3].rstrip() + "..."
            line = f"- {memory}"
            if total_chars + len(line) + 1 > self._MAX_CONTEXT_CHARS:
                break
            lines.append(line)
            total_chars += len(line) + 1
        return "\n".join(lines) if len(lines) > 1 else ""
