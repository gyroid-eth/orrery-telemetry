# Contributing

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

This repository is under the [PolyForm Perimeter License 1.0.1 with an
OpenAI/Anthropic Rider](LICENSE). It is source-available, not open source in
the OSI sense: you may use, modify, and redistribute the software for any
purpose except providing others with a product that competes with it.

By submitting a contribution you agree that it is licensed under those same
terms.
