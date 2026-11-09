"""Selected full-screen mobile Conversation, using synthetic local APIs."""
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
