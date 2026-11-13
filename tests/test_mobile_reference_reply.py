"""Selected references use the existing one-line prepare/confirm transport."""
import base64
import json
import os
from pathlib import Path

import pytest

from dashboard.mobile_history import project_page
from mobile_reader_browser import phone

PASSAGE="Show each agent's role beneath its name."
MESSAGE='Keep search and Focus above the graph. '+PASSAGE
STAMP='2026-11-13T04:10:00Z'


def setup(phone,text=MESSAGE):
    client,js,wait,state=phone
    state['events']=[dict(id='reference',kind='text',role='assistant',text=text,ts=STAMP),
                     dict(id='after',kind='text',role='assistant',text='Keep the existing logo and fonts.',ts=STAMP)]
    js("OrreryMobileTerminal.close();OrreryMobileTerminal.open('SampleAgent')")
    wait("document.querySelector('[data-id=reference]')!==null&&OrreryMobileTerminal.canReference()")
    js("document.querySelector('.conversation-list').scrollTop=0")
    return client,js,wait,state


def select(js,wait,all_text=False):
    if all_text:
        js("(()=>{const box=document.querySelector('[data-id=reference] .conversation-text');box.scrollIntoView({block:'end'});const range=document.createRange();range.selectNodeContents(box);getSelection().removeAllRanges();getSelection().addRange(range);window.selectedAnchor=getSelection().anchorNode;window.selectedText=getSelection().toString();})()")
    else:
        js("(()=>{const box=document.querySelector('[data-id=reference] .conversation-text'),walker=document.createTreeWalker(box,NodeFilter.SHOW_TEXT);let node;while(node=walker.nextNode()){const at=node.textContent.indexOf("+json.dumps(PASSAGE)+");if(at<0)continue;node.parentElement.scrollIntoView({block:'center'});const range=document.createRange();range.setStart(node,at);range.setEnd(node,at+"+str(len(PASSAGE))+");getSelection().removeAllRanges();getSelection().addRange(range);window.selectedAnchor=node;window.selectedText=getSelection().toString();break;}})()")
    wait("document.querySelector('.reader-selection-reference')!==null")


def stage(client,js,wait,all_text=False):
    select(js,wait,all_text)
    point=js("(()=>{const r=document.querySelector('.reader-selection-reference').getBoundingClientRect();return{x:r.x+r.width/2,y:r.y+r.height/2}})()")
    client.call('Input.dispatchTouchEvent',type='touchStart',touchPoints=[dict(point,id=1)])
    client.call('Input.dispatchTouchEvent',type='touchEnd',touchPoints=[])
    wait("document.querySelector('.reader-reference-card')!==null")


def draft(js,text):
    js("(()=>{const field=document.querySelector('#terminal-text');field.value="+json.dumps(text)+";field.dispatchEvent(new Event('input',{bubbles:true}));})()")


def capture(client,name):
    directory=os.environ.get('MOBILE_REFERENCE_CAPTURE_DIR')
    if directory:
        path=Path(directory);path.mkdir(parents=True,exist_ok=True)
        (path/(name+'.png')).write_bytes(base64.b64decode(client.call('Page.captureScreenshot',format='png')['data']))


@pytest.mark.parametrize('theme',['dark','light'])
def test_selected_reference_review_and_once_send(phone,theme):
    client,js,wait,state=setup(phone)
    if theme=='light':
        js((Path(__file__).resolve().parents[1]/'dashboard/theme_core.js').read_text())
        js("document.documentElement.dataset.colorTheme='light';Object.entries(AgentStackColorThemeCore.deriveWarmPaperLightTheme().cssVariables).forEach(([k,v])=>document.documentElement.style.setProperty(k,v))")
    select(js,wait)
    assert js("document.querySelector('.reader-selection-reference').getBoundingClientRect().top>=getSelection().getRangeAt(0).getBoundingClientRect().bottom")
    capture(client,theme+'-selection')
    js("document.querySelector('.reader-selection-reference').click()")
    wait("document.querySelector('.reader-reference-card')!==null")
    assert js("document.querySelector('.reader-reference-quote').textContent")==PASSAGE
    draft(js,'Keep the role visible in Focus.')
    capture(client,theme+'-reference')
    js("document.querySelector('#terminal-review').click()")
    wait("document.querySelector('.reader-review .terminal-exact')!==null")
    expected='Reference: SampleAgent · assistant · '+STAMP+' | Recorded message: reference | Quote: '+json.dumps(PASSAGE)+' | Reply: Keep the role visible in Focus.'
    assert js("document.querySelector('.terminal-exact').textContent")==expected
    assert not any(c in expected for c in '\r\n')
    assert [body['text'] for op,body in state['calls'] if op=='prepare']==[expected]
    capture(client,theme+'-review')
    js("const send=[...document.querySelectorAll('button')].find(b=>b.textContent==='Send once');send.click();send.click()")
    wait("document.querySelector('.reader-receipt.accepted')!==null")
    assert sum(op=='commit' for op,_ in state['calls'])==1
    assert js("document.querySelector('.reader-reference-card')===null")
    state['events'].append(dict(id='recorded',kind='text',role='user',text=expected,ts=STAMP))
    js("document.dispatchEvent(new Event('visibilitychange'))")
    wait("document.querySelector('.conversation-pending')===null")


def test_remove_reference_keeps_the_reply(phone):
    client,js,wait,_=setup(phone)
    stage(client,js,wait)
    draft(js,'Keep this reply unchanged.')
    js("document.querySelector('.reader-reference-remove').click()")
    assert js("document.querySelector('.reader-reference-card')===null")
    assert js("document.querySelector('#terminal-text').value")=='Keep this reply unchanged.'


def test_reference_and_reply_share_the_byte_limit(phone):
    client,js,wait,state=setup(phone)
    stage(client,js,wait)
    draft(js,'x')
    overhead=int(js("document.querySelector('.reader-input-count').textContent.split(' / ')[0]"))-1
    draft(js,'x'*(4096-overhead))
    assert js("!document.querySelector('#terminal-review').disabled")
    draft(js,'x'*(4097-overhead))
    assert js("document.querySelector('#terminal-review').disabled")
    assert '4096 bytes' in js("document.querySelector('.reader-reference-error').textContent")
    js("document.querySelector('#terminal-review').click()")
    assert not any(op=='prepare' for op,_ in state['calls'])


def test_multiline_selection_becomes_a_single_line_quote(phone):
    client,js,wait,state=setup(phone,'First quoted line.\n\nSecond quoted line.')
    stage(client,js,wait,True)
    assert js("document.querySelector('.reader-reference-quote').textContent")=='First quoted line. Second quoted line.'
    draft(js,'Check both lines.')
    js("document.querySelector('#terminal-review').click()")
    wait("document.querySelector('.terminal-exact')!==null")
    sent=[body['text'] for op,body in state['calls'] if op=='prepare'][-1]
    assert 'Quote: "First quoted line. Second quoted line."' in sent
    assert '\n' not in sent and '\r' not in sent


def test_long_quote_is_retained_and_blocked_without_truncation(phone):
    text='long quote '*600
    client,js,wait,state=setup(phone,text)
    stage(client,js,wait,True)
    draft(js,'Check this quote.')
    assert js("document.querySelector('.reader-reference-quote').textContent")==text.strip()
    assert js("document.querySelector('#terminal-review').disabled")
    assert '4096 bytes' in js("document.querySelector('.reader-reference-error').textContent")
    capture(client,'long-quote-limit')
    assert not any(op=='prepare' for op,_ in state['calls'])


@pytest.mark.parametrize('when',['editing','review'])
def test_history_reset_blocks_reference_and_keeps_draft(phone,when):
    client,js,wait,state=setup(phone)
    stage(client,js,wait);draft(js,'Keep this draft.')
    if when=='review':
        js("document.querySelector('#terminal-review').click()")
        wait("document.querySelector('.reader-review')!==null")
    state['error']='history_changed'
    js("document.dispatchEvent(new Event('visibilitychange'))")
    wait("document.querySelector('.reader-reference-error')?.textContent.includes('changed')")
    assert js("document.querySelector('#terminal-review').disabled")
    assert not any(op=='commit' for op,_ in state['calls'])
    js("document.querySelector('.reader-reference-remove').click()")
    assert js("document.querySelector('#terminal-text').value")=='Keep this draft.'


def test_pane_revision_change_blocks_reference(phone):
    client,js,wait,state=setup(phone)
    stage(client,js,wait);draft(js,'Check this part.')
    state['targets']=[dict(choice='fixture-choice',name='SampleAgent',pane_label='Window 0 · pane 0',revision='new-revision')]
    js("document.dispatchEvent(new Event('visibilitychange'))")
    wait("document.querySelector('.reader-reference-error')?.textContent.includes('changed')")
    assert js("document.querySelector('#terminal-review').disabled")
    assert not any(op in {'prepare','commit'} for op,_ in state['calls'])


def test_agent_change_clears_the_previous_reference(phone):
    client,js,wait,state=setup(phone)
    stage(client,js,wait);draft(js,'Stay with the original agent.')
    js("OrreryMobileTerminal.open('OtherAgent')")
    wait("document.querySelector('.reader-name')?.textContent==='OtherAgent'||document.querySelector('.reader-bar')?.textContent.includes('OtherAgent')")
    assert js("document.querySelector('.reader-reference-card')===null")
    assert not any(op=='commit' for op,_ in state['calls'])


def test_selection_survives_poll_and_prevents_tab_swipe(phone):
    client,js,wait,state=setup(phone)
    select(js,wait)
    js("window.selectedButton=document.querySelector('.reader-selection-reference')")
    state['events'].append(dict(id='new',kind='text',role='assistant',text='A new update.',ts=STAMP))
    js("document.dispatchEvent(new Event('visibilitychange'))")
    wait("document.querySelector('[data-id=new]')!==null")
    assert js("getSelection().anchorNode===selectedAnchor&&getSelection().toString()===selectedText")
    assert js("document.querySelector('.reader-selection-reference')===selectedButton")
    client.call('Input.dispatchTouchEvent',type='touchStart',touchPoints=[dict(x=300,y=450,id=1)])
    client.call('Input.dispatchTouchEvent',type='touchMove',touchPoints=[dict(x=90,y=450,id=1)])
    client.call('Input.dispatchTouchEvent',type='touchEnd',touchPoints=[])
    assert js("document.querySelector('.reader-bar [aria-label=Conversation]').getAttribute('aria-pressed')")=='true'


def test_scrolled_selection_stays_with_its_message(phone):
    text='\n\n'.join(['Earlier paragraph '+str(i)+'. Read the saved layout.' for i in range(15)]+[PASSAGE]+['Later paragraph '+str(i)+'. More discussion follows.' for i in range(15)])
    client,js,wait,_=setup(phone,text)
    select(js,wait)
    assert js("document.querySelector('.conversation-list').scrollTop>100")
    capture(client,'scrolled-selection')
    js("document.querySelector('.reader-selection-reference').click()")
    assert js("document.querySelector('.reader-reference-quote').textContent")==PASSAGE


def test_selection_across_messages_is_not_a_reference(phone):
    _,js,_,_=setup(phone)
    js("(()=>{const first=document.querySelector('[data-id=reference] .conversation-text'),last=document.querySelector('[data-id=after] .conversation-text'),range=document.createRange();range.setStart(first,0);range.setEnd(last,last.childNodes.length);getSelection().removeAllRanges();getSelection().addRange(range);document.dispatchEvent(new Event('selectionchange'));})()")
    assert js("document.querySelector('.reader-selection-reference')===null")


@pytest.mark.parametrize('provider',['claude','codex','hermes'])
@pytest.mark.parametrize('outside',['','\n\n'])
def test_native_wrapper_projection_only_changes_claude_user_text(provider,outside):
    text='Reference: agent\nRecorded message: quote\n> Keep it\n\nCheck again.'
    wrapped=outside+'<pasted_content id="0c0d">\n'+text+'\n</pasted_content id="0c0d">'
    events,_,_,_=project_page(dict(ok=True,provider=provider,events=[dict(id='1',kind='text',role='user',text=wrapped)]))
    assert events[0]['text']==(text if provider=='claude' else wrapped)


@pytest.mark.parametrize('text',[
    'plain\n\ntext',
    '<pasted_content id="x">\nbody\n</pasted_content id="y">',
    'prefix <pasted_content id="x">\nbody\n</pasted_content id="x">',
    '<pasted_content id="x">\nbody\n</pasted_content id="x"> suffix',
    '<pasted_content id="x">a</pasted_content id="x"><pasted_content id="x">b</pasted_content id="x">',
    '<pasted_content id="x"><pasted_content id="y">body</pasted_content id="y"></pasted_content id="x">',
])
def test_mixed_or_inconsistent_paste_wrappers_stay_literal(text):
    events,_,_,_=project_page(dict(ok=True,provider='claude',events=[dict(id='1',kind='text',role='user',text=text)]))
    assert events[0]['text']==text


def test_unwrapped_mobile_record_settles_local_receipt(phone):
    _,js,wait,state=setup(phone)
    text='Reference: sample\nRecorded message: quote\n> Keep this\n\nCheck again.'
    js('OrreryMobileConversation.addPending('+json.dumps(text)+')')
    wrapped='\n\n<pasted_content id="0c0d">\n'+text+'\n</pasted_content id="0c0d">'
    state['events']=project_page(dict(ok=True,provider='claude',events=[dict(id='wrapped',kind='text',role='user',text=wrapped,ts=STAMP)]))[0]
    js("document.dispatchEvent(new Event('visibilitychange'))")
    wait("document.querySelector('[data-id=wrapped]')!==null&&document.querySelector('.conversation-pending')===null")
    assert '<pasted_content' not in js("document.querySelector('.conversation-list').textContent")
