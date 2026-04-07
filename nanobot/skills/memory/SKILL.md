---
name: memory
description: Two-layer memory system with structured archive recall and grep fallback.
always: true
---

# Memory

## Structure

- `memory/MEMORY.md` — Long-term facts (preferences, project context, relationships). Always loaded into your context.
- `memory/HISTORY.md` — Append-only event log. NOT loaded into context. Search it with grep-style tools or in-memory filters. Each entry starts with [YYYY-MM-DD HH:MM].
- `memory/archive/index.jsonl` + `memory/archive/chunks/*.json` — Structured archived conversation chunks for `history_search` / `history_expand`.

## Search Past Events

`memory/history.jsonl` is JSONL format — each line is a JSON object with `cursor`, `timestamp`, `content`.

- Preferred: use `history_search` first, then `history_expand` on a promising archive id
- Small `memory/HISTORY.md`: use `read_file`, then search in-memory
- Large or long-lived `memory/HISTORY.md`: use the `exec` tool for targeted search

Examples:
- `history_search(query="providerPool admin", limit=5)`
- `history_expand(id="20260330T120501_cli_direct_a1b2c3", maxMessages=20)`
- **Linux/macOS:** `grep -i "keyword" memory/HISTORY.md`
- **Windows:** `findstr /i "keyword" memory\HISTORY.md`
- **Cross-platform Python:** `python -c "from pathlib import Path; text = Path('memory/HISTORY.md').read_text(encoding='utf-8'); print('\n'.join([l for l in text.splitlines() if 'keyword' in l.lower()][-20:]))"`

Prefer structured archive tools first. Fall back to targeted command-line search when the answer is likely only in `HISTORY.md` or you need raw grep behavior.

## When to Update MEMORY.md

Write important facts immediately using `edit_file` or `write_file`:
- User preferences ("I prefer dark mode")
- Project context ("The API uses OAuth2")
- Relationships ("Alice is the project lead")

## Auto-consolidation

Old conversations are automatically summarized into `HISTORY.md`, extracted into `MEMORY.md`, and written to structured archive chunks under `memory/archive/`. You usually do not need to manage this manually.
