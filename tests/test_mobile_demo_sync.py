"""Real phone display changes preserve the original text, draft and pictures."""
import base64
import os
from pathlib import Path

import pytest

from mobile_reader_browser import phone
from test_mobile_multiple_images import start, select


ON=dict(version=1,supported=True,on=True,words=['Orchid'],identity=dict(users=['mira'],hosts=['mira-node']))
OFF=dict(version=1,supported=True,on=False,words=[],identity=None)


def test_desktop_demo_changes_mask_and_restore_without_losing_the_draft(phone):
    if not os.environ.get('ORRERY_DEMO_BRIDGE_ROOT'):
        pytest.skip('paired desktop scripts required')
    client,js,wait,state=phone
    start(js,wait,state);select(js,1)
    wait("document.querySelectorAll('.reader-image-thumb').length===1")
    raw='Orchid on mira-node /Users/mira/notes'
    js("document.querySelector('#terminal-text').value='Orchid on mira-node /Users/mira/notes';document.querySelector('#terminal-text').dispatchEvent(new Event('input'));const probe=document.createElement('p');probe.id='demo-probe';probe.title='mira-node Orchid';probe.textContent='Orchid on mira-node /Users/mira/notes';document.querySelector('.conversation-list').prepend(probe)")
    original_url=js("document.querySelector('.reader-image-thumb').src")
    state['demo']=ON
    wait("document.documentElement.dataset.demoSync==='ready'&&window.OrreryDemo?.on")
    assert js("document.querySelector('#demo-probe').textContent")=='****** on ********* /Users/****/notes'
    assert js("document.querySelector('#demo-probe').title")=='********* ******'
    assert js("document.querySelector('#terminal-text').value")==raw
    assert js("document.querySelector('.reader-image-thumb').src")==original_url
    js("const range=document.createRange();range.selectNodeContents(document.querySelector('#demo-probe'));getSelection().removeAllRanges();getSelection().addRange(range);window.copied='';const copy=new Event('copy',{bubbles:true,cancelable:true});Object.defineProperty(copy,'clipboardData',{value:{getData:()=>'',setData:(_,text)=>window.copied=text}});document.querySelector('#demo-probe').dispatchEvent(copy);getSelection().removeAllRanges()")
    assert js('copied')==raw
    state['demo']=dict(ON,words=['notes'])
    js('OrreryMobileDemo.refresh()')
    assert js("document.querySelector('#demo-probe').textContent")=='Orchid on ********* /Users/****/*****'
    state['demo']=OFF
    js('OrreryMobileDemo.refresh()')
    assert js("document.querySelector('#demo-probe').textContent")==raw
    assert js("document.querySelector('#demo-probe').title")=='mira-node Orchid'
    assert js("document.querySelector('#terminal-text').value")==raw
    assert js("document.querySelector('.reader-image-thumb').src")==original_url
    assert not any(op in {'prepare','commit'} for op,_ in state['calls'])
    state['demo']=ON
    js('OrreryMobileDemo.refresh()')
    folder=os.environ.get('MOBILE_DEMO_CAPTURE_DIR')
    if folder:
        output=Path(folder);output.mkdir(parents=True,exist_ok=True)
        (output/'mobile-demo.png').write_bytes(base64.b64decode(client.call('Page.captureScreenshot',format='png')['data']))
    state['demo']=dict(OFF,supported=False)
    js('OrreryMobileDemo.refresh()')
    assert js("document.documentElement.dataset.demoSync")=='failed'
    assert js("document.querySelector('#terminal-text').value")==raw


def test_unavailable_demo_hides_then_recovers_without_reloading(phone):
    client,js,wait,state=phone
    js("document.querySelector('#terminal-text').value='Keep this draft';document.querySelector('#terminal-text').dispatchEvent(new Event('input'))")
    state['demo_error']=True
    js('OrreryMobileDemo.refresh()')
    assert js("document.documentElement.dataset.demoSync")=='failed'
    assert js("getComputedStyle(document.body).visibility")=='hidden'
    assert js("getComputedStyle(document.querySelector('#terminal-sheet')).visibility")=='hidden'
    assert js("getComputedStyle(document.querySelector('.mobile-demo-status')).visibility")=='visible'
    assert js("document.querySelector('.mobile-demo-status').matches(':modal')")
    assert js("document.querySelector('.mobile-demo-status').textContent.includes('Retry')")
    folder=os.environ.get('MOBILE_DEMO_CAPTURE_DIR')
    if folder:
        output=Path(folder);output.mkdir(parents=True,exist_ok=True)
        (output/'mobile-demo-error.png').write_bytes(base64.b64decode(client.call('Page.captureScreenshot',format='png')['data']))
    state['demo_error']=False
    js('OrreryMobileDemo.refresh()')
    assert js("document.documentElement.dataset.demoSync")=='ready'
    assert js("document.querySelector('#terminal-text').value")=='Keep this draft'


def test_first_demo_load_is_hidden_until_the_mask_is_ready(phone):
    if not os.environ.get('ORRERY_DEMO_BRIDGE_ROOT'):
        pytest.skip('paired desktop scripts required')
    client,js,wait,state=phone
    state['demo']=ON;state['demo_delay']=.35
    state['events'][0]['text']='Orchid on mira-node /Users/mira/notes'
    client.call('Page.addScriptToEvaluateOnNewDocument',source="window.demoLeaks=[];function probe(){for(const el of document.querySelectorAll('.conversation-list')){if(getComputedStyle(el).visibility==='visible'&&el.textContent.includes('mira-node'))demoLeaks.push(document.documentElement.dataset.demoSync)}requestAnimationFrame(probe)}requestAnimationFrame(probe)")
    client.call('Page.reload')
    wait("document.documentElement.dataset.demoSync==='ready'&&window.OrreryDemo?.known")
    js("OrreryMobileTerminal.open('SampleAgent')")
    wait("document.querySelector('[data-id=result]')!==null")
    js('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
    assert js('demoLeaks')==[]
    assert 'mira-node' not in js("document.querySelector('.conversation-list').textContent")


def test_a_masked_agent_label_still_opens_the_original_agent(phone):
    if not os.environ.get('ORRERY_DEMO_BRIDGE_ROOT'):
        pytest.skip('paired desktop scripts required')
    _,js,wait,state=phone
    state['demo']=dict(ON,words=['SampleAgent'])
    js('OrreryMobileDemo.refresh();OrreryMobileTerminal.close()')
    wait("document.querySelector('#gsvg .node text:not([class])').textContent==='***********'")
    js("document.querySelector('#gsvg .node').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}))")
    wait("document.querySelector('#terminal-sheet').open")
    assert any('/view/conversation?name=SampleAgent' in path for path in state['get_calls'])
    assert not any('name=%2A' in path for path in state['get_calls'])
