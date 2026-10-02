"""Inside the ORRERY cockpit, RESUME must ask this server to resume (2026-10-02).

Embedded, jump() only handed the name to the cockpit (postMessage
'orrery-jump'), and the cockpit only waits for a tmux session to appear. For a
retired or gone agent nothing ever appeared: RESUME did nothing. The real
jump() and bulkDispatch() run here in node with fetch / postMessage / toast
stubbed.
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


def _run(script_body: str, rows: list[dict], *, embed: bool, reply: dict) -> dict:
    if not shutil.which("node"):
        pytest.skip("node unavailable")
    html = INDEX.read_text(encoding="utf-8")
    parts = [
        _block(html, r"function isResumeCategory\(category\)\{.*?\n\}\n"),
        _block(html, r"function needsResumeBeforeJump\(name\)\{.*?\n\}\n"),
        _block(html, r"async function jump\(name,ev\)\{.*?\n\}\n"),
        _block(html, r"async function bulkDispatch\(kind,names,btn\)\{.*?\n\}\n"),
    ]
    harness = f"""
const EMBED_MODE={json.dumps(embed)};
let lastData={json.dumps(rows)};
const gmap=new Map();
const calls=[], posted=[], toasts=[];
const location={{origin:'http://127.0.0.1:8770'}};
const window={{parent:{{postMessage:(m,o)=>posted.push(m)}}}};
function toast(p,m,err){{toasts.push([p,m,!!err]);}}
async function fetch(url,opts){{calls.push([url,JSON.parse(opts.body)]);
  return {{json:async()=>({json.dumps(reply)})}};}}
let bulkBusy=false; const selectedSet=new Set();
function refreshSelClasses(){{}} function updateSelBar(){{}}
const btn={{classList:{{add(){{}},remove(){{}}}},querySelector:()=>({{textContent:''}})}};
const ev={{stopPropagation(){{}}}};
{''.join(parts)}
(async()=>{{ {script_body}
  console.log(JSON.stringify({{calls,posted,toasts}})); }})();
"""
    done = subprocess.run(["node", "-e", harness], capture_output=True, text=True, check=False, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


RETIRED = {"name": "HazyCurie", "category": "retired", "running": False}
LIVE = {"name": "LiveCurie", "category": "agent", "running": True}
HUSK = {"name": "HuskCurie", "category": "finished", "running": False}


@pytest.mark.parametrize("row", [RETIRED, HUSK, {"name": "GoneCurie", "category": "gone"}])
def test_embedded_resume_asks_the_server_first_then_hands_over(row):
    out = _run(f"await jump({json.dumps(row['name'])},ev);", [row], embed=True,
               reply={"ok": True, "action": "resumed"})
    assert out["calls"] == [["/api/jump", {"session": row["name"], "open": False}]]
    assert out["posted"] == [{"type": "orrery-jump", "name": row["name"]}]


def test_a_refused_embedded_resume_is_shown_and_not_handed_over():
    out = _run("await jump('HazyCurie',ev);", [RETIRED], embed=True,
               reply={"ok": False, "error": "Resume unavailable: no_history"})
    assert out["posted"] == []
    assert out["toasts"][-1] == ["✕ FAIL", "> Resume unavailable: no_history", True]


@pytest.mark.parametrize("rows", [[LIVE], []])
def test_a_running_or_unknown_agent_is_only_handed_over(rows):
    name = rows[0]["name"] if rows else "NobodyCurie"
    out = _run(f"await jump({json.dumps(name)},ev);", rows, embed=True, reply={"ok": True})
    assert out["calls"] == []
    assert out["posted"] == [{"type": "orrery-jump", "name": name}]


def test_the_standalone_dashboard_is_unchanged():
    out = _run("await jump('HazyCurie',ev);", [RETIRED], embed=False, reply={"ok": True, "action": "resumed"})
    assert out["calls"] == [["/api/jump", {"session": "HazyCurie"}]]
    assert out["posted"] == []


@pytest.mark.parametrize("embed,body", [(True, {"session": "HazyCurie", "open": False}),
                                        (False, {"session": "HazyCurie"})])
def test_bulk_resume_is_headless_when_embedded(embed, body):
    out = _run("await bulkDispatch('resume',['HazyCurie'],btn);", [RETIRED], embed=embed, reply={"ok": True})
    assert out["calls"] == [["/api/jump", body]]


def test_bulk_exit_body_is_unchanged():
    out = _run("await bulkDispatch('exit',['LiveCurie'],btn);", [LIVE], embed=True, reply={"ok": True})
    assert out["calls"] == [["/api/exit", {"session": "LiveCurie"}]]
