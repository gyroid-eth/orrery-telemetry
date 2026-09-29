# Using it with Obsidian

> 日本語版: [obsidian.md](obsidian.md)

[Back to README](../README.en.md)

ORRERY Telemetry works without Obsidian. This page shows one useful way to work: using an Obsidian vault as the place where your agents work.

![Typing /adddone to an agent adds the finished task to "Done today" in the Obsidian Daily Note](img/obsidian-daily.gif)

## What you get

- **What agents produce becomes Markdown notes as it is**
  - Work logs from `/log`: what the session decided, changed, and checked
  - Paper notes from [digest-paper](https://github.com/gyroid-eth/orrery-digest-paper) (an ORRERY add-on): written by Claude and checked by Codex against the text and figures
  - Tasks from `/addtodo` and `/adddone`: cards on a Kanban board
- **People only need to read them in Obsidian**
  - The Daily Note uses Dataview to list the day's work logs and finished tasks automatically. When an agent writes a log, a link appears in that day's Daily Note
  - A Kanban board shows how the tasks are going. Checking a card adds the time it was done and moves it to the done column
- **Open them from the cockpit**: in the terminals of [ORRERY cockpit](https://github.com/gyroid-eth/orrery), a note path an agent prints opens in Finder (Explorer on Windows) with a click

## Getting started

### (a) Start from the demo vault

[gyroid-eth/orrery-demo-vault](https://github.com/gyroid-eth/orrery-demo-vault) is a vault where you can try all of the above right away.

- A Daily Note template (today's tasks, done today, and today's work logs, shown with Dataview)
- A Kanban task board (`01_Planning/タスク.md`) and a plugin that records when a task was done (Task Done At)
- Skills for agents: `/log` (work logs in `05_Agents/`), `/addtodo` and `/adddone` (Kanban cards)
- A QuickAdd macro for people: "タスク追加" (add a task)
- `CLAUDE.md` / `AGENTS.md` (the rules agents follow in this vault)
- The PDF conversion plugin that digest-paper uses (pdf-mistral), and one CC BY paper to try it on

Download it, open it in Obsidian, and follow `00_Inbox/はじめに.md` from the top.

### (b) Add it to your own vault

To add this to the vault you already use, copy these from the demo vault.

1. `.claude/skills/log`, `addtodo`, and `adddone` into your vault's `.claude/skills/`
2. `30_Templates/Daily Note.md` into your Daily Note template (change the folder names `05_Agents` and `01_Planning` to match your vault)
3. Write where logs and tasks go in `CLAUDE.md` (and `AGENTS.md` if you use Codex)
4. Install the Obsidian community plugins Dataview, Kanban, and Templater (and Task Done At from the demo vault if you want the done time added automatically)

Start the agent in the vault folder (`cd <vault> && claude`). In the demo vault, the agent follows the vault's `CLAUDE.md` and skills and writes logs to `05_Agents/` (confirmed in the example below). To write into the vault with the `/log` bundled with ORRERY Telemetry instead of the vault's skill, use `AGENTSTACK_OBSIDIAN_APP` in [Configuration: Skill](configuration.en.md#skill).

## Example (2026-09-29, checked on Windows with WSL2)

We put the demo vault in a Windows folder, opened it in Obsidian on Windows, and went through this flow with agents inside WSL2.

1. Convert a paper's PDF to Markdown and figures with pdf-mistral in Obsidian
2. Ask the Claude in WSL to run digest-paper. After the exchange with the Codex reviewer, the note is saved to the vault's `10_Reference/Notes/` with `review_status: checked`
3. Type `/log` to the Claude in WSL. `05_Agents/LOG_<time> <theme>.md` is created and shows up automatically under "today's work logs" in the Daily Note in Obsidian on Windows
4. Add a card with "タスク追加" in Obsidian (or `/addtodo` to the agent), then say `/adddone` to the agent. The card moves to the done column and shows up under "done today" in the Daily Note

## Notes for Mac and Windows (WSL2)

- **Mac**: keep the vault where you usually do (for example `~/Documents/MyVault`) and start agents inside it
- **Windows**: keep the vault in a Windows folder (for example `C:\Users\<you>\Documents\MyVault`) and open it in Obsidian on Windows. Agents inside WSL2 use that folder as `/mnt/c/Users/<you>/Documents/MyVault` (`wslpath -u 'C:\Users\...'` converts it)
- **digest-paper's work in progress** (drafts, figures, reviews) stays on the WSL side in `~/.agentstack/addons/digest-paper/runs/`. Only the finished note and its check record (`evidence/`) are written to the vault
- **The dashboard's Output**: a dashboard card's Output lists `LOG_*.md` in the project's `logs/` ([Configuration](configuration.en.md)). In a vault that writes logs to `05_Agents/`, like the demo vault, read them in the Obsidian Daily Note rather than in Output

## Related documents

- [Installation](install.en.md) (Obsidian as an optional dependency)
- [Configuration](configuration.en.md) (`AGENTSTACK_VAULT`, `AGENTSTACK_OBSIDIAN_APP`)
- [Dashboard](dashboard.en.md) (a card's Output)
