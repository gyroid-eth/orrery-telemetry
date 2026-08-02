from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "dashboard" / "index.html"


def _live_bubble_cases() -> dict:
    html = INDEX.read_text(encoding="utf-8")
    match = re.search(
        r"(function renderLiveStateBubble\(grp,actState\)\{.*?\n\})\n\nfunction buildEls",
        html,
        re.DOTALL,
    )
    assert match, "live state bubble renderer must remain a standalone testable function"
    harness = r"""
const NR=26;
class El {
  constructor(tag,attrs={}) { this.tag=tag; this.attrs=attrs; this.children=[]; this.parent=null; this.textContent=''; }
  appendChild(child) { child.parent=this; this.children.push(child); return child; }
  remove() { if(this.parent)this.parent.children=this.parent.children.filter(x=>x!==this); }
  querySelectorAll(selector) {
    const classes=new Set(selector.split(',').map(s=>s.trim().slice(1)));
    const found=[];
    const walk=node=>{ for(const child of node.children){
      if(classes.has(child.attrs.class))found.push(child); walk(child);
    }};
    walk(this); return found;
  }
}
function svgEl(tag,attrs={}) { return new El(tag,attrs); }
const root=new El('g');
const simplify=el=>el?{
  tag:el.tag, cls:el.attrs.class, label:el.attrs['aria-label'],
  children:el.children.map(child=>({tag:child.tag,cls:child.attrs.class,text:child.textContent}))
}:null;
const ask=simplify(renderLiveStateBubble(root,'ask'));
const askCount=root.children.length;
const cleared=renderLiveStateBubble(root,'work');
const afterWorkCount=root.children.length;
const question=simplify(renderLiveStateBubble(root,'question'));
const questionCount=root.children.length;
const clearedAgain=renderLiveStateBubble(root,'wait');
process.stdout.write(JSON.stringify({ask,askCount,cleared,afterWorkCount,question,questionCount,clearedAgain,finalCount:root.children.length}));
"""
    result = subprocess.run(
        ["node", "-e", match.group(1) + "\n" + harness],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return json.loads(result.stdout)


def test_live_question_and_approval_bubbles_render_and_clear():
    cases = _live_bubble_cases()
    assert cases["ask"] == {
        "tag": "g",
        "cls": "askbub",
        "label": "approval requested",
        "children": [
            {"tag": "rect", "cls": "bubp", "text": ""},
            {"tag": "text", "cls": "bx", "text": "!"},
        ],
    }
    assert cases["askCount"] == 1
    assert cases["cleared"] is None
    assert cases["afterWorkCount"] == 0
    assert cases["question"] == {
        "tag": "g",
        "cls": "qbub",
        "label": "agent question",
        "children": [
            {"tag": "rect", "cls": "bubq", "text": ""},
            {"tag": "text", "cls": "bq", "text": "?"},
        ],
    }
    assert cases["questionCount"] == 1
    assert cases["clearedAgain"] is None
    assert cases["finalCount"] == 0


def test_live_state_bubbles_follow_dense_and_replay_visibility_contracts():
    html = INDEX.read_text(encoding="utf-8")
    assert "#net.dense .askbub,#net.dense .qbub{display:none}" in html
    assert "body.replay-on .node .askbub,\n  body.replay-on .node .qbub{display:none}" in html
    assert "renderLiveStateBubble(grp,g.actState);" in html
    assert "el.className='ask-replay-glyph';" in html
