"""Import helpers for persona assets."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from nanobot.agent.personas import (
    PERSONA_METADATA_DIRNAME,
    PERSONA_ST_MANIFEST_FILENAME,
    normalize_persona_name,
    normalize_response_filter_tags,
    personas_root,
)
from nanobot.utils.helpers import ensure_dir

_ST_CHARACTER_FILENAME = "st_character.json"
_ST_PRESET_FILENAME = "st_preset.json"
_ST_WORLD_INFO_FILENAME = "st_world_info.json"
_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9_-]+")
_DUP_SEP_RE = re.compile(r"[-_]{2,}")


@dataclass(frozen=True)
class SillyTavernCharacterCard:
    """Normalized subset of a SillyTavern character card."""

    name: str
    description: str
    personality: str
    scenario: str
    first_mes: str
    mes_example: str
    creator_notes: str
    system_prompt: str
    post_history_instructions: str
    extensions: dict[str, Any]
    raw: dict[str, Any]


@dataclass(frozen=True)
class ImportedPersonaResult:
    """Summary of an imported persona asset."""

    persona_name: str
    display_name: str
    persona_dir: Path
    overwritten: bool


@dataclass(frozen=True)
class SillyTavernPresetEntry:
    """Normalized preset prompt entry."""

    name: str
    role: str
    content: str
    enabled: bool
    marker: bool
    injection_order: int


@dataclass(frozen=True)
class SillyTavernPreset:
    """Normalized SillyTavern preset payload."""

    prompts: list[SillyTavernPresetEntry]
    raw: dict[str, Any]


@dataclass(frozen=True)
class SillyTavernWorldInfoEntry:
    """Normalized world info entry."""

    title: str
    content: str
    enabled: bool
    order: int
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class SillyTavernWorldInfo:
    """Normalized SillyTavern world info payload."""

    entries: list[SillyTavernWorldInfoEntry]
    raw: dict[str, Any]


def parse_sillytavern_character_card_text(text: str) -> SillyTavernCharacterCard:
    """Parse a SillyTavern character card JSON payload."""
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {exc}") from exc
    return parse_sillytavern_character_card_object(obj)


def parse_sillytavern_character_card_object(obj: Any) -> SillyTavernCharacterCard:
    """Parse a SillyTavern character card object."""
    if not isinstance(obj, dict):
        raise ValueError("Character card JSON must be an object")

    data = obj.get("data") if isinstance(obj.get("data"), dict) else obj
    if not isinstance(data, dict):
        raise ValueError("Unknown character card format: missing object-like data payload")

    name = _string_value(data.get("name"))
    if not name:
        raise ValueError("Character card name is required")

    extensions = data.get("extensions")
    if not isinstance(extensions, dict):
        extensions = {}

    return SillyTavernCharacterCard(
        name=name,
        description=_string_value(data.get("description")),
        personality=_string_value(data.get("personality")),
        scenario=_string_value(data.get("scenario")),
        first_mes=_string_value(data.get("first_mes")),
        mes_example=_string_value(data.get("mes_example")),
        creator_notes=_string_value(data.get("creator_notes")),
        system_prompt=_string_value(data.get("system_prompt")),
        post_history_instructions=_string_value(data.get("post_history_instructions")),
        extensions=extensions,
        raw=obj,
    )


def import_sillytavern_character_card(
    workspace: Path,
    source_path: Path,
    *,
    persona_name: str | None = None,
    force: bool = False,
) -> ImportedPersonaResult:
    """Import a SillyTavern character card into the workspace persona structure."""
    source = source_path.expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"Character card file not found: {source_path}")

    card = parse_sillytavern_character_card_text(source.read_text(encoding="utf-8"))
    root = ensure_dir(personas_root(workspace))
    resolved_name = _resolve_persona_name(persona_name or card.name)
    persona_dir = root / resolved_name
    overwritten = False

    if persona_dir.exists():
        has_existing_content = any(persona_dir.iterdir())
        if has_existing_content and not force:
            raise ValueError(
                f"Persona '{resolved_name}' already exists at {persona_dir}. "
                "Re-run with --force to overwrite managed files."
            )
        overwritten = has_existing_content

    ensure_dir(persona_dir)
    ensure_dir(persona_dir / "memory")
    ensure_dir(persona_dir / PERSONA_METADATA_DIRNAME)

    _write_text(persona_dir / "SOUL.md", _build_soul_markdown(card))
    _write_text(persona_dir / "USER.md", _build_user_markdown(card))
    _write_if_missing(persona_dir / "memory" / "MEMORY.md", "")
    _write_if_missing(persona_dir / "memory" / "HISTORY.md", "")
    _write_json(persona_dir / PERSONA_METADATA_DIRNAME / _ST_CHARACTER_FILENAME, card.raw)
    _write_json(
        persona_dir / PERSONA_METADATA_DIRNAME / PERSONA_ST_MANIFEST_FILENAME,
        _build_manifest(card, source, resolved_name),
    )

    return ImportedPersonaResult(
        persona_name=resolved_name,
        display_name=card.name,
        persona_dir=persona_dir,
        overwritten=overwritten,
    )


def parse_sillytavern_preset_text(text: str) -> SillyTavernPreset:
    """Parse a SillyTavern preset JSON payload."""
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {exc}") from exc
    return parse_sillytavern_preset_object(obj)


def parse_sillytavern_preset_object(obj: Any) -> SillyTavernPreset:
    """Parse a SillyTavern preset object."""
    if not isinstance(obj, dict):
        raise ValueError("Preset JSON must be an object")

    prompts_raw = obj.get("prompts", obj.get("prompt_order", []))
    if not isinstance(prompts_raw, list):
        raise ValueError("Preset JSON has no prompt list")

    prompts: list[SillyTavernPresetEntry] = []
    for entry in prompts_raw:
        if not isinstance(entry, dict):
            continue
        prompts.append(
            SillyTavernPresetEntry(
                name=_string_value(entry.get("name") or entry.get("identifier")) or "Prompt",
                role=_string_value(entry.get("role")) or "system",
                content=_string_value(entry.get("content")),
                enabled=bool(entry.get("enabled", True)),
                marker=bool(entry.get("marker", False)),
                injection_order=_int_value(
                    entry.get("injection_order", entry.get("order", 100)),
                    default=100,
                ),
            )
        )

    return SillyTavernPreset(prompts=prompts, raw=obj)


def import_sillytavern_preset(
    workspace: Path,
    source_path: Path,
    *,
    persona_name: str,
    force: bool = False,
) -> ImportedPersonaResult:
    """Import a SillyTavern preset into an existing persona."""
    source = source_path.expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"Preset file not found: {source_path}")

    preset = parse_sillytavern_preset_text(source.read_text(encoding="utf-8"))
    resolved_name, persona_dir = _require_existing_persona(workspace, persona_name)
    style_path = persona_dir / "STYLE.md"
    raw_path = persona_dir / PERSONA_METADATA_DIRNAME / _ST_PRESET_FILENAME
    overwritten = style_path.exists()
    _ensure_managed_target(style_path, force=force, description="STYLE.md")

    ensure_dir(persona_dir / PERSONA_METADATA_DIRNAME)
    _write_text(style_path, _build_style_markdown(preset))
    _write_json(raw_path, preset.raw)

    return ImportedPersonaResult(
        persona_name=resolved_name,
        display_name=resolved_name,
        persona_dir=persona_dir,
        overwritten=overwritten,
    )


def parse_sillytavern_world_info_text(text: str) -> SillyTavernWorldInfo:
    """Parse a SillyTavern world info JSON payload."""
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {exc}") from exc
    return parse_sillytavern_world_info_object(obj)


def parse_sillytavern_world_info_object(obj: Any) -> SillyTavernWorldInfo:
    """Parse a SillyTavern world info object."""
    if not isinstance(obj, dict):
        raise ValueError("World info JSON must be an object")

    entries_raw = obj.get("entries")
    if isinstance(entries_raw, dict):
        entry_items = [value for value in entries_raw.values() if isinstance(value, dict)]
    elif isinstance(entries_raw, list):
        entry_items = [value for value in entries_raw if isinstance(value, dict)]
    else:
        raise ValueError("World info JSON has no entries")

    entries: list[SillyTavernWorldInfoEntry] = []
    for entry in entry_items:
        content = _string_value(entry.get("content"))
        title = _string_value(entry.get("comment")) or _string_value(entry.get("name")) or "Lore Entry"
        entries.append(
            SillyTavernWorldInfoEntry(
                title=title,
                content=content,
                enabled=not bool(entry.get("disable", False)),
                order=_int_value(entry.get("order"), default=100),
                keywords=_normalize_keywords(entry.get("key")),
            )
        )

    return SillyTavernWorldInfo(entries=entries, raw=obj)


def import_sillytavern_world_info(
    workspace: Path,
    source_path: Path,
    *,
    persona_name: str,
    force: bool = False,
) -> ImportedPersonaResult:
    """Import SillyTavern world info into an existing persona."""
    source = source_path.expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"World info file not found: {source_path}")

    world_info = parse_sillytavern_world_info_text(source.read_text(encoding="utf-8"))
    resolved_name, persona_dir = _require_existing_persona(workspace, persona_name)
    lore_path = persona_dir / "LORE.md"
    raw_path = persona_dir / PERSONA_METADATA_DIRNAME / _ST_WORLD_INFO_FILENAME
    overwritten = lore_path.exists()
    _ensure_managed_target(lore_path, force=force, description="LORE.md")

    ensure_dir(persona_dir / PERSONA_METADATA_DIRNAME)
    _write_text(lore_path, _build_lore_markdown(world_info))
    _write_json(raw_path, world_info.raw)

    return ImportedPersonaResult(
        persona_name=resolved_name,
        display_name=resolved_name,
        persona_dir=persona_dir,
        overwritten=overwritten,
    )


def _resolve_persona_name(preferred: str) -> str:
    normalized = normalize_persona_name(preferred)
    if normalized and normalized.lower() != "default":
        return normalized
    return _slugify_persona_name(preferred)


def _slugify_persona_name(name: str) -> str:
    cleaned = _NON_ALNUM_RE.sub("-", name.strip())
    cleaned = _DUP_SEP_RE.sub("-", cleaned).strip("-_")
    if cleaned and cleaned[0].isalnum():
        return cleaned[:64].rstrip("-_") or _fallback_persona_name(name)
    return _fallback_persona_name(name)


def _fallback_persona_name(seed: str) -> str:
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:8]
    return f"persona-{digest}"


def _require_existing_persona(workspace: Path, preferred: str) -> tuple[str, Path]:
    from nanobot.agent.personas import persona_workspace, resolve_persona_name

    resolved = resolve_persona_name(workspace, preferred)
    if resolved is None:
        raise ValueError(
            f"Unknown persona: {preferred}. Import a character card first or create the persona manually."
        )
    return resolved, persona_workspace(workspace, resolved)


def _ensure_managed_target(path: Path, *, force: bool, description: str) -> None:
    if path.exists() and not force:
        raise ValueError(
            f"{description} already exists at {path}. Re-run with --force to overwrite it."
        )


def _build_manifest(card: SillyTavernCharacterCard, source_path: Path, persona_name: str) -> dict[str, Any]:
    ext = _nanobot_extensions(card.extensions)
    reference_images = ext.get("reference_images")
    if not isinstance(reference_images, dict):
        reference_images = {}

    normalized_reference_images = {
        str(key): str(value)
        for key, value in reference_images.items()
        if isinstance(key, str) and isinstance(value, str) and value.strip()
    }

    return {
        "version": 1,
        "source": "sillytavern-character-card",
        "character_name": card.name,
        "persona_name": persona_name,
        "imported_at": datetime.now().astimezone().isoformat(),
        "source_path": str(source_path),
        "response_filter_tags": list(_extract_response_filter_tags(ext)),
        "reference_image": _string_or_none(ext.get("reference_image")),
        "reference_images": normalized_reference_images,
    }


def _build_soul_markdown(card: SillyTavernCharacterCard) -> str:
    lines = ["# Identity", f"Name: {card.name}"]
    _append_section(lines, "Description", card.description)
    _append_section(lines, "Personality", card.personality)
    _append_section(lines, "System Guidance", card.system_prompt)
    _append_section(lines, "Creator Notes", card.creator_notes)
    return "\n".join(lines).strip() + "\n"


def _build_user_markdown(card: SillyTavernCharacterCard) -> str:
    lines = ["# Relationship"]
    relationship = card.scenario or "Imported from a SillyTavern character card."
    lines.append(relationship)
    _append_section(lines, "Opening Message", card.first_mes)
    _append_section(lines, "Example Dialogue", card.mes_example)
    _append_section(lines, "Interaction Notes", card.post_history_instructions)
    return "\n".join(lines).strip() + "\n"


def _build_style_markdown(preset: SillyTavernPreset) -> str:
    entries = [
        entry
        for entry in sorted(preset.prompts, key=lambda item: item.injection_order)
        if entry.enabled and not entry.marker and entry.content
    ]
    if not entries:
        return "# Imported SillyTavern Preset\n\nNo enabled prompt entries were found.\n"

    lines = [
        "# Imported SillyTavern Preset",
        "",
        "Use the following imported preset guidance as persona style and interaction instructions.",
    ]
    for entry in entries:
        lines.extend(
            [
                "",
                f"## {entry.name} ({entry.role})",
                entry.content,
            ]
        )
    return "\n".join(lines).strip() + "\n"


def _build_lore_markdown(world_info: SillyTavernWorldInfo) -> str:
    entries = [
        entry
        for entry in sorted(world_info.entries, key=lambda item: item.order)
        if entry.enabled and entry.content
    ]
    if not entries:
        return "# Imported SillyTavern Lore\n\nNo enabled world-info entries were found.\n"

    lines = [
        "# Imported SillyTavern Lore",
        "",
        "Use the following imported world information as persona background knowledge.",
    ]
    for entry in entries:
        lines.extend(["", f"## {entry.title}"])
        if entry.keywords:
            lines.append(f"Keywords: {', '.join(entry.keywords)}")
            lines.append("")
        lines.append(entry.content)
    return "\n".join(lines).strip() + "\n"


def _append_section(lines: list[str], title: str, body: str) -> None:
    text = body.strip()
    if not text:
        return
    lines.extend(["", f"# {title}", text])


def _extract_response_filter_tags(ext: dict[str, Any]) -> tuple[str, ...]:
    for key in (
        "response_filter_tags",
        "responseFilterTags",
        "response_filter_tag",
        "responseFilterTag",
    ):
        raw = ext.get(key)
        if raw is None:
            continue
        return normalize_response_filter_tags(raw)
    return ()


def _nanobot_extensions(extensions: dict[str, Any]) -> dict[str, Any]:
    raw = extensions.get("nanobot")
    return raw if isinstance(raw, dict) else {}


def _write_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _write_if_missing(path: Path, content: str) -> None:
    if not path.exists():
        _write_text(path, content)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _string_value(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _string_or_none(value: Any) -> str | None:
    text = _string_value(value)
    return text or None


def _int_value(value: Any, *, default: int) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def _normalize_keywords(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",")]
    elif isinstance(value, list):
        items = [part.strip() for part in value if isinstance(part, str)]
    else:
        return ()
    return tuple(item for item in items if item)
