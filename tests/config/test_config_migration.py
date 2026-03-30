import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from nanobot.cli.commands import _resolve_channel_default_config, app
from nanobot.config.loader import load_config, save_config

runner = CliRunner()


def test_load_config_keeps_max_tokens_and_ignores_legacy_memory_window(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {
                        "maxTokens": 1234,
                        "memoryWindow": 42,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.agents.defaults.max_tokens == 1234
    assert config.agents.defaults.context_window_tokens == 65_536
    assert not hasattr(config.agents.defaults, "memory_window")


def test_save_config_writes_context_window_tokens_but_not_memory_window(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {
                        "maxTokens": 2222,
                        "memoryWindow": 30,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)
    save_config(config, config_path)
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    defaults = saved["agents"]["defaults"]

    assert defaults["maxTokens"] == 2222
    assert defaults["contextWindowTokens"] == 65_536
    assert "memoryWindow" not in defaults


def test_save_config_persists_memory_shadow_write_settings(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config = load_config(config_path)
    config.memory.user.shadow_write_mem0 = True
    config.memory.user.mem0.llm.provider = "ollama"
    config.memory.user.mem0.llm.model = "qwen3:8b"
    config.memory.user.mem0.embedder.provider = "openai"
    config.memory.user.mem0.embedder.api_key = "embed-key"
    config.memory.user.mem0.embedder.url = "https://embed.example.com/v1"
    config.memory.user.mem0.vector_store.provider = "qdrant"
    config.memory.user.mem0.vector_store.url = "https://qdrant.example.com"
    config.memory.user.mem0.vector_store.headers = {"Authorization": "Bearer test"}
    config.memory.user.mem0.vector_store.config = {"collectionName": "nanobot_user_memory"}

    save_config(config, config_path)
    saved = json.loads(config_path.read_text(encoding="utf-8"))

    assert saved["memory"]["user"]["shadowWriteMem0"] is True
    assert saved["memory"]["user"]["mem0"]["llm"]["provider"] == "ollama"
    assert saved["memory"]["user"]["mem0"]["llm"]["model"] == "qwen3:8b"
    assert saved["memory"]["user"]["mem0"]["embedder"]["apiKey"] == "embed-key"
    assert saved["memory"]["user"]["mem0"]["embedder"]["url"] == "https://embed.example.com/v1"
    assert saved["memory"]["user"]["mem0"]["vectorStore"]["url"] == "https://qdrant.example.com"
    assert saved["memory"]["user"]["mem0"]["vectorStore"]["headers"] == {
        "Authorization": "Bearer test"
    }
    assert saved["memory"]["user"]["mem0"]["vectorStore"]["config"]["collectionName"] == (
        "nanobot_user_memory"
    )


def test_load_config_parses_reserved_mem0_provider_api_fields(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "memory": {
                    "user": {
                        "mem0": {
                            "llm": {
                                "provider": "openai",
                                "apiKey": "llm-key",
                                "url": "https://llm.example.com/v1",
                                "model": "gpt-4.1-mini",
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
                                "config": {"collectionName": "nanobot_user_memory"},
                            },
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.memory.user.mem0.llm.api_key == "llm-key"
    assert config.memory.user.mem0.llm.url == "https://llm.example.com/v1"
    assert config.memory.user.mem0.llm.model == "gpt-4.1-mini"
    assert config.memory.user.mem0.embedder.api_key == "embed-key"
    assert config.memory.user.mem0.embedder.url == "https://embed.example.com/v1"
    assert config.memory.user.mem0.embedder.model == "text-embedding-3-small"
    assert config.memory.user.mem0.vector_store.url == "https://qdrant.example.com"
    assert config.memory.user.mem0.vector_store.headers == {"api-key": "qdrant-key"}
    assert config.memory.user.mem0.vector_store.config == {
        "collectionName": "nanobot_user_memory"
    }


def test_onboard_does_not_crash_with_legacy_memory_window(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.json"
    workspace = tmp_path / "workspace"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {
                        "maxTokens": 3333,
                        "memoryWindow": 50,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr("nanobot.config.loader.get_config_path", lambda: config_path)
    monkeypatch.setattr("nanobot.cli.commands.get_workspace_path", lambda _workspace=None: workspace)

    result = runner.invoke(app, ["onboard"], input="n\n")

    assert result.exit_code == 0


def test_onboard_refresh_backfills_missing_channel_fields(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace

    config_path = tmp_path / "config.json"
    workspace = tmp_path / "workspace"
    config_path.write_text(
        json.dumps(
            {
                "channels": {
                    "qq": {
                        "enabled": False,
                        "appId": "",
                        "secret": "",
                        "allowFrom": [],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr("nanobot.config.loader.get_config_path", lambda: config_path)
    monkeypatch.setattr("nanobot.cli.commands.get_workspace_path", lambda _workspace=None: workspace)
    monkeypatch.setattr(
        "nanobot.channels.registry.discover_all",
        lambda: {
            "qq": SimpleNamespace(
                default_config=lambda: {
                    "enabled": False,
                    "appId": "",
                    "secret": "",
                    "allowFrom": [],
                    "msgFormat": "plain",
                }
            )
        },
    )

    result = runner.invoke(app, ["onboard"], input="n\n")

    assert result.exit_code == 0
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["channels"]["qq"]["msgFormat"] == "plain"


@pytest.mark.parametrize(
    ("channel_cls", "expected"),
    [
        (SimpleNamespace(), None),
        (SimpleNamespace(default_config="invalid"), None),
        (SimpleNamespace(default_config=lambda: None), None),
        (SimpleNamespace(default_config=lambda: ["invalid"]), None),
        (SimpleNamespace(default_config=lambda: {"enabled": False}), {"enabled": False}),
    ],
)
def test_resolve_channel_default_config_validates_payload(channel_cls, expected) -> None:
    assert _resolve_channel_default_config(channel_cls) == expected


def test_resolve_channel_default_config_skips_exceptions() -> None:
    def _raise() -> dict[str, object]:
        raise RuntimeError("boom")

    assert _resolve_channel_default_config(SimpleNamespace(default_config=_raise)) is None


def test_onboard_refresh_skips_invalid_channel_default_configs(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.json"
    workspace = tmp_path / "workspace"
    config_path.write_text(json.dumps({"channels": {}}), encoding="utf-8")

    def _raise() -> dict[str, object]:
        raise RuntimeError("boom")

    monkeypatch.setattr("nanobot.config.loader.get_config_path", lambda: config_path)
    monkeypatch.setattr("nanobot.cli.commands.get_workspace_path", lambda _workspace=None: workspace)
    monkeypatch.setattr(
        "nanobot.channels.registry.discover_all",
        lambda: {
            "missing": SimpleNamespace(),
            "noncallable": SimpleNamespace(default_config="invalid"),
            "none": SimpleNamespace(default_config=lambda: None),
            "wrong_type": SimpleNamespace(default_config=lambda: ["invalid"]),
            "raises": SimpleNamespace(default_config=_raise),
            "qq": SimpleNamespace(
                default_config=lambda: {
                    "enabled": False,
                    "appId": "",
                    "secret": "",
                    "allowFrom": [],
                    "msgFormat": "plain",
                }
            ),
        },
    )

    result = runner.invoke(app, ["onboard"], input="n\n")

    assert result.exit_code == 0
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert "missing" not in saved["channels"]
    assert "noncallable" not in saved["channels"]
    assert "none" not in saved["channels"]
    assert "wrong_type" not in saved["channels"]
    assert "raises" not in saved["channels"]
    assert saved["channels"]["qq"]["msgFormat"] == "plain"
