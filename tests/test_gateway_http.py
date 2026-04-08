import json
import re
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from multidict import MultiDict, MultiDictProxy

import nanobot.gateway.admin as admin_mod
from nanobot.config.loader import save_config
from nanobot.config.schema import Config
from nanobot.gateway.http import create_http_app
from nanobot.gateway.runtime_status import GatewayRuntimeStatusTracker
from nanobot.heartbeat.service import HeartbeatService


def test_gateway_admin_config_parses_camel_case() -> None:
    config = Config.model_validate(
        {
            "gateway": {
                "admin": {
                    "enabled": True,
                    "authKey": "secret-key",
                }
            }
        }
    )

    assert config.gateway.admin.enabled is True
    assert config.gateway.admin.auth_key == "secret-key"


def test_gateway_status_config_parses_camel_case() -> None:
    config = Config.model_validate(
        {
            "gateway": {
                "status": {
                    "enabled": True,
                    "authKey": "status-secret",
                    "push": {
                        "enabled": True,
                        "mode": "guest",
                        "officeUrl": "https://office.example.com",
                        "joinKey": "join-secret",
                        "agentName": "nanobot-dev",
                        "timeout": 15,
                    },
                }
            }
        }
    )

    assert config.gateway.status.enabled is True
    assert config.gateway.status.auth_key == "status-secret"
    assert config.gateway.status.push.enabled is True
    assert config.gateway.status.push.mode == "guest"
    assert config.gateway.status.push.office_url == "https://office.example.com"
    assert config.gateway.status.push.join_key == "join-secret"
    assert config.gateway.status.push.agent_name == "nanobot-dev"
    assert config.gateway.status.push.timeout == 15


def test_gateway_status_main_push_config_allows_blank_join_key() -> None:
    config = Config.model_validate(
        {
            "gateway": {
                "status": {
                    "push": {
                        "enabled": True,
                        "mode": "main",
                        "officeUrl": "http://127.0.0.1:19000",
                        "timeout": 8,
                    }
                }
            }
        }
    )

    assert config.gateway.status.push.enabled is True
    assert config.gateway.status.push.mode == "main"
    assert config.gateway.status.push.join_key == ""
    assert config.gateway.status.push.office_url == "http://127.0.0.1:19000"


async def _call_route(
    app,
    method: str,
    path: str,
    *,
    cookies: dict[str, str] | None = None,
    data: dict[str, str] | list[tuple[str, str]] | None = None,
    headers: dict[str, str] | None = None,
):
    request_headers = dict(headers or {})
    if cookies:
        request_headers["Cookie"] = "; ".join(f"{key}={value}" for key, value in cookies.items())

    request = make_mocked_request(method, path, headers=request_headers, app=app)
    if data is not None:
        form = MultiDictProxy(MultiDict(data))

        async def _post():
            return form

        request.post = _post  # type: ignore[method-assign]

    match = await app.router.resolve(request)
    match.add_app(app)
    request._match_info = match  # type: ignore[attr-defined]
    try:
        return await match.handler(request)
    except web.HTTPException as exc:
        return exc


@pytest.mark.asyncio
async def test_gateway_health_route_exists() -> None:
    app = create_http_app()
    request = make_mocked_request("GET", "/healthz", app=app)
    match = await app.router.resolve(request)

    assert match.route.resource.canonical == "/healthz"


@pytest.mark.asyncio
async def test_gateway_public_route_is_not_registered() -> None:
    app = create_http_app()
    request = make_mocked_request("GET", "/public/hello.txt", app=app)
    match = await app.router.resolve(request)

    assert match.http_exception.status == 404
    assert [resource.canonical for resource in app.router.resources()] == ["/healthz"]


@pytest.mark.asyncio
async def test_gateway_admin_route_returns_404_when_disabled(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    workspace = tmp_path / "workspace"
    save_config(Config(), config_path)

    app = create_http_app(config_path=config_path, workspace=workspace)
    response = await _call_route(app, "GET", "/admin")

    assert response.status == 404


@pytest.mark.asyncio
async def test_gateway_status_route_returns_404_when_disabled(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    workspace = tmp_path / "workspace"
    save_config(Config(), config_path)

    app = create_http_app(config_path=config_path, workspace=workspace)
    response = await _call_route(app, "GET", "/status")

    assert response.status == 404


@pytest.mark.asyncio
async def test_gateway_status_route_returns_tracker_snapshot_and_requires_auth(tmp_path: Path) -> None:
    from nanobot.star_office import StarOfficeStatusTracker

    config_path = tmp_path / "config.json"
    workspace = tmp_path / "workspace"
    config = Config()
    config.gateway.status.enabled = True
    config.gateway.status.auth_key = "status-secret"
    save_config(config, config_path)

    tracker = StarOfficeStatusTracker()
    tracker.update(123, state="executing", detail="Running exec")

    app = create_http_app(
        config_path=config_path,
        workspace=workspace,
        star_office_tracker=tracker,
    )

    denied = await _call_route(app, "GET", "/status")
    assert denied.status == 401

    response = await _call_route(
        app,
        "GET",
        "/status",
        headers={"Authorization": "Bearer status-secret"},
    )
    assert response.status == 200

    payload = json.loads(response.text)
    assert payload["source"] == "nanobot"
    assert payload["state"] == "executing"
    assert payload["detail"] == "Running exec"
    assert payload["activeRuns"] == 1
    assert payload["updatedAt"]
    assert payload["updatedAtMs"] > 0


@pytest.mark.asyncio
async def test_gateway_status_route_renders_html_status_page_for_browser_requests(
    tmp_path: Path,
) -> None:
    from nanobot.star_office import StarOfficeStatusTracker

    config_path = tmp_path / "config.json"
    workspace = tmp_path / "workspace"
    config = Config()
    config.gateway.status.enabled = True
    save_config(config, config_path)

    star_tracker = StarOfficeStatusTracker()
    runtime_tracker = GatewayRuntimeStatusTracker(model="openrouter/sonnet")
    runtime_tracker.note_task_started(7, "整理最近的待办任务")
    runtime_tracker.note_task_finished(
        7,
        status="ok",
        response_preview="已经整理出最新任务清单。",
    )
    heartbeat = HeartbeatService(
        workspace=workspace,
        provider=MagicMock(),
        model="openrouter/sonnet",
        interval_s=600,
        enabled=True,
    )
    heartbeat._running = True
    heartbeat._set_status("ok", "最近一次 heartbeat 检测成功", checked=True)

    app = create_http_app(
        config_path=config_path,
        workspace=workspace,
        star_office_tracker=star_tracker,
        runtime_status_tracker=runtime_tracker,
        heartbeat_service=heartbeat,
    )

    response = await _call_route(
        app,
        "GET",
        "/status?format=html",
        headers={"Accept": "text/html"},
    )

    assert response.status == 200
    assert response.content_type == "text/html"
    assert "运行状态页" in response.text
    assert "正常运行" in response.text
    assert "连续运行时间" in response.text
    assert "整理最近的待办任务" in response.text
    assert "openrouter/sonnet" in response.text
    assert "最近一次成功" in response.text


@pytest.mark.asyncio
async def test_gateway_admin_uses_default_chinese_theme_and_visual_config_save(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    workspace = tmp_path / "workspace"
    config = Config()
    config.gateway.admin.enabled = True
    config.gateway.admin.auth_key = "secret-key"
    save_config(config, config_path)

    reload_calls: list[str] = []

    async def _reload_runtime() -> None:
        reload_calls.append("called")

    app = create_http_app(
        config_path=config_path,
        workspace=workspace,
        reload_runtime=_reload_runtime,
    )

    response = await _call_route(app, "GET", "/admin")
    assert response.status == 302
    assert response.headers["Location"].startswith("/admin/login")

    bad_login = await _call_route(
        app,
        "POST",
        "/admin/login",
        data={"auth_key": "wrong", "next": "/admin"},
    )
    assert bad_login.status == 200
    assert "授权密钥错误" in bad_login.text

    login = await _call_route(
        app,
        "POST",
        "/admin/login",
        data={"auth_key": "secret-key", "next": "/admin"},
    )
    assert login.status == 302
    assert login.headers["Location"] == "/admin"
    cookie = login.cookies["nanobot_admin_session"].value

    config_page = await _call_route(
        app,
        "GET",
        "/admin/config",
        cookies={"nanobot_admin_session": cookie},
    )
    assert config_page.status == 200
    assert "配置编辑" in config_page.text
    assert "/admin/commands" in config_page.text
    assert 'name="agents_defaults_model"' in config_page.text
    assert 'name="agents_defaults_provider_pool_strategy"' in config_page.text
    assert 'name="agents_defaults_provider_pool_targets_provider"' in config_page.text
    assert 'name="agents_defaults_provider_pool_targets_model"' in config_page.text
    assert 'name="providers_openrouter_api_key"' in config_page.text
    assert 'name="providers_custom_extra_headers"' in config_page.text
    assert 'name="providers_ollama_api_base"' in config_page.text
    assert 'name="gateway_status_enabled"' in config_page.text
    assert 'name="gateway_status_auth_key"' in config_page.text
    assert 'name="gateway_status_push_enabled"' in config_page.text
    assert 'name="gateway_status_push_mode"' in config_page.text
    assert 'name="gateway_status_push_office_url"' in config_page.text
    assert 'name="gateway_status_push_join_key"' in config_page.text
    assert 'name="gateway_status_push_agent_name"' in config_page.text
    assert 'name="gateway_status_push_timeout"' in config_page.text
    assert "常用渠道凭据" in config_page.text
    assert 'data-channel-group="whatsapp"' in config_page.text
    assert 'data-channel-group="telegram"' in config_page.text
    assert 'data-channel-group="matrix"' in config_page.text
    assert 'data-channel-group="weixin"' in config_page.text
    assert 'name="channels_whatsapp_bridge_url"' in config_page.text
    assert 'name="channels_whatsapp_bridge_token"' in config_page.text
    assert 'name="channels_telegram_token"' in config_page.text
    assert 'name="channels_telegram_proxy"' in config_page.text
    assert 'name="channels_matrix_homeserver"' in config_page.text
    assert 'name="channels_weixin_allow_from"' in config_page.text
    assert 'name="channels_weixin_token"' in config_page.text
    assert 'name="channels_weixin_poll_timeout"' in config_page.text
    assert 'name="tools_exec_enable"' in config_page.text
    assert 'name="tools_exec_timeout"' in config_page.text
    assert 'name="tools_exec_path_append"' in config_page.text
    assert 'data-provider-group="openrouter"' in config_page.text
    assert 'data-provider-group="custom"' in config_page.text
    assert 'data-provider-pool-editor' in config_page.text
    assert 'data-provider-pool-move-up' in config_page.text
    assert 'data-provider-pool-move-down' in config_page.text
    assert 'name="memory_user_backend"' in config_page.text
    assert 'name="memory_user_mem0_llm_api_key"' in config_page.text
    assert 'name="memory_user_mem0_llm_headers"' in config_page.text
    assert 'name="memory_user_mem0_metadata"' in config_page.text
    assert 'name="tools_mcp_memorix_enabled"' in config_page.text
    assert "tooltip-anchor" in config_page.text
    assert "默认工作区路径" in config_page.text
    assert "Mem0 用户记忆" in config_page.text
    assert "Memorix MCP" in config_page.text
    assert "Star Office 推送" in config_page.text
    assert "Shell 执行" in config_page.text
    assert "可热重载" in config_page.text
    assert "需重启" in config_page.text
    assert 'agents.defaults.workspace</span><span class="pill hot">可热重载</span>' in config_page.text
    assert 'agents.defaults.provider</span><span class="pill restart">需重启</span>' in config_page.text
    assert "color-scheme: light dark" in config_page.text
    assert "@media (prefers-color-scheme: dark)" in config_page.text

    commands_page = await _call_route(
        app,
        "GET",
        "/admin/commands",
        cookies={"nanobot_admin_session": cookie},
    )
    assert commands_page.status == 200
    assert "命令总览" in commands_page.text
    assert "命令列表" in commands_page.text
    assert "/language" in commands_page.text
    assert "/skill update" in commands_page.text
    assert "/restart" in commands_page.text
    assert 'data-command-browser' in commands_page.text
    assert 'data-command-target="command-help"' in commands_page.text
    assert 'data-command-panel="command-help"' in commands_page.text

    save_resp = await _call_route(
        app,
        "POST",
        "/admin/config",
        cookies={"nanobot_admin_session": cookie},
        data=[
            ("mode", "visual"),
            ("__bool_fields", "tools_mcp_memorix_enabled"),
            ("__bool_fields", "memory_user_shadow_write_mem0"),
            ("__bool_fields", "gateway_status_enabled"),
            ("__bool_fields", "gateway_status_push_enabled"),
            ("__bool_fields", "channels_telegram_enabled"),
            ("__bool_fields", "channels_weixin_enabled"),
            ("__bool_fields", "tools_exec_enable"),
            ("memory_user_backend", "mem0"),
            ("memory_user_shadow_write_mem0", "1"),
            ("gateway_status_enabled", "1"),
            ("gateway_status_auth_key", "status-secret"),
            ("gateway_status_push_enabled", "1"),
            ("gateway_status_push_mode", "guest"),
            ("gateway_status_push_office_url", "https://office.example.com"),
            ("gateway_status_push_join_key", "join-secret"),
            ("gateway_status_push_agent_name", "nanobot-dev"),
            ("gateway_status_push_timeout", "12"),
            ("channels_whatsapp_bridge_url", "ws://localhost:3301"),
            ("channels_whatsapp_bridge_token", "wa-bridge-token"),
            ("channels_telegram_enabled", "1"),
            ("channels_telegram_token", "tg-admin-token"),
            ("channels_telegram_proxy", "socks5://127.0.0.1:1080"),
            ("channels_matrix_homeserver", "https://matrix.example.com"),
            ("channels_matrix_user_id", "@nanobot:example.com"),
            ("channels_weixin_enabled", "1"),
            ("channels_weixin_allow_from", "wxid_alpha, wxid_beta"),
            ("channels_weixin_token", "wx-token"),
            ("channels_weixin_route_tag", "blue-route"),
            ("channels_weixin_state_dir", "/tmp/nanobot-weixin"),
            ("channels_weixin_poll_timeout", "42"),
            ("memory_user_mem0_llm_provider", "openai"),
            ("memory_user_mem0_llm_api_key", "mem0-llm-key"),
            ("memory_user_mem0_llm_url", "https://api.mem0.ai/v1"),
            ("memory_user_mem0_llm_model", "gpt-4.1-mini"),
            ("memory_user_mem0_llm_headers", '{"Authorization":"Bearer llm-header"}'),
            ("memory_user_mem0_llm_config", '{"temperature":0.1}'),
            ("memory_user_mem0_embedder_provider", "openai"),
            ("memory_user_mem0_embedder_api_key", "mem0-embed-key"),
            ("memory_user_mem0_embedder_url", "https://embed.mem0.ai/v1"),
            ("memory_user_mem0_embedder_model", "text-embedding-3-small"),
            ("memory_user_mem0_embedder_headers", '{"X-Embed":"1"}'),
            ("memory_user_mem0_embedder_config", '{"dimensions":1536}'),
            ("memory_user_mem0_vector_store_provider", "qdrant"),
            ("memory_user_mem0_vector_store_api_key", "mem0-vs-key"),
            ("memory_user_mem0_vector_store_url", "https://qdrant.mem0.ai"),
            ("memory_user_mem0_vector_store_headers", '{"api-key":"vector-header"}'),
            ("memory_user_mem0_vector_store_config", '{"collectionName":"nanobot_user_memory"}'),
            ("memory_user_mem0_metadata", '{"tenant":"paid-mem0","env":"prod"}'),
            ("tools_mcp_memorix_enabled", "1"),
            ("tools_mcp_memorix_type", "streamableHttp"),
            ("tools_mcp_memorix_url", "http://127.0.0.1:3211/mcp"),
            ("tools_mcp_memorix_tool_timeout", "75"),
            ("agents_defaults_model", "openai/gpt-4.1"),
            ("agents_defaults_provider_pool_strategy", "failover"),
            ("agents_defaults_provider_pool_targets_provider", "openrouter"),
            ("agents_defaults_provider_pool_targets_model", "openai/gpt-4.1"),
            ("agents_defaults_provider_pool_targets_provider", "deepseek"),
            ("agents_defaults_provider_pool_targets_model", "deepseek-chat"),
            ("providers_openrouter_api_key", "sk-or-v1-admin"),
            ("providers_openrouter_api_base", "https://openrouter.ai/api/v1"),
            ("providers_deepseek_api_key", "sk-deepseek-admin"),
            ("providers_deepseek_api_base", "https://api.deepseek.com"),
            ("providers_custom_api_key", "custom-admin-key"),
            ("providers_custom_api_base", "https://custom.example.com/v1"),
            ("providers_custom_extra_headers", '{"APP-Code":"admin-demo"}'),
            ("providers_ollama_api_base", "http://localhost:11434/v1"),
            ("providers_vllm_api_base", "http://localhost:8000"),
            ("tools_exec_timeout", "90"),
            ("tools_exec_path_append", "/usr/local/bin:/usr/sbin"),
            ("channels_voice_reply_provider", "sovits"),
            ("channels_voice_reply_sovits_api_url", "http://127.0.0.1:9880"),
            ("gateway_admin_auth_key", "secret-key"),
        ],
    )
    assert save_resp.status == 302
    assert save_resp.headers["Location"] == "/admin/config?saved=1&reloaded=1"
    assert reload_calls == ["called"]

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["agents"]["defaults"]["model"] == "openai/gpt-4.1"
    assert saved["channels"]["voiceReply"]["provider"] == "sovits"
    assert saved["channels"]["voiceReply"]["sovitsApiUrl"] == "http://127.0.0.1:9880"
    assert saved["memory"]["user"]["backend"] == "mem0"
    assert saved["memory"]["user"]["shadowWriteMem0"] is True
    assert saved["memory"]["user"]["mem0"]["llm"]["provider"] == "openai"
    assert saved["memory"]["user"]["mem0"]["llm"]["apiKey"] == "mem0-llm-key"
    assert saved["memory"]["user"]["mem0"]["llm"]["url"] == "https://api.mem0.ai/v1"
    assert saved["memory"]["user"]["mem0"]["llm"]["model"] == "gpt-4.1-mini"
    assert saved["memory"]["user"]["mem0"]["llm"]["headers"] == {
        "Authorization": "Bearer llm-header"
    }
    assert saved["memory"]["user"]["mem0"]["llm"]["config"] == {"temperature": 0.1}
    assert saved["memory"]["user"]["mem0"]["embedder"]["apiKey"] == "mem0-embed-key"
    assert saved["memory"]["user"]["mem0"]["embedder"]["url"] == "https://embed.mem0.ai/v1"
    assert saved["memory"]["user"]["mem0"]["embedder"]["headers"] == {"X-Embed": "1"}
    assert saved["memory"]["user"]["mem0"]["embedder"]["config"] == {"dimensions": 1536}
    assert saved["memory"]["user"]["mem0"]["vectorStore"]["provider"] == "qdrant"
    assert saved["memory"]["user"]["mem0"]["vectorStore"]["apiKey"] == "mem0-vs-key"
    assert saved["memory"]["user"]["mem0"]["vectorStore"]["url"] == "https://qdrant.mem0.ai"
    assert saved["memory"]["user"]["mem0"]["vectorStore"]["headers"] == {
        "api-key": "vector-header"
    }
    assert saved["memory"]["user"]["mem0"]["vectorStore"]["config"] == {
        "collectionName": "nanobot_user_memory"
    }
    assert saved["memory"]["user"]["mem0"]["metadata"] == {"tenant": "paid-mem0", "env": "prod"}
    assert saved["tools"]["mcpServers"]["memorix"]["type"] == "streamableHttp"
    assert saved["tools"]["mcpServers"]["memorix"]["command"] == "memorix"
    assert saved["gateway"]["status"]["enabled"] is True
    assert saved["gateway"]["status"]["authKey"] == "status-secret"
    assert saved["gateway"]["status"]["push"]["enabled"] is True
    assert saved["gateway"]["status"]["push"]["mode"] == "guest"
    assert saved["gateway"]["status"]["push"]["officeUrl"] == "https://office.example.com"
    assert saved["gateway"]["status"]["push"]["joinKey"] == "join-secret"
    assert saved["gateway"]["status"]["push"]["agentName"] == "nanobot-dev"
    assert saved["gateway"]["status"]["push"]["timeout"] == 12
    assert saved["channels"]["whatsapp"]["bridgeUrl"] == "ws://localhost:3301"
    assert saved["channels"]["whatsapp"]["bridgeToken"] == "wa-bridge-token"
    assert saved["channels"]["telegram"]["enabled"] is True
    assert saved["channels"]["telegram"]["token"] == "tg-admin-token"
    assert saved["channels"]["telegram"]["proxy"] == "socks5://127.0.0.1:1080"
    assert saved["channels"]["matrix"]["homeserver"] == "https://matrix.example.com"
    assert saved["channels"]["matrix"]["userId"] == "@nanobot:example.com"
    assert saved["channels"]["weixin"]["enabled"] is True
    assert saved["channels"]["weixin"]["allowFrom"] == ["wxid_alpha", "wxid_beta"]
    assert saved["channels"]["weixin"]["token"] == "wx-token"
    assert saved["channels"]["weixin"]["routeTag"] == "blue-route"
    assert saved["channels"]["weixin"]["stateDir"] == "/tmp/nanobot-weixin"
    assert saved["channels"]["weixin"]["pollTimeout"] == 42
    assert saved["tools"]["exec"]["enable"] is False
    assert saved["tools"]["exec"]["timeout"] == 90
    assert saved["tools"]["exec"]["pathAppend"] == "/usr/local/bin:/usr/sbin"
    assert saved["tools"]["mcpServers"]["memorix"]["args"] == ["serve"]
    assert saved["tools"]["mcpServers"]["memorix"]["url"] == "http://127.0.0.1:3211/mcp"
    assert saved["tools"]["mcpServers"]["memorix"]["toolTimeout"] == 75
    assert saved["providers"]["openrouter"]["apiKey"] == "sk-or-v1-admin"
    assert saved["providers"]["openrouter"]["apiBase"] == "https://openrouter.ai/api/v1"
    assert saved["providers"]["deepseek"]["apiKey"] == "sk-deepseek-admin"
    assert saved["providers"]["deepseek"]["apiBase"] == "https://api.deepseek.com"
    assert saved["providers"]["custom"]["apiKey"] == "custom-admin-key"
    assert saved["providers"]["custom"]["apiBase"] == "https://custom.example.com/v1"
    assert saved["providers"]["custom"]["extraHeaders"] == {"APP-Code": "admin-demo"}
    assert saved["providers"]["ollama"]["apiBase"] == "http://localhost:11434/v1"
    assert saved["providers"]["vllm"]["apiBase"] == "http://localhost:8000"
    assert saved["agents"]["defaults"]["providerPool"]["strategy"] == "failover"
    assert saved["agents"]["defaults"]["providerPool"]["targets"] == [
        {"provider": "openrouter", "model": "openai/gpt-4.1"},
        {"provider": "deepseek", "model": "deepseek-chat"},
    ]

    overview_page = await _call_route(
        app,
        "GET",
        "/admin",
        cookies={"nanobot_admin_session": cookie},
    )
    assert overview_page.status == 200
    assert "providerPool/failover" in overview_page.text
    assert "openrouter, deepseek" in overview_page.text


@pytest.mark.asyncio
async def test_gateway_admin_language_switch_and_raw_json_editor(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    workspace = tmp_path / "workspace"
    config = Config()
    config.gateway.admin.enabled = True
    config.gateway.admin.auth_key = "secret-key"
    save_config(config, config_path)

    app = create_http_app(config_path=config_path, workspace=workspace)

    login_page = await _call_route(app, "GET", "/admin/login?lang=en")
    assert login_page.status == 200
    assert "Admin Login" in login_page.text
    assert login_page.cookies["nanobot_admin_lang"].value == "en"

    login = await _call_route(
        app,
        "POST",
        "/admin/login",
        cookies={"nanobot_admin_lang": "en"},
        data={"auth_key": "secret-key", "next": "/admin"},
    )
    assert login.status == 302
    session_cookie = login.cookies["nanobot_admin_session"].value

    config_page = await _call_route(
        app,
        "GET",
        "/admin/config",
        cookies={"nanobot_admin_session": session_cookie, "nanobot_admin_lang": "en"},
    )
    assert config_page.status == 200
    assert "Config Editor" in config_page.text
    assert "/admin/commands" in config_page.text
    assert "Advanced JSON editor" in config_page.text
    assert "Default workspace path" in config_page.text
    assert "Common channel credentials" in config_page.text
    assert 'name="agents_defaults_provider_pool_strategy"' in config_page.text
    assert 'name="agents_defaults_provider_pool_targets_provider"' in config_page.text
    assert 'name="agents_defaults_provider_pool_targets_model"' in config_page.text
    assert 'name="providers_openrouter_api_key"' in config_page.text
    assert 'name="providers_custom_extra_headers"' in config_page.text
    assert 'name="gateway_status_push_mode"' in config_page.text
    assert 'data-provider-group="openrouter"' in config_page.text
    assert 'data-provider-group="custom"' in config_page.text
    assert 'data-channel-group="telegram"' in config_page.text
    assert 'data-channel-group="weixin"' in config_page.text
    assert 'name="channels_telegram_token"' in config_page.text
    assert 'name="channels_weixin_allow_from"' in config_page.text
    assert 'data-provider-pool-move-up' in config_page.text
    assert 'data-provider-pool-move-down' in config_page.text
    assert 'name="memory_user_backend"' in config_page.text
    assert "Mem0 User Memory" in config_page.text
    assert 'name="memory_user_mem0_llm_api_key"' in config_page.text
    assert 'name="memory_user_mem0_llm_headers"' in config_page.text
    assert 'name="memory_user_mem0_metadata"' in config_page.text
    assert "Memorix MCP" in config_page.text
    assert 'name="tools_mcp_memorix_enabled"' in config_page.text
    assert "Hot reload" in config_page.text
    assert "Requires restart" in config_page.text
    assert 'agents.defaults.workspace</span><span class="pill hot">Hot reload</span>' in config_page.text
    assert 'agents.defaults.provider</span><span class="pill restart">Requires restart</span>' in config_page.text

    commands_page = await _call_route(
        app,
        "GET",
        "/admin/commands",
        cookies={"nanobot_admin_session": session_cookie, "nanobot_admin_lang": "en"},
    )
    assert commands_page.status == 200
    assert "Command Reference" in commands_page.text
    assert "Command list" in commands_page.text
    assert "/language" in commands_page.text
    assert "/mcp list" in commands_page.text
    assert "Supported forms" in commands_page.text
    assert 'data-command-browser' in commands_page.text
    assert 'data-command-target="command-help"' in commands_page.text
    assert 'data-command-panel="command-help"' in commands_page.text

    updated = json.loads(config_path.read_text(encoding="utf-8"))
    updated["agents"]["defaults"]["model"] = "openai/gpt-5-mini"
    updated["gateway"]["host"] = "127.0.0.1"
    save_resp = await _call_route(
        app,
        "POST",
        "/admin/config",
        cookies={"nanobot_admin_session": session_cookie, "nanobot_admin_lang": "en"},
        data={
            "mode": "raw",
            "config_json": json.dumps(updated, ensure_ascii=False, indent=2),
        },
    )
    assert save_resp.status == 302
    assert save_resp.headers["Location"] == "/admin/config?saved=1"

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["agents"]["defaults"]["model"] == "openai/gpt-5-mini"
    assert saved["gateway"]["host"] == "127.0.0.1"


@pytest.mark.asyncio
async def test_gateway_admin_provider_group_summaries_are_compact_and_safe(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    workspace = tmp_path / "workspace"
    config = Config()
    config.gateway.admin.enabled = True
    config.gateway.admin.auth_key = "secret-key"
    config.providers.openrouter.api_key = "sk-or-v1-summary"
    config.providers.openrouter.api_base = "https://openrouter.ai/api/v1"
    config.providers.custom.api_key = "custom-summary-key"
    config.providers.custom.api_base = "https://custom.example.com/v1"
    config.providers.custom.extra_headers = {"APP-Code": "demo", "X-Tenant": "team-a"}
    config.providers.ollama.api_base = "http://localhost:11434/v1"
    save_config(config, config_path)

    app = create_http_app(config_path=config_path, workspace=workspace)
    login = await _call_route(
        app,
        "POST",
        "/admin/login",
        cookies={"nanobot_admin_lang": "en"},
        data={"auth_key": "secret-key", "next": "/admin/config"},
    )
    assert login.status == 302
    session_cookie = login.cookies["nanobot_admin_session"].value

    config_page = await _call_route(
        app,
        "GET",
        "/admin/config",
        cookies={"nanobot_admin_session": session_cookie, "nanobot_admin_lang": "en"},
    )
    assert config_page.status == 200
    assert 'data-provider-group-meta="openrouter"' in config_page.text
    assert 'data-provider-group-meta="custom"' in config_page.text
    assert 'data-provider-group-meta="ollama"' in config_page.text

    openrouter_summary = re.search(
        r'data-provider-group="openrouter"[^>]*>\s*<summary>(.*?)</summary>',
        config_page.text,
        re.S,
    )
    assert openrouter_summary is not None
    assert "API key" in openrouter_summary.group(1)
    assert "openrouter.ai/api/v1" in openrouter_summary.group(1)
    assert "sk-or-v1-summary" not in openrouter_summary.group(1)

    custom_summary = re.search(
        r'data-provider-group="custom"[^>]*>\s*<summary>(.*?)</summary>',
        config_page.text,
        re.S,
    )
    assert custom_summary is not None
    assert "API key" in custom_summary.group(1)
    assert "custom.example.com/v1" in custom_summary.group(1)
    assert "2 headers" in custom_summary.group(1)
    assert "custom-summary-key" not in custom_summary.group(1)

    ollama_summary = re.search(
        r'data-provider-group="ollama"[^>]*>\s*<summary>(.*?)</summary>',
        config_page.text,
        re.S,
    )
    assert ollama_summary is not None
    assert "localhost:11434/v1" in ollama_summary.group(1)


@pytest.mark.asyncio
async def test_gateway_admin_visual_empty_provider_pool_targets_remove_pool(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    workspace = tmp_path / "workspace"
    config = Config.model_validate(
        {
            "gateway": {"admin": {"enabled": True, "authKey": "secret-key"}},
            "agents": {
                "defaults": {
                    "providerPool": {
                        "strategy": "failover",
                        "targets": [
                            {"provider": "openrouter", "model": "openai/gpt-4o-mini"},
                        ],
                    }
                }
            },
        }
    )
    save_config(config, config_path)

    app = create_http_app(config_path=config_path, workspace=workspace)
    login = await _call_route(
        app,
        "POST",
        "/admin/login",
        data={"auth_key": "secret-key", "next": "/admin"},
    )
    cookie = login.cookies["nanobot_admin_session"].value

    save_resp = await _call_route(
        app,
        "POST",
        "/admin/config",
        cookies={"nanobot_admin_session": cookie},
        data=[
            ("mode", "visual"),
            ("agents_defaults_provider_pool_strategy", "failover"),
            ("agents_defaults_provider_pool_targets_provider", ""),
            ("agents_defaults_provider_pool_targets_model", ""),
            ("gateway_admin_auth_key", "secret-key"),
        ],
    )
    assert save_resp.status == 302

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert "providerPool" not in saved["agents"]["defaults"]


@pytest.mark.asyncio
async def test_gateway_admin_visual_provider_pool_row_requires_provider(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    workspace = tmp_path / "workspace"
    config = Config()
    config.gateway.admin.enabled = True
    config.gateway.admin.auth_key = "secret-key"
    save_config(config, config_path)

    app = create_http_app(config_path=config_path, workspace=workspace)
    login = await _call_route(
        app,
        "POST",
        "/admin/login",
        data={"auth_key": "secret-key", "next": "/admin"},
    )
    cookie = login.cookies["nanobot_admin_session"].value

    response = await _call_route(
        app,
        "POST",
        "/admin/config",
        cookies={"nanobot_admin_session": cookie},
        data=[
            ("mode", "visual"),
            ("agents_defaults_provider_pool_strategy", "failover"),
            ("agents_defaults_provider_pool_targets_provider", ""),
            ("agents_defaults_provider_pool_targets_model", "deepseek-chat"),
            ("gateway_admin_auth_key", "secret-key"),
        ],
    )

    assert response.status == 200
    assert "providerPool target" in response.text

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert "providerPool" not in saved["agents"]["defaults"]


@pytest.mark.asyncio
async def test_gateway_admin_channel_cards_preserve_multi_instance_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    workspace = tmp_path / "workspace"
    config = Config.model_validate(
        {
            "gateway": {"admin": {"enabled": True, "authKey": "secret-key"}},
            "channels": {
                "telegram": {
                    "enabled": True,
                    "instances": [
                        {
                            "name": "primary",
                            "enabled": True,
                            "token": "instance-token",
                            "proxy": "socks5://127.0.0.1:7890",
                        }
                    ],
                }
            },
        }
    )
    save_config(config, config_path)

    app = create_http_app(config_path=config_path, workspace=workspace)
    login = await _call_route(
        app,
        "POST",
        "/admin/login",
        cookies={"nanobot_admin_lang": "en"},
        data={"auth_key": "secret-key", "next": "/admin/config"},
    )
    assert login.status == 302
    cookie = login.cookies["nanobot_admin_session"].value

    config_page = await _call_route(
        app,
        "GET",
        "/admin/config",
        cookies={"nanobot_admin_session": cookie, "nanobot_admin_lang": "en"},
    )
    assert config_page.status == 200
    assert 'data-channel-group="telegram"' in config_page.text
    telegram_group = re.search(
        r'<details class="provider-group" data-channel-group="telegram"[^>]*>(.*?)</details>',
        config_page.text,
        re.S,
    )
    assert telegram_group is not None
    assert "Multi-instance" in telegram_group.group(1)
    assert "channels.telegram.instances" in telegram_group.group(1)
    assert 'name="channels_telegram_token"' not in telegram_group.group(1)

    save_resp = await _call_route(
        app,
        "POST",
        "/admin/config",
        cookies={"nanobot_admin_session": cookie, "nanobot_admin_lang": "en"},
        data=[
            ("mode", "visual"),
            ("__bool_fields", "channels_telegram_enabled"),
            ("channels_telegram_enabled", "1"),
            ("channels_telegram_token", "should-not-apply"),
            ("gateway_admin_auth_key", "secret-key"),
        ],
    )
    assert save_resp.status == 302

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["channels"]["telegram"]["enabled"] is True
    assert "token" not in saved["channels"]["telegram"]
    assert saved["channels"]["telegram"]["instances"][0]["token"] == "instance-token"
    assert saved["channels"]["telegram"]["instances"][0]["proxy"] == "socks5://127.0.0.1:7890"


@pytest.mark.asyncio
async def test_gateway_admin_visual_main_push_mode_allows_blank_join_key(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    workspace = tmp_path / "workspace"
    config = Config()
    config.gateway.admin.enabled = True
    config.gateway.admin.auth_key = "secret-key"
    save_config(config, config_path)

    app = create_http_app(config_path=config_path, workspace=workspace)
    login = await _call_route(
        app,
        "POST",
        "/admin/login",
        data={"auth_key": "secret-key", "next": "/admin"},
    )
    cookie = login.cookies["nanobot_admin_session"].value

    save_resp = await _call_route(
        app,
        "POST",
        "/admin/config",
        cookies={"nanobot_admin_session": cookie},
        data=[
            ("mode", "visual"),
            ("__bool_fields", "gateway_status_push_enabled"),
            ("gateway_status_push_enabled", "1"),
            ("gateway_status_push_mode", "main"),
            ("gateway_status_push_office_url", "http://127.0.0.1:19000"),
            ("gateway_status_push_join_key", ""),
            ("gateway_status_push_agent_name", "nanobot-main"),
            ("gateway_status_push_timeout", "9"),
            ("gateway_admin_auth_key", "secret-key"),
        ],
    )
    assert save_resp.status == 302

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["gateway"]["status"]["push"]["enabled"] is True
    assert saved["gateway"]["status"]["push"]["mode"] == "main"
    assert saved["gateway"]["status"]["push"]["officeUrl"] == "http://127.0.0.1:19000"
    assert saved["gateway"]["status"]["push"]["joinKey"] == ""
    assert saved["gateway"]["status"]["push"]["agentName"] == "nanobot-main"
    assert saved["gateway"]["status"]["push"]["timeout"] == 9


@pytest.mark.asyncio
async def test_gateway_admin_weixin_login_page_starts_and_renders_pending_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    workspace = tmp_path / "workspace"
    config = Config()
    config.gateway.admin.enabled = True
    config.gateway.admin.auth_key = "secret-key"
    save_config(config, config_path)

    async def _fake_start(_request, *, force: bool):
        assert force is False
        now = time.time()
        return admin_mod.WeixinAdminLoginSession(
            session_id="weixin-session",
            qrcode_id="qr-1",
            scan_url="weixin://qr/demo",
            qr_image_data_url="data:image/svg+xml;base64,qr-demo",
            poll_base_url="https://ilinkai.weixin.qq.com",
            started_at=now,
            updated_at=now,
        )

    async def _fake_advance(_request, session):
        return session

    monkeypatch.setattr(admin_mod, "_start_weixin_login_session", _fake_start)
    monkeypatch.setattr(admin_mod, "_advance_weixin_login_session", _fake_advance)

    app = create_http_app(config_path=config_path, workspace=workspace)
    login = await _call_route(
        app,
        "POST",
        "/admin/login",
        cookies={"nanobot_admin_lang": "en"},
        data={"auth_key": "secret-key", "next": "/admin/weixin"},
    )
    cookie = login.cookies["nanobot_admin_session"].value

    start_resp = await _call_route(
        app,
        "POST",
        "/admin/weixin/start",
        cookies={"nanobot_admin_session": cookie, "nanobot_admin_lang": "en"},
        data={},
    )
    assert start_resp.status == 302
    assert start_resp.headers["Location"] == "/admin/weixin?session=weixin-session"

    page = await _call_route(
        app,
        "GET",
        "/admin/weixin?session=weixin-session",
        cookies={"nanobot_admin_session": cookie, "nanobot_admin_lang": "en"},
    )
    assert page.status == 200
    assert "Weixin QR Login" in page.text
    assert "Saved channel state" in page.text
    assert 'src="data:image/svg+xml;base64,qr-demo"' in page.text
    assert "weixin://qr/demo" in page.text
    assert "Stop polling" in page.text
    assert 'href="/admin/weixin"' in page.text


@pytest.mark.asyncio
async def test_gateway_admin_weixin_login_page_handles_confirm_and_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    workspace = tmp_path / "workspace"
    config = Config()
    config.gateway.admin.enabled = True
    config.gateway.admin.auth_key = "secret-key"
    save_config(config, config_path)

    async def _fake_advance(_request, session):
        session.status = "confirmed"
        session.bot_id = "bot-123"
        session.user_id = "wx-user"
        return session

    monkeypatch.setattr(admin_mod, "_advance_weixin_login_session", _fake_advance)

    app = create_http_app(config_path=config_path, workspace=workspace)
    app[admin_mod._ADMIN_WEIXIN_LOGIN_SESSIONS_KEY] = {
        "weixin-session": admin_mod.WeixinAdminLoginSession(
            session_id="weixin-session",
            qrcode_id="qr-1",
            scan_url="weixin://qr/demo",
            qr_image_data_url=None,
            poll_base_url="https://ilinkai.weixin.qq.com",
            started_at=time.time(),
            updated_at=time.time(),
        )
    }

    login = await _call_route(
        app,
        "POST",
        "/admin/login",
        cookies={"nanobot_admin_lang": "en"},
        data={"auth_key": "secret-key", "next": "/admin/weixin"},
    )
    cookie = login.cookies["nanobot_admin_session"].value

    page = await _call_route(
        app,
        "GET",
        "/admin/weixin?session=weixin-session",
        cookies={"nanobot_admin_session": cookie, "nanobot_admin_lang": "en"},
    )
    assert page.status == 200
    assert "Weixin login confirmed" in page.text
    assert "bot-123" in page.text
    assert "wx-user" in page.text

    cancel_resp = await _call_route(
        app,
        "POST",
        "/admin/weixin/cancel",
        cookies={"nanobot_admin_session": cookie, "nanobot_admin_lang": "en"},
        data={"session": "weixin-session"},
    )
    assert cancel_resp.status == 302
    assert cancel_resp.headers["Location"] == "/admin/weixin?cancelled=1"
    assert app[admin_mod._ADMIN_WEIXIN_LOGIN_SESSIONS_KEY] == {}


@pytest.mark.asyncio
async def test_gateway_admin_persona_editor_updates_files(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    workspace = tmp_path / "workspace"
    config = Config()
    config.gateway.admin.enabled = True
    config.gateway.admin.auth_key = "secret-key"
    save_config(config, config_path)

    app = create_http_app(config_path=config_path, workspace=workspace)
    login = await _call_route(
        app,
        "POST",
        "/admin/login",
        data={"auth_key": "secret-key", "next": "/admin"},
    )
    cookie = login.cookies["nanobot_admin_session"].value

    create_resp = await _call_route(
        app,
        "POST",
        "/admin/personas/new",
        cookies={"nanobot_admin_session": cookie},
        data={"name": "Aria"},
    )
    assert create_resp.status == 302
    assert create_resp.headers["Location"].startswith("/admin/personas/Aria")

    persona_page = await _call_route(
        app,
        "GET",
        "/admin/personas/Aria",
        cookies={"nanobot_admin_session": cookie},
    )
    assert persona_page.status == 200
    assert "这里编辑当前角色在 runtime workspace 下的提示词与元数据文件" in persona_page.text
    assert "角色的核心设定、价值观和长期人格基调" in persona_page.text
    assert "角色对用户的默认态度、关系定位和互动边界" in persona_page.text
    assert "可选的长期用户画像" in persona_page.text
    assert "可选的长期协作洞察" in persona_page.text
    assert "迁移预览" in persona_page.text
    assert "当前没有检测到明显需要迁移的旧版“用户画像型”" in persona_page.text
    assert "可选的语音/TTS 覆盖配置" in persona_page.text
    assert "可选的角色元数据" in persona_page.text

    save_resp = await _call_route(
        app,
        "POST",
        "/admin/personas/Aria",
        cookies={"nanobot_admin_session": cookie},
        data={
            "soul_md": "# Soul\n\nCalm and observant.",
            "user_md": "# User\n\nStay close.",
            "profile_md": "# Profile\n\nPrefers concise technical collaboration.",
            "insights_md": "# Insights\n\nDo best with short iterative review loops.",
            "style_md": "# Style\n\nShort replies.",
            "lore_md": "",
            "voice_json": json.dumps({"provider": "edge", "edgeVoice": "zh-CN-XiaoyiNeural"}),
            "manifest_json": json.dumps({"reference_image": "assets/avatar.png"}),
        },
    )
    assert save_resp.status == 302
    assert save_resp.headers["Location"] == "/admin/personas/Aria?saved=updated"

    persona_dir = workspace / "personas" / "Aria"
    assert (persona_dir / "SOUL.md").read_text(encoding="utf-8") == "# Soul\n\nCalm and observant.\n"
    assert (persona_dir / "USER.md").read_text(encoding="utf-8") == "# User\n\nStay close.\n"
    assert (persona_dir / "PROFILE.md").read_text(encoding="utf-8") == (
        "# Profile\n\nPrefers concise technical collaboration.\n"
    )
    assert (persona_dir / "INSIGHTS.md").read_text(encoding="utf-8") == (
        "# Insights\n\nDo best with short iterative review loops.\n"
    )
    assert (persona_dir / "STYLE.md").read_text(encoding="utf-8") == "# Style\n\nShort replies.\n"
    assert not (persona_dir / "LORE.md").exists()
    assert json.loads((persona_dir / "VOICE.json").read_text(encoding="utf-8"))["provider"] == "edge"
    manifest_path = persona_dir / ".nanobot" / "st_manifest.json"
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["reference_image"] == "assets/avatar.png"


@pytest.mark.asyncio
async def test_gateway_admin_persona_migrates_legacy_user_md(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    config_path = tmp_path / "config.json"

    config = Config()
    config.gateway.admin.enabled = True
    config.gateway.admin.auth_key = "secret-key"
    save_config(config, config_path)

    app = create_http_app(config_path=config_path, workspace=workspace)
    login = await _call_route(
        app,
        "POST",
        "/admin/login",
        data={"auth_key": "secret-key", "next": "/admin"},
    )
    cookie = login.cookies["nanobot_admin_session"].value

    create_resp = await _call_route(
        app,
        "POST",
        "/admin/personas/new",
        cookies={"nanobot_admin_session": cookie},
        data={"name": "Aria"},
    )
    assert create_resp.status == 302

    persona_dir = workspace / "personas" / "Aria"
    (persona_dir / "USER.md").write_text(
        "# User Profile\n\n"
        "Information about the user to help personalize interactions.\n\n"
        "## Basic Information\n\n"
        "- **Timezone**: UTC+8\n"
        "- **Language**: Chinese\n\n"
        "## Preferences\n\n"
        "### Communication Style\n\n"
        "- [x] Technical\n\n"
        "## Special Instructions\n\n"
        "- Prefer short iterative review loops.\n\n"
        "## Relationship\n\n"
        "- Stay collaborative.\n",
        encoding="utf-8",
    )
    (persona_dir / "PROFILE.md").write_text("# Profile\n\n- Uses Arch Linux.\n", encoding="utf-8")

    preview_page = await _call_route(
        app,
        "GET",
        "/admin/personas/Aria",
        cookies={"nanobot_admin_session": cookie},
    )
    assert preview_page.status == 200
    assert "迁移预览" in preview_page.text
    assert "迁移后文件" in preview_page.text
    assert "迁移后的 USER.md" in preview_page.text
    assert "迁移后的 PROFILE.md" in preview_page.text
    assert "迁移后的 INSIGHTS.md" in preview_page.text
    assert "# Relationship" in preview_page.text
    assert "Uses Arch Linux." in preview_page.text
    assert "Basic Information" in preview_page.text
    assert "Special Instructions" in preview_page.text

    migrate_resp = await _call_route(
        app,
        "POST",
        "/admin/personas/Aria/migrate-user",
        cookies={"nanobot_admin_session": cookie},
    )
    assert migrate_resp.status == 302
    assert migrate_resp.headers["Location"] == "/admin/personas/Aria?migrated=1&profile=2&insights=1"

    assert (persona_dir / "PROFILE.md").read_text(encoding="utf-8") == (
        "# Profile\n\n"
        "- Uses Arch Linux.\n\n"
        "## Basic Information\n\n"
        "- **Timezone**: UTC+8\n"
        "- **Language**: Chinese\n\n"
        "## Preferences\n\n"
        "### Communication Style\n\n"
        "- [x] Technical\n"
    )
    assert (persona_dir / "INSIGHTS.md").read_text(encoding="utf-8") == (
        "## Special Instructions\n\n"
        "- Prefer short iterative review loops.\n"
    )
    assert (persona_dir / "USER.md").read_text(encoding="utf-8") == (
        "# Relationship\n\n"
        "- Stay collaborative.\n"
    )

    migrated_page = await _call_route(
        app,
        "GET",
        "/admin/personas/Aria?migrated=1&profile=2&insights=1",
        cookies={"nanobot_admin_session": cookie},
    )
    assert migrated_page.status == 200
    assert "迁移完成" in migrated_page.text
    assert "PROFILE.md" in migrated_page.text
    assert "INSIGHTS.md" in migrated_page.text


@pytest.mark.asyncio
async def test_gateway_admin_persona_shows_memory_metadata_summary(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    config_path = tmp_path / "config.json"

    config = Config()
    config.gateway.admin.enabled = True
    config.gateway.admin.auth_key = "secret-key"
    save_config(config, config_path)

    app = create_http_app(config_path=config_path, workspace=workspace)
    login = await _call_route(
        app,
        "POST",
        "/admin/login",
        data={"auth_key": "secret-key", "next": "/admin"},
    )
    cookie = login.cookies["nanobot_admin_session"].value

    create_resp = await _call_route(
        app,
        "POST",
        "/admin/personas/new",
        cookies={"nanobot_admin_session": cookie},
        data={"name": "Aria"},
    )
    assert create_resp.status == 302

    persona_dir = workspace / "personas" / "Aria"
    (persona_dir / "PROFILE.md").write_text(
        "# Profile\n\n"
        "- Likes concise replies <!-- nanobot-meta: confidence=high last_verified=2026-04-01 -->\n"
        "- Uses Arch Linux <!-- nanobot-meta: confidence=medium -->\n",
        encoding="utf-8",
    )
    (persona_dir / "INSIGHTS.md").write_text(
        "# Insights\n\n"
        "- Prefer short iterative loops <!-- nanobot-meta: confidence=low -->\n"
        "- Reconfirm risky assumptions with the user (verify)\n",
        encoding="utf-8",
    )

    persona_page = await _call_route(
        app,
        "GET",
        "/admin/personas/Aria",
        cookies={"nanobot_admin_session": cookie},
    )
    assert persona_page.status == 200
    assert "画像 / 洞察元信息" in persona_page.text
    assert "PROFILE.md 元信息" in persona_page.text
    assert "INSIGHTS.md 元信息" in persona_page.text
    assert "示例 metadata 写法" in persona_page.text
    assert "nanobot-meta: confidence=high last_verified=2026-04-08" in persona_page.text
    assert re.search(r"已标记条目</span>\s*<strong>2</strong>", persona_page.text)
    assert re.search(r"带 last_verified</span>\s*<strong>1</strong>", persona_page.text)
    assert re.search(r"高置信度</span>\s*<strong>1</strong>", persona_page.text)
    assert re.search(r"中置信度</span>\s*<strong>1</strong>", persona_page.text)
    assert re.search(r"低置信度</span>\s*<strong>1</strong>", persona_page.text)
    assert re.search(r"旧版 \(verify\)</span>\s*<strong>1</strong>", persona_page.text)
