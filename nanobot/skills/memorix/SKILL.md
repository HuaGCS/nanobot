---
name: memorix
description: Workspace memory skill for codebase history, design rationale, prior fixes, and reusable engineering knowledge via Memorix MCP tools.
homepage: https://github.com/AVIDS2/memorix
metadata: {"nanobot":{"emoji":"🧠"}}
---

# Memorix

Use Memorix as workspace memory for engineering work. It is for project history, design rationale,
previous fixes, failed attempts, and reusable implementation knowledge. It is not the user-profile
memory system.

nanobot automatically calls `memorix_session_start` when the Memorix MCP tools are connected, so
the active workspace is already bound before you start reasoning.

## When to use

Use Memorix when the task is about:

- Why the codebase is structured this way
- Prior refactors, regressions, workarounds, or incidents
- Repeated fixes, gotchas, and debugging patterns worth reusing
- Project-level architecture, subsystem relationships, or historical context
- Storing new implementation rationale after finishing meaningful engineering work

Do not use Memorix for:

- User preferences or personal facts
- Replacing the current source of truth in the repository
- Blindly trusting stale memories without checking the current code

## Read flow

Prefer this sequence:

1. `memorix_search` for the question or subsystem
2. `memorix_detail` for a promising result
3. Read the current code and reconcile any mismatch

If the result is clearly outdated, say so and treat current code as authoritative.

## Write flow

After completing a meaningful code or debugging task, store compact engineering knowledge:

- what changed
- why it changed
- what constraint or bug it addressed
- what future maintainers should remember

Use:

- `memorix_store` for durable project knowledge
- `memorix_store_reasoning` for implementation rationale or debugging conclusions

Keep stored entries specific and concise. Prefer one solid memory over many noisy fragments.

## Scope

Default to project-scoped memories tied to the active workspace. Use broader scopes only when the
knowledge is intentionally cross-project.

## Safety

- Verify critical memories against the current repository state
- Do not copy secrets, tokens, or raw private chat logs into Memorix
- If Memorix is unavailable or returns nothing useful, continue with normal code inspection
