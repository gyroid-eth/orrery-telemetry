# Claude Code in this repository

The full procedure for agents lives in [AGENTS.md](AGENTS.md) — read it before
you install anything. The rules below are repeated here because they are the
ones that cause real damage when skipped, and you should not have to follow a
link to learn them.

**Approvals belong to the human.** The installer asks before merging into
`~/.claude/settings.json`, before adding the `mcp-agent-mail` entry to
`~/.claude.json`, and before adding a managed block to their `CLAUDE.md` or
`AGENTS.md` — four typed confirmations, plus one to reuse a running
agent-mail. Note that those are the user's own files elsewhere on disk, not
this one. Run non-interactively, it skips those steps instead of assuming
consent — the install "succeeds", the dashboard starts, and the skills
silently do not work. Hand the terminal back so the user can answer, or pass
`--assume-yes` **only because they told you to**, never to get a clean run.

**Never invent a value to get past an error.** The installer stops when it
cannot determine which database agent-mail uses, whether the port is free, or
whether the interpreter is new enough. Those stops are the product working.
Setting `AGENTSTACK_MAIL_DB` to a plausible-looking path will point the
dashboard at a database that does not exist.

**When the documented path fails, stop — do not find another one.** Use only
the recovery documented for the thing that failed. If that does not restore
it, report the exact failure and stop that step. Do not reach for something
that produces a similar-looking result: reading mailbox files instead of
calling `fetch_inbox`, a polling loop instead of waiting for delivery, the
built-in Agent or Task tool instead of `/delegate`. Each of those has happened
here. Each time the work finished, the report came back, and this project had
not run — and the outcome gave no sign of it.

**Report the parts you did not finish.** A half-installed system described as
installed is worse than an honest "this stopped here". If you worked around
something, say so in the same breath.

Start with the machine checks in [AGENTS.md](AGENTS.md#before-you-install-anything),
then `./scripts/install.sh --dry-run`, and read that output with the user
before doing anything that writes. Afterwards run both `agentstack-doctor` and
`agentstack-selftest`: the first says everything is present, the second says
it works, and only the second can tell you whether delegation is possible.

If something fails and the docs do not cover it, `agentstack-doctor --report`
prints a token-free block describing this machine. Give it to the user to file
with the bug.
