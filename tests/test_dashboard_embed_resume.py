"""Inside the ORRERY cockpit, RESUME must ask this server to resume (2026-10-02).

Embedded, jump() only handed the name to the cockpit (postMessage
'orrery-jump'), and the cockpit only waits for a tmux session to appear. For a
retired or gone agent nothing ever appeared: RESUME did nothing. Now the server
decides first (POST /api/jump with open:false): it leaves a running session as
it is, resumes one that is not running and replaces a husk -- from its own
state, not from this page's DECK or NETWORK snapshot, either of which can be
stale (#183 review). The real jump() and bulkDispatch() run here in node with
fetch / postMessage / toast stubbed.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "dashboard" / "index.html"


def _block(html: str, pattern: str) -> str:
    match = re.search(pattern, html, re.DOTALL)
    assert match, pattern
    return match.group(0)


def _run(script_body: str, *, embed: bool, reply: dict, rows: list[dict] | None = None,
         net_rows: list[dict] | None = None) -> dict:
    if not shutil.which("node"):
        pytest.skip("node unavailable")
    html = INDEX.read_text(encoding="utf-8")
    parts = [
        _block(html, r"function notifyTourAction\(action\)\{.*?\n\}\n"),
        _block(html, r"async function jump\(name,ev\)\{.*?\n\}\n"),
        _block(html, r"async function bulkDispatch\(kind,names,btn\)\{.*?\n\}\n"),
    ]
    harness = f"""
const EMBED_MODE={json.dumps(embed)};
let lastData={json.dumps(rows or [])};
const gmap=new Map({json.dumps([[row["name"], row] for row in (net_rows or [])])});
const calls=[], posted=[], toasts=[];
const location={{origin:'http://127.0.0.1:8770'}};
const window={{parent:{{postMessage:(m,o)=>posted.push(m)}}}};
function toast(p,m,err){{toasts.push([p,m,!!err]);}}
async function fetch(url,opts){{calls.push([url,JSON.parse(opts.body)]);
  return {{ok:true,json:async()=>({json.dumps(reply)})}};}}
let bulkBusy=false; const selectedSet=new Set();
function refreshSelClasses(){{}} function updateSelBar(){{}}
const btn={{classList:{{add(){{}},remove(){{}}}},querySelector:()=>({{textContent:''}})}};
const ev={{stopPropagation(){{}}}};
{''.join(parts)}
(async()=>{{ {script_body}
  console.log(JSON.stringify({{calls,posted:posted.filter(m=>m.type==='orrery-jump'),toasts}})); }})();
"""
    done = subprocess.run(["node", "-e", harness], capture_output=True, text=True, check=False, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


ASKED = [["/api/jump", {"session": "HazyCurie", "open": False}]]
HANDED = [{"type": "orrery-jump", "name": "HazyCurie"}]


def test_embedded_resume_asks_the_server_first_then_hands_over():
    out = _run("await jump('HazyCurie',ev);", embed=True,
               reply={"ok": True, "action": "resumed", "detail": "Resumed in tmux"})
    assert out["calls"] == ASKED and out["posted"] == HANDED
    assert out["toasts"] == [["▸ RESUME", "> HazyCurie  ::  Resumed in tmux", False]]


def test_a_running_agent_is_asked_about_and_handed_over_without_noise():
    """The server leaves it as it is; nothing to announce."""
    out = _run("await jump('HazyCurie',ev);", embed=True,
               reply={"ok": True, "action": "already_running", "terminal": "detached"})
    assert out["calls"] == ASKED and out["posted"] == HANDED and out["toasts"] == []


@pytest.mark.parametrize("reply", [{"ok": False, "error": "Resume unavailable: no_history"},
                                   {"ok": False, "error": "invalid session name"}])
def test_a_refusal_is_shown_and_not_handed_over(reply):
    out = _run("await jump('HazyCurie',ev);", embed=True, reply=reply)
    assert out["posted"] == []
    assert out["toasts"][-1] == ["✕ FAIL", "> " + reply["error"], True]


@pytest.mark.parametrize("deck,net", [
    ({"name": "HazyCurie", "category": "retired", "running": False},
     {"name": "HazyCurie", "category": "agent", "running": True}),
    ({"name": "HazyCurie", "category": "agent", "running": True},
     {"name": "HazyCurie", "category": "retired", "running": False}),
    (None, None),
])
def test_page_snapshots_do_not_decide(deck, net):
    """Stale DECK or NETWORK rows, or none at all: the server is asked every time."""
    out = _run("await jump('HazyCurie',ev);", embed=True, reply={"ok": True, "action": "resumed"},
               rows=[deck] if deck else [], net_rows=[net] if net else [])
    assert out["calls"] == ASKED and out["posted"] == HANDED


def test_the_standalone_dashboard_is_unchanged():
    out = _run("await jump('HazyCurie',ev);", embed=False, reply={"ok": True, "action": "resumed"})
    assert out["calls"] == [["/api/jump", {"session": "HazyCurie"}]]
    assert out["posted"] == []


@pytest.mark.parametrize("embed,body", [(True, {"session": "HazyCurie", "open": False}),
                                        (False, {"session": "HazyCurie"})])
def test_bulk_resume_is_headless_when_embedded(embed, body):
    out = _run("await bulkDispatch('resume',['HazyCurie'],btn);", embed=embed, reply={"ok": True})
    assert out["calls"] == [["/api/jump", body]]


def test_bulk_exit_body_is_unchanged():
    out = _run("await bulkDispatch('exit',['LiveCurie'],btn);", embed=True, reply={"ok": True})
    assert out["calls"] == [["/api/exit", {"session": "LiveCurie"}]]


@pytest.mark.parametrize("open_terminal,activated", [(False, False), (None, True), (True, True)])
def test_a_codex_app_agent_is_brought_forward_only_when_asked(monkeypatch, open_terminal, activated):
    """The embedded cockpit asks with open:false before every jump; that must not
    bring the native Codex App forward (#183 review). OPEN still does."""
    from dashboard import server

    opened = []
    monkeypatch.setattr(server, "_agent_program", lambda _name: "codex-app")
    monkeypatch.setattr(server, "_codex_app_runtimes", lambda: {"AppCurie": {}})
    monkeypatch.setattr(server, "_open_codex_app",
                        lambda name: opened.append(name) or {"ok": True, "action": "opened"})
    kwargs = {} if open_terminal is None else {"open_terminal": open_terminal}
    result = server.do_jump("AppCurie", **kwargs)
    assert result["ok"] is True
    assert (opened == ["AppCurie"]) is activated
    if not activated:
        assert result == {"ok": True, "action": "already_running", "terminal": "codex-app"}
