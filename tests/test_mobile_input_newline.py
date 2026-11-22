"""Clipboard line endings are removed only from the confirmed input payload."""
import base64
import json
import os
from pathlib import Path

import pytest

from mobile_reader_browser import phone
from test_mobile_multiple_images import start, select

COMMAND="printf 'sample-output'"


def view(client,js,wait,mode):
    if mode=='legacy':
        client.call('Page.addScriptToEvaluateOnNewDocument',source="const realFetch=window.fetch.bind(window);window.fetch=(url,...args)=>url==='/view/history'?Promise.resolve(new Response(JSON.stringify({enabled:false}),{status:200,headers:{'Content-Type':'application/json'}})):realFetch(url,...args)")
        client.call('Page.reload',ignoreCache=True)
        wait("typeof OrreryMobileTerminal!=='undefined'&&document.querySelector('body>header>span').textContent==='Terminal enabled'")
        js("OrreryMobileTerminal.open('SampleAgent')")
        wait("document.querySelector('#terminal-text')!==null&&!document.querySelector('#terminal-text').disabled")
    elif mode=='Terminal':
        js("document.querySelector('.reader-bar [aria-label=Terminal]').click()")


def paste(js,text):
    js("(()=>{const field=document.querySelector('#terminal-text');field.value="+json.dumps(text)+";field.dispatchEvent(new Event('input',{bubbles:true}));})()")


def capture(client,name):
    folder=os.environ.get('MOBILE_NEWLINE_CAPTURE_DIR')
    if folder:
        path=Path(folder);path.mkdir(parents=True,exist_ok=True)
        (path/(name+'.png')).write_bytes(base64.b64decode(client.call('Page.captureScreenshot',format='png')['data']))


@pytest.mark.parametrize('mode',['Conversation','Terminal','legacy'])
@pytest.mark.parametrize('ending',['\n','\r\n','\r','\r\n\n\r'])
def test_trailing_line_endings_are_removed_before_review(phone,mode,ending):
    client,js,wait,state=phone
    view(client,js,wait,mode);paste(js,COMMAND+ending)
    wait("!document.querySelector('#terminal-review').disabled")
    assert js("document.querySelector('#terminal-input-error').hidden")
    js("document.querySelector('#terminal-review').click()")
    wait("document.querySelector('.terminal-exact')!==null")
    assert js("document.querySelector('.terminal-exact').textContent")==COMMAND
    assert [body['text'] for op,body in state['calls'] if op=='prepare']==[COMMAND]


@pytest.mark.parametrize('mode',['Conversation','Terminal','legacy'])
@pytest.mark.parametrize('text,reason',[
    ('first command\nsecond command','Line breaks cannot be sent'),
    ('x'*4097,'4096 bytes'),
    ('before\tafter','Unsupported characters'),
])
def test_invalid_input_has_a_visible_reason_and_cannot_prepare(phone,mode,text,reason):
    client,js,wait,state=phone
    view(client,js,wait,mode);paste(js,text)
    assert js("document.querySelector('#terminal-review').disabled")
    assert not js("document.querySelector('#terminal-input-error').hidden")
    assert reason in js("document.querySelector('#terminal-input-error').textContent")
    assert js("document.querySelector('#terminal-text').getAttribute('aria-describedby')")=='terminal-input-error'
    if mode=='Conversation' and '\n' in text:capture(client,'internal-newline')
    js("document.querySelector('#terminal-review').click()")
    assert not any(op in {'prepare','commit'} for op,_ in state['calls'])
    paste(js,COMMAND+'\n')
    wait("!document.querySelector('#terminal-review').disabled")
    assert js("document.querySelector('#terminal-input-error').hidden")


def test_actual_code_copy_paste_review_and_once_send(phone):
    client,js,wait,state=phone
    state['events']=[dict(id='command',kind='text',role='assistant',text='Copy this command:\n\n```sh\n'+COMMAND+'\n```',ts='2026-11-13T05:00:00Z')]
    js("OrreryMobileTerminal.close();OrreryMobileTerminal.open('SampleAgent');Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async value=>{window.copiedCommand=value}}})")
    wait("document.querySelector('.conversation-code-copy')!==null")
    js("document.querySelector('.conversation-code-copy').click()")
    wait("typeof copiedCommand==='string'")
    assert js('copiedCommand')==COMMAND+'\n'
    paste(js,js('copiedCommand'))
    wait("!document.querySelector('#terminal-review').disabled")
    capture(client,'trailing-newline-ready')
    js("document.querySelector('#terminal-review').click()")
    wait("document.querySelector('.terminal-exact')!==null")
    assert js("document.querySelector('.terminal-exact').textContent")==COMMAND
    capture(client,'trailing-newline-review')
    js("const send=[...document.querySelectorAll('button')].find(b=>b.textContent==='Send once');send.click();send.click()")
    wait("document.querySelector('.reader-receipt.accepted')!==null")
    assert sum(op=='commit' for op,_ in state['calls'])==1
    assert state['draft']==COMMAND


def test_image_marker_is_added_after_removing_trailing_line_endings(phone):
    _,js,wait,state=phone
    start(js,wait,state);paste(js,COMMAND+'\r\n')
    select(js,1)
    wait("document.querySelectorAll('.reader-image-thumb').length===1&&!document.querySelector('#terminal-review').disabled")
    js("document.querySelector('#terminal-review').click()")
    wait("document.querySelector('.terminal-exact')!==null")
    expected=COMMAND+" [image: '"+state['image_paths'][0]+"']"
    assert js("document.querySelector('.terminal-exact').textContent")==expected
    assert [body['text'] for op,body in state['calls'] if op=='prepare']==[expected]


@pytest.mark.parametrize('extra',[0,1])
def test_utf8_limit_is_applied_after_removing_trailing_newline(phone,extra):
    _,js,wait,_=phone
    text='あ'*1365+'x'*(1+extra)
    paste(js,text+'\n')
    assert js("document.querySelector('#terminal-review').disabled")==bool(extra)
    assert js("document.querySelector('.reader-input-count').textContent")==str(4096+extra)+' / 4096 bytes'


def test_spaces_before_the_trailing_newline_are_preserved(phone):
    _,js,wait,_=phone
    paste(js,COMMAND+'  \n')
    wait("!document.querySelector('#terminal-review').disabled")
    js("document.querySelector('#terminal-review').click()")
    wait("document.querySelector('.terminal-exact')!==null")
    assert js("document.querySelector('.terminal-exact').textContent")==COMMAND+'  '
