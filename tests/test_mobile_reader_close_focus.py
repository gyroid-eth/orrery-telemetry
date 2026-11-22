"""Graph-entry Readers return to the last agent's Focus, via the real phone page."""
import base64
import json
import os
import time
from pathlib import Path

import pytest

from mobile_reader_browser import phone
from test_mobile_graph_navigation import focus_graph, node_point


def setup_graph(js, wait, state, mode='all'):
    focus_graph(js, wait, state)
    wait("history.state?.orreryConversation!==true")
    select_target(state, 'SampleAgent')
    if mode == 'all':
        js("document.querySelector('[data-mode=all]').click()")
    else:
        js("testNetwork.select('SampleAgent')")
    wait('testNetwork.raf===0')


def select_target(state, name):
    state['targets'] = [dict(name=name, choice='choice-'+name,
                             pane_label='Window 0 · pane 0', revision='fixture-revision')]


def touch_node(client, js, wait, name='SampleAgent'):
    point = node_point(js, wait, name)
    client.call('Input.dispatchTouchEvent', type='touchStart', touchPoints=[dict(point, id=1)])
    client.call('Input.dispatchTouchEvent', type='touchEnd', touchPoints=[])
    wait("document.querySelector('#terminal-sheet').open&&document.querySelector('[data-id=result]')!==null")
    wait("document.querySelector('#terminal-text:not([disabled])')!==null")


def finish(js, wait, action):
    js('history.back()' if action == 'back' else "document.querySelector('.terminal-close').click()")
    wait("!document.querySelector('#terminal-sheet').open&&history.state?.orreryConversation!==true")


def assert_focus(js, wait, state, name='SampleAgent'):
    wait('testNetwork.raf===0')
    assert js('testNetwork.mode') == 'focus'
    assert js('testNetwork.selected') == name
    peers = {}
    for edge in state['snapshot']['graph']['edges']:
        peer = edge['target'] if edge['source'] == name else edge['source'] if edge['target'] == name else None
        if peer:
            peers[peer] = peers.get(peer, 0) + edge['count']
    assert js('[...testNetwork.map.keys()]') == [name] + sorted(peers, key=lambda p: (-peers[p], p))[:5]
    assert js("document.querySelector('[data-mode=focus]').classList.contains('active')&&!document.querySelector('[data-mode=all]').classList.contains('active')")
    assert js("!testNetwork.userView&&document.querySelector('#agent-results').hidden&&document.querySelector('#agent-search').value===''")
    assert js("document.querySelector('#gsvg :focus')===null&&!document.querySelector('#gsvg').contains(document.activeElement)")
    assert js("(()=>{const v=OrreryNetworkCore.fit(testNetwork.map,testNetwork.dimensions());return ['vx','vy','k'].every(key=>Math.abs(v[key]-testNetwork.view[key])<.001)})()")
    assert not any(op in {'prepare', 'commit'} for op, _ in state['calls'])


def capture(client, label):
    folder = os.environ.get('MOBILE_CLOSE_FOCUS_CAPTURE_DIR')
    if folder:
        path = Path(folder)
        path.mkdir(parents=True, exist_ok=True)
        (path/(label+'.png')).write_bytes(base64.b64decode(client.call('Page.captureScreenshot', format='png')['data']))


@pytest.mark.parametrize('mode', ['all', 'focus'])
@pytest.mark.parametrize('action', ['close', 'back'])
def test_node_reader_close_focuses_last_agent(phone, mode, action):
    client, js, wait, state = phone
    setup_graph(js, wait, state, mode)
    # Simulate a previously keyboard-focused node: modal restoration must not
    # leave a blue SVG focus outline when returning to the rebuilt graph.
    if mode == 'all' and action == 'close':
        capture(client, '01-all')
    js("testNetwork.elements.node.get('SampleAgent').grp.focus()")
    touch_node(client, js, wait)
    if mode == 'all' and action == 'close':
        capture(client, '02-reader')
    finish(js, wait, action)
    assert_focus(js, wait, state)
    if mode == 'all' and action == 'close':
        capture(client, '03-focus')


@pytest.mark.parametrize('action', ['close', 'back'])
def test_search_name_reader_returns_to_focus_and_clears_search(phone, action):
    _, js, wait, state = phone
    setup_graph(js, wait, state)
    js("document.querySelector('#agent-search').value='Sample';document.querySelector('#agent-search').dispatchEvent(new Event('input'));document.querySelector('.agent-result-open').click()")
    wait("document.querySelector('[data-id=result]')!==null")
    finish(js, wait, action)
    assert_focus(js, wait, state)


@pytest.mark.parametrize('action', ['close', 'back'])
def test_reader_mail_navigation_keeps_graph_entry_and_uses_last_agent(phone, action):
    client, js, wait, state = phone
    setup_graph(js, wait, state)
    state['events'].append(dict(id='peer-mail', kind='mail', role='assistant', text='', ts='',
                                mail=[dict(direction='received', sender='Agent7', recipients=['SampleAgent'],
                                           importance='normal', subject='Review result', body='Ready.',
                                           coverage='request', truncated=False)]))
    touch_node(client, js, wait)
    wait("document.querySelector('[data-id=peer-mail] .conversation-mail-sender')!==null")
    select_target(state, 'Agent7')
    js("document.querySelector('[data-id=peer-mail] .conversation-mail-sender').click()")
    wait("document.querySelector('#terminal-title').textContent==='Agent7'")
    finish(js, wait, action)
    assert_focus(js, wait, state, 'Agent7')


@pytest.mark.parametrize('action', ['close', 'back'])
def test_activity_reader_leaves_previous_view_selection_and_zoom(phone, action):
    _, js, wait, state = phone
    setup_graph(js, wait, state)
    state['snapshot']['timeline']['states'] = [dict(name='SampleAgent', to='wait', observed_at=time.time())]
    js("document.dispatchEvent(new Event('visibilitychange'))")
    wait("document.querySelector('#events .terminal-entry')!==null")
    js("document.querySelector('[data-view=activity]').click()")
    # Let the existing resize observer finish hiding Graph before taking the
    # baseline: switching views itself changes its hidden SVG dimensions.
    js('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
    js("testNetwork.view={vx:11,vy:22,k:1.3};testNetwork.userView=true;testNetwork.applyView();window.beforeBox=document.querySelector('#gsvg').getAttribute('viewBox');document.querySelector('#events .terminal-entry').click()")
    wait("document.querySelector('[data-id=result]')!==null")
    finish(js, wait, action)
    assert js("!document.querySelector('#activity').hidden&&document.querySelector('#graph-panel').hidden")
    assert js("[testNetwork.mode,testNetwork.selected,testNetwork.userView,document.querySelector('#gsvg').getAttribute('viewBox')===beforeBox]") == ['all', 'Agent0', True, True]


def test_close_route_does_not_leak_into_a_later_non_graph_reader(phone):
    client, js, wait, state = phone
    setup_graph(js, wait, state)
    touch_node(client, js, wait)
    finish(js, wait, 'close')
    assert_focus(js, wait, state)
    js("document.querySelector('[data-mode=all]').click()")
    wait('testNetwork.raf===0')
    select_target(state, 'Agent7')
    js("OrreryMobileTerminal.open('Agent7')")
    wait("document.querySelector('[data-id=result]')!==null")
    finish(js, wait, 'close')
    assert js("testNetwork.mode==='all'&&testNetwork.selected==='SampleAgent'")


def test_disappeared_agent_does_not_focus_a_different_agent(phone):
    client, js, wait, state = phone
    setup_graph(js, wait, state)
    touch_node(client, js, wait)
    state['snapshot']['agents'][:] = [a for a in state['snapshot']['agents'] if a['name'] != 'SampleAgent']
    state['snapshot']['graph']['nodes'] = state['snapshot']['agents']
    state['snapshot']['graph']['edges'][:] = [e for e in state['snapshot']['graph']['edges'] if 'SampleAgent' not in (e['source'], e['target'])]
    js('testNetwork.update('+json.dumps(state['snapshot'])+')')
    wait('testNetwork.raf===0')
    finish(js, wait, 'close')
    assert js("testNetwork.mode==='all'&&testNetwork.selected==='Agent0'")


def test_back_peels_file_preview_before_closing_graph_reader(phone):
    client, js, wait, state = phone
    setup_graph(js, wait, state)
    state['events'].append(dict(id='file', kind='tool', role='assistant', text='Read',
                                files=[dict(ref='a'*32, kind='file', label='notes.md')], ts=''))
    touch_node(client, js, wait)
    wait("document.querySelector('.previewable')!==null")
    js("document.querySelector('.previewable').click()")
    wait("document.querySelector('.conversation-preview-text')!==null")
    js('history.back()')
    wait("document.querySelector('.conversation-preview')===null")
    assert js("document.querySelector('#terminal-sheet').open&&testNetwork.mode==='all'")
    finish(js, wait, 'back')
    assert_focus(js, wait, state)
