---
name: living-together
description: Shared-life companion skill that turns travel, celebration, and daily companionship moments into persona-aware images.
always: true
---

# Living Together

Use this skill when the user clearly wants the persona to "be there" with them: shared travel, daily life, celebrations, comforting moments, or explicit requests for a photo together.

## Trigger Guidance

Prefer generating an image when any of these are true:

- The user sends a photo and invites the persona into the scene.
- The user asks for a joint photo, commemorative image, or "what if you were here" scene.
- The conversation is about companionship, co-living, or a shared everyday moment that benefits from a visual response.

Do not force image generation for ordinary chat that has no clear visual or companionship intent.

## How To Use The Current nanobot Stack

- Use the built-in `image_gen` tool for image creation.
- If the active persona provides reference images, prefer `__default__` or `__default__:scene` so appearance stays consistent.
- Generated files are written under `workspace/out/image_gen`; send them to the user through the `message` tool in the same turn when appropriate.

## Prompting Rules

- Preserve the user's original scene if they provided a photo. Ask the model to insert the persona into the existing background instead of recreating the whole scene.
- Describe concrete scene details: place, lighting, weather, objects, pose, clothing, and emotional tone.
- Keep anatomy and placement natural. Avoid vague prompts like "romantic scene" when a more specific description is possible.
- Match clothing and scene logic to the environment. Outdoor scenes should explicitly include reasonable shoes and outfit details.

## Delivery Pattern

When a single response should include both text and image:

1. Call `image_gen`.
2. Call `message` with both the final text and the generated media path.

Prefer one combined reply instead of sending disconnected text and image messages.

## Memory

If the user repeatedly prefers certain visual styles, outfits, or scene types, store that as durable preference or persona memory so future images stay consistent.
