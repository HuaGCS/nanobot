"""Minimal session-level localization helpers."""

from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files as pkg_files
from typing import Any

DEFAULT_LANGUAGE = "en"
SUPPORTED_LANGUAGES = ("en", "zh")

_LANGUAGE_ALIASES = {
    "en": "en",
    "en-us": "en",
    "en-gb": "en",
    "english": "en",
    "zh": "zh",
    "zh-cn": "zh",
    "zh-hans": "zh",
    "zh-sg": "zh",
    "cn": "zh",
    "chinese": "zh",
    "中文": "zh",
}

@lru_cache(maxsize=len(SUPPORTED_LANGUAGES))
def _load_locale(language: str) -> dict[str, Any]:
    """Load one locale file from packaged JSON resources."""
    lang = resolve_language(language)
    locale_file = pkg_files("nanobot") / "locales" / f"{lang}.json"
    with locale_file.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def normalize_language_code(value: Any) -> str | None:
    """Normalize a language identifier into a supported code."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower()
    if not cleaned:
        return None
    return _LANGUAGE_ALIASES.get(cleaned)


def resolve_language(value: Any) -> str:
    """Resolve the active language, defaulting to English."""
    return normalize_language_code(value) or DEFAULT_LANGUAGE


def list_languages() -> list[str]:
    """Return supported language codes in display order."""
    return list(SUPPORTED_LANGUAGES)


def language_label(code: str, ui_language: str | None = None) -> str:
    """Return a display label for a language code."""
    active_ui = resolve_language(ui_language)
    normalized = resolve_language(code)
    locale = _load_locale(active_ui)
    return f"{normalized} ({locale['language_labels'][normalized]})"


def text(language: Any, key: str, **kwargs: Any) -> str:
    """Return localized UI text."""
    active = resolve_language(language)
    template = _load_locale(active)["texts"][key]
    return template.format(**kwargs)


def help_lines(language: Any) -> list[str]:
    """Return localized slash-command help lines."""
    active = resolve_language(language)
    return [
        text(active, "help_header"),
        text(active, "cmd_new"),
        text(active, "cmd_lang_current"),
        text(active, "cmd_lang_list"),
        text(active, "cmd_lang_set"),
        text(active, "cmd_persona_current"),
        text(active, "cmd_persona_list"),
        text(active, "cmd_persona_set"),
        text(active, "cmd_stop"),
        text(active, "cmd_restart"),
        text(active, "cmd_help"),
    ]


def telegram_command_descriptions(language: Any) -> dict[str, str]:
    """Return Telegram command descriptions for a locale."""
    return _load_locale(resolve_language(language))["telegram_commands"]
