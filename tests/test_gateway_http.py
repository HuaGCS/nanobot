import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from multidict import MultiDict, MultiDictProxy

from nanobot.config.loader import save_config
from nanobot.config.schema import Config
from nanobot.gateway.http import create_http_app


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


async def _call_route(
    app,
    method: str,
    path: str,
    *,
    cookies: dict[str, str] | None = None,
    data: dict[str, str] | list[tuple[str, str]] | None = None,
):
    headers = {}
    if cookies:
        headers["Cookie"] = "; ".join(f"{key}={value}" for key, value in cookies.items())

    request = make_mocked_request(method, path, headers=headers, app=app)
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
    assert "tooltip-anchor" in config_page.text
    assert "默认工作区路径" in config_page.text
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
        data={
            "mode": "visual",
            "agents_defaults_model": "openai/gpt-4.1",
            "channels_voice_reply_provider": "sovits",
            "channels_voice_reply_sovits_api_url": "http://127.0.0.1:9880",
            "gateway_admin_auth_key": "secret-key",
        },
    )
    assert save_resp.status == 302
    assert save_resp.headers["Location"] == "/admin/config?saved=1&reloaded=1"
    assert reload_calls == ["called"]

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["agents"]["defaults"]["model"] == "openai/gpt-4.1"
    assert saved["channels"]["voiceReply"]["provider"] == "sovits"
    assert saved["channels"]["voiceReply"]["sovitsApiUrl"] == "http://127.0.0.1:9880"


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
    assert (persona_dir / "STYLE.md").read_text(encoding="utf-8") == "# Style\n\nShort replies.\n"
    assert not (persona_dir / "LORE.md").exists()
    assert json.loads((persona_dir / "VOICE.json").read_text(encoding="utf-8"))["provider"] == "edge"
    manifest_path = persona_dir / ".nanobot" / "st_manifest.json"
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["reference_image"] == "assets/avatar.png"
