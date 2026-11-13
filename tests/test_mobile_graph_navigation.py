"""The original graph opens a verified Reader and supports name/role search."""
import base64
import os
from pathlib import Path

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


def test_real_node_tap_opens_reader_and_keeps_graph_view(phone):
    client,js,wait,state=phone
    graph(js,wait,state)
    # Restore SampleAgent so the fixture's selectable pane matches the tap.
    js("testNetwork.snapshot.agents[0].name='SampleAgent';testNetwork.update(testNetwork.snapshot);testNetwork.pause();testNetwork.fit();window.viewBefore=document.querySelector('#gsvg').getAttribute('viewBox')")
    js('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
    wait("testNetwork.elements.node.get('SampleAgent').grp.querySelector('.pimg')===null")
    point=js("(()=>{const m=testNetwork.elements.node.get('SampleAgent').grp.getScreenCTM();return{x:m.e,y:m.f}})()")
    client.call('Input.dispatchTouchEvent',type='touchStart',touchPoints=[dict(point,id=1)])
    client.call('Input.dispatchTouchEvent',type='touchEnd',touchPoints=[])
    wait("document.querySelector('#terminal-sheet').open&&document.querySelector('[data-id=result]')!==null")
    assert [body for op,body in state['calls'] if op=='targets'][-1]['name']=='SampleAgent'
    js('OrreryMobileTerminal.close()')
    assert js("document.querySelector('#gsvg').getAttribute('viewBox')===viewBefore")
    assert not any(op in {'prepare','commit'} for op,_ in state['calls'])


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
    js("window.opened=[];window.expectedIdentity={mail_instance_id:'a'.repeat(32),mail_agent_id:7};OrreryMobileIdentity.forName=name=>name==='Agent0'?expectedIdentity:null;OrreryMobileTerminal.open=(...args)=>opened.push(args);document.querySelector('#agent-search').value='Agent0';document.querySelector('#agent-search').dispatchEvent(new Event('input'));document.querySelector('.agent-result-open strong').textContent='Masked';document.querySelector('.agent-result-open').click()")
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
