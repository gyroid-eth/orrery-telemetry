from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from dashboard.server import _render_dashboard_index


ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "dashboard" / "index.html"


def _route_cases() -> list[dict]:
    html = INDEX.read_text(encoding="utf-8")
    match = re.search(
        r"(function initialDashboardRoute\(search,serverDefaults=\{\},browserLanguages=\[\]\)\{.*?\n\})\nconst INITIAL_ROUTE=",
        html,
        re.DOTALL,
    )
    assert match, "initial route parser must remain a standalone testable function"
    cases = [
        ["", {}, ["ja-JP", "en-US"]],
        ["?view=net&window=all&lang=en", {"language": "ja"}, ["ja-JP"]],
        ["?view=deck&showAll=1&embed=1", {}, ["ja"]],
        ["?view=invalid&showAll=true&embed=0", {"language": "ja"}, ["en-US"]],
        ["?view=NET&showAll=0", {}, ["en-US"]],
        ["?lang=ja&murmur=off", {"language": "en"}, ["en-US"]],
        ["", {"murmur": "off"}, ["ja-JP"]],
        ["?murmur=on", {"murmur": "off"}, ["en-US"]],
        ["?murmur=invalid", {"murmur": "off"}, ["en-US"]],
    ]
    script = (
        match.group(1)
        + "\nconst cases="
        + json.dumps(cases)
        + "; process.stdout.write(JSON.stringify(cases.map(args=>initialDashboardRoute(...args))));"
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
        {"view": "deck", "showAll": False, "embed": False, "networkWindow": "1", "language": "ja", "murmurEnabled": True},
        {"view": "net", "showAll": False, "embed": False, "networkWindow": "all", "language": "en", "murmurEnabled": True},
        {"view": "deck", "showAll": True, "embed": True, "networkWindow": "1", "language": "ja", "murmurEnabled": True},
        {"view": "deck", "showAll": False, "embed": False, "networkWindow": "1", "language": "ja", "murmurEnabled": True},
        {"view": "deck", "showAll": False, "embed": False, "networkWindow": "1", "language": "en", "murmurEnabled": True},
        {"view": "deck", "showAll": False, "embed": False, "networkWindow": "1", "language": "ja", "murmurEnabled": False},
        {"view": "deck", "showAll": False, "embed": False, "networkWindow": "1", "language": "ja", "murmurEnabled": False},
        {"view": "deck", "showAll": False, "embed": False, "networkWindow": "1", "language": "en", "murmurEnabled": True},
        {"view": "deck", "showAll": False, "embed": False, "networkWindow": "1", "language": "en", "murmurEnabled": False},
    ]


def test_dashboard_applies_route_without_changing_default_initialization():
    html = INDEX.read_text(encoding="utf-8")
    assert "INITIAL_ROUTE.embed ||\n  window.parent!==window" in html
    assert "showAllInput.checked=INITIAL_ROUTE.showAll;" in html
    assert 'let gWin=INITIAL_ROUTE.networkWindow' in html
    assert "if(gWin==='all'){\n    gWinLabel='ALL';lbl.textContent='ALL';btn.classList.add('on');" in html
    assert "if(INITIAL_ROUTE.view==='net')setView('net');\ntick();" in html
    assert '<body data-view="deck">' in html


def test_dashboard_server_injects_only_allowlisted_murmur_defaults():
    source = INDEX.read_bytes()
    rendered = _render_dashboard_index(source, "en", "off")
    assert b'const AGENTSTACK_SERVER_DEFAULTS={"language":"en","murmur":"off"};' in rendered
    ignored = _render_dashboard_index(source, "javascript:alert(1)", "on")
    assert b'const AGENTSTACK_SERVER_DEFAULTS={"language":null,"murmur":null};' in ignored
