"""Tests for bundled built-in skills."""

from __future__ import annotations

from pathlib import Path

from nanobot.agent.skills import SkillsLoader


def test_builtin_skills_include_localized_companion_skills(tmp_path: Path) -> None:
    loader = SkillsLoader(tmp_path)

    names = {skill["name"] for skill in loader.list_skills(filter_unavailable=False)}

    assert {"translate", "living-together", "emotional-companion"}.issubset(names)


def test_living_together_is_loaded_as_always_on_skill(tmp_path: Path) -> None:
    loader = SkillsLoader(tmp_path)

    assert "living-together" in loader.get_always_skills()
