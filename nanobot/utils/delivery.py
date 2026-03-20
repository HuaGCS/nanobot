"""Helpers for workspace-scoped delivery artifacts."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote, urljoin

from loguru import logger

from nanobot.utils.helpers import detect_image_mime


def delivery_artifacts_root(workspace: Path) -> Path:
    """Return the workspace root used for generated delivery artifacts."""
    return workspace.resolve(strict=False) / "out"


def is_image_file(path: Path) -> bool:
    """Return True when a local file looks like a supported image."""
    try:
        with path.open("rb") as f:
            header = f.read(16)
    except OSError:
        return False
    return detect_image_mime(header) is not None


def resolve_delivery_media(
    media_path: str | Path,
    workspace: Path,
    media_base_url: str = "",
) -> tuple[Path | None, str | None, str | None]:
    """Resolve a local delivery artifact and optionally map it to a public URL."""

    source = Path(media_path).expanduser()
    try:
        resolved = source.resolve(strict=True)
    except FileNotFoundError:
        return None, None, "local file not found"
    except OSError as e:
        logger.warning("Failed to resolve local delivery media path {}: {}", media_path, e)
        return None, None, "local file unavailable"

    if not resolved.is_file():
        return None, None, "local file not found"

    artifacts_root = delivery_artifacts_root(workspace)
    try:
        relative_path = resolved.relative_to(artifacts_root)
    except ValueError:
        return None, None, f"local delivery media must stay under {artifacts_root}"

    if not is_image_file(resolved):
        return None, None, "local delivery media must be an image"

    if not media_base_url:
        return resolved, None, None

    media_url = urljoin(
        f"{media_base_url.rstrip('/')}/",
        quote(relative_path.as_posix(), safe="/"),
    )
    return resolved, media_url, None
