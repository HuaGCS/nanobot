---
name: clawhub
description: Search and install agent skills from ClawHub, the public skill registry.
homepage: https://clawhub.ai
metadata: {"nanobot":{"emoji":"🦞"}}
---

# ClawHub

Public skill registry for AI agents. Search by natural language (vector search).

## When to use

Use this skill when the user asks any of:
- "find a skill for …"
- "search for skills"
- "install a skill"
- "what skills are available?"
- "update my skills"

## Search

Query the live registry API directly:

```bash
curl 'https://lightmake.site/api/skills?page=1&pageSize=5&sortBy=score&order=desc&keyword=web%20scraping' \
  -H 'accept: */*' \
  -H 'origin: https://skillhub.tencent.com' \
  -H 'referer: https://skillhub.tencent.com/'
```

## Install

```bash
npx --yes clawhub@latest --workdir <nanobot-workspace> --no-input install <slug>
```

Replace `<slug>` with the skill name from search results. Replace `<nanobot-workspace>` with the
active workspace for the current nanobot process. This places the skill into
`<nanobot-workspace>/skills/`, where nanobot loads workspace skills from. Always include
`--workdir`.

## Update

```bash
npx --yes clawhub@latest --workdir <nanobot-workspace> --no-input update --all
```

## List installed

```bash
npx --yes clawhub@latest --workdir <nanobot-workspace> --no-input list
```

## Uninstall from nanobot workspace

Current ClawHub docs do not document a local uninstall subcommand. In nanobot, remove a
workspace-installed skill with:

```text
/skill uninstall <slug>
```

This deletes `<nanobot-workspace>/skills/<slug>` and best-effort prunes
`<nanobot-workspace>/.clawhub/lock.json`.

## Notes

- Search uses the public registry API directly and does not require Node.js.
- Install/list/update require Node.js (`npx` comes with it).
- No API key needed for search and install.
- Login (`npx --yes clawhub@latest login`) is only required for publishing.
- `--workdir <nanobot-workspace>` is critical — without it, skills install to the current directory
  instead of the active nanobot workspace.
- Keep global options before the subcommand: `--workdir ... --no-input install ...`.
- After install, remind the user to start a new session to load the skill.
