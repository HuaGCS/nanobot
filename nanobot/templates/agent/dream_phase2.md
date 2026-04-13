Update memory files based on the analysis below.
- [FILE] entries: add the described content to the appropriate file
- [FILE-REMOVE] entries: delete the corresponding content from memory files
- [SKILL] entries: create a new skill under skills/<name>/SKILL.md using write_file

## File paths (relative to workspace root)
- SOUL.md
- USER.md
- PROFILE.md
- INSIGHTS.md
- memory/MEMORY.md
- skills/<name>/SKILL.md (for [SKILL] entries only)

Do NOT guess paths.

## Editing rules
- Edit directly — file contents provided below, no read_file needed
- If `PROFILE.md` or `INSIGHTS.md` is missing and you need it, create it with `write_file`
- Keep file roles strict: PROFILE = stable user facts/preferences, INSIGHTS = collaboration heuristics, USER = relationship framing, SOUL = persona identity, MEMORY = project/work context
- For touched PROFILE / INSIGHTS bullets, prefer structured metadata comments over raw `(verify)`, for example `- Prefers short review loops. <!-- nanobot-meta: confidence=high last_verified=2026-04-08 -->`
- `confidence` must be one of `low`, `medium`, or `high`
- Only set `last_verified=YYYY-MM-DD` when the current batch explicitly reconfirms the fact or pattern
- Legacy `(verify)` markers may remain on untouched bullets, but normalize them when editing that bullet
- Use exact text as old_text, include surrounding blank lines for unique match
- Batch changes to the same file into one edit_file call
- For deletions: section header + all bullets as old_text, new_text empty
- Resolve contradictions by editing or deleting older bullets; never leave stale and corrected versions side by side
- If a surviving bullet is tentative, keep one canonical bullet with `confidence=low` instead of creating duplicate maybe-variants
- Merge near-duplicate bullets into one canonical line when possible
- Surgical edits only — never rewrite entire files
- If nothing to update, stop without calling tools

## Skill creation rules (for [SKILL] entries)
- Use write_file to create skills/<name>/SKILL.md
- Before writing, read_file `{{ skill_creator_path }}` for format reference (frontmatter structure, naming conventions, quality standards)
- **Dedup check**: read existing skills listed below to verify the new skill is not functionally redundant. Skip creation if an existing skill already covers the same workflow.
- Include YAML frontmatter with name and description fields
- Keep SKILL.md under 2000 words — concise and actionable
- Include: when to use, steps, output format, at least one example
- Do NOT overwrite existing skills — skip if the skill directory already exists
- Reference specific tools the agent has access to (read_file, write_file, exec, web_search, etc.)
- Skills are instruction sets, not code — do not include implementation code

## Quality
- Every line must carry standalone value
- Concise bullets under clear headers
- When reducing (not deleting): keep essential facts, drop verbose details
- If uncertain whether to delete, keep one canonical bullet and lower confidence instead of inventing duplicate hedges
