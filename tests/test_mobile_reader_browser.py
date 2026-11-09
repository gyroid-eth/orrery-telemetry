"""Selected full-screen mobile Conversation, using synthetic local APIs."""
import time
import json

import pytest

from mobile_reader_browser import AREA, phone  # noqa: F401


def test_tool_group_keyboard_keeps_position_open_state_and_nodes_on_poll(phone):
    client, js, wait, state = phone
    state['events'].extend(dict(id='tool-'+str(i),kind='tool',role='assistant',text=t,ts='2026-01-01T08:45:00Z')
                           for i,t in enumerate(['Read','Bash','Read','Edit','Read','Bash']))
    wait("document.querySelector('.conversation-tool-group')!==null",8)
    assert js("document.querySelector('.conversation-tool-count').textContent==='6 tool calls'")
    assert js("document.querySelector('.conversation-tool-names').textContent==='· Read ×3, Bash ×2, Edit ×1'")
    assert js("[...document.querySelectorAll('.conversation-tool')].every(e=>e.hidden)")
    js("window.__group=document.querySelector('.conversation-tool-group');window.__tool=document.querySelector('[data-id=tool-0]');__group.focus({preventScroll:true});window.__top=__group.getBoundingClientRect().top")
    client.call('Input.dispatchKeyEvent',type='keyDown',key='Enter',code='Enter',windowsVirtualKeyCode=13)
    client.call('Input.dispatchKeyEvent',type='char',text='\r',unmodifiedText='\r',key='Enter',windowsVirtualKeyCode=13)
    client.call('Input.dispatchKeyEvent',type='keyUp',key='Enter',code='Enter',windowsVirtualKeyCode=13)
    wait("__group.getAttribute('aria-expanded')==='true'")
    assert js("Math.abs(__group.getBoundingClientRect().top-__top)<=1")
    state['events'].append(dict(id='tool-extra',kind='tool',role='assistant',text='Read',ts='2026-01-01T08:46:00Z'))
    wait("document.querySelector('[data-id=tool-extra]')!==null",8)
    assert js("document.querySelector('.conversation-tool-group')===__group&&document.activeElement===__group&&document.querySelector('[data-id=tool-0]')===__tool")
    assert js("__group.getAttribute('aria-expanded')==='true'&&document.querySelector('[data-id=tool-extra]').hidden===false")
    js("window.__top=__group.getBoundingClientRect().top")
    client.call('Input.dispatchKeyEvent',type='keyDown',key=' ',code='Space',windowsVirtualKeyCode=32)
    client.call('Input.dispatchKeyEvent',type='keyUp',key=' ',code='Space',windowsVirtualKeyCode=32)
    wait("__group.getAttribute('aria-expanded')==='false'")
    assert js("Math.abs(__group.getBoundingClientRect().top-__top)<=1")
    assert js("document.querySelector('.conversation-tool-group').scrollWidth<=document.querySelector('.conversation-tool-group').clientWidth+1")


def test_received_mail_without_recipient_uses_owner_portrait_and_sent_is_explicit(phone):
    _,js,wait,state=phone
    for direction in ['received','sent']:
        state['events'].append(dict(id='no-recipient-'+direction,kind='mail',role='assistant',text='',ts='',mail=[dict(
            direction=direction,sender='Lead',recipients=[],importance='normal',subject='Recorded subject',
            body='Recorded body',coverage='complete',truncated=False)]))
    wait("document.querySelector('[data-id=no-recipient-sent]')!==null",8)
    assert js("document.querySelector('[data-id=no-recipient-received] .conversation-mail-recipients .conversation-mail-peer-name').textContent==='SampleAgent'")
    assert js("document.querySelector('[data-id=no-recipient-received] .conversation-mail-recipients .conversation-avatar')!==null")
    assert js("document.querySelector('[data-id=no-recipient-sent] .conversation-mail-recipients .conversation-mail-peer-name').textContent==='recorded recipient unavailable'")


def test_reader_speaker_uses_profile_model_and_missing_portrait_initials(phone):
    _, js, wait, state = phone
    assert js("document.querySelector('[data-id=question] .conversation-speaker-name')?.textContent==='SampleAgent'")
    wait("document.querySelector('[data-id=question] .conversation-avatar .initials')!==null")
    assert js("document.querySelector('[data-id=question] .conversation-avatar .initials').textContent==='S'")
    state['snapshot']['agents'][0].update(model='gpt-6.1-sol')
    js("document.dispatchEvent(new Event('visibilitychange'))")
    wait("document.querySelector('[data-id=question] .conversation-speaker-model')?.textContent==='GPT-6.1-Sol'")
    assert js("document.querySelector('[data-id=question] .conversation-byline time').textContent.length>0")
    assert js("document.querySelector('[data-id=question] .conversation-avatar [data-provider=openai]')!==null")
    state['events'].append(dict(id='recorded-model',kind='text',role='assistant',text='Recorded reply',model='claude-opus-5-5',ts=''))
    wait("document.querySelector('[data-id=recorded-model]')!==null",8)
    assert js("document.querySelector('[data-id=recorded-model] .conversation-speaker-model').textContent==='Opus 5.5'")
    assert js("document.querySelector('[data-id=recorded-model] [data-provider=anthropic]')!==null")


@pytest.mark.parametrize('theme',['dark','light'])
def test_reader_mail_route_keeps_recipient_count_and_unknown_peer_has_no_logo(phone,theme):
    _, js, wait, state = phone
    if theme=='light':
        from pathlib import Path
        js((Path(__file__).resolve().parents[1]/'dashboard/theme_core.js').read_text())
        js("(()=>{document.documentElement.dataset.colorTheme='light';Object.entries(AgentStackColorThemeCore.deriveWarmPaperLightTheme().cssVariables).forEach(([k,v])=>document.documentElement.style.setProperty(k,v))})()")
    recipients=['ExtraordinarilyLongRecipientName'+str(i) for i in range(12)]
    state['events'].append(dict(id='route',kind='mail',role='assistant',text='',ts='2026-01-01T08:45:00Z',mail=[dict(
        direction='sent',sender='SampleAgent',recipients=recipients,importance='high',subject='Long subject '+('確認 '*25),
        body='Recorded body',coverage='request',truncated=False)]))
    wait("document.querySelector('[data-id=route]')!==null",8)
    assert js("document.querySelector('[data-id=route] .conversation-mail-more')?.textContent==='+12'")
    assert js("document.querySelectorAll('[data-id=route] .conversation-mail-peer')[1].querySelector('.conversation-avatar')===null")
    assert js("document.querySelectorAll('[data-id=route] .conversation-mail-peer')[0].querySelector('.conversation-avatar')!==null")
    assert js("document.querySelector('[data-id=route] [data-importance=high]')?.getAttribute('aria-label')==='High importance'")
    assert js("(()=>{const c=document.querySelector('[data-id=route]'),n=c.querySelector('.conversation-mail-more'),r=n.getBoundingClientRect(),p=c.getBoundingClientRect();return n.clientWidth>=n.scrollWidth&&r.left>=p.left&&r.right<=p.right&&c.scrollWidth<=c.clientWidth+1})()")
    js("document.querySelector('[data-id=route] .conversation-mail').click()")
    assert js("document.querySelector('[data-id=route] .conversation-mail').getAttribute('aria-expanded')==='true'&&document.querySelector('[data-id=route] .conversation-mail-body').hidden===false")


def test_selection_across_replies_excludes_decorative_avatar_initials(phone):
    _, js, wait, state = phone
    texts = ['COPY_BEGIN', 'COPY_MIDDLE', 'COPY_END']
    state['events'].extend(dict(id='copy-'+str(i), kind='text', role='assistant', text=text, ts='')
                           for i, text in enumerate(texts))
    wait("document.querySelector('[data-id=copy-2] .conversation-avatar .initials')!==null", 8)
    selected = js("""(()=>{
      const first=document.querySelector('[data-id=copy-0] .conversation-text'),
        last=document.querySelector('[data-id=copy-2] .conversation-text');
      const start=document.createTreeWalker(first,NodeFilter.SHOW_TEXT).nextNode(),
        walker=document.createTreeWalker(last,NodeFilter.SHOW_TEXT);
      let end,node;while(node=walker.nextNode())end=node;
      const range=document.createRange();range.setStart(start,0);range.setEnd(end,end.length);
      const selection=getSelection();selection.removeAllRanges();selection.addRange(range);
      return selection.toString();
    })()""")
    assert all(text in selected for text in texts)
    assert 'SampleAgent' in selected
    assert 'S' not in selected.splitlines(), selected


def test_mail_uses_registered_peer_portraits_without_copying_initials(phone):
    _, js, wait, state = phone
    state['snapshot']['agents'].append(dict(name='Recipient', provider='anthropic', model='claude-opus-5-5'))
    js("document.dispatchEvent(new Event('visibilitychange'))")
    wait("OrreryMobileProfile.forName('Recipient')!==null")
    state['events'].extend([
        dict(id='mail-copy-before',kind='text',role='assistant',text='MAIL_BEGIN',ts=''),
        dict(id='portrait-route',kind='mail',role='assistant',text='',ts='',mail=[dict(
            direction='sent',sender='SampleAgent',recipients=['Recipient','Unknown'],importance='urgent',
            subject='Mail subject',body='Mail body',coverage='request',truncated=False)]),
        dict(id='mail-copy-after',kind='text',role='assistant',text='MAIL_END',ts='')])
    wait("document.querySelector('[data-id=mail-copy-after]')!==null",8)
    wait("document.querySelectorAll('[data-id=portrait-route] .conversation-avatar .initials').length===2")
    assert js("[...document.querySelectorAll('[data-id=portrait-route] .conversation-avatar')].every(e=>{const s=e.getBoundingClientRect();return s.width>=16&&s.width<=18&&s.height===s.width})")
    assert js("document.querySelector('[data-id=portrait-route] [data-provider=openai]')!==null&&document.querySelector('[data-id=portrait-route] [data-provider=anthropic]')!==null")
    selected = js("""(()=>{
      const range=document.createRange();
      range.setStartBefore(document.querySelector('[data-id=mail-copy-before] .conversation-text'));
      range.setEndAfter(document.querySelector('[data-id=mail-copy-after] .conversation-text'));
      const s=getSelection();s.removeAllRanges();s.addRange(range);return s.toString();
    })()""")
    assert all(text in selected for text in ['MAIL_BEGIN','Mail subject','MAIL_END'])
    assert not {'S','R'}.intersection(selected.splitlines()), selected


def test_mail_recipients_adapt_to_width_and_preserve_sender(phone):
    client, js, wait, state = phone
    names = ['RecipientAlpha','RecipientBeta','RecipientGamma','RecipientDelta',
             'RecipientEpsilon','RecipientZeta','RecipientEta','UnregisteredPeer']
    state['snapshot']['agents'].extend(dict(name=n,provider='anthropic',model='claude-opus-5-5') for n in names[:-1])
    js("document.dispatchEvent(new Event('visibilitychange'))")
    wait("OrreryMobileProfile.forName('RecipientEta')!==null")
    for count in [1,3,8]:
        state['events'].append(dict(id='adaptive-'+str(count),kind='mail',role='assistant',text='',ts='2026-01-01T08:45:00Z',mail=[dict(
            direction='sent',sender='SampleAgent',recipients=names[:count],importance='normal',
            subject='Recipients '+str(count),body='Recorded body',coverage='request',truncated=False)]))
    wait("document.querySelector('[data-id=adaptive-8]')!==null",8)
    assert js("document.querySelector('[data-id=adaptive-3] .conversation-mail-recipients')!==null")
    for width in [1200,600,360,1200]:
        client.call('Emulation.setDeviceMetricsOverride',width=width,height=844,deviceScaleFactor=2,mobile=True)
        mode = 'icons' if width==360 else 'names'
        wait("document.querySelector('[data-id=adaptive-3] .conversation-mail-recipients').dataset.mode==='"+mode+"'")
        assert js("document.querySelector('[data-id=adaptive-1] .conversation-mail-recipients').dataset.mode==='single'")
        assert js("""[...document.querySelectorAll('[data-id^=adaptive-] .conversation-mail')].every(card=>{
          const label=card.querySelector('.conversation-mail-sender .conversation-mail-peer-name');
          return card.scrollWidth<=card.clientWidth+1&&label.scrollWidth<=label.clientWidth+1;
        })""")
        assert js("document.querySelector('[data-id=adaptive-8] .conversation-mail-recipients').getAttribute('aria-label').includes('UnregisteredPeer')")
        assert js("document.querySelector('[data-id=adaptive-8] .conversation-mail-recipients').title.includes('RecipientAlpha')")
        if width==360:
            assert js("""(()=>{const r=document.querySelector('[data-id=adaptive-8] .conversation-mail-recipients'),
              peers=[...r.querySelectorAll('.conversation-mail-peer')].filter(p=>!p.hidden),
              extra=Number(r.querySelector('.conversation-mail-more').textContent.slice(1));
              return peers.length>0&&peers.length+extra===8&&peers.every(p=>p.querySelector('.conversation-avatar'))&&
                !peers.some(p=>p.title==='UnregisteredPeer')&&peers.every(p=>getComputedStyle(p.querySelector('.conversation-mail-peer-name')).display==='none');})()""")


def test_compact_record_stays_a_short_activity_line(phone):
    _, js, wait, state=phone
    state['events'].append(dict(id='compact',kind='tool',role='assistant',text='Context compacted',ts=''))
    wait("document.querySelector('[data-id=compact]')!==null",8)
    assert js("document.querySelector('[data-id=compact]').classList.contains('conversation-tool')")
    assert js("document.querySelector('[data-id=compact] .conversation-speaker-name')===null")


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
    assert js("document.querySelector('.reader-info').innerText.includes('120 lines')&&document.querySelector('.reader-info').innerText.includes('Shown')")


def test_terminal_long_output_pauses_while_reading_and_failures_stay_visible(phone):
    _, js, wait, state = phone
    wait("document.querySelector('.reader-compose-toggle')!==null")
    js("document.querySelector('.reader-bar [aria-label=Terminal]').click()")
    old = 'X'*180 + '\n' + '\n'.join('fixture line '+str(i) for i in range(40))
    new = '\n'.join('fixture line '+str(i) for i in range(100, 140))
    state['preview_text'] = old
    wait("document.querySelector('#terminal-output').textContent.includes('fixture line 39')", 8)
    assert js("document.querySelector('#terminal-output').scrollWidth<=document.querySelector('#terminal-output').clientWidth+1")
    js("{const pre=document.querySelector('#terminal-output');pre.scrollTop=0;pre.dispatchEvent(new Event('scroll'))}")
    state['preview_text'] = new
    wait("document.querySelector('.reader-terminal-state').textContent==='New output · paused'", 8)
    assert js("document.querySelector('#terminal-output').textContent") == old
    js("{const pre=document.querySelector('#terminal-output');pre.scrollTop=pre.scrollHeight;pre.dispatchEvent(new Event('scroll'))}")
    wait("document.querySelector('#terminal-output').textContent.includes('fixture line 139')")
    state['preview_error'] = True
    wait("document.querySelector('.terminal-message').textContent.includes('Preview unavailable')", 8)
    assert js("document.querySelector('.terminal-message').getBoundingClientRect().height>0")


@pytest.mark.parametrize('phone', [(390,844), (430,932)], indirect=True)
@pytest.mark.parametrize('provider', ['Claude', 'Codex'])
def test_expanded_terminal_tail_fills_phone_and_retains_all_120_lines(phone, provider):
    client, js, wait, state = phone
    lines = [f'{i:03d} {provider} · 計測結果を確認しています。処理は正常に完了しました。' for i in range(120)]
    text = '\n'.join(lines)
    assert 6144 < len(text.encode()) <= 12288
    state['preview_text'] = text
    js("document.querySelector('.reader-bar [aria-label=Terminal]').click()")
    wait("document.querySelector('#terminal-output')?.textContent.startsWith('000')===true")
    got = js("""(()=>{const p=document.querySelector('#terminal-output'),r=p.getBoundingClientRect(),bar=document.querySelector('.reader-bar').getBoundingClientRect();return {text:p.textContent,top:r.top,bar:bar.bottom,height:r.height,visible:innerHeight,scroll:p.scrollHeight,at:p.scrollTop}})()""")
    assert got['text'] == text
    assert got['top'] <= got['bar'] + 16
    assert got['height'] >= got['visible'] * .8
    assert got['scroll'] > got['height']
    assert abs(got['scroll'] - got['height'] - got['at']) <= 2


def test_older_preview_limits_are_reported_accurately_in_info(phone):
    client, js, wait, state = phone
    state.update(preview_lines=40, preview_bytes=6144, preview_text="Legacy preview fixture")
    js("document.querySelector('.reader-bar [aria-label=Terminal]').click()")
    wait("document.querySelector('#terminal-output')?.textContent==='Legacy preview fixture'")
    js("document.querySelector('.reader-info-toggle').click()")
    assert js("document.querySelector('.reader-info').innerText.includes('40 lines · up to 6 KiB')")


def touch_swipe(client, start, end):
    client.call('Emulation.setTouchEmulationEnabled', enabled=True, maxTouchPoints=1)
    point=lambda x,y:dict(x=x,y=y,radiusX=6,radiusY=6,force=1,id=0)
    client.call('Input.dispatchTouchEvent',type='touchStart',touchPoints=[point(*start)])
    for step in range(1,7):
        time.sleep(.025)
        x=start[0]+(end[0]-start[0])*step/6
        y=start[1]+(end[1]-start[1])*step/6
        client.call('Input.dispatchTouchEvent',type='touchMove',touchPoints=[point(x,y)])
    client.call('Input.dispatchTouchEvent',type='touchEnd',touchPoints=[])


def synthetic_swipe(js, selector, start=(280,400), end=(90,404)):
    return js("""(()=>{const node=document.querySelector(SELECTOR),start=START,end=END,cancelled=[];
      const touch=p=>new Touch({identifier:7,target:node,clientX:p[0],clientY:p[1]});
      for(const [type,p]of [['touchstart',start],['touchmove',end],['touchend',end]]){
       const t=touch(p);cancelled.push(!node.dispatchEvent(new TouchEvent(type,{bubbles:true,cancelable:true,touches:type==='touchend'?[]:[t],changedTouches:[t]}))); 
      }return cancelled;})()""".replace('SELECTOR',json.dumps(selector)).replace('START',json.dumps(start)).replace('END',json.dumps(end)))


def test_terminal_wrap_keeps_indentation_and_clips_only_rules(phone):
    _,js,wait,state=phone
    line='    artifacts/mobile-reader/'+ 'android-preview-results/'*8 + 'report.json'
    content='\n'.join(['Earlier output '+str(i) for i in range(40)]+['日本語の長い説明を最後まで読めるように折り返します。'*8,line,'─'*100,'━'*100,'═'*100,'<b>literal text</b>'])
    state['preview_text']=content
    js("document.querySelector('.reader-bar [aria-label=Terminal]').click()")
    wait('document.querySelector("#terminal-output").textContent==='+json.dumps(content),8)
    got=js("""(()=>{const p=document.querySelector('#terminal-output'),lines=[...p.querySelectorAll('.terminal-output-line')],path=lines.find(n=>n.textContent.startsWith('    artifacts/'));if(!path)return {lines:0};const text=path.firstChild,r=document.createRange();r.setStart(text,4);r.setEnd(text,5);const first=r.getBoundingClientRect();let next=null;for(let i=5;i<text.length;i++){r.setStart(text,i);r.setEnd(text,i+1);const b=r.getBoundingClientRect();if(b.top>first.top+1){next={left:b.left,top:b.top};break;}}return {lines:lines.length,width:p.clientWidth,scroll:p.scrollWidth,first:{left:first.left,top:first.top},next,rules:[...p.querySelectorAll('.terminal-output-rule')].map(n=>({height:n.getBoundingClientRect().height,width:n.clientWidth,scroll:n.scrollWidth})),html:p.querySelector('b')!==null}})()""")
    assert got['lines']==46,got
    assert got['scroll']<=got['width']+1,got
    assert got['next'] and abs(got['next']['left']-got['first']['left'])<=1,got
    assert all(r['height']<21 and r['scroll']>r['width'] for r in got['rules']),got
    assert got['html'] is False
    # A held snapshot keeps its exact text and character geometry after a poll.
    js("(()=>{const p=document.querySelector('#terminal-output');p.scrollTop=40;p.dispatchEvent(new Event('scroll'))})()")
    before=js("document.querySelector('#terminal-output').scrollTop")
    state['preview_text']=content+'\nNew terminal output'
    wait("document.querySelector('.terminal-stream').textContent==='New output · paused'",8)
    assert js("document.querySelector('#terminal-output').textContent")==content
    assert abs(js("document.querySelector('#terminal-output').scrollTop")-before)<=1
    js("(()=>{const p=document.querySelector('#terminal-output');p.scrollTop=p.scrollHeight;p.dispatchEvent(new Event('scroll'))})()")
    wait("document.querySelector('#terminal-output').textContent.endsWith('New terminal output')")
    assert js("(()=>{const p=document.querySelector('#terminal-output');return p.scrollHeight-p.clientHeight-p.scrollTop<=2})()")


def test_real_touch_swipes_switch_between_conversation_and_terminal(phone):
    client,js,wait,_=phone
    y=js("(()=>{const r=document.querySelector('[data-id=question] .conversation-text').getBoundingClientRect();return (r.top+r.bottom)/2})()")
    touch_swipe(client,(285,y),(95,y+4))
    wait("document.querySelector('.reader-bar [aria-label=Terminal]').getAttribute('aria-pressed')==='true'")
    touch_swipe(client,(95,410),(285,414))
    wait("document.querySelector('.reader-bar [aria-label=Conversation]').getAttribute('aria-pressed')==='true'")


@pytest.mark.parametrize('kind',['vertical','short','diagonal','left-edge','right-edge','selection','horizontal-code'])
def test_reader_swipe_ignores_other_gestures_and_scrollable_content(phone,kind):
    _,js,_,_=phone
    selector='.conversation-list';start,end=(280,400),(90,404)
    if kind=='vertical':end=(275,550)
    if kind=='short':end=(250,402)
    if kind=='diagonal':end=(160,480)
    if kind=='left-edge':start,end=(8,400),(200,404)
    if kind=='right-edge':start,end=(382,400),(190,404)
    if kind=='selection':js("getSelection().selectAllChildren(document.querySelector('[data-id=question] .conversation-text'))")
    if kind=='horizontal-code':
        selector='.conversation-code pre'
        js("document.querySelector('.conversation-code pre').textContent='very_long_command_'+ 'x'.repeat(300)")
        assert js("document.querySelector('.conversation-code pre').scrollWidth>document.querySelector('.conversation-code pre').clientWidth")
    assert not any(synthetic_swipe(js,selector,start,end))
    assert js("document.querySelector('.reader-bar [aria-label=Conversation]').getAttribute('aria-pressed')==='true'")


@pytest.mark.parametrize('panel',['composer','review','sending'])
def test_reader_swipe_is_blocked_while_editing_reviewing_or_sending(phone,panel):
    _,js,wait,state=phone
    wait("document.querySelector('.reader-compose-toggle')!==null")
    js("document.querySelector('.reader-compose-toggle').click()")
    if panel!='composer':
        js("window.__realFetch=fetch;window.__release=null;window.fetch=async(...args)=>{const response=await __realFetch(...args);if(String(args[0]).endsWith('/commit'))return new Promise(resolve=>__release=()=>resolve(response));return response};const f=document.querySelector('#terminal-text');f.value='Single fixture input';f.dispatchEvent(new Event('input',{bubbles:true}))")
        wait("document.querySelector('#terminal-review').disabled===false")
        js("document.querySelector('#terminal-review').click()")
        wait("document.querySelector('.reader-review')!==null")
        if panel=='sending':
            js("[...document.querySelectorAll('button')].find(b=>b.textContent==='Send once').click()")
            wait('window.__release!==null')
    calls=list(state['calls'])
    assert not any(synthetic_swipe(js,'.conversation-list'))
    assert js("document.querySelector('.reader-bar [aria-label=Conversation]').getAttribute('aria-pressed')==='true'")
    assert state['calls']==calls
    if panel=='sending':
        js('window.__release();void 0')
        wait("document.querySelector('.reader-receipt.accepted')!==null")
        assert sum(op=='commit' for op,_ in state['calls'])==1


def test_native_vertical_scroll_and_horizontal_code_scroll_keep_their_gestures(phone):
    client,js,wait,_=phone
    js("(()=>{const p=document.querySelector('.conversation-code pre');p.textContent='command_'+ 'x'.repeat(300);p.scrollIntoView({block:'center'})})()")
    y=js("(()=>{const r=document.querySelector('.conversation-code pre').getBoundingClientRect();return (r.top+r.bottom)/2})()")
    touch_swipe(client,(285,y),(95,y+2))
    wait("document.querySelector('.conversation-code pre').scrollLeft>10")
    assert js("document.querySelector('.reader-bar [aria-label=Conversation]').getAttribute('aria-pressed')==='true'")
    js("(()=>{const list=document.querySelector('.conversation-list');for(let i=0;i<60;i++){const p=document.createElement('p');p.textContent='Synthetic scroll fixture '+i;list.append(p)}list.scrollTop=500})()")
    before=js("document.querySelector('.conversation-list').scrollTop")
    touch_swipe(client,(180,350),(183,520))
    wait('document.querySelector(".conversation-list").scrollTop<'+str(before-20))
    assert js("document.querySelector('.reader-bar [aria-label=Conversation]').getAttribute('aria-pressed')==='true'")


def test_open_info_updates_when_the_first_legacy_preview_arrives(phone):
    _,js,wait,state=phone
    js("OrreryMobileTerminal.close()")
    wait("!document.querySelector('#terminal-sheet').open&&history.state?.orreryConversation!==true")
    state.update(preview_text='First legacy preview',preview_lines=40,preview_bytes=6144)
    js("window.__realFetch=fetch;window.__release=null;window.fetch=async(...args)=>{const response=await __realFetch(...args);if(String(args[0]).endsWith('/preview'))return new Promise(resolve=>__release=()=>resolve(response));return response};OrreryMobileTerminal.open('SampleAgent')")
    wait('window.__release!==null')
    js("document.querySelector('.reader-bar [aria-label=Terminal]').click();document.querySelector('.reader-info-toggle').click();window.__infoBefore=document.querySelector('.reader-info')")
    assert js("!document.querySelector('.reader-info').innerText.includes('40 lines')")
    js('window.__release();void 0')
    wait("document.querySelector('#terminal-output').textContent==='First legacy preview'")
    assert js("document.querySelector('.reader-info')===window.__infoBefore")
    assert js("document.querySelector('.reader-info').innerText.includes('40 lines · up to 6 KiB')")


@pytest.mark.parametrize('phone',[(390,844),(390,640)],indirect=True)
def test_terminal_info_keeps_held_snapshot_position_and_pending_update(phone):
    _,js,wait,state=phone
    raw='\n'.join(['    日本語と long/path/to/'+('artifacts/'*10)+str(i) for i in range(35)]+['','\t\ttab indent','─'*120,'<b>literal</b>',''])
    state['preview_text']=raw
    js("document.querySelector('.reader-bar [aria-label=Terminal]').click()")
    wait('document.querySelector("#terminal-output")?.textContent==='+json.dumps(raw),8)
    js("(()=>{const p=document.querySelector('#terminal-output');p.scrollTop=160;p.dispatchEvent(new Event('scroll'));const lines=[...p.querySelectorAll('.terminal-output-line')];window.__heldAnchor=lines.findIndex(n=>n.getBoundingClientRect().top>p.getBoundingClientRect().top+20)})()")
    position="""(()=>{const p=document.querySelector('#terminal-output'),node=p.querySelectorAll('.terminal-output-line')[window.__heldAnchor].firstChild,r=document.createRange();r.setStart(node,20);r.setEnd(node,21);return {text:p.textContent,top:p.scrollTop,character:r.getBoundingClientRect().top,observed:p._preview.observed_at,following:p._scrollView.following,pending:document.querySelector('.reader-terminal-state').textContent}})()"""
    before=js(position)
    assert before['top']==160 and before['following'] is False
    state['preview_text']=raw+'New snapshot text'
    wait("document.querySelector('.reader-terminal-state').textContent==='New output · paused'",8)
    for opened in [True,False,True,False]:
        js("document.querySelector('.reader-info-toggle').click()")
        wait("(document.querySelector('.reader-info')!==null)==="+str(opened).lower())
        after=js(position)
        assert after['text']==raw
        assert abs(after['top']-before['top'])<=1
        assert abs(after['character']-before['character'])<=1
        assert after['observed']==before['observed']
        assert after['following'] is False
        assert after['pending']=='New output · paused'
    # Resuming remains explicit and must display the pending snapshot at its tail.
    js("(()=>{const p=document.querySelector('#terminal-output');p.scrollTop=p.scrollHeight;p.dispatchEvent(new Event('scroll'))})()")
    wait('document.querySelector("#terminal-output").textContent==='+json.dumps(raw+'New snapshot text'))
    assert js("(()=>{const p=document.querySelector('#terminal-output');return p.scrollHeight-p.clientHeight-p.scrollTop<=2&&document.querySelector('.reader-terminal-state').textContent==='';})()")
