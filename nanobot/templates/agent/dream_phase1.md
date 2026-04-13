Compare conversation history against current memory files. Also scan memory files for stale content — even if not mentioned in history.

Output one line per finding:
[FILE] atomic fact (not already in memory)
[FILE-REMOVE] reason for removal
[SKILL] kebab-case-name: one-line description of the reusable pattern

Files:
- PROFILE (user facts, preferences, long-term habits)
- INSIGHTS (learned collaboration patterns, decision heuristics, recurring pitfalls)
- USER (relationship framing, boundaries, interaction stance)
- SOUL (bot behavior, tone)
- MEMORY (knowledge, project context)

Rules:
- Atomic facts: "has a cat named Luna" not "discussed pet care"
- Corrections: [PROFILE] location is Tokyo, not Osaka
- Use [INSIGHTS] for proven workflow guidance, not raw biography
- PROFILE: prefer directly stated facts or stable repeated preferences; if a useful detail is still tentative, keep only one canonical bullet and prefer metadata like `<!-- nanobot-meta: confidence=low -->` instead of relying only on `(verify)`
- INSIGHTS: keep only patterns validated by explicit user feedback or repeated successful turns; one-off hunches do not belong here
- For PROFILE / INSIGHTS bullets, `confidence` should be `low`, `medium`, or `high`
- When the current batch explicitly reconfirms a PROFILE / INSIGHTS bullet, preserve or add `last_verified=YYYY-MM-DD`
- If new information contradicts an existing PROFILE or INSIGHTS bullet, replace the old bullet instead of keeping both versions
- Prefer one canonical bullet per fact or pattern
- Capture confirmed approaches the user validated

Staleness — flag for [FILE-REMOVE]:
- Time-sensitive data older than 14 days: weather, daily status, one-time meetings, passed events
- Completed one-time tasks: triage, one-time reviews, finished research, resolved incidents
- Resolved tracking: merged/closed PRs, fixed issues, completed migrations
- Detailed incident info after 14 days — reduce to one-line summary
- Superseded: approaches replaced by newer solutions, deprecated dependencies

Skill discovery — flag [SKILL] when ALL of these are true:
- A specific, repeatable workflow appeared 2+ times in the conversation history
- It involves clear steps (not vague preferences like "likes concise answers")
- It is substantial enough to warrant its own instruction set (not trivial like "read a file")
- Do not worry about duplicates — the next phase will check against existing skills

Do not add: current weather, transient status, temporary errors, conversational filler.

[SKIP] if nothing needs updating.
