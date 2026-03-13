"""Helpers for resolving session personas within a workspace."""

from __future__ import annotations

import re
from pathlib import Path

DEFAULT_PERSONA = "default"
PERSONAS_DIRNAME = "personas"
_VALID_PERSONA_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def normalize_persona_name(name: str | None) -> str | None:
    """Normalize a user-supplied persona name."""
    if not isinstance(name, str):
        return None

    cleaned = name.strip()
    if not cleaned:
        return None
    if cleaned.lower() == DEFAULT_PERSONA:
        return DEFAULT_PERSONA
    if not _VALID_PERSONA_RE.fullmatch(cleaned):
        return None
    return cleaned


def personas_root(workspace: Path) -> Path:
    """Return the workspace-local persona root directory."""
    return workspace / PERSONAS_DIRNAME


def list_personas(workspace: Path) -> list[str]:
    """List available personas, always including the built-in default persona."""
    personas: dict[str, str] = {DEFAULT_PERSONA.lower(): DEFAULT_PERSONA}
    root = personas_root(workspace)
    if root.exists():
        for child in root.iterdir():
            if not child.is_dir():
                continue
            normalized = normalize_persona_name(child.name)
            if normalized is None:
                continue
            personas.setdefault(normalized.lower(), child.name)

    return sorted(personas.values(), key=lambda value: (value.lower() != DEFAULT_PERSONA, value.lower()))


def resolve_persona_name(workspace: Path, name: str | None) -> str | None:
    """Resolve a persona name to the canonical workspace directory name."""
    normalized = normalize_persona_name(name)
    if normalized is None:
        return None
    if normalized == DEFAULT_PERSONA:
        return DEFAULT_PERSONA

    available = {persona.lower(): persona for persona in list_personas(workspace)}
    return available.get(normalized.lower())


def persona_workspace(workspace: Path, persona: str | None) -> Path:
    """Return the effective workspace root for a persona."""
    resolved = resolve_persona_name(workspace, persona)
    if resolved in (None, DEFAULT_PERSONA):
        return workspace
    return personas_root(workspace) / resolved
