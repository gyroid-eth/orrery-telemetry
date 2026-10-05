"""The cockpit tour's cue inside Telemetry (dashboard/tour_cue.js).

The cockpit's Full tour rings the control to press next, but its later steps
happen inside this page, embedded in the cockpit, which the cockpit cannot
reach into. It sends the step; this page rings its own control.

Node cases need no browser. The page cases connect to an existing CDP browser
when ORRERY_TEST_CDP_URL is set (they never launch one): a parent page embeds
the demo bundle and sends the cue the way the cockpit does.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.request
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from test_dashboard_help_map import _QuietHandler, _WebSocket

DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard"
CUE = DASHBOARD / "tour_cue.js"


def node(script: str):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_page_server_and_demo_bundle_carry_the_cue():
    index = (DASHBOARD / "index.html").read_text()
    assert '<link rel="stylesheet" href="tour_cue.css">' in index
    assert '<script src="tour_cue.js" defer></script>' in index
    server = (DASHBOARD / "server.py").read_text()
    assert '"/tour_cue.js"' in server and '"/tour_cue.css"' in server
    assert '"$DASH/tour_cue.js" "$DASH/tour_cue.css"' in (DASHBOARD / "demo" / "build.sh").read_text()


def test_every_telemetry_step_of_the_full_tour_has_controls():
    # The cockpit's Full tour steps that point at the Telemetry overlay.
    steps = node(f"const c=require({json.dumps(str(CUE))});console.log(JSON.stringify(c.STEPS));")
    assert sorted(steps) == sorted(['full-exit', 'full-edge', 'full-select', 'full-replay',
                                    'full-resume', 'full-network-settings', 'full-return'])
    # A selector, [selector, label text] for a control whose label decides its
    # action, or [selector, null, 'pair'] for the edge of the cue's pair.
    for sels in steps.values():
        for entry in sels:
            assert (isinstance(entry, str) and entry) or (
                isinstance(entry, list) and len(entry) == 2 and all(isinstance(x, str) and x for x in entry)) or (
                isinstance(entry, list) and len(entry) == 3 and isinstance(entry[0], str) and entry[0]
                and entry[1] is None and entry[2] == 'pair'), entry
    assert steps['full-edge'][0] == ['#net .edge-count', None, 'pair']


def test_here_tag_placement_matches_the_cockpit_rule():
    assert node(f"""const t=require({json.dumps(str(CUE))});
      const size={{w:60,h:20}},view={{w:1000,h:600}},r=(l,tp,rr,b)=>({{l,t:tp,r:rr,b}});
      console.log(JSON.stringify([
        t.placeHereTag(r(100,100,200,140),size,view),
        t.placeHereTag(r(100,560,200,590),size,view),
        t.placeHereTag(r(100,100,200,140),size,view,[r(80,140,300,600),r(80,0,300,100)]),
        t.placeHereTag(r(0,0,1000,600),size,view)]));""") == [
        {'x': 120, 'y': 148, 'side': 'below'}, {'x': 120, 'y': 532, 'side': 'above'},
        {'x': 208, 'y': 110, 'side': 'right'}, None]


@pytest.fixture
def embedded(tmp_path):
    endpoint = os.environ.get("ORRERY_TEST_CDP_URL")
    if not endpoint:
        pytest.skip("set ORRERY_TEST_CDP_URL to an existing CDP browser")
    bundle = tmp_path / "demo"
    subprocess.run(["bash", str(DASHBOARD / "demo" / "build.sh"), str(bundle)], check=True,
                   capture_output=True, timeout=60)
    # The cockpit's part, reduced to an iframe that sends the cue.
    (bundle / "parent.html").write_text(
        '<!doctype html><meta charset="utf-8"><style>html,body{margin:0;height:100%}'
        'iframe{position:fixed;inset:0;width:100%;height:100%;border:0}</style>'
        '<iframe id="f" src="index.html?embed=1"></iframe>'
        '<script>window.cue=(step,avoid,pair)=>document.getElementById("f").contentWindow'
        '.postMessage({type:"orrery-tour-cue",version:1,step,avoid,...(pair?{pair}:{})},location.origin);'
        'window.covers=[];addEventListener("message",e=>{if(e.data&&e.data.type==="orrery-tour-cover")covers.push(e.data.rects);});</script>')
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_QuietHandler, directory=str(bundle)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    request = urllib.request.Request(endpoint + "/json/new?about:blank", method="PUT")
    tab = json.load(urllib.request.urlopen(request, timeout=5))
    client = _WebSocket(tab["webSocketDebuggerUrl"])

    def evaluate(expression: str):
        result = client.call("Runtime.evaluate", expression=expression, awaitPromise=True, returnByValue=True)
        assert "exceptionDetails" not in result, result
        return result.get("result", {}).get("value")

    def inner(expression: str):
        return evaluate(f"document.getElementById('f').contentWindow.eval({json.dumps(expression)})")

    def wait(expression: str, seconds: float = 15) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if inner(expression):
                return
            time.sleep(.1)
        pytest.fail("timed out waiting for " + expression)

    try:
        client.call("Page.enable")
        client.call("Emulation.setDeviceMetricsOverride", width=1440, height=900, deviceScaleFactor=1, mobile=False)
        client.call("Page.navigate", url=f"http://127.0.0.1:{server.server_port}/parent.html")
        wait("document.readyState==='complete'&&!!window.TelemetryTourCue&&!!TelemetryTourCue.set")
        inner("""(()=>{const go=[...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='START WATCHING');
          if(go)go.click();const s=document.createElement('style');s.textContent='#demo-strip{display:none!important}';
          document.head.appendChild(s);})()""")
        wait("document.querySelectorAll('.bay .exitbtn').length>0")
        yield client, evaluate, inner, wait
    finally:
        client.close()
        with __import__("contextlib").suppress(OSError):
            urllib.request.urlopen(endpoint + "/json/close/" + tab["id"], timeout=5)
        server.shutdown()
        server.server_close()


CUE_STATE = """(()=>{
  const ring=document.querySelector('.tour-cue-ring'),here=document.querySelector('.tour-cue-here');
  if(ring.hidden)return {ring:false,here:!here.hidden};
  const rect=el=>{const r=el.getBoundingClientRect();return {l:r.left,t:r.top,r:r.right,b:r.bottom};};
  const g=rect(ring),h=!here.hidden&&rect(here);
  const arrow={below:'▲ HERE',above:'▼ HERE',right:'◀ HERE',left:'HERE ▶'};
  return {ring:true,target:ring.dataset.target,colour:getComputedStyle(ring).borderTopColor,
    pulse:getComputedStyle(ring,'::after').animationName,here:!!h,
    arrowMatches:!h||here.textContent===arrow[here.dataset.side],
    hereOnScreen:!h||(h.l>=0&&h.t>=0&&h.r<=innerWidth&&h.b<=innerHeight),
    ringBox:g,hereBox:h||null};
})()"""


# The two ends of the network's first edge count, as the cockpit would send them.
FIRST_PAIR = """(()=>{const b=gEls.badge.find(o=>o.tx.getBoundingClientRect().width>0);
  const n=e=>e&&typeof e==='object'?e.name??e.id:e;return [n(b.s),n(b.t)];})()"""


def _cue(evaluate, inner, step, avoid=None, pair=None):
    evaluate(f"cue({json.dumps(step)},{json.dumps(avoid)},{json.dumps(pair)})")
    time.sleep(.3)
    return inner(CUE_STATE)


def test_each_step_rings_the_control_on_screen(embedded):
    _client, evaluate, inner, wait = embedded
    rows = {}
    rows['exit'] = _cue(evaluate, inner, 'full-exit')
    rows['edge-on-deck'] = _cue(evaluate, inner, 'full-edge')
    inner("setView('net')")
    wait("[...document.querySelectorAll('#net .edge-count')].some(e=>e.getBoundingClientRect().width>0)", 30)
    time.sleep(2)
    rows['edge'] = _cue(evaluate, inner, 'full-edge', pair=inner(FIRST_PAIR))
    rows['select'] = _cue(evaluate, inner, 'full-select')
    rows['replay-before-selecting'] = _cue(evaluate, inner, 'full-replay')
    rows['settings'] = _cue(evaluate, inner, 'full-network-settings')
    inner("setView('deck')")
    time.sleep(.5)
    inner("openPanel(document.querySelector('.bay').dataset.name)")
    time.sleep(1)
    # The demo's agents are live: the panel offers OPEN IN COCKPIT, which is
    # the Return step's action, not Resume's (review of #202). The panel
    # covers the rest, so Resume points at closing it.
    rows['resume-of-a-live-agent'] = _cue(evaluate, inner, 'full-resume')
    rows['return'] = _cue(evaluate, inner, 'full-return')
    targets = {k: (v['ring'], v.get('target')) for k, v in rows.items()}
    assert targets == {
        'exit': (True, 'exitbtn'), 'edge-on-deck': (True, 'viewtog'), 'edge': (True, 'edge-count'),
        'select': (True, 'selToggle'), 'replay-before-selecting': (True, 'selToggle'),
        'settings': (True, 'settings-btn'), 'resume-of-a-live-agent': (True, 'tm-x'),
        'return': (True, 'tm-open')}, rows
    for name, row in rows.items():
        assert row['colour'] == 'rgb(63, 210, 230)' and row['pulse'] == 'tour-cue-pulse', (name, row)
        assert row['arrowMatches'] and row['hereOnScreen'], (name, row)


def test_the_cue_ends_and_only_the_cockpit_can_send_it(embedded):
    _client, evaluate, inner, _wait = embedded
    shown = _cue(evaluate, inner, 'full-exit')
    stopped = _cue(evaluate, inner, None)
    # The page itself (not the cockpit) sending the message is ignored.
    inner("window.postMessage({type:'orrery-tour-cue',version:1,step:'full-exit'},location.origin)")
    time.sleep(.3)
    forged = inner(CUE_STATE)
    unknown = _cue(evaluate, inner, 'full-start')
    assert shown['ring'] and stopped == {'ring': False, 'here': False}
    assert forged == {'ring': False, 'here': False} and unknown == {'ring': False, 'here': False}


def test_resume_needs_the_resume_label_and_nothing_disabled_is_ringed(embedded):
    _client, evaluate, inner, _wait = embedded
    inner("openPanel(document.querySelector('.bay').dataset.name)")
    time.sleep(1)
    # The same panel button, as it reads for an ended agent.
    inner("document.getElementById('tm-open').textContent='RESUME IN COCKPIT'")
    resume = _cue(evaluate, inner, 'full-resume')
    returning = _cue(evaluate, inner, 'full-return')
    inner("document.getElementById('tm-open').textContent='OPEN IN COCKPIT';document.getElementById('tm-open').disabled=true")
    disabled = _cue(evaluate, inner, 'full-return')
    inner("document.getElementById('tm-open').disabled=false")
    assert (resume['ring'], resume['target']) == (True, 'tm-open')
    # Return is not RESUME, and a disabled OPEN is not pressed. Nor is the
    # panel's Close a way back to the cockpit: no ring, the checklist guides.
    assert returning == {'ring': False, 'here': False}, returning
    assert disabled == {'ring': False, 'here': False}, disabled


@pytest.mark.parametrize('before,after,label', [
    ({'category': 'retired', 'running': False, 'retired': True, 'resume_capability': 'ready'},
     {'category': 'agent', 'running': True, 'retired': False}, 'OPEN IN COCKPIT'),
    ({'category': 'agent', 'running': True, 'retired': False},
     {'category': 'retired', 'running': False, 'retired': True, 'resume_capability': 'ready'}, 'RESUME IN COCKPIT'),
], ids=['resumed-while-open', 'exited-while-open'])
def test_an_open_panel_follows_the_agent(embedded, before, after, label):
    """Full tour on a Mac: with the panel left open on the child, RESUME took
    it running but the button kept saying RESUME IN COCKPIT, and Return's ring
    went to the panel's Close. The button now follows each new agents list."""
    _client, evaluate, inner, _wait = embedded
    name = inner("document.querySelector('.bay').dataset.name")
    patch = lambda row: inner(
        "(()=>{lastData=lastData.map(a=>a.name===%s?{...a,...%s}:a);})()" % (json.dumps(name), json.dumps(row)))
    patch(before)
    inner("openPanel(%s)" % json.dumps(name))
    time.sleep(.5)
    first = inner("document.getElementById('tm-open').textContent.trim()")
    patch(after)
    inner("refreshPanelButtons()")
    second = inner("document.getElementById('tm-open').textContent.trim()")
    returning = _cue(evaluate, inner, 'full-return')
    inner("document.getElementById('tm-x').click()")
    assert first != label and second == label, (first, second)
    if label == 'OPEN IN COCKPIT':
        assert (returning['ring'], returning['target']) == (True, 'tm-open'), returning
    else:
        assert returning.get('target') != 'tm-x', returning


# Edits the page's own /api/agents and /api/graph answers for one agent, so
# the real tick and netTick see it change: window.__mut = {name, agent, node,
# gone}.
MUTATE = """(()=>{if(window.__mutOn)return;window.__mutOn=true;window.__mut={};
  const orig=window.fetch;
  window.fetch=async(u,i)=>{const r=await orig(u,i);const url=String(u);
    if(!/\\/api\\/(agents|graph)/.test(url))return r;
    const m=window.__mut;if(!m.name)return r;
    const j=await r.clone().json();
    if(Array.isArray(j.agents))j.agents=j.agents.filter(a=>!(m.gone&&a.name===m.name))
      .map(a=>a.name===m.name&&m.agent?{...a,...m.agent}:a);
    if(Array.isArray(j.nodes))j.nodes=j.nodes.filter(n=>!(m.gone&&n.name===m.name))
      .map(n=>n.name===m.name&&m.node?{...n,...m.node}:n);
    return new Response(JSON.stringify(j),{status:200,headers:{'Content-Type':'application/json'}});};})()"""

RETIRED = {'running': False, 'retired': True, 'category': 'retired', 'resume_capability': 'ready'}
RUNNING = {'running': True, 'retired': False, 'category': 'agent'}
BUTTONS = """(()=>{const o=document.getElementById('tm-open'),x=document.getElementById('tm-exit-btn');
  const p=TelemetryTourCue.pick('full-return');
  return {open:o.textContent.trim(),disabled:o.disabled,exit:x.style.display!=='none',return:p?(p.id||p.className):null};})()"""


@pytest.mark.parametrize('case', ['net-retired-then-running', 'net-gone', 'net-then-deck', 'deck-gone', 'net-reopen',
                                  'net-gone-reopen', 'deck-shows-what-graph-left-out'])
def test_an_open_panel_follows_the_newest_list(embedded, case):
    """Review of #211: in Network only netTick runs, and a panel whose agent
    left the list kept a pressable OPEN IN COCKPIT and Exit."""
    _client, evaluate, inner, wait = embedded
    name = inner("document.querySelector('.bay').dataset.name")
    inner(MUTATE)
    mut = lambda m: inner("window.__mut=%s" % json.dumps(dict(m, name=name)))
    if case == 'deck-gone':
        inner("openPanel(%s)" % json.dumps(name))
        mut({'gone': True})
        inner("tick()")
        time.sleep(.5)
        gone = inner(BUTTONS)
        inner("window.__mut={};document.getElementById('tm-x').click()")
        assert gone['open'] == 'NO LONGER LISTED' and gone['disabled'] and not gone['exit'], gone
        assert gone['return'] != 'tm-x', gone
        return
    inner("setView('net')")
    wait("gmap.size>0", 30)
    inner("openPanel(%s)" % json.dumps(name))
    time.sleep(.3)
    if case == 'net-retired-then-running':
        mut({'agent': RETIRED, 'node': RETIRED})
        inner("netTick()")
        time.sleep(.5)
        retired = inner(BUTTONS)
        mut({'agent': RUNNING, 'node': RUNNING})
        inner("netTick()")
        time.sleep(.5)
        running = inner(BUTTONS)
        assert retired['open'] == 'RESUME IN COCKPIT' and not retired['exit'], retired
        assert running['open'] == 'OPEN IN COCKPIT' and running['exit'] and running['return'] == 'tm-open', running
    elif case == 'net-gone':
        mut({'gone': True})
        inner("netTick()")
        time.sleep(.5)
        gone = inner(BUTTONS)
        assert gone['open'] == 'NO LONGER LISTED' and gone['disabled'] and not gone['exit'], gone
        assert gone['return'] != 'tm-x', gone
    elif case == 'net-gone-reopen':
        # Review of #211: gone from the newest graph but still running in the
        # older deck list, reopened: it must not offer OPEN and Exit.
        mut({'gone': True})
        inner("netTick()")
        time.sleep(.5)
        inner("document.getElementById('tm-x').click()")
        inner("openPanel(%s)" % json.dumps(name))
        reopened = inner(BUTTONS)
        assert reopened['open'] == 'NO LONGER LISTED' and reopened['disabled'] and not reopened['exit'], reopened
    elif case == 'deck-shows-what-graph-left-out':
        # The graph's window can leave out an agent the deck shows; back on the
        # deck, before its next list, a card opened is not "no longer listed".
        mut({'gone': True})
        inner("netTick()")
        time.sleep(.5)
        inner("document.getElementById('tm-x').click()")
        inner("window.__mut={};view='deck'")
        inner("openPanel(%s)" % json.dumps(name))
        opened = inner(BUTTONS)
        assert opened['open'] == 'OPEN IN COCKPIT' and opened['exit'], opened
    elif case == 'net-reopen':
        # Review of #211: reopened right after the graph changed, the panel
        # drew from the older deck list until the next netTick.
        mut({'agent': RETIRED, 'node': RETIRED})
        inner("netTick()")
        time.sleep(.5)
        inner("document.getElementById('tm-x').click()")
        inner("openPanel(%s)" % json.dumps(name))
        reopened = inner(BUTTONS)
        assert reopened['open'] == 'RESUME IN COCKPIT' and not reopened['exit'], reopened
    else:
        mut({'agent': RETIRED, 'node': RETIRED})
        inner("netTick()")
        time.sleep(.5)
        mut({'agent': RUNNING, 'node': RUNNING})
        inner("setView('deck')")
        inner("tick()")
        time.sleep(.8)
        back = inner(BUTTONS)
        assert back['open'] == 'OPEN IN COCKPIT' and back['exit'], back
    inner("window.__mut={};document.getElementById('tm-x').click();setView('deck')")


def test_an_exit_on_its_way_keeps_its_button(embedded):
    _client, _evaluate, inner, _wait = embedded
    name = inner("document.querySelector('.bay').dataset.name")
    inner("openPanel(%s)" % json.dumps(name))
    time.sleep(.5)
    inner("(()=>{const b=document.getElementById('tm-exit-btn');b.textContent='…';b.disabled=true;})()")
    inner("refreshPanelButtons()")
    kept = inner("(()=>{const b=document.getElementById('tm-exit-btn');return [b.textContent,b.disabled];})()")
    inner("(()=>{const b=document.getElementById('tm-exit-btn');b.textContent='Exit';b.disabled=false;document.getElementById('tm-x').click();})()")
    assert kept == ['…', True], kept


@pytest.mark.parametrize('category', ['retired', 'finished', 'gone'])
def test_resume_rings_an_ended_card_whose_resume_is_ready(embedded, category):
    """Review of #202: only gone cards were candidates, but the child after
    EXIT is usually retired (and finished counts too, as isResumeCategory)."""
    _client, evaluate, inner, _wait = embedded
    def add_card(capability):
        inner(f"""(()=>{{document.querySelectorAll('.qa-ended').forEach(n=>n.remove());
          const row={{name:'QaEnded',category:{json.dumps(category)},running:false,present:false,
            resume_capability:{json.dumps(capability)},model:'claude-sonnet-5-5',provider:'anthropic',
            task:'shiritori',live:'',rel:'1m',state:'',ctx_used:null}};
          const box=document.createElement('div');box.className='qa-ended';box.innerHTML=bay(row,99);
          document.getElementById('wrap').prepend(box);}})()""")
    add_card('ready')
    ready = _cue(evaluate, inner, 'full-resume')
    ready_name = inner("(()=>{const r=document.querySelector('.tour-cue-ring').getBoundingClientRect(),"
                       "t=document.querySelector('.qa-ended .top').getBoundingClientRect();"
                       "return r.left<=t.left&&r.top<=t.top&&r.right>=t.right&&r.bottom>=t.bottom})()")
    add_card('verification_required')
    verify = _cue(evaluate, inner, 'full-resume')
    inner("document.querySelectorAll('.qa-ended').forEach(n=>n.remove())")
    assert (ready['ring'], ready['target'], ready_name) == (True, 'top', True), ready
    # A card whose resume is not ready is not the step's action.
    assert verify['target'] == 'history', verify


def test_names_of_object_properties_are_not_steps(embedded):
    # They used to reach the table's inherited properties, throw, and leave
    # the previous step's ring on screen.
    _client, evaluate, inner, _wait = embedded
    for name in ('__proto__', 'constructor', 'toString'):
        before = _cue(evaluate, inner, 'full-exit')
        state = _cue(evaluate, inner, name)
        assert before['ring'] and state == {'ring': False, 'here': False}, (name, state)


def test_an_edge_count_under_anything_but_its_own_hit_line_is_covered(embedded):
    """Review of #202: any SVG over an edge count, or a mail card (they take
    clicks), covers it; only that edge's own hit line does not."""
    _client, evaluate, inner, wait = embedded
    inner("setView('net')")
    wait("[...document.querySelectorAll('#net .edge-count')].some(e=>e.getBoundingClientRect().width>0)", 30)
    time.sleep(2)
    pair = inner(FIRST_PAIR)
    free = _cue(evaluate, inner, 'full-edge', pair=pair)
    # Lay another SVG shape over every count.
    inner("""(()=>{const svg=document.querySelector('#net svg')||document.getElementById('gsvg');
      for(const tx of document.querySelectorAll('#net .edge-count')){const b=tx.getBBox();
        const r=document.createElementNS('http://www.w3.org/2000/svg','rect');
        for(const [k,v] of Object.entries({x:b.x-4,y:b.y-4,width:b.width+8,height:b.height+8,fill:'transparent','class':'qa-cover'}))r.setAttribute(k,v);
        r.style.pointerEvents='all';tx.parentNode.appendChild(r);}})()""")
    covered = _cue(evaluate, inner, 'full-edge', pair=pair)
    inner("document.querySelectorAll('.qa-cover').forEach(n=>n.remove())")
    # A mail card over every count covers it too.
    inner("""(()=>{for(const tx of document.querySelectorAll('#net .edge-count')){const b=tx.getBoundingClientRect();
      const d=document.createElement('div');d.className='mail-card on qa-cover';
      Object.assign(d.style,{position:'fixed',left:(b.left-6)+'px',top:(b.top-6)+'px',width:(b.width+12)+'px',height:(b.height+12)+'px'});
      document.body.appendChild(d);}})()""")
    carded = _cue(evaluate, inner, 'full-edge', pair=pair)
    inner("document.querySelectorAll('.qa-cover').forEach(n=>n.remove())")
    assert free['target'] == 'edge-count', free
    assert covered['target'] == 'viewtog', covered
    assert carded['target'] == 'viewtog', carded


def test_only_the_games_edge_is_ringed(embedded):
    """The recording ringed an edge between two other agents: the step names
    the game's parent and child, so only their edge is pointed at; without
    them no edge is, and the ring stays on the view toggle."""
    _client, evaluate, inner, wait = embedded
    inner("setView('net')")
    wait("gEls.badge.filter(o=>o.tx.getBoundingClientRect().width>0).length>1", 30)
    time.sleep(2)
    pairs = inner("""(()=>{const n=e=>e&&typeof e==='object'?e.name??e.id:e;
      return gEls.badge.filter(o=>o.tx.getBoundingClientRect().width>0).map(o=>[n(o.s),n(o.t)]);})()""")
    ringed = []
    for pair in pairs[:3]:
        _cue(evaluate, inner, 'full-edge', pair=pair[::-1])
        # A mail card can pass over a count for a moment; wait for it to show.
        got, deadline = None, time.monotonic() + 5
        while got is None and time.monotonic() < deadline:
            got = inner("""(()=>{const el=TelemetryTourCue.pick('full-edge',%s);
              const n=e=>e&&typeof e==='object'?e.name??e.id:e;const b=el&&gEls.badge.find(o=>o.tx===el);
              return b?[n(b.s),n(b.t)]:null;})()""" % json.dumps(pair[::-1]))
            if got is None:
                time.sleep(.2)
        ringed.append(got)
    assert ringed == pairs[:3]
    none = _cue(evaluate, inner, 'full-edge')
    unknown = _cue(evaluate, inner, 'full-edge', pair=['NoSuchParent', 'NoSuchChild'])
    assert none['target'] == 'viewtog' and unknown['target'] == 'viewtog'


def test_an_open_panel_is_reported_so_the_checklist_can_fold(embedded):
    """The checklist hid the edge drawer and the agent panel in the recording:
    while a step is on, the page tells the cockpit what it has open."""
    _client, evaluate, inner, wait = embedded
    _cue(evaluate, inner, 'full-network-settings')
    # A page's first report is sent even when nothing is open, so one loaded
    # again clears what the page before it reported.
    first = evaluate("covers.slice()")
    inner("document.getElementById('settings-btn').click()")
    # Settings slides in; the page reports again as it moves, so wait for the
    # report to match where the panel settles.
    box_js = "(()=>{const b=document.getElementById('settings').getBoundingClientRect();return {l:Math.round(b.left),r:Math.round(b.right)};})()"
    opened, box, deadline = None, None, time.monotonic() + 8
    while time.monotonic() < deadline:
        box = inner(box_js)
        opened = evaluate("covers.length?covers.at(-1):null")
        if opened and len(opened) == 1 and (opened[0]['l'], opened[0]['r']) == (box['l'], box['r']):
            break
        time.sleep(.2)
    inner("document.getElementById('settings-close').click()")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and evaluate("covers.at(-1).length"):
        time.sleep(.1)
    closed = evaluate("covers.at(-1)")
    # No step, nothing reported even with a panel open.
    inner("document.getElementById('settings-btn').click()")
    _cue(evaluate, inner, None)
    time.sleep(1)
    after = evaluate("covers.at(-1)")
    inner("document.getElementById('settings-close').click()")
    assert len(opened) == 1 and (opened[0]['l'], opened[0]['r']) == (box['l'], box['r']), (opened, box)
    assert closed == [] and after == []
    assert first == [[]], first


def test_a_toast_sits_above_the_selection_bar(embedded):
    # The replay's summary toast covered the bar's RESUME in the recording.
    _client, _evaluate, inner, _wait = embedded
    boxes = inner("""(()=>{const bar=document.getElementById('selbar');bar.classList.add('show');
      toast('▸ REPLAY','8 events · 2 agents');
      return new Promise(done=>setTimeout(()=>{const t=document.getElementById('toast').getBoundingClientRect(),
        b=bar.getBoundingClientRect();bar.classList.remove('show');done({toastBottom:t.bottom,barTop:b.top});},400));})()""")
    assert boxes['toastBottom'] <= boxes['barTop'], boxes


def test_the_tag_keeps_clear_of_what_the_cockpit_lays_over_it(embedded):
    _client, evaluate, inner, _wait = embedded
    free = _cue(evaluate, inner, 'full-exit')
    box = free['hereBox']
    # The cockpit's tour panel over the same spot: the tag goes elsewhere.
    avoid = [{'l': box['l'] - 2, 't': box['t'] - 2, 'r': box['r'] + 2, 'b': box['b'] + 2}]
    moved = _cue(evaluate, inner, 'full-exit', avoid)
    h = moved['hereBox']
    assert moved['here'] and moved['arrowMatches']
    assert not (h['l'] < avoid[0]['r'] and h['r'] > avoid[0]['l'] and h['t'] < avoid[0]['b'] and h['b'] > avoid[0]['t'])


def test_light_theme_and_reduced_motion(embedded):
    client, evaluate, inner, _wait = embedded
    inner("document.documentElement.dataset.colorTheme='light'")
    light = _cue(evaluate, inner, 'full-exit')
    inner("delete document.documentElement.dataset.colorTheme")
    client.call('Emulation.setEmulatedMedia', features=[{'name': 'prefers-reduced-motion', 'value': 'reduce'}])
    try:
        still = _cue(evaluate, inner, 'full-exit')
    finally:
        client.call('Emulation.setEmulatedMedia', features=[])
    assert light['colour'] == 'rgb(10, 143, 163)'
    assert (still['ring'], still['pulse']) == (True, 'none')
