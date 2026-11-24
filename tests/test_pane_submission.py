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
