---
name: log
description: Create a structured session log, preferably in an Obsidian vault with daily-note and graph integration, or fall back to a local logs directory when Obsidian is unavailable.
argument-hint: <theme> [project]
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
user-invocable: true
maturity: draft
tags:
  - maturity/draft
---

# Session Log

Use this skill when the user asks to create a session log, summarize the current work, preserve decisions, or record what changed.

Always get the current timestamp from the shell. Do not rely on the model's internal clock.

```bash
date '+%Y-%m-%dT%H%M'
date '+%Y-%m-%d'
```

## Destination Strategy

Use a two-layer destination strategy.

### 1. Obsidian Mode, Recommended

Use Obsidian mode when both conditions are true:

- `AGENTSTACK_OBSIDIAN_APP` is set and points to an Obsidian-capable launcher or CLI.
- `AGENTSTACK_PROJECT_KEY` points to an Obsidian vault or a project directory managed inside a vault.

In this mode, create the log inside the vault so it benefits from backlinks, tags, graph view, search, and daily-note navigation.

Generic placement:

- If the current project has a `logs/` directory, write there.
- Otherwise create or use a project-level `logs/` directory under the vault or current project.
- Use a generic daily-note location configured by the project if one exists. If the project does not define one, skip the daily-note link rather than inventing a private directory convention.

Use frontmatter suitable for Obsidian:

```yaml
---
tags: [claude]
agent: <agent-name-if-known>
maturity: draft
---
```

If a daily note exists, add a link under its log section. Re-read the daily note immediately before editing because another agent may have updated it.

Daily-note entry format:

```markdown
- HH:mm [[LOG_YYYY-MM-DDTHHmm Theme]]
```

Keep entries sorted by time when there are existing entries.

### 2. Fallback Mode

Use fallback mode when Obsidian is not configured or the current work is outside a vault.

Destination:

```text
<git-root-or-current-working-directory>/logs/LOG_<timestamp> <theme>.md
```

Fallback mode is intentionally minimal:

- Create `logs/` if needed.
- Write the log file.
- Do not edit daily notes.
- Use normal Markdown links or relative paths.

## Argument Handling

The first argument is the theme and is required. Use a concise filesystem-safe theme.

The second argument is optional. If present, treat it as a project hint and use it to find the most relevant `logs/` directory.

If the destination is ambiguous and a reasonable project `logs/` directory cannot be inferred, ask the user one concise question before writing.

## Related Context Search

Before writing, search for related prior logs and notes:

```bash
rg -n "<keyword>" logs
rg --files | rg '(^|/)LOG_[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{4} .+\.md$'
```

Link prior logs that explain why the current work exists, important earlier decisions, or known failure modes.

When in Obsidian mode, prefer wiki links for notes inside the vault:

```markdown
[[LOG_YYYY-MM-DDTHHmm Previous Theme]]
[[Related Note]]
```

When in fallback mode, use relative Markdown links:

```markdown
[Previous log](logs/LOG_YYYY-MM-DDTHHmm Previous Theme.md)
```

## File Name

Use this format:

```text
LOG_<YYYY-MM-DDTHHmm> <Theme>.md
```

Examples:

```text
LOG_2026-01-15T0930 API Migration.md
LOG_2026-01-15T1412 Test Repair.md
```

## Log Template

```markdown
---
tags: [claude]
agent: <agent-name-if-known>
maturity: draft
---

## Goal

What this session attempted to accomplish.

## Decisions

The important options considered, tradeoffs, and final choices.

## Work Performed

Changed files, created artifacts, commands run, and implementation notes.

## Verification

Tests, checks, screenshots, review steps, or reasons verification was not run.

## Related Notes

- [[Related prior log or note]] - why it matters

## Next Actions

- [ ] Remaining task, if any
```

Omit empty sections only when they do not apply.

## Attachments

If the session includes screenshots, generated images, reports, or other artifacts that matter for later review:

- In Obsidian mode, place attachments in a project-appropriate attachment directory if one exists, then use Obsidian links.
- In fallback mode, place attachments under `logs/assets/` or link to their relative path.
- Do not move user files when a link is enough.

## Editing Existing Logs

When updating an existing log:

- Read the whole note first.
- Integrate new facts into the right section instead of appending scattered fragments.
- Avoid duplicate statements.
- Preserve links and frontmatter unless they are wrong.

## Quality Rules

- Record facts, decisions, and verification, not a transcript.
- Use exact file names and command names where useful.
- State uncertainty and skipped verification explicitly.
- Do not put random child-agent names in prose unless the identity itself matters; prefer roles such as "review child" or "test child".
- Keep the log useful to someone returning weeks later.
