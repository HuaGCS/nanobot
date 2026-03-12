from typing import Any

import pytest

from nanobot.agent.tools import web as web_module
from nanobot.agent.tools.web import WebSearchTool
from nanobot.config.schema import Config


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


@pytest.mark.asyncio
async def test_web_search_tool_brave_formats_results(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    payload = {
        "web": {
            "results": [
                {
                    "title": "Nanobot",
                    "url": "https://example.com/nanobot",
                    "description": "A lightweight personal AI assistant.",
                }
            ]
        }
    }

    class _FakeAsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.proxy = kwargs.get("proxy")

        async def __aenter__(self) -> "_FakeAsyncClient":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(
            self,
            url: str,
            *,
            params: dict[str, Any] | None = None,
            headers: dict[str, str] | None = None,
            timeout: float | None = None,
        ) -> _FakeResponse:
            calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
            return _FakeResponse(payload)

    monkeypatch.setattr(web_module.httpx, "AsyncClient", _FakeAsyncClient)

    tool = WebSearchTool(provider="brave", api_key="test-key")
    result = await tool.execute(query="nanobot", count=3)

    assert "Nanobot" in result
    assert "https://example.com/nanobot" in result
    assert "A lightweight personal AI assistant." in result
    assert calls == [
        {
            "url": "https://api.search.brave.com/res/v1/web/search",
            "params": {"q": "nanobot", "count": 3},
            "headers": {"Accept": "application/json", "X-Subscription-Token": "test-key"},
            "timeout": 10.0,
        }
    ]


@pytest.mark.asyncio
async def test_web_search_tool_searxng_formats_results(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    payload = {
        "results": [
            {
                "title": "Nanobot Docs",
                "url": "https://example.com/docs",
                "content": "Self-hosted search works.",
            }
        ]
    }

    class _FakeAsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.proxy = kwargs.get("proxy")

        async def __aenter__(self) -> "_FakeAsyncClient":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(
            self,
            url: str,
            *,
            params: dict[str, Any] | None = None,
            headers: dict[str, str] | None = None,
            timeout: float | None = None,
        ) -> _FakeResponse:
            calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
            return _FakeResponse(payload)

    monkeypatch.setattr(web_module.httpx, "AsyncClient", _FakeAsyncClient)

    tool = WebSearchTool(provider="searxng", base_url="http://localhost:8080")
    result = await tool.execute(query="nanobot", count=4)

    assert "Nanobot Docs" in result
    assert "https://example.com/docs" in result
    assert "Self-hosted search works." in result
    assert calls == [
        {
            "url": "http://localhost:8080/search",
            "params": {"q": "nanobot", "format": "json"},
            "headers": {"Accept": "application/json"},
            "timeout": 10.0,
        }
    ]


def test_web_search_tool_searxng_keeps_explicit_search_path() -> None:
    tool = WebSearchTool(provider="searxng", base_url="https://search.example.com/search/")

    assert tool._build_searxng_search_url() == "https://search.example.com/search"


def test_web_search_config_accepts_searxng_fields() -> None:
    config = Config.model_validate(
        {
            "tools": {
                "web": {
                    "search": {
                        "provider": "searxng",
                        "baseUrl": "http://localhost:8080",
                        "maxResults": 7,
                    }
                }
            }
        }
    )

    assert config.tools.web.search.provider == "searxng"
    assert config.tools.web.search.base_url == "http://localhost:8080"
    assert config.tools.web.search.max_results == 7


@pytest.mark.asyncio
async def test_web_search_tool_uses_env_provider_and_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    payload = {
        "results": [
            {
                "title": "Nanobot Env",
                "url": "https://example.com/env",
                "content": "Resolved from environment variables.",
            }
        ]
    }

    class _FakeAsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.proxy = kwargs.get("proxy")

        async def __aenter__(self) -> "_FakeAsyncClient":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(
            self,
            url: str,
            *,
            params: dict[str, Any] | None = None,
            headers: dict[str, str] | None = None,
            timeout: float | None = None,
        ) -> _FakeResponse:
            calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
            return _FakeResponse(payload)

    monkeypatch.setattr(web_module.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "searxng")
    monkeypatch.setenv("WEB_SEARCH_BASE_URL", "http://localhost:9090")

    tool = WebSearchTool()
    result = await tool.execute(query="nanobot", count=2)

    assert "Nanobot Env" in result
    assert calls == [
        {
            "url": "http://localhost:9090/search",
            "params": {"q": "nanobot", "format": "json"},
            "headers": {"Accept": "application/json"},
            "timeout": 10.0,
        }
    ]
