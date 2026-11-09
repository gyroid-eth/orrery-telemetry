"""Selected full-screen mobile Conversation, using synthetic local APIs."""
import time
import json

import pytest

from mobile_reader_browser import AREA, phone  # noqa: F401


@pytest.mark.parametrize('phone', [(390, 844), (360, 800)], indirect=True)
def test_body_uses_at_least_eighty_percent(phone):
    _, js, _, _ = phone
    assert js(AREA) >= 80
    assert js("document.querySelector('#terminal-text')===null")


def test_bar_composer_and_guarded_send(phone):
    _, js, wait, state = phone
    js("document.querySelector('.reader-info-toggle').click()")
    assert js("document.querySelector('.reader-info').innerText.includes('One line + Enter')")
    js("document.querySelector('.reader-info-toggle').click();document.querySelector('.reader-compose-toggle').click()")
    wait("document.activeElement.id==='terminal-text'")
    js("document.querySelector('.reader-compose-close').click()")
    assert js("document.activeElement.classList.contains('reader-compose-toggle')")
    js("document.querySelector('.reader-compose-toggle').click();const f=document.querySelector('#terminal-text');f.value='Please continue';f.dispatchEvent(new Event('input',{bubbles:true}))")
    wait("document.querySelector('#terminal-review').disabled===false")
    js("document.querySelector('#terminal-review').click()")
    wait("[...document.querySelectorAll('button')].some(b=>b.textContent==='Send once')")
    assert js("document.querySelector('#terminal-output').textContent.includes('Generated terminal tail')")
    assert not any(op == 'commit' for op, _ in state['calls'])
    js("[...document.querySelectorAll('button')].find(b=>b.textContent==='Send once').click()")
    wait("document.querySelector('#terminal-sheet').innerText.includes('Received by terminal')")
    assert sum(op == 'commit' for op, _ in state['calls']) == 1


def test_markdown_copy_safe_links_and_shell(phone):
    _, js, wait, state = phone
    assert js("document.querySelector('[data-id=result] strong').textContent==='Benchmark result'")
    assert js("document.querySelectorAll('[data-id=result] table tr').length===4")
    js("window.__copied='';Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async t=>{window.__copied=t}}});document.querySelector('.conversation-code-copy').click()")
    wait("window.__copied.includes('python bench.py')")
    state['events'].extend([
        dict(id='markup', kind='text', role='assistant', text='# Heading\n\n- **one**\n- two\n\n[bad](javascript:alert(1)) <img src=x onerror=alert(1)> [safe](https://example.test/path)', ts=''),
        dict(id='shell-input', kind='text', role='user', text='<bash-input>printf &gt; file</bash-input>', ts=''),
        dict(id='shell-out', kind='text', role='user', text='<bash-stdout>done</bash-stdout><bash-stderr></bash-stderr>', ts='')])
    wait("document.querySelector('[data-id=markup]')!==null", 8)
    assert js("document.querySelector('[data-id=markup] h1').textContent==='Heading'")
    assert js("document.querySelectorAll('[data-id=markup] li').length===2")
    assert js("document.querySelector('[data-id=markup] img')===null&&document.querySelector('[data-id=markup] a').protocol==='https:'")
    assert js("document.querySelectorAll('.conversation-shell').length===1&&document.querySelector('.conversation-shell').innerText.includes('SHELL')&&document.querySelector('.conversation-shell').innerText.includes('done')")


def test_errors_remain_visible_without_opening_info_or_composer(phone):
    _, js, wait, state = phone
    state['error'] = 'history_unconfirmed'
    wait("document.querySelector('.conversation-status').innerText.includes('not verified')", 8)
    assert js("document.querySelector('.conversation-status').getBoundingClientRect().height>0")
    state['preview_error'] = True
    wait("document.querySelector('.terminal-message').innerText.includes('Preview unavailable')", 8)
    assert js("document.querySelector('.terminal-message').getBoundingClientRect().height>0")
    assert js("document.querySelector('.terminal-message').getBoundingClientRect().bottom<=document.querySelector('.conversation-status').getBoundingClientRect().top+1")


def test_close_and_back(phone):
    _, js, wait, _ = phone
    js("document.querySelector('.terminal-close').click()")
    wait("!document.querySelector('#terminal-sheet').open")
    wait("history.state?.orreryConversation!==true")
    js("OrreryMobileTerminal.open('SampleAgent')")
    wait("document.querySelector('#terminal-sheet').open")
    js('history.back()')
    wait("!document.querySelector('#terminal-sheet').open")


def test_visual_viewport_keeps_the_composer_above_the_keyboard(phone):
    _, js, wait, _ = phone
    js("document.querySelector('.reader-compose-toggle').click();Object.defineProperty(visualViewport,'height',{configurable:true,value:460});Object.defineProperty(visualViewport,'offsetTop',{configurable:true,value:24});visualViewport.dispatchEvent(new Event('resize'))")
    assert js("(()=>{const s=document.querySelector('#terminal-sheet').getBoundingClientRect(),f=document.querySelector('#terminal-text').getBoundingClientRect();return Math.abs(s.top-24)<1&&Math.abs(s.bottom-484)<1&&f.top>=24&&f.bottom<=484;})()")
    js("document.querySelector('.reader-compose-close').click()")
    wait("document.activeElement.classList.contains('reader-compose-toggle')")


def test_preview_back_and_escape_peel_the_front_layer(phone):
    _, js, wait, state = phone
    state['events'].append(dict(id='file', kind='tool', role='assistant', text='Read', files=[dict(ref='a'*32, kind='file', label='notes.md')], ts=''))
    wait("document.querySelector('.previewable')!==null", 8)
    js("document.querySelector('.previewable').click()")
    wait("document.querySelector('.conversation-preview-text')!==null")
    js('history.back()')
    wait("document.querySelector('.conversation-preview')===null")
    assert js("document.querySelector('#terminal-sheet').open")
    js("document.querySelector('.reader-compose-toggle').click()")
    js("document.querySelector('#terminal-sheet').dispatchEvent(new Event('cancel',{cancelable:true}))")
    assert js("document.querySelector('#terminal-sheet').open&&document.querySelector('#terminal-text')===null")


def test_pending_shell_matching_and_dismiss_keep_focus(phone):
    _, js, wait, state = phone
    state['events'].append(dict(id='old-shell', kind='text', role='user', text='<bash-input>echo ok</bash-input>', ts=''))
    wait("document.querySelector('[data-id=old-shell]')!==null", 8)
    js("OrreryMobileConversation.addPending('! echo ok')")
    wait("document.querySelector('.conversation-pending')!==null")
    state['events'].append(dict(id='new-shell', kind='text', role='user', text='<bash-input>echo ok</bash-input>', ts=''))
    wait("document.querySelector('.conversation-pending')===null", 8)
    js("OrreryMobileConversation.addPending('different text');document.querySelector('.conversation-dismiss').focus();document.querySelector('.conversation-dismiss').click()")
    assert js("document.activeElement.classList.contains('conversation-list')")


def test_reply_needed_is_a_small_visible_red_row(phone):
    _, js, wait, state = phone
    state['snapshot']['agents'][0]['act_state'] = 'question'
    wait("document.querySelector('.reply-needed')!==null", 12)
    assert js("document.querySelector('.reply-needed').textContent==='Reply needed · question'")
    assert js("getComputedStyle(document.querySelector('.reply-needed')).color===getComputedStyle(document.querySelector('.reply-needed'),'::before').color")


def test_working_shows_only_an_upstream_duration(phone):
    _, js, wait, state = phone
    assert js("document.querySelector('.conversation-activity').textContent==='Working'")
    state['snapshot']['agents'][0]['work_disp'] = '2m05s'
    wait("document.querySelector('.conversation-activity').textContent==='Working · 2m05s'", 12)


@pytest.mark.parametrize('view', ['activity', 'graph'])
def test_mail_detail_fetches_and_displays_body_from_both_views(phone, view):
    _, js, wait, state = phone
    js("OrreryMobileTerminal.close()")
    wait("!document.querySelector('#terminal-sheet').open&&history.state?.orreryConversation!==true")
    peer = dict(state['snapshot']['agents'][0], name='OtherAgent')
    state['snapshot']['agents'].append(peer)
    state['snapshot']['graph']['nodes'].append(peer)
    event = dict(key='a'*32, source='SampleAgent', target='OtherAgent', kind='to', event_at=time.time())
    state['mail'] = event
    state['snapshot']['timeline']['mail'] = [event]
    state['snapshot']['graph']['edges'] = [dict(source='SampleAgent', target='OtherAgent', kind='to', count=1, last_ts=event['event_at'])]
    js("window.__mailErrors=[];addEventListener('unhandledrejection',e=>{__mailErrors.push(String(e.reason));e.preventDefault()});document.dispatchEvent(new Event('visibilitychange'))")
    wait("document.querySelector('#events .mail-route')!==null")
    if view == 'graph':
        js("document.querySelector('[data-view=graph]').click()")
        wait("document.querySelector('#gsvg .edge-hit[data-kind=comm]')!==null")
        js("document.querySelector('#gsvg .edge-hit[data-kind=comm]').dispatchEvent(new MouseEvent('click',{bubbles:true}))")
        wait("document.querySelector('#pair-sheet').open&&document.querySelector('#pair-events .mail-route')!==null")
        js("document.querySelector('#pair-events .mail-route').click()")
    else:
        js("document.querySelector('[data-view=activity]').click();document.querySelector('#events .mail-route').click()")
    wait("__mailErrors.length>0||document.querySelector('#message-body').textContent!==''")
    assert js('window.__mailErrors') == []
    assert [p for p in state['get_calls'] if p.startswith('/view/message?')] == ['/view/message?window=day&event=' + event['key']]
    assert js("document.querySelector('#message-sheet').open&&document.querySelector('#message-subject').textContent==='Fixture subject'&&document.querySelector('#message-body').textContent==='Fixture message body'")
    assert js("history.state?.orreryConversation!==true")


@pytest.mark.parametrize(('operation', 'outcome'), [('prepare', 'accepted'), ('commit', 'accepted'), ('commit', 'unknown')])
def test_pending_send_cannot_change_pane_or_discard_its_receipt(phone, operation, outcome):
    _, js, wait, state = phone
    wait("document.querySelector('.reader-compose-toggle')!==null")
    js("document.querySelector('.reader-info-toggle').click();window.__oldPaneAction=document.querySelector('.reader-info .terminal-pane-button').onclick;document.querySelector('.reader-info-toggle').click()")
    js("window.__realFetch=fetch;window.__release=null;window.fetch=async(...args)=>{const response=await __realFetch(...args);if(String(args[0]).endsWith('/'+" + json.dumps(operation) + "))return new Promise((resolve,reject)=>{__release=()=>" + ("reject(Error('Fixture response lost'))" if outcome == 'unknown' else "resolve(response)") + "});return response};document.querySelector('.reader-compose-toggle').click();const field=document.querySelector('#terminal-text');field.value='One fixture action';field.dispatchEvent(new Event('input',{bubbles:true}))")
    wait("document.querySelector('#terminal-review').disabled===false")
    js("document.querySelector('#terminal-review').click()")
    if operation == 'commit':
        wait("[...document.querySelectorAll('button')].some(b=>b.textContent==='Send once')")
        js("[...document.querySelectorAll('button')].find(b=>b.textContent==='Send once').click()")
    wait('window.__release!==null')
    before = list(state['calls'])
    js("document.querySelector('.reader-info-toggle').click()")
    assert js("document.querySelector('.reader-info .terminal-pane-button').disabled")
    assert js("[...document.querySelectorAll('.reader-bar [aria-pressed]')].every(b=>b.disabled)")
    if operation == 'prepare':
        assert js("document.querySelector('.reader-composer .terminal-pane-button').disabled")
    js("document.querySelector('.reader-info .terminal-pane-button').click();document.querySelector('.reader-bar [aria-label=Terminal]').click()")
    # A callback retained from before the operation must obey the same guard.
    js("__oldPaneAction();OrreryMobileTerminal.open('OtherAgent')")
    assert state['calls'] == before
    js('window.__release();void 0')
    if operation == 'prepare':
        wait("[...document.querySelectorAll('button')].some(b=>b.textContent==='Send once')")
        assert js("document.querySelector('.terminal-exact').textContent==='One fixture action'")
        assert not any(op == 'commit' for op, _ in state['calls'])
    else:
        wait("document.querySelector('#terminal-sheet').innerText.includes(" + json.dumps('Outcome unknown' if outcome == 'unknown' else 'Received by terminal') + ")")
        assert sum(op == 'commit' for op, _ in state['calls']) == 1
        assert not any(op == 'cancel' for op, _ in state['calls'])
        if outcome == 'unknown':
            js("[...document.querySelectorAll('button')].find(b=>b.textContent==='Check receipt').click()")
            wait("document.querySelector('#terminal-sheet').innerText.includes('Receipt unavailable')")
            assert next(p for op, p in state['calls'] if op == 'status')['intent'] == 'fixture-intent'
            assert sum(op == 'commit' for op, _ in state['calls']) == 1


@pytest.mark.parametrize('initial_tab', ['Conversation', 'Terminal'])
def test_review_stays_over_conversation_and_send_returns_to_its_tail(phone, initial_tab):
    _, js, wait, state = phone
    wait("document.querySelector('.reader-compose-toggle')!==null")
    if initial_tab == 'Terminal':
        js("document.querySelector('.reader-bar [aria-label=Terminal]').click()")
    js("document.querySelector('.reader-compose-toggle').click();const field=document.querySelector('#terminal-text');field.value='A fixture reply';field.dispatchEvent(new Event('input',{bubbles:true}))")
    wait("document.querySelector('#terminal-review').disabled===false")
    js("document.querySelector('#terminal-review').click()")
    wait("[...document.querySelectorAll('button')].some(b=>b.textContent==='Send once')")
    assert js("document.querySelector('.conversation-list')?.isConnected===true")
    assert js("document.querySelector('.reader-composer #terminal-output')?.textContent.includes('Generated terminal tail')===true")
    assert js("document.querySelector('.reader-bar [aria-label=Conversation]').getAttribute('aria-pressed')==='true'")
    assert js("document.querySelector('.reader-composer').innerText.includes('Window 0 · pane 0')")
    assert not any(op == 'commit' for op, _ in state['calls'])
    js("[...document.querySelectorAll('button')].find(b=>b.textContent==='Send once').click()")
    wait("document.querySelector('#terminal-sheet').innerText.includes('Received by terminal')")
    assert js("document.querySelector('.reader-bar [aria-label=Conversation]').getAttribute('aria-pressed')==='true'")
    assert js("document.querySelector('.conversation-list')?.isConnected===true&&document.querySelector('#terminal-output')===null&&document.querySelector('.reader-composer')===null")
    assert js("document.querySelector('.conversation-pending').innerText.includes('A fixture reply')")
    assert js("document.querySelector('.reader-compose-toggle')!==null")
    assert sum(op == 'commit' for op, _ in state['calls']) == 1
    # A deliberate switch after receipt keeps the new Terminal reading layout.
    js("document.querySelector('.reader-bar [aria-label=Terminal]').click()")
    assert js("document.querySelector('.reader-receipt')!==null&&document.querySelector('#terminal-sheet>.terminal-actions')===null")


@pytest.mark.parametrize('phone', [(390, 844), (360, 800)], indirect=True)
def test_bar_has_large_touch_targets_and_info_opens_by_touch(phone):
    client, js, wait, _ = phone
    client.call('Emulation.setTouchEmulationEnabled', enabled=True, maxTouchPoints=1)
    sizes = js("[...document.querySelectorAll('.reader-bar button')].map(b=>{const r=b.getBoundingClientRect();return {width:r.width,height:r.height}})")
    assert all(r['width'] >= 44 and r['height'] >= 44 for r in sizes), sizes
    for opened in (True, False):
        point = js("(()=>{const r=document.querySelector('.reader-info-toggle').getBoundingClientRect();return {x:r.x+4,y:r.y+4}})()")
        client.call('Input.dispatchTouchEvent', type='touchStart', touchPoints=[dict(**point, radiusX=10, radiusY=10, force=1)])
        client.call('Input.dispatchTouchEvent', type='touchEnd', touchPoints=[])
        wait("(document.querySelector('.reader-info')!==null)===" + str(opened).lower())


@pytest.mark.parametrize('phone', [(390, 844), (360, 800)], indirect=True)
def test_terminal_fills_the_screen_with_tail_at_the_bottom_and_notes_in_info(phone):
    _, js, wait, _ = phone
    wait("document.querySelector('.reader-compose-toggle')!==null")
    js("document.querySelector('.reader-bar [aria-label=Terminal]').click()")
    wait("document.querySelector('#terminal-output')?.textContent.includes('Generated terminal tail')===true")
    assert js("(()=>{const r=document.querySelector('#terminal-output').getBoundingClientRect();return r.height>=innerHeight*.75;})()")
    assert js("getComputedStyle(document.querySelector('.terminal-preview-head')).display==='none'&&getComputedStyle(document.querySelector('.terminal-observed')).display==='none'")
    assert js("(()=>{const pre=document.querySelector('#terminal-output'),range=document.createRange();range.selectNodeContents(pre);const text=range.getBoundingClientRect(),box=pre.getBoundingClientRect();return text.bottom<=box.bottom+1&&box.bottom-text.bottom<12;})()")
    js("document.querySelector('.reader-info-toggle').click()")
    assert js("document.querySelector('.reader-info').innerText.includes('40 lines')&&document.querySelector('.reader-info').innerText.includes('Shown')")


def test_terminal_long_output_pauses_while_reading_and_failures_stay_visible(phone):
    _, js, wait, state = phone
    wait("document.querySelector('.reader-compose-toggle')!==null")
    js("document.querySelector('.reader-bar [aria-label=Terminal]').click()")
    old = 'X'*180 + '\n' + '\n'.join('fixture line '+str(i) for i in range(40))
    new = '\n'.join('fixture line '+str(i) for i in range(100, 140))
    state['preview_text'] = old
    wait("document.querySelector('#terminal-output').textContent.includes('fixture line 39')", 8)
    assert js("document.querySelector('#terminal-output').scrollWidth>document.querySelector('#terminal-output').clientWidth")
    js("{const pre=document.querySelector('#terminal-output');pre.scrollTop=0;pre.dispatchEvent(new Event('scroll'))}")
    state['preview_text'] = new
    wait("document.querySelector('.reader-terminal-state').textContent==='New output · paused'", 8)
    assert js("document.querySelector('#terminal-output').textContent") == old
    js("{const pre=document.querySelector('#terminal-output');pre.scrollTop=pre.scrollHeight;pre.dispatchEvent(new Event('scroll'))}")
    wait("document.querySelector('#terminal-output').textContent.includes('fixture line 139')")
    state['preview_error'] = True
    wait("document.querySelector('.terminal-message').textContent.includes('Preview unavailable')", 8)
    assert js("document.querySelector('.terminal-message').getBoundingClientRect().height>0")
