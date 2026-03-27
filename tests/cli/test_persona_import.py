"""CLI tests for persona import commands."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from nanobot.cli.commands import app
from nanobot.config.schema import Config

runner = CliRunner()


def _config_for_workspace(workspace: Path) -> Config:
    config = Config()
    config.agents.defaults.workspace = str(workspace)
    return config


def test_persona_import_st_card_creates_persona_files(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    card_path = tmp_path / "aria.json"
    card_path.write_text(
        json.dumps(
            {
                "spec": "chara_card_v2",
                "data": {
                    "name": "Aria",
                    "description": "Warm and curious.",
                    "personality": "Gentle and attentive.",
                    "scenario": "Aria is your creative coding partner.",
                    "first_mes": "Want to build something together?",
                    "mes_example": "{{user}}: hi\n{{char}}: hello",
                    "system_prompt": "Stay in character.",
                    "post_history_instructions": "Keep the tone playful.",
                    "extensions": {
                        "nanobot": {
                            "responseFilterTags": "inner, thought",
                            "reference_image": "/tmp/aria.png",
                        }
                    },
                },
            },
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "nanobot.cli.commands._load_runtime_config",
        lambda _config=None, workspace_override=None: _config_for_workspace(
            Path(workspace_override) if workspace_override else workspace
        ),
    )

    result = runner.invoke(
        app,
        ["persona", "import-st-card", str(card_path), "--workspace", str(workspace)],
    )

    assert result.exit_code == 0
    persona_dir = workspace / "personas" / "Aria"
    assert (persona_dir / "SOUL.md").read_text(encoding="utf-8").startswith("# Identity")
    assert "Warm and curious." in (persona_dir / "SOUL.md").read_text(encoding="utf-8")
    assert "Aria is your creative coding partner." in (persona_dir / "USER.md").read_text(
        encoding="utf-8"
    )
    assert (persona_dir / ".nanobot" / "st_character.json").exists()
    manifest = json.loads((persona_dir / ".nanobot" / "st_manifest.json").read_text(encoding="utf-8"))
    assert manifest["response_filter_tags"] == ["inner", "thought"]
    assert manifest["reference_image"] == "/tmp/aria.png"
    assert "/persona set Aria" in result.stdout


def test_persona_import_st_card_rejects_existing_persona_without_force(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    persona_dir = workspace / "personas" / "Aria"
    persona_dir.mkdir(parents=True)
    (persona_dir / "SOUL.md").write_text("existing soul", encoding="utf-8")

    card_path = tmp_path / "aria.json"
    card_path.write_text(json.dumps({"name": "Aria"}), encoding="utf-8")

    monkeypatch.setattr(
        "nanobot.cli.commands._load_runtime_config",
        lambda _config=None, workspace_override=None: _config_for_workspace(
            Path(workspace_override) if workspace_override else workspace
        ),
    )

    result = runner.invoke(
        app,
        ["persona", "import-st-card", str(card_path), "--workspace", str(workspace)],
    )

    assert result.exit_code == 1
    assert "already exists" in result.stdout


def test_persona_import_st_card_force_overwrites_managed_files(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    persona_dir = workspace / "personas" / "Aria"
    (persona_dir / ".nanobot").mkdir(parents=True)
    (persona_dir / "SOUL.md").write_text("old soul", encoding="utf-8")

    card_path = tmp_path / "aria.json"
    card_path.write_text(
        json.dumps({"name": "Aria", "description": "New soul"}),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "nanobot.cli.commands._load_runtime_config",
        lambda _config=None, workspace_override=None: _config_for_workspace(
            Path(workspace_override) if workspace_override else workspace
        ),
    )

    result = runner.invoke(
        app,
        [
            "persona",
            "import-st-card",
            str(card_path),
            "--workspace",
            str(workspace),
            "--force",
        ],
    )

    assert result.exit_code == 0
    assert "Updated SillyTavern card 'Aria'" in result.stdout
    assert "New soul" in (persona_dir / "SOUL.md").read_text(encoding="utf-8")


def test_persona_import_st_preset_creates_style_file(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    persona_dir = workspace / "personas" / "Aria"
    persona_dir.mkdir(parents=True)
    (persona_dir / "SOUL.md").write_text("soul", encoding="utf-8")

    preset_path = tmp_path / "preset.json"
    preset_path.write_text(
        json.dumps(
            {
                "prompts": [
                    {"name": "Marker", "content": "ignored", "marker": True, "enabled": True},
                    {"name": "Warmth", "content": "Be warm.", "enabled": True, "role": "system", "order": 20},
                    {"name": "Reply", "content": "Keep replies vivid.", "enabled": True, "role": "assistant", "order": 10},
                ]
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "nanobot.cli.commands._load_runtime_config",
        lambda _config=None, workspace_override=None: _config_for_workspace(
            Path(workspace_override) if workspace_override else workspace
        ),
    )

    result = runner.invoke(
        app,
        [
            "persona",
            "import-st-preset",
            str(preset_path),
            "--workspace",
            str(workspace),
            "--persona",
            "Aria",
        ],
    )

    assert result.exit_code == 0
    style = (persona_dir / "STYLE.md").read_text(encoding="utf-8")
    assert "# Imported SillyTavern Preset" in style
    assert "Keep replies vivid." in style
    assert "Be warm." in style
    assert "ignored" not in style
    assert (persona_dir / ".nanobot" / "st_preset.json").exists()


def test_persona_import_st_preset_requires_existing_persona(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    preset_path = tmp_path / "preset.json"
    preset_path.write_text(json.dumps({"prompts": []}), encoding="utf-8")

    monkeypatch.setattr(
        "nanobot.cli.commands._load_runtime_config",
        lambda _config=None, workspace_override=None: _config_for_workspace(
            Path(workspace_override) if workspace_override else workspace
        ),
    )

    result = runner.invoke(
        app,
        [
            "persona",
            "import-st-preset",
            str(preset_path),
            "--workspace",
            str(workspace),
            "--persona",
            "Aria",
        ],
    )

    assert result.exit_code == 1
    assert "Unknown persona" in result.stdout


def test_persona_import_st_worldinfo_creates_lore_file(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    persona_dir = workspace / "personas" / "Aria"
    persona_dir.mkdir(parents=True)
    (persona_dir / "SOUL.md").write_text("soul", encoding="utf-8")

    worldinfo_path = tmp_path / "worldinfo.json"
    worldinfo_path.write_text(
        json.dumps(
            {
                "entries": {
                    "0": {"comment": "Always On", "content": "Aria loves sketching.", "constant": True, "order": 5},
                    "1": {"comment": "Disabled", "content": "ignore me", "disable": True, "order": 1},
                    "2": {"comment": "Travel", "content": "She keeps a travel journal.", "key": ["trip", "journey"], "order": 10},
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "nanobot.cli.commands._load_runtime_config",
        lambda _config=None, workspace_override=None: _config_for_workspace(
            Path(workspace_override) if workspace_override else workspace
        ),
    )

    result = runner.invoke(
        app,
        [
            "persona",
            "import-st-worldinfo",
            str(worldinfo_path),
            "--workspace",
            str(workspace),
            "--persona",
            "Aria",
        ],
    )

    assert result.exit_code == 0
    lore = (persona_dir / "LORE.md").read_text(encoding="utf-8")
    assert "# Imported SillyTavern Lore" in lore
    assert "Aria loves sketching." in lore
    assert "She keeps a travel journal." in lore
    assert "Keywords: trip, journey" in lore
    assert "ignore me" not in lore
    assert (persona_dir / ".nanobot" / "st_world_info.json").exists()
