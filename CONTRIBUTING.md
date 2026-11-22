# Contributing

## Test environment

`packages/agentstack_mail` has real dependencies (fastmcp, sqlmodel,
python-decouple, …), so a bare `python3 -m pytest` cannot even collect its
tests. Create the repo-local venv once and run the suite from it:

```bash
python3 -m venv .venv
.venv/bin/pip install -e packages/agentstack_mail pytest pytest-asyncio pytest-timeout pytest-xdist
PYTHONPATH=. .venv/bin/python -m pytest -q -n auto --dist loadfile
```

`.venv/` is gitignored. Do not install anything into a service venv under
`~/.agentstack/` — those are production artifacts whose contents are pinned
by cutover receipts.

The full suite is about 1,650 tests. Serially it takes 20 minutes on a
laptop; most of that is a long tail of installer and service rehearsals that
each spawn real processes, not any one slow test. `-n auto --dist loadfile`
runs files on parallel workers (6 minutes with six) while keeping the tests
of one file together, since files share fixtures and fake homes. Every run
prints its 25 slowest tests; a test over 120 s is failed by `pytest-timeout`
(both set in `pytest.ini`). Before calling a test "environment-dependent",
note that `tests/conftest.py` strips `AGENTSTACK_*` from the environment for
the whole session, so a shell started by an installed stack runs the suite
the way CI does.

Caveats learned by measurement:

- Do not pipe pytest through `tail` without `pipefail`: the pipeline exits
  with `tail`'s status and a red suite reads as exit 0. Do not cut the
  output with `tail -3` either: the FAILED lines above the summary are the
  part that says what to fix.
- Run the suite without other heavy processes (or a second concurrent
  pytest): the SIGKILL-timing parity tests in
  `packages/agentstack_mail/tests/test_pending_decision_d8_d9.py` can fail
  under CPU contention and pass in a clean single run.
- A test that starts a daemon must stop it in `finally`, on every path. A
  run that is interrupted or times out otherwise leaves the daemon behind;
  `ps -eo pid,command | grep pytest-of-` after a run should print nothing.

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

Facts that drift silently — the default ORRERY Mail port, the number of
approval prompts, the number of Claude event hooks, the guide list — are
checked against the implementation by `tests/test_docs_consistency.py`.
Extend that test when you add such a fact instead of relying on review.

When a change adds an endpoint or field that other programs (ORRERY cockpit
and others) rely on, or changes what one means, raise the `api` generation in
`GET /api/version` (`dashboard/server.py`) by one and say so in the
CHANGELOG fragment. Do not raise it for anything else; see `docs/api.md`.

The docs must not contradict the implementation. If no docs change is needed,
that should be an explicit review decision, not an accidental omission.

## Changelog fragments and releases

Each PR adds one `changelog.d/NNNNNN-description.md`: its PR number padded
with zeros to six digits, followed by a lowercase ASCII slug (letters, digits,
hyphens). Examples: `changelog.d/000251-changelog-fragments.md` and
`changelog.d/000260-branch-name.md`. After opening the PR, set its actual number
before merge. Release notes use descending PR-number order (larger numbers
first); this is PR order, not merge-time order. Migrated notes use their
original PR numbers too. Multiple change headings from one PR share its file.
Start with `### A user-facing change`, followed by the same prose or bullets
used in a released CHANGELOG section. Use Japanese, describe the resulting
behavior, and record any API generation change here. Use `../docs/...` for
links from a fragment to repository documentation; the release script rebases
inline and reference-definition links one directory up.

Do not add an `Unreleased` section or pending items to `CHANGELOG.md`, and do
not duplicate a fragment there. CI and the release script share one structural
check: level 2 headings are release dates only, and no change headings may
precede the first release. Heading checks and link rebasing ignore fenced
code blocks. Every fence must close within its own fragment. CI and release
also share the checks for every fragment's filename, leading `###`, heading
levels, and closed fences, so invalid fragments fail before merge.
`CHANGELOG.md` contains released changes only.

To release, start from an up-to-date clean branch and run:

```bash
python3 scripts/changelog-release.py YYYY.MM.DD
```

Replace `YYYY.MM.DD` with the new release date.

For another release on the same date, use `YYYY.MM.DD.N`, starting at `.1`
and increasing the positive integer each time. For example, `2026.10.22.1`
follows `2026.10.22`. Revisions are ordered numerically, so `.10` follows
`.9`. Do not reuse a version, overwrite its tag, or choose a future date to
make the ordering check pass.

The stdlib-only script inserts
a version section before the newest release, in descending six-digit
PR-number order, then removes the consumed `NNNNNN-description.md` fragments. Other `.md` filenames are
rejected. The permanent `changelog.d/README.txt` is never consumed and keeps
the directory and documentation links present in a fresh release checkout.
It leaves the introduction and older releases intact. With valid CHANGELOG
structure and no fragments, including a second run, it does nothing. It refuses
an already released version when fragments remain; do not append new changes
to an existing release. If interrupted after the CHANGELOG write, review the
saved section and remaining fragments before completing their removal; do not
rerun with another version. Run only one release process at a time.

Review the generated diff and update the root `VERSION` to the same release
version (including any same-day revision) in the release commit. The script deliberately does not change `VERSION`
or the dashboard `API_GENERATION`: that generation changes only for API
compatibility changes, not for assembling release notes. Check the release
with:

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_changelog_release.py tests/test_docs_consistency.py
```

Commit `CHANGELOG.md`, `VERSION`, and the consumed fragment removals together.
The PR adding a change edits only its own fragment; assembling notes is a
release operation.

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

## Windows contributions (community lane)

Native Windows is not a supported platform; WSL2 is the Windows path the
maintainer verifies. Windows-native work is still welcome, on one condition:
**it must not be able to change how the macOS install behaves.** The layout
below makes that a property of the tree rather than of each review.

- **Placement.** Windows-only scripts go in `scripts/windows/`, Windows-only
  tests in `tests/windows/` (collected only on `win32`, see its `conftest.py`),
  Windows-only guides as `docs/windows-*.md`. Do not add Windows files next to
  the macOS ones.
- **Shared code.** A change to a shared module (for example `storage.py` or
  `dashboard/server.py`) is acceptable only when the non-Windows path is
  unchanged and the Windows branch is guarded by `sys.platform == "win32"` and
  covered by a test in `tests/windows/`. Say so in the PR description.
- **Off limits without prior discussion in an issue:** `scripts/install.sh`,
  the hooks, launchers, `docs/install.md`, and the README support table. The
  support table stays as it is; a Windows guide may be linked from it in one
  sentence at most.
- **Docs.** Start every `docs/windows-*.md` with a line that says it is
  community-maintained and experimental, and list what was actually run
  (Windows build, Python, shell). Do not write "supported".
- **CI.** The `portable startup (Windows)` job runs `tests/windows/` plus the
  portable startup tests. The macOS matrix ignores `tests/windows/`, so a red
  Windows job never blocks a macOS fix and a macOS change never has to know
  about Windows.
- **Review.** The maintainer reviews Windows PRs for the boundary above, not
  for Windows correctness; the contributor's own validation log in the PR is
  the evidence. Keep one concern per PR (see #7 for the shape that merges
  quickly).

## Dashboard UI changes

The dashboard has a design language, and it is the maintainer's, not a
matter of taste per PR. Read [docs/design.en.md](docs/design.en.md)
([日本語](docs/design.md)) before touching `dashboard/index.html`,
`theme_light.css`, or anything the page renders, and before asking an AI to
build UI for it. Compose new elements from the tokens, the two typefaces, and
the hierarchy described there; a new hue, a new face, or a literal colour is a
regression. If what you need does not fit, open an issue with a screenshot
before writing code. Include dark and light screenshots in the PR (light is
one console call away, see the design document's light-theme section), and
run `scripts/dashboard_theme_manifest.py --write` then `--check` after any
CSS change so the embedded theme keeps applying.

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
