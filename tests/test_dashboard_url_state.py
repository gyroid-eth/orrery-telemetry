from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "dashboard" / "index.html"


def _route_cases() -> list[dict]:
    html = INDEX.read_text(encoding="utf-8")
    match = re.search(
        r"(function initialDashboardRoute\(search\)\{.*?\n\})\nconst INITIAL_ROUTE=",
        html,
        re.DOTALL,
    )
    assert match, "initial route parser must remain a standalone testable function"
    queries = [
        "",
        "?view=net",
        "?view=deck&showAll=1&embed=1",
        "?view=invalid&showAll=true&embed=0",
        "?view=NET&showAll=0",
    ]
    script = (
        match.group(1)
        + "\nconst queries="
        + json.dumps(queries)
        + "; process.stdout.write(JSON.stringify(queries.map(initialDashboardRoute)));"
    )
    result = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return json.loads(result.stdout)


def test_dashboard_route_parameters_are_allowlisted_and_composable():
    assert _route_cases() == [
        {"view": "deck", "showAll": False, "embed": False},
        {"view": "net", "showAll": False, "embed": False},
        {"view": "deck", "showAll": True, "embed": True},
        {"view": "deck", "showAll": False, "embed": False},
        {"view": "deck", "showAll": False, "embed": False},
    ]


def test_dashboard_applies_route_without_changing_default_initialization():
    html = INDEX.read_text(encoding="utf-8")
    assert "INITIAL_ROUTE.embed ||\n  window.parent!==window" in html
    assert "showAllInput.checked=INITIAL_ROUTE.showAll;" in html
    assert "if(INITIAL_ROUTE.view==='net')setView('net');\ntick();" in html
    assert '<body data-view="deck">' in html
