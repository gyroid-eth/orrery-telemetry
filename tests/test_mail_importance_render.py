"""Mail importance is a sender-chosen string; only the four known values reach markup."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")


def test_both_renderers_take_importance_through_the_allowlist() -> None:
    assert '<span class="ed-imp ${imp}">' in HTML
    assert HTML.count("const imp=mailImportance(m.importance);") == 2


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_unknown_importance_becomes_normal() -> None:
    source = re.search(r"function mailImportance\(value\)\{.*?\n\}\n", HTML, re.S).group(0)
    cases = ["HIGH", "urgent", "low", None, "", 'x" onclick="y', "<b>"]
    script = source + f"console.log(JSON.stringify({json.dumps(cases)}.map(mailImportance)))"
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == ["high", "urgent", "low", "normal", "normal", "normal", "normal"]
