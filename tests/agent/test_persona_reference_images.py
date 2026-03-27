"""Tests for persona reference-image metadata resolution."""

from __future__ import annotations

import json
from pathlib import Path

from nanobot.agent.personas import (
    load_persona_reference_images,
    resolve_persona_reference_image,
)


def test_persona_reference_images_resolve_persona_and_workspace_relative_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    persona_dir = workspace / "personas" / "Aria"
    assets_dir = persona_dir / "assets"
    metadata_dir = persona_dir / ".nanobot"
    assets_dir.mkdir(parents=True)
    metadata_dir.mkdir(parents=True)

    default_image = assets_dir / "default.png"
    beach_image = assets_dir / "beach.png"
    winter_image = assets_dir / "winter.png"
    for path in (default_image, beach_image, winter_image):
        path.write_bytes(b"image")

    (metadata_dir / "st_manifest.json").write_text(
        json.dumps(
            {
                "reference_image": "assets/default.png",
                "reference_images": {
                    "Beach": "assets/beach.png",
                    "winter": "personas/Aria/assets/winter.png",
                },
            }
        ),
        encoding="utf-8",
    )

    refs = load_persona_reference_images(workspace, "Aria")

    assert refs.default == str(default_image.resolve())
    assert refs.scenes == {
        "beach": str(beach_image.resolve()),
        "winter": str(winter_image.resolve()),
    }
    assert resolve_persona_reference_image(workspace, "Aria", "BEACH") == str(beach_image.resolve())
    assert resolve_persona_reference_image(workspace, "Aria", "missing") == str(
        default_image.resolve()
    )
