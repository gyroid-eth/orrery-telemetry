# Working in this repository as an agent

This repo installs a coordination layer for local Claude Code and Codex agents:
a launcher, hooks, skills, and a live telemetry dashboard backed by the
bundled ORRERY Mail service.

You are most likely reading this because the person you are working for said
something like "install this for me". That job is mostly *checking their
machine*, not running one command. What follows is the procedure and, more
importantly, the three things you must not do.

## The four rules

**1. Approvals belong to the human.** Follow the single
[consent contract](docs/instruction-migration.en.md#consent) for installer
and standalone setup. Hand the terminal back at human approval steps. Use
`--assume-yes` only when the user explicitly chose it.

**2. Never invent a path or a value to get past an error.** The installer
stops when it cannot determine something — which database ORRERY Mail is
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
python3 --version                     # must be 3.11 or newer
command -v tmux git uv                # all three are required
lsof -i :8770                         # dashboard port; must be free
lsof -i :18765                        # ORRERY Mail (default port; see below)
uname -s                              # macOS is the primary target
```

**If something is already listening on 18765** (or on the port named by an
existing `AGENTSTACK_MCP_URL`), the user likely has ORRERY Mail running. That is
good and normal — the installer will detect it and reuse both the server and
its database. Do not stop it. Do not start a second one.

**If `python3` is older than 3.11**, do not upgrade their system Python.
Point `AGENTSTACK_PYTHON` at a newer interpreter they already have, or tell
them what to install.

## Installing

```sh
git clone https://github.com/gyroid-eth/orrery-telemetry.git
cd orrery-telemetry
./scripts/install.sh --project-key /absolute/project/path --dry-run
```

The project key is a coordination namespace, not the reservation scope. Top-level and delegated-child
launches protect their selected working directory plus `AGENTSTACK_EXTRA_PROTECTED_ROOTS`; the Git root is not added automatically.
Install/update automatically migrates the installed legacy protection list once,
preserving its literal value and order; it never imports a shell's old runtime
roots. Review the migration source, value, and destination in `--dry-run`.
Finish work or release active reservations before cutover, then restart affected
sessions together after updating. See `docs/configuration.md` for precedence,
explicit empty values, and invalid legacy settings that stop the update.

Read the dry run with the user. It prints the planned service mode, the
ORRERY Mail database it resolved, and the settings diff. If the resolved
database is not the one they actually use, stop and ask — that is worth more
than a completed install.

Then, for the real run, choose one of two paths.

Run the installer in a terminal the user controls, or tell them to run it.
The [consent contract](docs/instruction-migration.en.md#consent) explains
interactive prompts, non-interactive skips, and explicit `--assume-yes`.
Reuse of a running ORRERY Mail is reported without another approval.

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
developer's — the ORRERY Mail commit, the name-enforcement mode, the database
schema, the file-descriptor limit — and that block answers all of those at
once instead of over several rounds of questions.

## Reference

For local daemon helper development use [docs/daemon-agents.md](docs/daemon-agents.md). Its operator-only creation is not a startup ritual for ordinary agents; live enrollment and deployment require separate approval.

| Topic | File |
|---|---|
| Install, requirements, service modes | `docs/install.md` |
| Configuration and environment variables | `docs/configuration.md` |
| Dashboard views and API | `docs/dashboard.md`, `docs/api.md` |
| Launchers and child agents | `docs/launchers.md` |
| Hooks | `docs/hooks.md` |
| Failure modes | `docs/troubleshooting.md` |

If you are changing this repository rather than installing it, read
`CONTRIBUTING.md`, create the dev venv once (see "Test environment" there —
the mail package's dependencies are not importable from a bare `python3`),
and run `PYTHONPATH=. .venv/bin/python -m pytest -q` before you report a
change as done.

隔離 global client の子・再開・予約 hook の準備は [global child client](docs/global-child-client.md) を参照してください。実起動と配送は [global delivery client](docs/global-delivery-client.md) を参照してください。既定の旧運用は維持し、実サービスの切替は後続です。

User instruction migration uses the shared setup plan/receipt engine. Read [instruction migration](docs/instruction-migration.md) before editing user blocks. Bare sessions do not auto-register; editing guards retain their configured protection.
