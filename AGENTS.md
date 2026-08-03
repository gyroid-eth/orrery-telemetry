# Working in this repository as an agent

This repo installs a coordination layer for local Claude Code and Codex agents:
a launcher, hooks, skills, and a live telemetry dashboard backed by
[mcp-agent-mail](https://github.com/Dicklesworthstone/mcp_agent_mail).

You are most likely reading this because the person you are working for said
something like "install this for me". That job is mostly *checking their
machine*, not running one command. What follows is the procedure and, more
importantly, the three things you must not do.

## The four rules

**1. Approvals belong to the human.** The installer asks before it merges
anything into `~/.claude/settings.json` or adds a managed block to a
`CLAUDE.md` / `AGENTS.md`. Those are the user's files. When you run the
installer non-interactively it *skips* those steps rather than assuming
consent — which means the install completes, the dashboard starts, and the
skills quietly do not work. Do not paper over this. Either hand the terminal
back so the user can answer, or use `--assume-yes` **only if they told you
to**. Never add that flag on your own initiative to get a clean run.

**2. Never invent a path or a value to get past an error.** The installer
stops when it cannot determine something — which database agent-mail is
actually using, whether the port is free, whether the interpreter is new
enough. Those stops are the product working. Guessing `AGENTSTACK_MAIL_DB`
to make the message go away will point the dashboard at a database that does
not exist, and the user will discover it days later as an empty screen.

**3. When the documented path fails, stop — do not find another one.** If a
tool, helper or transport this project relies on is missing or failing, use
only the recovery documented for it. If that does not restore it, report the
exact failure and stop that step. Do not substitute something that produces a
similar-looking result: reading the mailbox files instead of calling
`fetch_inbox`, polling in a loop instead of waiting for delivery, using the
runtime's built-in subagent instead of `/delegate`. Every one of those has
happened, and each time the task finished, the report came back, and the
product had not run. Nobody could tell from the outcome.

**4. Report what happened, including the parts you did not finish.** A
half-installed system that is described as installed costs the user far more
than an honest "this stopped here, and I do not know why". If you work around
something, say so in the same breath — a workaround is an unreported bug.

## Before you install anything

Check the machine first and tell the user what you found. Every one of these
has already broken a real install.

```sh
python3 --version                     # must be 3.10 or newer
command -v tmux git uv                # all three are required
lsof -i :8770                         # dashboard port; must be free
lsof -i :8765                         # agent-mail; see below
uname -s                              # macOS is the primary target
```

**If something is already listening on 8765**, the user likely has agent-mail
running. That is good and normal — the installer will detect it and reuse both
the server and its database. Do not stop it. Do not start a second one.

**If `python3` is older than 3.10**, do not upgrade their system Python.
Point `AGENTSTACK_PYTHON` at a newer interpreter they already have, or tell
them what to install.

## Installing

```sh
git clone https://github.com/gyroid-eth/claude-agent-stack.git
cd claude-agent-stack
./scripts/install.sh --dry-run        # writes nothing; read this output
```

Read the dry run with the user. It prints the planned service mode, the
agent-mail database it resolved, and the settings diff. If the resolved
database is not the one they actually use, stop and ask — that is worth more
than a completed install.

Then, for the real run, choose one of two paths.

**Default — hand back at the approvals.** Run `./scripts/install.sh` in a
terminal the user controls, or tell them to run it themselves. It pauses for a
typed `yes` four times — the settings merge, the `~/.claude.json` MCP entry,
the Codex `AGENTS.md` block, and the Claude `CLAUDE.md` block — plus once more
to confirm reuse of an already-running agent-mail. This is the right choice when you are unsure, and
when the user has not said otherwise.

**Opt-in — `./scripts/install.sh --assume-yes`.** Use this only when the user
has explicitly said they trust the repo and do not want to be asked. It
pre-approves the settings merge and the managed blocks, and prints one line
per item it approved so the decision stays auditable. It does **not** suppress
errors: the version, port, and database checks above still stop the install.

## After installing

```sh
export PATH="$HOME/.agentstack/bin:$PATH"
agentstack-doctor                     # is everything present and configured?
agentstack-selftest                   # does it actually work?
~/.agentstack/dashboard/agentctl.sh status
```

Those two answer different questions and you need both. `agentstack-doctor`
reports the actual service mode and whether the dashboard is running — not
merely registered. `agentstack-selftest` registers agents, sends mail between
them and reads it back, which is the only thing that distinguishes "installed"
from "works"; an install can pass every presence check and still be unable to
delegate. If either reports a warning, quote it to the user verbatim rather
than summarising it as "mostly fine".

A note on the dashboard: on a machine with no GUI session, or with the
display asleep, macOS may refuse to bootstrap a launchd job. The installer
detects this and supervises the process itself. `service mode:
supervised-background` in `agentctl.sh status` is a healthy outcome, not a
fallback you need to fix.

## When something goes wrong

Read `docs/troubleshooting.md` before improvising. If the failure is not
covered there, run

```sh
agentstack-doctor --report
```

and give the user the block it prints, between the `copy from here` and `copy
to here` markers, to file with the failure. It carries no tokens. Every defect
found so far has been a difference between the reporter's machine and the
developer's — the agent-mail commit, the name-enforcement mode, the database
schema, the file-descriptor limit — and that block answers all of those at
once instead of over several rounds of questions.

## Reference

| Topic | File |
|---|---|
| Install, requirements, service modes | `docs/install.md` |
| Configuration and environment variables | `docs/configuration.md` |
| Dashboard views and API | `docs/dashboard.md`, `docs/api.md` |
| Launchers and child agents | `docs/launchers.md` |
| Hooks | `docs/hooks.md` |
| Failure modes | `docs/troubleshooting.md` |

If you are changing this repository rather than installing it, read
`CONTRIBUTING.md` and run `PYTHONPATH=. python3 -m pytest -q` before you
report a change as done.
