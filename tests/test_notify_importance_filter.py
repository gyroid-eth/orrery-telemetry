#!/usr/bin/env python3
"""The mail watcher's interrupt threshold.

A notification is typed straight into the recipient's prompt. That is right for
"your child finished", and wrong for the fourth progress note in a minute while
a human is mid-sentence with the parent — a tester running several children at
once reported exactly that. `AGENTSTACK_MAIL_NOTIFY_MIN_IMPORTANCE` sets how
important a message has to be to interrupt.

What it must not do is lose mail. The filter declines to *interrupt*; the
message stays in the inbox for the next `fetch_inbox`. That property lives in
`handle_signal_file` (it records a result and returns without consuming the
signal), and is asserted here by reading the source, because the ordering is
the whole point.

Runnable two ways:
    python3 tests/test_notify_importance_filter.py
    pytest tests/test_notify_importance_filter.py
"""
from __future__ import annotations

import pathlib
import re
import subprocess

WATCHER = pathlib.Path(__file__).resolve().parents[1] / "hooks" / "watch_agent_mail_signals.sh"

# The script runs its watch loop at the bottom, so it cannot be sourced. Lift
# the two pure helpers out by name instead, and fail loudly if they move rather
# than silently testing nothing.
_FUNCS = ("importance_rank", "importance_at_least")


def _extract_helpers() -> str:
    source = WATCHER.read_text(encoding="utf-8")
    out = []
    for name in _FUNCS:
        match = re.search(
            rf"^{name}\(\) \{{\n(?:.*\n)*?^\}}\n", source, re.MULTILINE
        )
        assert match, f"{name}() not found in {WATCHER.name} — did it get renamed?"
        out.append(match.group(0))
    return "".join(out)


def _delivers(importance: str, minimum: str | None) -> bool:
    script = _extract_helpers() + (
        f'NOTIFY_MIN_IMPORTANCE="{minimum}"\n' if minimum is not None
        # No env set: the script's own default must be the one under test.
        else 'NOTIFY_MIN_IMPORTANCE="${AGENTSTACK_MAIL_NOTIFY_MIN_IMPORTANCE:-low}"\n'
    ) + f'importance_at_least "{importance}" "$NOTIFY_MIN_IMPORTANCE"\n'
    return subprocess.run(["/bin/bash", "-c", script]).returncode == 0


def test_the_default_interrupts_for_everything():
    """The null case: unconfigured, nothing may start being dropped."""
    for importance in ("low", "normal", "high", "urgent"):
        assert _delivers(importance, None), importance


def test_a_high_threshold_keeps_routine_notes_out_of_the_conversation():
    assert not _delivers("low", "high")
    assert not _delivers("normal", "high")
    assert _delivers("high", "high")
    assert _delivers("urgent", "high")


def test_urgent_only_is_reachable():
    for importance in ("low", "normal", "high"):
        assert not _delivers(importance, "urgent"), importance
    assert _delivers("urgent", "urgent")


def test_an_unknown_importance_is_treated_as_normal():
    """agent-mail takes importance as free text, so unknown words arrive.

    Dropping what we do not recognise would silently stop delivery, which is
    the failure this whole filter is supposed to avoid causing.
    """
    assert _delivers("chatty", "normal")
    assert _delivers("", "normal")
    assert not _delivers("chatty", "high")


def test_case_does_not_decide_whether_you_are_interrupted():
    assert _delivers("HIGH", "high")
    assert not _delivers("Normal", "high")


def test_a_filtered_message_is_not_consumed():
    """Declining to interrupt must not decline to deliver.

    The filter returns before the delivery lease and before the worker, so the
    signal file survives and the message is still there to be fetched.
    """
    source = WATCHER.read_text(encoding="utf-8")
    body = re.search(
        r"^handle_signal_file\(\) \{\n(?:.*\n)*?^\}\n", source, re.MULTILINE
    )
    assert body, "handle_signal_file() not found — did it get renamed?"
    # Match the calls, not the words: this function's comments name
    # acquire_delivery_lease before it calls it, and matching prose would order
    # the two by where they were explained rather than where they run.
    text = body.group(0)
    filter_at = text.find('"below_min_importance" "watcher"')
    lease_at = text.find('acquire_delivery_lease "$agent_name"')
    assert filter_at != -1, "the importance filter is not in handle_signal_file"
    assert lease_at != -1, "the delivery lease is not in handle_signal_file"
    assert filter_at < lease_at, (
        "the filter must decline before taking a delivery lease, or a message "
        "that was merely too quiet to interrupt gets marked as handled"
    )


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
