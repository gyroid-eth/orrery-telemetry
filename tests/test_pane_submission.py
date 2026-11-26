"""Provider prompt recognition for the guarded tmux input protocol."""

from pathlib import Path
import runpy

import pytest


frame = runpy.run_path(str(Path(__file__).resolve().parents[1] / "bin/lib/pane_submission.py"))["frame"]
BORDER = "─" * 80
METADATA = "/unused/tmux.sock\t100\t%1\t200\t1\t0\t2\t1\tpython"


@pytest.mark.parametrize(
    ("lines", "draft"),
    [
        ([BORDER, "> ", BORDER, "Gemini 4 Neon (Low) · low"], ""),
        ([BORDER, "> Keep this draft", BORDER, "Gemini 4 Neon (Low) · low"], "Keep this draft"),
        ([BORDER, "> ", BORDER, "Another model · low"], None),
        ([BORDER, "> ", BORDER, "Gemini 4 Neon (Low) · launching"], None),
        (["", "> ", BORDER, "Gemini 4 Neon (Low) · low"], None),
    ],
)
def test_antigravity_model_only_footer_requires_the_framed_editor(lines, draft):
    assert frame(METADATA, lines, "%1").draft == draft


HERMES_IDLE_HINT = "Ask anything, or type / for commands…"
HERMES_BUSY_HINT = "msg=interrupt · /queue · /bg · /steer · Ctrl+C cancel"


@pytest.mark.parametrize(
    ("prompt", "timer", "draft", "busy"),
    [
        (f"\x1b[38;5;230m❯ \x1b[3m\x1b[38;5;136m{HERMES_IDLE_HINT}\x1b[0m", "16m", "", False),
        (f"❯ {HERMES_IDLE_HINT}", "16m", HERMES_IDLE_HINT, False),
        ("❯ Keep this draft", "16m", "Keep this draft", False),
        (f"\x1b[3m☤ {HERMES_BUSY_HINT}\x1b[0m", "16m", "", True),
        (f"\x1b[3m☤ \x1b[38;5;136m{HERMES_BUSY_HINT}\x1b[0m", "⏱ 4s", "", True),
        ("☤ ❯ \x1b[3mcli.tui.placeholder_busy\x1b[0m", "⏱ 4s", "", True),
        (f"\x1b[3m☤ \x1b[23m{HERMES_BUSY_HINT}", "⏱ 4s", HERMES_BUSY_HINT, True),
    ],
)
def test_hermes_compact_composer_without_lower_rule(prompt, timer, draft, busy):
    status = f"\x1b[48;5;234m ☤ fixture-model · ~26% · {timer}\x1b[0m"
    column = 4 if "☤ ❯" in prompt else 2
    metadata = f"/unused/tmux.sock\t100\t%1\t200\t2\t0\t{column}\t1\tzsh"
    lines = [status, "─" * 56, prompt, ""]
    view = frame(metadata, lines, "%1")
    assert view.hermes and view.editor == "hermes"
    assert view.draft == draft
    assert view.empty is (draft == "")
    assert view.hermes_busy is busy
    assert view.running is (busy and timer.startswith("⏱"))
    assert view.allows_paste("ORRERY Mail notification: fixture") is not busy


def test_hermes_compact_hint_requires_an_adjacent_status_bar():
    metadata = "/unused/tmux.sock\t100\t%1\t200\t2\t0\t2\t1\tzsh"
    lines = ["Earlier output mentioned ☤ Hermes", "─" * 56, f"❯ \x1b[3m{HERMES_IDLE_HINT}\x1b[0m"]
    view = frame(metadata, lines, "%1")
    assert not view.hermes and view.draft == HERMES_IDLE_HINT
