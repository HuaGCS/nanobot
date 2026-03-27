"""Runtime path helpers derived from the active config context."""

from __future__ import annotations

from pathlib import Path

from nanobot.config.loader import get_config_path, get_default_config_path
from nanobot.utils.helpers import ensure_dir


def get_data_dir() -> Path:
    """Return the instance-level runtime data directory."""
    return ensure_dir(get_config_path().parent)


def get_runtime_subdir(name: str) -> Path:
    """Return a named runtime subdirectory under the instance data dir."""
    return ensure_dir(get_data_dir() / name)


def get_media_dir(channel: str | None = None) -> Path:
    """Return the media directory, optionally namespaced per channel."""
    base = get_runtime_subdir("media")
    return ensure_dir(base / channel) if channel else base


def get_cron_dir() -> Path:
    """Return the cron storage directory."""
    return get_runtime_subdir("cron")


def get_logs_dir() -> Path:
    """Return the logs directory."""
    return get_runtime_subdir("logs")


def get_global_home_dir() -> Path:
    """Return nanobot's default global home directory."""
    return get_default_config_path().parent


def get_default_workspace_path(config_path: str | Path | None = None) -> Path:
    """Return the default workspace path for a config file."""
    base_config = (
        Path(config_path).expanduser().resolve(strict=False)
        if config_path is not None
        else get_config_path()
    )
    return base_config.parent / "workspace"


def resolve_workspace_path(
    workspace: str | Path | None = None, *, config_path: str | Path | None = None
) -> Path:
    """Resolve a workspace path without creating it."""
    if workspace is not None:
        raw = str(workspace).strip()
        if raw:
            return Path(workspace).expanduser()
    return get_default_workspace_path(config_path)


def get_workspace_path(workspace: str | Path | None = None, *, config_path: str | Path | None = None) -> Path:
    """Resolve and ensure the agent workspace path."""
    return ensure_dir(resolve_workspace_path(workspace, config_path=config_path))


def is_default_workspace(workspace: str | Path | None, *, config_path: str | Path | None = None) -> bool:
    """Return whether a workspace resolves to the config-derived default workspace path."""
    current = resolve_workspace_path(workspace, config_path=config_path)
    default = get_default_workspace_path(config_path)
    return current.resolve(strict=False) == default.resolve(strict=False)


def get_cli_history_path() -> Path:
    """Return the shared CLI history file path."""
    return get_global_home_dir() / "history" / "cli_history"


def get_bridge_install_dir() -> Path:
    """Return the shared WhatsApp bridge installation directory."""
    return get_global_home_dir() / "bridge"


def get_legacy_sessions_dir() -> Path:
    """Return the legacy global session directory used for migration fallback."""
    return get_global_home_dir() / "sessions"
