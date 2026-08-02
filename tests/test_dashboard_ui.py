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
            {"tag": "circle", "cls": "bubo", "text": ""},
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
            {"tag": "circle", "cls": "bubo", "text": ""},
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


def test_live_mail_retries_until_graph_endpoints_exist_and_dedupes_overlap():
    html = INDEX.read_text(encoding="utf-8")
    assert "const url='/api/messages-since?since='+Math.max(0,mailLastTs-1);" in html
    assert "if(mailSeenKeys.has(key))continue;" in html
    assert "if(!a||!b)return false;" in html
    assert "await netTick();\n      mailDrain();" in html

    key_match = re.search(r"function mailMessageKey\(m\)\{.*?\n\}", html, re.DOTALL)
    pulse_match = re.search(r"async function mailPulseTick\(\)\{.*?\n\}", html, re.DOTALL)
    group_match = re.search(
        r"function annotateBroadcastMessages\(messages\)\{.*?\n\}",
        html,
        re.DOTALL,
    )
    assert key_match and pulse_match and group_match
    harness = r"""
const runs=[];
for(let play=0;play<3;play++){
  const ts=1000+play*40;
  const rows=annotateBroadcastMessages([
    {id:1,ts,sender:'Bright-Curie',recipient:'Warm-Lovelace',kind:'to'},
    {id:1,ts,sender:'Bright-Curie',recipient:'Keen-Faraday',kind:'to'},
  ]);
  runs.push(rows.map(m=>({key:mailMessageKey(m),group:m._gkey,count:m._rcount})));
}
process.stdout.write(JSON.stringify(runs));
"""
    result = subprocess.run(
        ["node", "-e", key_match.group(0) + "\n" + group_match.group(0) + "\n" + harness],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    runs = json.loads(result.stdout)
    assert [run[0]["group"] for run in runs] == ["id:1@1000", "id:1@1040", "id:1@1080"]
    assert len({item["key"] for run in runs for item in run}) == 6
    assert all(item["count"] == 2 for run in runs for item in run)

    pulse_harness = r"""
let mailLastTs=1000,mailQueue=[];
const mailSeenKeys=new Set(),mailSeenOrder=[];
const MAIL_SEEN_MAX=400,MAIL_QUEUE_MAX=40;
const document={hidden:false};
const view='net',netSuspended=false;
const requests=[];
let play=0;
async function fetch(url){
  requests.push(url);
  const ts=1040+play++*40;
  return {json:async()=>({ok:true,now:ts,messages:[
    {id:1,ts,sender:'Bright-Curie',recipient:'Warm-Lovelace',kind:'to'},
    {id:1,ts,sender:'Bright-Curie',recipient:'Keen-Faraday',kind:'to'},
  ]})};
}
function mailDrain(){}
(async()=>{
  await mailPulseTick();await mailPulseTick();await mailPulseTick();
  process.stdout.write(JSON.stringify({requests,keys:mailQueue.map(mailMessageKey)}));
})().catch(error=>{console.error(error);process.exit(1)});
"""
    pulse_result = subprocess.run(
        [
            "node",
            "-e",
            key_match.group(0)
            + "\n"
            + group_match.group(0)
            + "\n"
            + pulse_match.group(0)
            + "\n"
            + pulse_harness,
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    pulse = json.loads(pulse_result.stdout)
    assert pulse["requests"] == [
        "/api/messages-since?since=999",
        "/api/messages-since?since=1039",
        "/api/messages-since?since=1079",
    ]
    assert len(pulse["keys"]) == 6
    assert len(set(pulse["keys"])) == 6


def test_mail_cards_keep_only_the_three_newest_during_a_storm():
    html = INDEX.read_text(encoding="utf-8")
    register = re.search(r"function registerMailCard\(card\)\{.*?\n\}", html, re.DOTALL)
    unregister = re.search(r"function unregisterMailCard\(card\)\{.*?\n\}", html, re.DOTALL)
    assert register and unregister
    harness = r"""
const MAIL_CARD_MAX=3,mailVisibleCards=[];
const dismissed=[];
for(let id=1;id<=5;id++){
  const card={id,_mailDismiss(){dismissed.push(id)}};
  registerMailCard(card);
}
process.stdout.write(JSON.stringify({
  visible:mailVisibleCards.map(card=>card.id),dismissed
}));
"""
    result = subprocess.run(
        ["node", "-e", register.group(0) + "\n" + unregister.group(0) + "\n" + harness],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert json.loads(result.stdout) == {"visible": [3, 4, 5], "dismissed": [1, 2]}
    assert "const mailCardSlots=[false,false,false];" in html


def test_network_growth_refits_and_reel_murmurs_have_an_english_table():
    html = INDEX.read_text(encoding="utf-8")
    assert "if(mapWasCleared||gmap.size!==previousNodeCount)fitPending=true;" in html
    assert "g.x=Math.max(76,Math.min(w-76,g.x));" in html
    assert "g.y=Math.max(36,Math.min(h-66,g.y));" in html
    assert "if(fitPending&&!viewUserAdjusted&&gmap.size&&++fitFollowTick%6===0)fitView();" in html
    assert "const table=INITIAL_ROUTE.language==='en'?MURMURS_EN:MURMURS;" in html

    table = re.search(r"const MURMURS_EN=(\{.*?\});\nfunction pickMurmur", html, re.DOTALL)
    picker = re.search(r"function pickMurmur\(name,kind\)\{.*?\n\}", html, re.DOTALL)
    assert table and picker
    harness = r"""
const INITIAL_ROUTE={language:'en'};
const MURMURS={_:{say:['日本語']}};
const MURMURS_EN=TABLE;
function scientistOf(name){return name.split('-').pop()}
const roster=['Curie','Noether','Turing','Lovelace','Hopper','Franklin',
  'Faraday','Lamarr','Bose','Galileo','Somerville','Feynman'];
const sample=roster.map(name=>pickMurmur('Demo-'+name,'say'));
sample.push(pickMurmur('Demo-Unknown','say'));
process.stdout.write(JSON.stringify({keys:Object.keys(MURMURS_EN),sample,
  ascii:/^[\x00-\x7F]+$/.test(JSON.stringify(MURMURS_EN))}));
""".replace("TABLE", table.group(1))
    result = subprocess.run(
        ["node", "-e", picker.group(0) + "\n" + harness],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    data = json.loads(result.stdout)
    assert set(data["keys"]) == {
        "_", "Curie", "Noether", "Turing", "Lovelace", "Hopper", "Franklin",
        "Faraday", "Lamarr", "Bose", "Galileo", "Somerville", "Feynman",
    }
    assert data["ascii"] is True
    assert all(sample != "日本語" for sample in data["sample"])
    assert "if(!INITIAL_ROUTE.murmurEnabled)return;" in html
