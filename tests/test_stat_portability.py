#!/usr/bin/env python3
"""Lint: `stat -c` must be tried before `stat -f` (silent-wrong-answer hazard).

Background
----------
The two `stat` implementations do not disagree politely.

- BSD (macOS): `-f FORMAT` is the format flag. `-c` is not an option at all,
  so `stat -c '%a' file` exits non-zero.
- GNU (Linux): `-c FORMAT` is the format flag. `-f` means *filesystem status*
  — a different subcommand that takes its own specifiers, does not understand
  `%Lp`, and **exits 0** after printing something like `?p`.

So the natural-looking fallback

    mode="$(stat -f '%Lp' "$f" 2>/dev/null || stat -c '%a' "$f" 2>/dev/null)"

works on macOS and, on Linux, never reaches the second branch: the first one
succeeds and returns nonsense. The caller then compares a file mode against
`?p` and reports a permission problem that does not exist. Three call sites in
`doctor-codex-app-integration.sh` did this, and the failure they produced ran
in CI for weeks reading `fail: env mode is` with nothing after it.

Written the other way round the fallback is correct on both, because the BSD
form genuinely fails for an unknown option:

    mode="$(stat -c '%a' "$f" 2>/dev/null || stat -f '%Lp' "$f" 2>/dev/null)"

This is not a style rule. One order gives the right answer or an error; the
other gives the right answer or a wrong one.

Runnable two ways:
    python3 tests/test_stat_portability.py
    pytest tests/test_stat_portability.py
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", "node_modules", "__pycache__"}

# `stat -f` appearing before `stat -c` on the same line.
BSD_FIRST = re.compile(r"stat\s+-f\b(?![^\n]*\|\|[^\n]*stat\s+-c\b)[^\n]*\|\|[^\n]*stat\s+-c\b")
BSD_ANYWHERE = re.compile(r"stat\s+-f\b")
GNU_ANYWHERE = re.compile(r"stat\s+-c\b")


def _shell_files() -> list[pathlib.Path]:
    found = []
    for path in ROOT.rglob("*.sh"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        found.append(path)
    return found


def _offending_lines(text: str) -> list[tuple[int, str]]:
    bad = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not BSD_ANYWHERE.search(line):
            continue
        gnu = GNU_ANYWHERE.search(line)
        if gnu is None:
            # A lone `stat -f` is either deliberate filesystem status or a
            # macOS-only script; this lint is about the fallback pair.
            continue
        if BSD_ANYWHERE.search(line).start() < gnu.start():
            bad.append((number, line.strip()))
    return bad


def test_no_shell_script_tries_bsd_stat_before_gnu_stat():
    offenders = []
    for path in _shell_files():
        for number, line in _offending_lines(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(ROOT)}:{number}: {line}")
    assert not offenders, (
        "`stat -f` is tried before `stat -c` here. On Linux the `-f` form "
        "succeeds with a nonsense value instead of failing, so the `||` never "
        "runs and the caller acts on garbage. Put `stat -c '%a'` first:\n  "
        + "\n  ".join(offenders)
    )


def test_the_lint_would_catch_the_bug_it_was_written_for():
    """The null case: a lint nobody can trip is a lint that proves nothing."""
    reintroduced = "mode=\"$(stat -f '%Lp' \"$F\" 2>/dev/null || stat -c '%a' \"$F\" 2>/dev/null)\""
    assert _offending_lines(reintroduced)
    fixed = "mode=\"$(stat -c '%a' \"$F\" 2>/dev/null || stat -f '%Lp' \"$F\" 2>/dev/null)\""
    assert not _offending_lines(fixed)


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    raise SystemExit(1 if failures else 0)
