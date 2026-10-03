"""Run real dashboard handlers: attempted/failed actions must not notify a host."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SOURCE = (Path(__file__).resolve().parents[1] / 'dashboard/index.html').read_text()


def function(name):
    return re.search(r'(?:async )?function ' + name + r'\(.*?\n}', SOURCE, re.S).group()


def run(js, embedded=True):
    if not shutil.which('node'):
        pytest.skip('node unavailable')
    helper = function('notifyTourAction')
    prelude = f"const EMBED_MODE={str(embedded).lower()};" + """
      const events=[];const location={origin:'http://localhost:1234'};
      const window={parent:{postMessage:(message,origin)=>events.push({...message,origin})}};
      const toast=()=>{};
    """
    result = subprocess.run(['node', '-e', prelude + helper + js],
                            text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def actions(events):
    return [e['action'] for e in events if e['type'] == 'orrery-tour-action']


def test_notifications_are_minimal_same_origin_and_embedded_only():
    js = "notifyTourAction('edge');notifyTourAction('unknown');console.log(JSON.stringify(events));"
    assert run(js) == [{'type': 'orrery-tour-action', 'version': 1,
                       'action': 'edge', 'origin': 'http://localhost:1234'}]
    assert run(js, embedded=False) == []
    assert run("window.parent=window;" + js) == []
    assert run("window.parent.postMessage=()=>{throw Error('gone')};" + js) == []


@pytest.mark.parametrize('ok,http_ok,action,expected', [
    (True, True, 'resumed', ['resume', 'return']),
    (True, True, 'already_running', ['return']),
    (False, True, 'resumed', []),
    (True, False, 'resumed', []),
])
def test_resume_and_host_handoff_only_after_success(ok, http_ok, action, expected):
    js = f"async function fetch(){{return {{ok:{str(http_ok).lower()},json:async()=>({json.dumps({'ok': ok, 'action': action})})}};}}"
    js += function('jump') + "jump('Pilot',{stopPropagation(){}}).then(()=>console.log(JSON.stringify(events)));"
    result = run(js)
    assert actions(result) == expected
    assert sum(e['type'] == 'orrery-jump' for e in result) == bool(expected)


def test_resume_transport_failure_never_hands_off():
    js = "async function fetch(){throw Error('offline');}" + function('jump')
    assert run(js + "jump('Pilot',{stopPropagation(){}}).then(()=>console.log(JSON.stringify(events)));") == []


@pytest.mark.parametrize('ok,http_ok,action,expected', [
    (True, True, 'resumed', ['resume']),
    (True, True, 'already_running', []),
    (False, True, 'resumed', []),
    (True, False, 'resumed', []),
])
def test_bulk_resume_only_notifies_completed_resume(ok, http_ok, action, expected):
    js = """
      let bulkBusy=false;const selectedSet=new Set(['Pilot']);
      const btn={classList:{add(){},remove(){}},querySelector:()=>({textContent:''})};
      const refreshSelClasses=()=>{},updateSelBar=()=>{};
    """
    js += f"async function fetch(){{return {{ok:{str(http_ok).lower()},json:async()=>({json.dumps({'ok':ok,'action':action})})}};}}"
    js += function('bulkDispatch') + "bulkDispatch('resume',['Pilot'],btn).then(()=>console.log(JSON.stringify(events)));"
    assert actions(run(js)) == expected


@pytest.mark.parametrize('payload,http_ok,expected', [
    ({'ok': True, 'actions': ['exit-sent']}, True, ['exit']),
    ({'ok': True, 'actions': ['shell-exit-sent']}, True, []),
    ({'ok': False, 'actions': ['exit-sent']}, True, []),
    ({'ok': True, 'actions': ['exit-sent']}, False, []),
])
def test_deck_exit_arming_failures_and_cleanup_do_not_complete(payload, http_ok, expected):
    js = """
      const exitingSet=new Set(),exitTimers=new Map(),ARM_MS=5000;
      const card={classList:{add(){},remove(){}},querySelector:()=>({textContent:''})};
      const document={querySelector:()=>card},CSS={escape:x=>x};
      const setTimeout=()=>1,clearTimeout=()=>{};
      let requests=0;
    """
    js += f"async function fetch(){{requests++;return {{ok:{str(http_ok).lower()},json:async()=>({json.dumps(payload)})}};}}"
    js += function('exitAgent') + """
      (async()=>{await exitAgent({stopPropagation(){}},'Pilot');
      const armed={events:[...events],requests};
      await exitAgent({stopPropagation(){}},'Pilot');
      console.log(JSON.stringify({armed,events,requests}));})();
    """
    result = run(js)
    assert result['armed'] == {'events': [], 'requests': 0}
    assert result['requests'] == 1
    assert actions(result['events']) == expected


@pytest.mark.parametrize('count,mode,expected', [(0, True, []), (1, True, []),
                                               (2, True, ['select']), (2, False, [])])
def test_selection_requires_two_registered_nodes(count, mode, expected):
    js = f"const selectMode={str(mode).lower()};const selectedSet=new Set(['A','B','not-registered']);"
    js += f"const gmap=new Map({json.dumps([[x, {}] for x in ['A', 'B'][:count]])});"
    result = run(js + function('notifyTourSelection') +
                 "notifyTourSelection();console.log(JSON.stringify(events));")
    assert actions(result) == expected


@pytest.mark.parametrize('value,fail,expected', [(13, False, []), (16, False, ['settings']),
                                               (16, True, [])])
def test_network_parameter_completion_follows_changed_successful_application(value, fail, expected):
    js = f"let value=13;const input={{value:{value},addEventListener:(_,fn)=>input.apply=fn}};"
    js += """
      const cfg={get:()=>value,set:v=>value=v,apply:'repaint'};
      const NC_CFG={NSIZE:cfg};
      const row={dataset:{p:'NSIZE'},querySelector:()=>input};
      const rows={querySelectorAll:()=>[row]};
      const document={getElementById:id=>id==='nctl-rows'?rows:{addEventListener(){}}};
      const ncSyncRow=()=>{};
    """
    js += "const ncApply=()=>{" + ("throw Error('render failed');" if fail else "") + "};"
    js += function('initNetCtl') + "initNetCtl();try{input.apply();}catch(_){}console.log(JSON.stringify(events));"
    assert actions(run(js)) == expected


@pytest.mark.parametrize('ok,http_ok,stale,expected', [
    (True, True, False, ['edge']), (False, True, False, []),
    (True, False, False, []), (True, True, True, []),
])
def test_edge_notification_requires_current_successfully_rendered_response(ok, http_ok, stale, expected):
    js = """
      let _edCur=null,_edSeq=0;
      const gEls={edge:[]},edrawer={querySelector:()=>({textContent:''}),classList:{add(){}},setAttribute(){}};
      const document={getElementById:()=>({innerHTML:''})};
      function renderDrawerThread(){}function renderDrawerError(){}
    """
    js += f"async function fetch(){{{'_edSeq++;' if stale else ''}return {{ok:{str(http_ok).lower()},json:async()=>({json.dumps({'ok':ok,'messages':[]})})}};}}"
    js += function('openDrawer') + "openDrawer('A','B').then(()=>console.log(JSON.stringify(events)));"
    assert actions(run(js)) == expected


def test_cancelled_rectangle_cannot_notify_and_replay_requires_loaded_events():
    # The browser suite exercises both full handlers; these guards protect their placement.
    assert "if(ev.type==='pointerup'&&(W>=2||H>=2))notifyTourSelection();" in SOURCE
    replay = function('startReplay')
    assert "if(!r.ok)j=null;" in replay
    assert replay.index("if(!j||!j.ok)") < replay.index("notifyTourAction('replay')")
    assert replay.index('_rbPlay();') < replay.index("if(RP.active&&RP.events.length)notifyTourAction('replay');")
