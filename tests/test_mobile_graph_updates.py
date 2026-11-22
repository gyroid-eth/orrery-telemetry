"""Metadata changes preserve graph geometry and unaffected portrait nodes."""
import base64
import os
from pathlib import Path

from mobile_reader_browser import phone


def graph(js,wait,state,count=3):
    sample=state['snapshot']['agents'][0]
    agents=[dict(sample,name='Agent'+str(i),ctx_used=30) for i in range(count)]
    state['snapshot']['agents']=agents
    state['snapshot']['graph'].update(nodes=agents,edges=[],spawn=[])
    js("OrreryMobileTerminal.close();window.testNetwork=null;const original=OrreryMobileNetwork.prototype.update;OrreryMobileNetwork.prototype.update=function(...args){testNetwork=this;return original.apply(this,args)};document.dispatchEvent(new Event('visibilitychange'))")
    wait('testNetwork?.agents.length==='+str(count),12)
    js("testNetwork.mode='all';testNetwork.renderKey='';testNetwork.update(testNetwork.snapshot);testNetwork.pause();testNetwork.hot=0;[...testNetwork.map.values()].forEach((g,i)=>{g.x=80+i*80;g.y=70+i%2*100});OrreryNetworkCore.paint(testNetwork.map,testNetwork.elements,testNetwork.params.NSIZE);window.savedNodes=new Map([...testNetwork.elements.node].map(([name,entry])=>[name,entry.grp]));window.savedPositions=[...testNetwork.map].map(([name,g])=>[name,g.x,g.y]);testNetwork.view={vx:11,vy:22,k:1.3};testNetwork.userView=true;testNetwork.applyView();window.savedView=document.querySelector('#gsvg').getAttribute('viewBox')")


def test_context_refresh_preserves_positions_unaffected_nodes_and_zoom(phone):
    _,js,wait,state=phone
    graph(js,wait,state)
    js("window.changed=structuredClone(testNetwork.snapshot);changed.agents[0].ctx_used=41;testNetwork.update(changed)")
    assert js("testNetwork.elements.node.get('Agent1').grp===savedNodes.get('Agent1')")
    assert js("testNetwork.elements.node.get('Agent2').grp===savedNodes.get('Agent2')")
    assert js("testNetwork.elements.node.get('Agent0').grp.querySelector('.ctx-arc').dataset.remaining")=='59'
    assert js("JSON.stringify([...testNetwork.map].map(([name,g])=>[name,g.x,g.y]))===JSON.stringify(savedPositions)")
    assert js("document.querySelector('#gsvg').getAttribute('viewBox')===savedView")
    assert js('testNetwork.raf===0&&testNetwork.hot===0')
    js('testNetwork.update(changed)')
    assert js('testNetwork.raf===0&&testNetwork.hot===0')


def test_question_refresh_stays_in_front_without_restarting_layout(phone):
    client,js,wait,state=phone
    graph(js,wait,state)
    js("window.changed=structuredClone(testNetwork.snapshot);changed.agents[0].act_state='question';testNetwork.update(changed)")
    assert js("[...document.querySelectorAll('#gsvg>.node')].at(-1).dataset.name")=='Agent0'
    assert js("testNetwork.elements.node.get('Agent1').grp===savedNodes.get('Agent1')")
    assert js("testNetwork.elements.node.get('Agent0').grp.querySelector('.qbub')!==null")
    assert js('testNetwork.raf===0&&testNetwork.hot===0')
    assert js("JSON.stringify([...testNetwork.map].map(([name,g])=>[name,g.x,g.y]))===JSON.stringify(savedPositions)")
    folder=os.environ.get('MOBILE_GRAPH_CAPTURE_DIR')
    if folder:
        js('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
        path=Path(folder);path.mkdir(parents=True,exist_ok=True)
        (path/'graph-metadata.png').write_bytes(base64.b64decode(client.call('Page.captureScreenshot',format='png')['data']))


def test_edges_roster_and_repeated_scope_selection_still_rebuild(phone):
    _,js,wait,state=phone
    graph(js,wait,state)
    js("window.changed=structuredClone(testNetwork.snapshot);changed.graph.edges=[{source:'Agent0',target:'Agent1',kind:'to',count:3}];testNetwork.update(changed);testNetwork.pause()")
    assert js("testNetwork.edges.length===1&&document.querySelector('#gsvg .edge-count').textContent==='3'")
    assert js("testNetwork.elements.node.get('Agent1').grp!==savedNodes.get('Agent1')")
    js("document.querySelector('[data-mode=all]').click();testNetwork.pause()")
    assert js('testNetwork.map.size')==3
    js("changed=structuredClone(testNetwork.snapshot);changed.agents.pop();changed.graph.nodes=changed.agents;testNetwork.update(changed);testNetwork.pause()")
    assert js('testNetwork.map.size')==2


def test_focus_keeps_the_strongest_peers_when_counts_change(phone):
    _,js,wait,state=phone
    graph(js,wait,state,7)
    js("window.changed=structuredClone(testNetwork.snapshot);changed.graph.edges=changed.agents.slice(1).map((a,i)=>({source:'Agent0',target:a.name,kind:'to',count:10-i}));testNetwork.mode='focus';testNetwork.selected='Agent0';testNetwork.renderKey='';testNetwork.update(changed);testNetwork.pause()")
    assert js("testNetwork.map.has('Agent1')&&!testNetwork.map.has('Agent6')")
    js("changed=structuredClone(changed);changed.graph.edges.at(-1).count=100;testNetwork.update(changed);testNetwork.pause()")
    assert js("testNetwork.map.has('Agent6')&&!testNetwork.map.has('Agent5')")
