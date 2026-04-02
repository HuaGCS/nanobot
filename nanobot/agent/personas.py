"""Helpers for resolving session personas within a workspace."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

DEFAULT_PERSONA = "default"
PERSONAS_DIRNAME = "personas"
PERSONA_VOICE_FILENAME = "VOICE.json"
PERSONA_METADATA_DIRNAME = ".nanobot"
PERSONA_ST_MANIFEST_FILENAME = "st_manifest.json"
_VALID_PERSONA_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_VOICE_MARKDOWN_RE = re.compile(r"(```[\s\S]*?```|`[^`]*`|!\[[^\]]*\]\([^)]+\)|[#>*_~-]+)")
_VOICE_WHITESPACE_RE = re.compile(r"\s+")
_VOICE_MAX_GUIDANCE_CHARS = 1200


@dataclass(frozen=True)
class PersonaVoiceSettings:
    """Optional persona-level voice synthesis overrides."""

    provider: str | None = None
    api_base: str | None = None
    voice: str | None = None
    instructions: str | None = None
    speed: float | None = None
    rate: str | None = None
    volume: str | None = None
    refer_wav_path: str | None = None
    prompt_text: str | None = None
    prompt_language: str | None = None
    text_language: str | None = None
    cut_punc: str | None = None
    top_k: int | None = None
    top_p: float | None = None
    temperature: float | None = None


@dataclass(frozen=True)
class PersonaReferenceImages:
    """Resolved reference images configured for a persona."""

    default: str | None = None
    scenes: dict[str, str] = field(default_factory=dict)


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


def persona_metadata_dir(workspace: Path, persona: str | None) -> Path:
    """Return the persona-local metadata directory."""
    return persona_workspace(workspace, persona) / PERSONA_METADATA_DIRNAME


def persona_manifest_path(workspace: Path, persona: str | None) -> Path:
    """Return the persona-local SillyTavern manifest path."""
    return persona_metadata_dir(workspace, persona) / PERSONA_ST_MANIFEST_FILENAME


def load_persona_manifest(workspace: Path, persona: str | None) -> dict[str, Any]:
    """Load persona metadata from `.nanobot/st_manifest.json` when present."""
    path = persona_manifest_path(workspace, persona)
    if not path.exists():
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("Failed to load persona manifest {}: {}", path, exc)
        return {}

    if not isinstance(data, dict):
        logger.warning("Ignoring persona manifest {} because it is not a JSON object", path)
        return {}
    return data


def _string_or_none(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return int(stripped)
        except ValueError:
            return None
    return None


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return float(stripped)
        except ValueError:
            return None
    return None


def _voice_provider_or_none(value: Any) -> str | None:
    provider = _string_or_none(value)
    if provider is None:
        return None
    normalized = provider.lower()
    if normalized in {"openai", "edge", "sovits"}:
        return normalized
    return None


def normalize_reference_image_map(value: Any) -> dict[str, str]:
    """Normalize scene -> reference image mappings from persona metadata."""
    if not isinstance(value, dict):
        return {}

    normalized: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str):
            continue
        key = raw_key.strip().lower()
        path = _string_or_none(raw_value)
        if key and path:
            normalized[key] = path
    return normalized


def resolve_persona_asset_path(workspace: Path, persona: str | None, value: str | None) -> str | None:
    """Resolve a persona-related asset path from either persona or workspace scope."""
    raw = _string_or_none(value)
    if raw is None:
        return None

    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        return str(candidate.resolve(strict=False))

    candidates: list[Path] = []
    if candidate.parts and candidate.parts[0] == PERSONAS_DIRNAME:
        candidates.append(workspace / candidate)
    else:
        candidates.append(persona_workspace(workspace, persona) / candidate)
        if persona_workspace(workspace, persona) != workspace:
            candidates.append(workspace / candidate)

    for path in candidates:
        if path.exists():
            return str(path.resolve())

    fallback = candidates[0] if candidates else candidate
    return str(fallback.resolve(strict=False))


def load_persona_reference_images(workspace: Path, persona: str | None) -> PersonaReferenceImages:
    """Load and resolve reference-image metadata for the active persona."""
    manifest = load_persona_manifest(workspace, persona)
    default = resolve_persona_asset_path(
        workspace,
        persona,
        manifest.get("reference_image") or manifest.get("referenceImage"),
    )

    raw_scenes = manifest.get("reference_images")
    if raw_scenes is None:
        raw_scenes = manifest.get("referenceImages")
    scenes = {
        key: resolved
        for key, value in normalize_reference_image_map(raw_scenes).items()
        if (resolved := resolve_persona_asset_path(workspace, persona, value)) is not None
    }
    return PersonaReferenceImages(default=default, scenes=scenes)


def resolve_persona_reference_image(
    workspace: Path,
    persona: str | None,
    scene: str | None = None,
) -> str | None:
    """Resolve the best reference image for a persona, optionally by scene."""
    images = load_persona_reference_images(workspace, persona)
    if scene:
        match = images.scenes.get(scene.strip().lower())
        if match:
            return match
    return images.default


def normalize_response_filter_tags(value: Any) -> tuple[str, ...]:
    """Normalize response filter tags from strings or string lists."""
    values: list[str] = []
    if isinstance(value, str):
        values = [item.strip() for item in value.split(",")]
    elif isinstance(value, list):
        values = [item.strip() for item in value if isinstance(item, str)]
    else:
        return ()

    tags: list[str] = []
    seen: set[str] = set()
    for item in values:
        if not item:
            continue
        lowered = item.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        tags.append(item)
    return tuple(tags)


def load_persona_response_filter_tags(workspace: Path, persona: str | None) -> tuple[str, ...]:
    """Return response filter tags configured for the active persona."""
    manifest = load_persona_manifest(workspace, persona)
    return normalize_response_filter_tags(manifest.get("response_filter_tags"))


def strip_tagged_response_content(content: str, tags: tuple[str, ...]) -> str:
    """Strip configured tagged blocks from a response while keeping a fallback."""
    if not content or not tags:
        return content

    filtered = content
    for tag in tags:
        escaped = re.escape(tag)
        filtered = re.sub(
            rf"<{escaped}\b[^>]*>[\s\S]*?</{escaped}>",
            "",
            filtered,
            flags=re.IGNORECASE,
        )
        filtered = re.sub(
            rf"<{escaped}\b[^>]*>[\s\S]*$",
            "",
            filtered,
            flags=re.IGNORECASE,
        )

    filtered = re.sub(r"[ \t]{2,}", " ", filtered)
    filtered = re.sub(r"\n{3,}", "\n\n", filtered)
    stripped = filtered.strip()
    return stripped or content


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

    provider = _voice_provider_or_none(data.get("provider"))
    api_base = _string_or_none(data.get("api_base") or data.get("apiBase") or data.get("url"))

    voice = _string_or_none(data.get("voice") or data.get("edge_voice") or data.get("edgeVoice"))

    instructions = data.get("instructions")
    if isinstance(instructions, str):
        instructions = instructions.strip() or None
    else:
        instructions = None

    speed = _float_or_none(data.get("speed"))
    if speed is not None:
        if not 0.25 <= speed <= 4.0:
            logger.warning(
                "Ignoring persona voice speed from {} because it is outside 0.25-4.0",
                path,
            )
            speed = None

    top_k = _int_or_none(data.get("top_k") or data.get("topK"))
    top_p = _float_or_none(data.get("top_p") or data.get("topP"))
    temperature = _float_or_none(data.get("temperature"))

    return PersonaVoiceSettings(
        provider=provider,
        api_base=api_base,
        voice=voice,
        instructions=instructions,
        speed=speed,
        rate=_string_or_none(data.get("rate") or data.get("edge_rate") or data.get("edgeRate")),
        volume=_string_or_none(
            data.get("volume") or data.get("edge_volume") or data.get("edgeVolume")
        ),
        refer_wav_path=_string_or_none(
            data.get("refer_wav_path")
            or data.get("referWavPath")
            or data.get("sovits_refer_wav_path")
            or data.get("sovitsReferWavPath")
        ),
        prompt_text=_string_or_none(
            data.get("prompt_text")
            or data.get("promptText")
            or data.get("sovits_prompt_text")
            or data.get("sovitsPromptText")
        ),
        prompt_language=_string_or_none(
            data.get("prompt_language")
            or data.get("promptLanguage")
            or data.get("sovits_prompt_language")
            or data.get("sovitsPromptLanguage")
        ),
        text_language=_string_or_none(
            data.get("text_language")
            or data.get("textLanguage")
            or data.get("sovits_text_language")
            or data.get("sovitsTextLanguage")
        ),
        cut_punc=_string_or_none(
            data.get("cut_punc")
            or data.get("cutPunc")
            or data.get("sovits_cut_punc")
            or data.get("sovitsCutPunc")
        ),
        top_k=top_k if top_k is None or top_k > 0 else None,
        top_p=top_p,
        temperature=temperature,
    )


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
