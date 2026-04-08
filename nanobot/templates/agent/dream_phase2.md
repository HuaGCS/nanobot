Update memory files based on the analysis below.
- [FILE] entries: add the described content to the appropriate file
- [FILE-REMOVE] entries: delete the corresponding content from memory files

## File paths (relative to workspace root)
- SOUL.md
- USER.md
- PROFILE.md
- INSIGHTS.md
- memory/MEMORY.md

Do NOT guess paths.

## Editing rules
- Edit directly — file contents provided below, no read_file needed
- If `PROFILE.md` or `INSIGHTS.md` is missing and you need it, create it with `write_file`
- Keep file roles strict: PROFILE = stable user facts/preferences, INSIGHTS = collaboration heuristics, USER = relationship framing, SOUL = persona identity, MEMORY = project/work context
- Use exact text as old_text, include surrounding blank lines for unique match
- Batch changes to the same file into one edit_file call
- For deletions: section header + all bullets as old_text, new_text empty
- Resolve contradictions by editing or deleting older bullets; never leave stale and corrected versions side by side
- If a surviving bullet is tentative, mark it once with "(verify)" instead of creating duplicate maybe-variants
- Merge near-duplicate bullets into one canonical line when possible
- Surgical edits only — never rewrite entire files
- If nothing to update, stop without calling tools

## Quality
- Every line must carry standalone value
- Concise bullets under clear headers
- When reducing (not deleting): keep essential facts, drop verbose details
- If uncertain whether to delete, keep but add "(verify currency)"
