# Contributing

## Test environment

`packages/agentstack_mail` has real dependencies (fastmcp, sqlmodel,
python-decouple, …), so a bare `python3 -m pytest` cannot even collect its
tests. Create the repo-local venv once and run the suite from it:

```bash
python3 -m venv .venv
.venv/bin/pip install -e packages/agentstack_mail pytest pytest-asyncio
PYTHONPATH=. .venv/bin/python -m pytest -q
```

`.venv/` is gitignored. Do not install anything into a service venv under
`~/.agentstack/` — those are production artifacts whose contents are pinned
by cutover receipts.

Two caveats learned by measurement:

- Do not pipe pytest through `tail` without `pipefail`: the pipeline exits
  with `tail`'s status and a red suite reads as exit 0.
- Run the suite without other heavy processes (or a second concurrent
  pytest): the SIGKILL-timing parity tests in
  `packages/agentstack_mail/tests/test_pending_decision_d8_d9.py` can fail
  under CPU contention and pass in a clean single run.

## Regression priority: a truly fresh install first

The first environment this project protects is a machine with no existing
ORRERY Mail database, virtual environment, or running service. Keeping an
existing installation working alongside local state is important, but it comes
second. A change that passes only by reusing a developer's machine is not done.

Installer tests construct isolated homes, fake external commands, and local
HTTP servers so the complete bundled-service path is exercised without reading
or changing a developer's production state. The package service tests cover
real process startup and message persistence from the repository-local venv.

## Docs Definition Of Done

When a change modifies behavior, startup flow, install behavior, or agent
coordination rules, review the docs in the same PR before calling the work
done:

- `README.md` and `README.en.md` (the English quick start and document table
  must list the same steps and guides as the Japanese original)
- `AGENTS.md` (the instructions an installing agent follows)
- `docs/install.md`, `docs/hooks.md` and the reference the change touches
- `claude/CLAUDE.md`
- `codex/AGENTS.md`

Facts that drift silently — the default agent-mail port, the number of
approval prompts, the number of Claude event hooks, the guide list — are
checked against the implementation by `tests/test_docs_consistency.py`.
Extend that test when you add such a fact instead of relying on review.

The docs must not contradict the implementation. If no docs change is needed,
that should be an explicit review decision, not an accidental omission.

## Shell compatibility (macOS bash 3.2)

The launchers and hooks use `#!/bin/bash`, and macOS ships GNU bash **3.2** as
`/bin/bash`, so they must run correctly there — not only on a newer homebrew
bash. The most common trap is a **self-referencing `local`/`declare`**: bash 4+/5
make an earlier name in the same statement visible to a later initializer, but
bash 3.2 does not, so under `set -u` it aborts with `<name>: unbound variable`.

```bash
# BROKEN on bash 3.2:
local agent_name="$1" state_file="$CHILD_STATE_DIR/$agent_name.json"
# OK — split into two statements:
local agent_name="$1"
local state_file="$CHILD_STATE_DIR/$agent_name.json"
```

Before pushing shell changes, run the tests (pure stdlib, no dependencies):

```bash
for t in tests/test_*.py; do python3 "$t"; done
```

`tests/test_bash32_local_selfref.py` fails the build on any self-referencing
`local`/`declare`. When feasible, also exercise the actual code path on
`/bin/bash` (3.2), not just a newer bash.

## License of contributions

Work written for ORRERY Telemetry in this repository ("AgentStack" in file names is the former project name) is under the
[PolyForm Perimeter License 1.0.1](LICENSE). It is source-available, not open
source in the OSI sense: you may use, modify, and redistribute the software
for any purpose except providing others with a product that competes with it.
The OpenAI/Anthropic Rider belongs only to copied or derived AgentMail
components; see [the third-party boundary](docs/third-party.md).

By submitting new work written for ORRERY Telemetry you agree that it is licensed under
the PolyForm terms. Changes to derived AgentMail components must retain the
upstream copyright and full upstream license, including its rider.

By submitting a contribution you keep your copyright, but you grant the
maintainer a perpetual, worldwide, non-exclusive, royalty-free licence to use,
reproduce, modify, distribute, sublicense and relicense the contribution under
any terms, including commercial ones. You confirm that you have the right to
grant this licence. Small fixes are welcome without further paperwork; this
paragraph is the whole agreement.
