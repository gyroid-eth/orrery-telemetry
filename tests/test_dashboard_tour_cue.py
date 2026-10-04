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
    # A selector, or [selector, label text] for a control whose label decides its action.
    for sels in steps.values():
        for entry in sels:
            assert (isinstance(entry, str) and entry) or (
                isinstance(entry, list) and len(entry) == 2 and all(isinstance(x, str) and x for x in entry)), entry


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
        '<script>window.cue=(step,avoid)=>document.getElementById("f").contentWindow'
        '.postMessage({type:"orrery-tour-cue",version:1,step,avoid},location.origin);</script>')
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


def _cue(evaluate, inner, step, avoid=None):
    evaluate(f"cue({json.dumps(step)},{json.dumps(avoid)})")
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
    rows['edge'] = _cue(evaluate, inner, 'full-edge')
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
    # Return is not RESUME, and a disabled OPEN is not pressed: close the panel.
    assert (returning['ring'], returning['target']) == (True, 'tm-x'), returning
    assert (disabled['ring'], disabled['target']) == (True, 'tm-x'), disabled


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
    free = _cue(evaluate, inner, 'full-edge')
    # Lay another SVG shape over every count.
    inner("""(()=>{const svg=document.querySelector('#net svg')||document.getElementById('gsvg');
      for(const tx of document.querySelectorAll('#net .edge-count')){const b=tx.getBBox();
        const r=document.createElementNS('http://www.w3.org/2000/svg','rect');
        for(const [k,v] of Object.entries({x:b.x-4,y:b.y-4,width:b.width+8,height:b.height+8,fill:'transparent','class':'qa-cover'}))r.setAttribute(k,v);
        r.style.pointerEvents='all';tx.parentNode.appendChild(r);}})()""")
    covered = _cue(evaluate, inner, 'full-edge')
    inner("document.querySelectorAll('.qa-cover').forEach(n=>n.remove())")
    # A mail card over every count covers it too.
    inner("""(()=>{for(const tx of document.querySelectorAll('#net .edge-count')){const b=tx.getBoundingClientRect();
      const d=document.createElement('div');d.className='mail-card on qa-cover';
      Object.assign(d.style,{position:'fixed',left:(b.left-6)+'px',top:(b.top-6)+'px',width:(b.width+12)+'px',height:(b.height+12)+'px'});
      document.body.appendChild(d);}})()""")
    carded = _cue(evaluate, inner, 'full-edge')
    inner("document.querySelectorAll('.qa-cover').forEach(n=>n.remove())")
    assert free['target'] == 'edge-count', free
    assert covered['target'] == 'viewtog', covered
    assert carded['target'] == 'viewtog', carded


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
