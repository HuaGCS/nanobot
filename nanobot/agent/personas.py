"""Helpers for resolving session personas within a workspace."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

DEFAULT_PERSONA = "default"
PERSONAS_DIRNAME = "personas"
PERSONA_VOICE_FILENAME = "VOICE.json"
_VALID_PERSONA_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_VOICE_MARKDOWN_RE = re.compile(r"(```[\s\S]*?```|`[^`]*`|!\[[^\]]*\]\([^)]+\)|[#>*_~-]+)")
_VOICE_WHITESPACE_RE = re.compile(r"\s+")
_VOICE_MAX_GUIDANCE_CHARS = 1200


@dataclass(frozen=True)
class PersonaVoiceSettings:
    """Optional persona-level voice synthesis overrides."""

    voice: str | None = None
    instructions: str | None = None
    speed: float | None = None


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


def load_persona_voice_settings(workspace: Path, persona: str | None) -> PersonaVoiceSettings:
    """Load optional persona voice overrides from VOICE.json."""
    path = persona_workspace(workspace, persona) / PERSONA_VOICE_FILENAME
    if not path.exists():
        return PersonaVoiceSettings()

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Failed to load persona voice config {}: {}", path, exc)
        return PersonaVoiceSettings()

    if not isinstance(data, dict):
        logger.warning("Ignoring persona voice config {} because it is not a JSON object", path)
        return PersonaVoiceSettings()

    voice = data.get("voice")
    if isinstance(voice, str):
        voice = voice.strip() or None
    else:
        voice = None

    instructions = data.get("instructions")
    if isinstance(instructions, str):
        instructions = instructions.strip() or None
    else:
        instructions = None

    speed = data.get("speed")
    if isinstance(speed, (int, float)):
        speed = float(speed)
        if not 0.25 <= speed <= 4.0:
            logger.warning(
                "Ignoring persona voice speed from {} because it is outside 0.25-4.0",
                path,
            )
            speed = None
    else:
        speed = None

    return PersonaVoiceSettings(voice=voice, instructions=instructions, speed=speed)


def build_persona_voice_instructions(
    workspace: Path,
    persona: str | None,
    *,
    extra_instructions: str | None = None,
) -> str:
    """Build voice-style instructions from the active persona prompt files."""
    resolved = resolve_persona_name(workspace, persona) or DEFAULT_PERSONA
    persona_dir = None if resolved == DEFAULT_PERSONA else personas_root(workspace) / resolved
    guidance_parts: list[str] = []

    for filename in ("SOUL.md", "USER.md"):
        file_path = workspace / filename
        if persona_dir:
            persona_file = persona_dir / filename
            if persona_file.exists():
                file_path = persona_file
        if not file_path.exists():
            continue
        try:
            raw = file_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Failed to read persona voice source {}: {}", file_path, exc)
            continue
        clean = _VOICE_WHITESPACE_RE.sub(" ", _VOICE_MARKDOWN_RE.sub(" ", raw)).strip()
        if clean:
            guidance_parts.append(clean)

    guidance = " ".join(guidance_parts).strip()
    if len(guidance) > _VOICE_MAX_GUIDANCE_CHARS:
        guidance = guidance[:_VOICE_MAX_GUIDANCE_CHARS].rstrip()

    segments = [
        f"Speak as the active persona '{resolved}'. Match that persona's tone, attitude, pacing, and emotional style while keeping the reply natural and conversational.",
    ]
    if extra_instructions:
        segments.append(extra_instructions.strip())
    if guidance:
        segments.append(f"Persona guidance: {guidance}")
    return " ".join(segment for segment in segments if segment)
