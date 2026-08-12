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
agent-mail clone, database, virtual environment, or running service. Keeping an
existing installation working alongside local state is important, but it comes
second. A change that passes only by reusing a developer's machine is not done.

The fast installer tests use fakes to cover our shell logic. The real boundary
test clones upstream agent-mail, runs a real `uv sync`, starts the real server,
and runs the installed `agentstack-selftest` through message exchange and the
dashboard. It is mandatory in CI and opt-in locally because it downloads
dependencies and uses the network:

```bash
AGENTSTACK_E2E=1 PYTHONPATH=. python3 -m pytest -q \
  tests/test_fresh_install_e2e.py
```

Local opt-in may skip when `git`, `uv`, or `tmux` is unavailable. The dedicated
CI job treats any missing prerequisite as a failure.

## Docs Definition Of Done

When a change modifies behavior, startup flow, install behavior, or agent
coordination rules, review the docs in the same PR before calling the work
done:

- `README.md`
- `claude/CLAUDE.md`
- `codex/AGENTS.md`

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

AgentStack-authored work in this repository is under the
[PolyForm Perimeter License 1.0.1](LICENSE). It is source-available, not open
source in the OSI sense: you may use, modify, and redistribute the software
for any purpose except providing others with a product that competes with it.
The OpenAI/Anthropic Rider belongs only to copied or derived AgentMail
components; see [the third-party boundary](docs/third-party.md).

By submitting new AgentStack-authored work you agree that it is licensed under
the PolyForm terms. Changes to derived AgentMail components must retain the
upstream copyright and full upstream license, including its rider.
