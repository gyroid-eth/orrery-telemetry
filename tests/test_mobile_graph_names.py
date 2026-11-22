"""Mobile labels share zoom rules without changing identity or desktop styling."""
import json
import os

import pytest

from mobile_reader_browser import phone
from test_mobile_graph_updates import graph


def named_graph(js,wait,state,scope='all',selected_running=True):
    graph(js,wait,state)
    changed=state['snapshot']
    for i,agent in enumerate(changed['agents']):agent['role']=['Coordinator','Review','Build'][i]
    changed['agents'][0].update(running=selected_running,state='run' if selected_running else 'finished')
    changed['graph']['edges']=[dict(source='Agent0',target='Agent'+str(i),kind='to',count=2) for i in [1,2]]
    # Run the real adapter's rebuild/metadata path in either display mode.
    js("testNetwork.selected='Agent0';testNetwork.mode="+json.dumps(scope)+";testNetwork.renderKey='';testNetwork.update("+json.dumps(changed)+");testNetwork.pause();testNetwork.view.k=1;testNetwork.applyView()")


@pytest.mark.parametrize('scope',['focus','all'])
@pytest.mark.parametrize('selected_running',[True,False])
def test_all_names_and_roles_share_the_75_percent_boundary(phone,scope,selected_running):
    _,js,wait,state=phone
    named_graph(js,wait,state,scope,selected_running)
    for zoom,visible in [(1,True),(.749,False),(.75,True),(1.5,True)]:
        js('testNetwork.view.k='+str(zoom)+';testNetwork.applyView()')
        assert js("[...document.querySelectorAll('#gsvg>.node')].map(n=>getComputedStyle(n.querySelector('text:not([class])')).display!=='none')")==[visible]*3
        assert js("[...document.querySelectorAll('#gsvg>.node .annot-lab,#gsvg>.node .annot-pill')].map(n=>getComputedStyle(n).display!=='none')")==[visible]*6
    assert js("getComputedStyle(document.querySelector('#gsvg .selnode>text:not([class])')).fontSize")==('11px' if selected_running else '10.5px')
    assert js("[...document.querySelectorAll('#gsvg .node:not(.selnode)>text:not([class])')].map(n=>getComputedStyle(n).fontSize)")==['10px']*2
    assert js("[...document.querySelectorAll('#gsvg>.node')].every(n=>Number(n.querySelector('text:not([class])').getAttribute('y'))<Number(n.querySelector('.annot-lab').getAttribute('y')))")
    js("testNetwork.select('Agent1');testNetwork.pause();testNetwork.view.k=1;testNetwork.applyView()")
    assert js("getComputedStyle(document.querySelector('#gsvg .selnode>text:not([class])')).fontSize")=='11px'
    assert js("getComputedStyle(testNetwork.elements.node.get('Agent0').grp.querySelector('text:not([class])')).fontSize")=='10px'


def test_mobile_name_rule_does_not_change_desktop_node_styles(phone):
    _,js,wait,state=phone
    named_graph(js,wait,state)
    js("document.body.classList.remove('mobile');window.mobileStyle=document.querySelector('link[href=\"/mobile.css\"]');mobileStyle.disabled=true;window.desktopStyle=()=>[...document.querySelectorAll('#gsvg>.node')].map(n=>{const s=getComputedStyle(n.querySelector('text:not([class])'));return[s.display,s.fontFamily,s.fontSize,s.fontWeight,s.fill]});window.desktopBefore=desktopStyle()")
    js('mobileStyle.disabled=false')
    assert js('desktopStyle()')==js('desktopBefore')
    assert js("desktopStyle().map(s=>s[2])")==['11px']*3


def test_large_all_view_uses_the_same_mobile_zoom_boundary(phone):
    _,js,wait,state=phone
    graph(js,wait,state,301)
    js("const changed=structuredClone(testNetwork.snapshot);changed.agents.forEach(a=>a.role='Review');testNetwork.update(changed);testNetwork.pause();testNetwork.view.k=.75;testNetwork.applyView()")
    assert js("document.querySelector('#net').classList.contains('dense')")
    assert js("[...document.querySelectorAll('#gsvg>.node>text:not([class])')].filter(n=>getComputedStyle(n).display!=='none').length")==301
    assert js("[...document.querySelectorAll('#gsvg>.node>.annot-lab')].filter(n=>getComputedStyle(n).display!=='none').length")==301
    js('testNetwork.view.k=.749;testNetwork.applyView()')
    assert js("[...document.querySelectorAll('#gsvg>.node>text:not([class]),#gsvg>.node>.annot-lab,#gsvg>.node>.annot-pill')].every(n=>getComputedStyle(n).display==='none')")


@pytest.mark.parametrize('scope',['focus','all'])
def test_demo_masks_each_name_without_changing_the_reader_target(phone,scope):
    if not os.environ.get('ORRERY_DEMO_BRIDGE_ROOT'):pytest.skip('paired desktop scripts required')
    _,js,wait,state=phone
    named_graph(js,wait,state,scope)
    state['demo']=dict(version=1,supported=True,on=True,words=['Agent0','Agent1','Agent2'],identity=dict(users=['viewer'],hosts=['viewer-host']))
    js('OrreryMobileDemo.refresh()')
    wait("[...document.querySelectorAll('#gsvg>.node>text:not([class])')].every(n=>n.textContent==='******')")
    assert js("testNetwork.elements.node.get('Agent1').grp.getAttribute('aria-label').includes('Agent1')") is False
    # A metadata replacement must also be remasked, not just the initial nodes.
    js("const changed=structuredClone(testNetwork.snapshot);changed.agents[1].ctx_used=41;testNetwork.update(changed);testNetwork.pause()")
    wait("testNetwork.elements.node.get('Agent1').grp.querySelector('text:not([class])').textContent==='******'")
    js("testNetwork.elements.node.get('Agent1').grp.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}))")
    if scope=='focus':
        assert js('testNetwork.selected')=='Agent1'
        js("testNetwork.elements.node.get('Agent1').grp.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}))")
    wait("document.querySelector('#terminal-sheet').open&&document.querySelector('[data-id=result]')!==null")
    assert any('/view/conversation?name=Agent1' in p for p in state['get_calls'])
    assert not any('name=%2A' in p for p in state['get_calls'])
