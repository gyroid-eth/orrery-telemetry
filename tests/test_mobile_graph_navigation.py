"""The original graph opens a verified Reader and supports name/role search."""
import base64
import json
import os
from pathlib import Path

import pytest

from mobile_reader_browser import phone
from test_mobile_graph_updates import graph


def test_roles_use_the_existing_renderer_and_update_without_relayout(phone):
    client,js,wait,state=phone
    graph(js,wait,state)
    js("window.changed=structuredClone(testNetwork.snapshot);changed.agents[0].role='Coordinator';changed.agents[1].role='Review';testNetwork.update(changed)")
    assert js("testNetwork.elements.node.get('Agent0').grp.querySelector('.annot-lab').textContent")=='Coordinator'
    assert js("testNetwork.elements.node.get('Agent2').grp===savedNodes.get('Agent2')")
    assert js('testNetwork.raf===0&&testNetwork.hot===0')
    assert js("document.querySelector('.mobile-brand img').getAttribute('src')")=='/assets/orbit.svg'
    assert 'Fraunces' in js("getComputedStyle(document.querySelector('.mobile-brand strong')).fontFamily")
    assert 'IBM Plex Mono' in js('getComputedStyle(document.body).fontFamily')
    assert js("document.querySelector('.selected-agent')===null&&document.querySelector('#terminal-graph-entry')===null&&document.querySelector('#graph-panel>.legend')===null")
    assert js('document.documentElement.scrollWidth<=innerWidth')
    capture(client,'graph-with-roles')


def test_all_node_tap_opens_reader_and_returns_to_focus(phone):
    client,js,wait,state=phone
    graph(js,wait,state)
    # Restore SampleAgent so the fixture's selectable pane matches the tap.
    js("testNetwork.snapshot.agents[0].name='SampleAgent';testNetwork.update(testNetwork.snapshot);testNetwork.pause();testNetwork.fit()")
    js('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
    wait("testNetwork.elements.node.get('SampleAgent').grp.querySelector('.pimg')===null")
    point=js("(()=>{const m=testNetwork.elements.node.get('SampleAgent').grp.getScreenCTM();return{x:m.e,y:m.f}})()")
    client.call('Input.dispatchTouchEvent',type='touchStart',touchPoints=[dict(point,id=1)])
    client.call('Input.dispatchTouchEvent',type='touchEnd',touchPoints=[])
    wait("document.querySelector('#terminal-sheet').open&&document.querySelector('[data-id=result]')!==null")
    assert [body for op,body in state['calls'] if op=='targets'][-1]['name']=='SampleAgent'
    js('OrreryMobileTerminal.close()')
    assert js("testNetwork.mode==='focus'&&testNetwork.selected==='SampleAgent'&&!testNetwork.userView")
    assert not any(op in {'prepare','commit'} for op,_ in state['calls'])


def focus_graph(js,wait,state):
    graph(js,wait,state,8)
    agents=state['snapshot']['agents']
    agents[0]['role']='Coordinator'
    agents[1].update(name='SampleAgent',role='Review')
    edges=[dict(source='Agent0',target=a['name'],kind='to',count=100 if i==1 else 80-i*10)
           for i,a in enumerate(agents[1:7],1)]
    edges.extend(dict(source='SampleAgent',target=a['name'],kind='to',count=200 if i==7 else 110-i*10)
                 for i,a in enumerate(agents[2:],2))
    state['snapshot']['graph']['edges']=edges
    js('testNetwork.update('+json.dumps(state['snapshot'])+');testNetwork.selected="Agent0";document.querySelector("[data-mode=focus]").click()')
    wait('testNetwork.raf===0')


def node_point(js,wait,name):
    # Let the compositor catch up after a layout/fit and the failed PNG's
    # initials fallback; these are actual touch inputs, not DOM clicks.
    js('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
    wait("testNetwork.elements.node.get("+json.dumps(name)+").grp.querySelector('.pimg')===null")
    return js('(()=>{const m=testNetwork.elements.node.get('+json.dumps(name)+').grp.getScreenCTM();return{x:m.e,y:m.f}})()')


@pytest.mark.parametrize('theme',['dark','light'])
@pytest.mark.parametrize('pointer',['touch','mouse'])
def test_focus_peer_tap_changes_center_then_center_tap_opens_reader(phone,theme,pointer):
    client,js,wait,state=phone
    focus_graph(js,wait,state)
    if theme=='light':
        js((Path(__file__).resolve().parents[1]/'dashboard/theme_core.js').read_text())
        js("document.documentElement.dataset.colorTheme='light';Object.entries(AgentStackColorThemeCore.deriveWarmPaperLightTheme().cssVariables).forEach(([k,v])=>document.documentElement.style.setProperty(k,v))")
    assert 'Focus: tap a peer to focus' in js("document.querySelector('#gsvg').getAttribute('aria-label')")
    assert js("testNetwork.map.has('Agent5')&&!testNetwork.map.has('Agent7')")
    assert js("testNetwork.elements.node.get('Agent0').grp.getAttribute('aria-label')")=='Open Reader for Agent0 · Coordinator'
    assert js("testNetwork.elements.node.get('SampleAgent').grp.getAttribute('aria-label')")=='Focus on SampleAgent · Review'
    capture(client,'focus-'+theme+'-'+pointer+'-before')
    calls=len(state['calls'])
    # Pointer activation must also clear any prior keyboard focus in the graph.
    js("testNetwork.elements.node.get('Agent0').grp.focus()")
    point=node_point(js,wait,'SampleAgent')
    if pointer=='touch':
        client.call('Input.dispatchTouchEvent',type='touchStart',touchPoints=[dict(point,id=1)])
        client.call('Input.dispatchTouchEvent',type='touchEnd',touchPoints=[])
    else:
        client.call('Input.dispatchMouseEvent',type='mousePressed',button='left',clickCount=1,**point)
        client.call('Input.dispatchMouseEvent',type='mouseReleased',button='left',clickCount=1,**point)
    wait("testNetwork.selected==='SampleAgent'&&testNetwork.raf===0")
    js('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
    assert js("!document.querySelector('#gsvg').contains(document.activeElement)")
    assert js("document.querySelector('#gsvg :focus')===null")
    assert js("testNetwork.mode==='focus'&&!document.querySelector('#terminal-sheet').open&&!testNetwork.userView")
    assert js("[...testNetwork.map.keys()]")==['SampleAgent','Agent7','Agent0','Agent2','Agent3','Agent4']
    assert len(state['calls'])==calls
    assert js("document.querySelector('#gsvg .selnode').dataset.name")=='SampleAgent'
    assert js("testNetwork.elements.node.get('SampleAgent').grp.getAttribute('aria-label')")=='Open Reader for SampleAgent · Review'
    assert js("testNetwork.elements.node.get('Agent0').grp.getAttribute('aria-label')")=='Focus on Agent0 · Coordinator'
    assert js("(()=>{const v=OrreryNetworkCore.fit(testNetwork.map,testNetwork.dimensions());return ['vx','vy','k'].every(key=>Math.abs(v[key]-testNetwork.view[key])<.001)})()")
    capture(client,'focus-'+theme+'-'+pointer+'-after')
    point=node_point(js,wait,'SampleAgent')
    client.call('Input.dispatchTouchEvent',type='touchStart',touchPoints=[dict(point,id=1)])
    client.call('Input.dispatchTouchEvent',type='touchEnd',touchPoints=[])
    wait("document.querySelector('#terminal-sheet').open&&document.querySelector('[data-id=result]')!==null")
    assert [body for op,body in state['calls'] if op=='targets'][-1]['name']=='SampleAgent'
    capture(client,'focus-'+theme+'-'+pointer+'-center-reader')
    assert not any(op in {'prepare','commit'} for op,_ in state['calls'])


@pytest.mark.parametrize('key,code,keycode',[('Enter','Enter',13),(' ','Space',32)])
def test_focus_keyboard_keeps_focus_for_the_second_activation(phone,key,code,keycode):
    client,js,wait,state=phone
    focus_graph(js,wait,state)
    js("testNetwork.elements.node.get('SampleAgent').grp.focus()")
    for step in range(2):
        client.call('Input.dispatchKeyEvent',type='keyDown',key=key,code=code,windowsVirtualKeyCode=keycode)
        client.call('Input.dispatchKeyEvent',type='keyUp',key=key,code=code,windowsVirtualKeyCode=keycode)
        if step==0:
            assert js("testNetwork.selected==='SampleAgent'&&!document.querySelector('#terminal-sheet').open")
            assert js("document.activeElement===testNetwork.elements.node.get('SampleAgent').grp")
            assert js("document.activeElement.getAttribute('aria-label')")=='Open Reader for SampleAgent · Review'
    wait("document.querySelector('#terminal-sheet').open&&document.querySelector('[data-id=result]')!==null")


@pytest.mark.parametrize('gesture',['drag','cancel','pinch'])
def test_focus_gestures_do_not_change_center_or_open_reader(phone,gesture):
    client,js,wait,state=phone
    focus_graph(js,wait,state)
    point=node_point(js,wait,'SampleAgent')
    first=dict(point,id=1)
    client.call('Input.dispatchTouchEvent',type='touchStart',touchPoints=[first])
    if gesture=='drag':
        client.call('Input.dispatchTouchEvent',type='touchMove',touchPoints=[dict(first,x=point['x']+35,y=point['y']+20)])
    elif gesture=='pinch':
        second=dict(point,id=2,x=point['x']+20)
        client.call('Input.dispatchTouchEvent',type='touchStart',touchPoints=[first,second])
        client.call('Input.dispatchTouchEvent',type='touchMove',touchPoints=[dict(first,x=point['x']-15),dict(second,x=point['x']+35)])
    client.call('Input.dispatchTouchEvent',type='touchCancel' if gesture=='cancel' else 'touchEnd',touchPoints=[])
    assert js("testNetwork.selected==='Agent0'&&!document.querySelector('#terminal-sheet').open")


def test_search_matches_role_and_focus_is_separate_from_opening_reader(phone):
    client,js,wait,state=phone
    graph(js,wait,state,18)
    js("window.changed=structuredClone(testNetwork.snapshot);changed.agents.forEach(a=>a.role='Review');changed.agents[1].role='Coordinator';testNetwork.update(changed);testNetwork.pause();document.querySelector('#agent-search').value='coordin';document.querySelector('#agent-search').dispatchEvent(new Event('input'))")
    assert js("document.querySelectorAll('.agent-result-open').length")==1
    assert js("document.querySelector('.agent-result-open strong').textContent")=='Agent1'
    js("document.querySelector('.agent-result-focus').click();testNetwork.pause()")
    assert js("testNetwork.mode==='focus'&&testNetwork.selected==='Agent1'")
    assert js("document.querySelector('#agent-results').hidden&&!document.querySelector('#terminal-sheet').open")
    js("document.querySelector('#agent-search').value='review';document.querySelector('#agent-search').dispatchEvent(new Event('input'))")
    assert js("document.querySelectorAll('.agent-result-open').length")==12
    assert js("document.querySelector('.agent-search-note').textContent")=='Showing 12 of 17 · keep typing to narrow your search'
    capture(client,'search-by-role')
    js("document.querySelector('#search-clear').click()")
    assert js("document.querySelector('#agent-results').hidden&&document.activeElement.id==='agent-search'")


def test_search_uses_raw_identity_and_drag_does_not_open_reader(phone):
    client,js,wait,state=phone
    graph(js,wait,state)
    # A spy at the Reader boundary checks the real graph/search adapter's
    # I/A argument independently of visible labels and target selection.
    js("window.opened=[];window.expectedIdentity={mail_instance_id:'a'.repeat(32),mail_agent_id:7};OrreryMobileIdentity.forName=name=>name==='Agent0'?expectedIdentity:null;OrreryMobileTerminal.open=(...args)=>opened.push(args.slice(0,2));document.querySelector('#agent-search').value='Agent0';document.querySelector('#agent-search').dispatchEvent(new Event('input'));document.querySelector('.agent-result-open strong').textContent='Masked';document.querySelector('.agent-result-open').click()")
    assert js('opened')==[['Agent0',dict(mail_instance_id='a'*32,mail_agent_id=7)]]
    js("document.querySelector('#search-clear').click();testNetwork.fit();window.opened=[]")
    js('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
    point=js("(()=>{const m=testNetwork.elements.node.get('Agent0').grp.getScreenCTM();return{x:m.e,y:m.f}})()")
    client.call('Input.dispatchMouseEvent',type='mousePressed',button='left',clickCount=1,**point)
    client.call('Input.dispatchMouseEvent',type='mouseMoved',button='left',buttons=1,x=point['x']+35,y=point['y']+20)
    client.call('Input.dispatchMouseEvent',type='mouseReleased',button='left',clickCount=1,x=point['x']+35,y=point['y']+20)
    assert js('opened')==[]
    js("OrreryMobileTerminal.available=()=>false;testNetwork.elements.node.get('Agent0').grp.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}))")
    assert js("!document.querySelector('#graph-reader-status').hidden")
    assert js('opened')==[]


def test_expanded_graph_fits_both_orientations_and_back_closes_reader_first(phone):
    client,js,wait,state=phone
    graph(js,wait,state)
    js("Object.defineProperty(document,'fullscreenEnabled',{configurable:true,value:false});document.querySelector('#graph-fullscreen').click()")
    wait("document.documentElement.classList.contains('graph-fullscreen')")
    for width,height in [(390,844),(844,390)]:
        client.call('Emulation.setDeviceMetricsOverride',width=width,height=height,deviceScaleFactor=2,mobile=True)
        js('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
        assert js("(()=>{const r=document.querySelector('#net').getBoundingClientRect(),b=document.querySelector('#fit').getBoundingClientRect();return r.height>120&&r.x>=0&&r.y>=0&&r.right<=innerWidth&&r.bottom<=innerHeight&&b.bottom<=r.bottom&&b.right<=r.right})()")
    js("testNetwork.elements.node.get('Agent0').grp.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}))")
    wait("document.querySelector('#terminal-sheet').open")
    js('history.back()')
    wait("!document.querySelector('#terminal-sheet').open")
    assert js("document.documentElement.classList.contains('graph-fullscreen')")
    js('history.back()')
    wait("!document.documentElement.classList.contains('graph-fullscreen')")


def capture(client,name):
    folder=os.environ.get('MOBILE_GRAPH_CAPTURE_DIR')
    if folder:
        path=Path(folder);path.mkdir(parents=True,exist_ok=True)
        (path/(name+'.png')).write_bytes(base64.b64decode(client.call('Page.captureScreenshot',format='png')['data']))
