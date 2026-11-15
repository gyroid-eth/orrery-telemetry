"""Selected A on the real mobile page, synthetic APIs and shared opt-in CDP."""
import base64
import json
import os
from pathlib import Path

import pytest

from mobile_reader_browser import phone  # noqa: F401
from test_mobile_steps import fixtures, raw
from dashboard.mobile_history import project_page

ROOT = Path(__file__).resolve().parents[1]


def project(provider, events):
    return project_page(dict(ok=True, provider=provider, events=events))[0]


@pytest.mark.parametrize('provider', ['claude', 'codex'])
@pytest.mark.parametrize('theme', ['light', 'dark', 'demo'])
def test_steps_add_finish_toggle_demo_and_keep_original_extras(phone, provider, theme, tmp_path):
    client, js, wait, state = phone
    if theme == 'light':
        js((ROOT/'dashboard/theme_core.js').read_text())
        js("document.documentElement.dataset.colorTheme='light';Object.entries(AgentStackColorThemeCore.deriveWarmPaperLightTheme().cssVariables).forEach(([k,v])=>document.documentElement.style.setProperty(k,v))")
    if theme == 'demo':
        if not os.environ.get('ORRERY_DEMO_BRIDGE_ROOT'):
            pytest.skip('paired Demo scripts required')
        state['demo'] = dict(version=1, supported=True, on=True, words=['report', 'chart'], identity=dict(users=['mira'], hosts=['mira-node']))
        js('OrreryMobileDemo.refresh()')
        wait("document.documentElement.dataset.demoSync==='ready'&&window.OrreryDemo?.on")
    # The normal roster interface is retained; only this fixture supplies its activity.
    js("window.stepsActivity={running:true,stale:false,state:'work',workDisplay:'2s'};window.OrreryMobileAgent={forName:()=>stepsActivity}")
    sequence = fixtures(provider)
    # Codex's explicit closing marker comes only when the work actually finishes.
    ongoing = sequence[:-1] if provider == 'codex' else sequence
    first = ongoing[:3]
    first[1 if provider == 'claude' else 2]['mail'] = [dict(direction='sent',sender='SampleAgent',recipients=['Reviewer'],subject='Check the report',importance='normal',coverage='complete',body='Please inspect the result.')]
    first[1 if provider == 'claude' else 2]['references'] = [dict(id='r'*32, label='report.md', kind='file')]
    state.update(provider=provider, events=project(provider, first))
    js("OrreryMobileTerminal.close();OrreryMobileTerminal.open('SampleAgent')")
    wait("document.querySelectorAll('.conversation-step').length===1")
    wait("document.querySelector('.conversation-steps-heading').textContent==='Working…'")
    assert js("document.querySelector('.conversation-steps-toggle').getAttribute('aria-expanded')") == 'true'
    before = js("document.querySelector('.conversation-steps-time').textContent")
    wait("document.querySelector('.conversation-steps-time').textContent!=="+json.dumps(before), timeout=3)
    folder = Path(os.environ.get('MOBILE_STEPS_CAPTURE_DIR', str(tmp_path)))
    folder.mkdir(parents=True, exist_ok=True)
    def capture(stage):
        js('new Promise(r=>setTimeout(r,300))')
        js("document.querySelector('.conversation-list').scrollTop=0")
        data = client.call('Page.captureScreenshot', format='png', captureBeyondViewport=False)['data']
        (folder/(provider+'-'+theme+'-'+stage+'.png')).write_bytes(base64.b64decode(data))
    capture('one-step')
    # Native pairing/IDs arrive through project_event, not an alternative renderer.
    state['events'] = project(provider, ongoing)
    js("document.dispatchEvent(new Event('visibilitychange'))")
    wait("document.querySelectorAll('.conversation-step').length===2")
    assert js("document.querySelectorAll('.conversation-steps').length") == 1
    assert js("document.querySelectorAll('.conversation-step-record:not([hidden]) .conversation-chip').length") == 1
    assert js("document.querySelectorAll('.conversation-step-record:not([hidden]) .conversation-mail').length") == 1
    assert js("getComputedStyle(document.querySelector('.conversation-step-record'),'::before').display") == 'none'
    assert js("document.querySelectorAll('.conversation-tool-group').length") == 1
    assert js("document.querySelector('[data-id=unknown]').hidden")
    assert js("document.querySelectorAll('.conversation-step-outcome')[1].textContent") == ('Failed' if provider == 'claude' else 'Observed')
    if theme == 'demo':
        wait("!document.querySelector('.conversation-step-target').textContent.includes('report')")
        assert js("document.querySelector('.conversation-step-tool').textContent") == 'Read'
    else:
        assert js("document.querySelector('.conversation-step-target').textContent") == 'report.md'
    assert js("getComputedStyle(document.querySelector('.conversation-step-target')).textOverflow") == 'ellipsis'
    assert js("document.querySelector('.conversation-steps').scrollWidth<=document.querySelector('.conversation-list').clientWidth")
    capture('working')
    if provider == 'codex':
        state['events'] = project(provider, sequence)
    js("stepsActivity.state='wait';OrreryMobileConversation.refreshActivity();document.dispatchEvent(new Event('visibilitychange'))")
    wait("document.querySelector('.conversation-steps-toggle').getAttribute('aria-expanded')==='false'")
    assert js("document.querySelector('.conversation-steps-heading').textContent.startsWith('Worked for ')")
    capture('collapsed')
    js("document.querySelector('.conversation-steps-toggle').click()")
    wait("document.querySelector('.conversation-steps-toggle').getAttribute('aria-expanded')==='true'")
    capture('opened')
    # Past turns stay collapsed when newly loaded; a later active turn opens.
    second = (raw('next', 'text', 'user', step_boundary=True) if provider == 'claude' else
              raw('next', 'system', 'system', step_turn=dict(id='d'*32, phase='start')))
    state['events'] = project(provider, sequence+[second])
    js("stepsActivity.state='work';document.dispatchEvent(new Event('visibilitychange'))")
    wait("document.querySelectorAll('.conversation-steps').length===2")
    assert js("document.querySelectorAll('.conversation-steps-toggle')[1].getAttribute('aria-expanded')") == 'true'
    # The prior manually opened card remains user-controlled; remount defaults to collapsed.
    js("OrreryMobileTerminal.close();OrreryMobileTerminal.open('SampleAgent')")
    wait("document.querySelectorAll('.conversation-steps').length===2")
    assert js("document.querySelector('.conversation-steps-toggle').getAttribute('aria-expanded')") == 'false'
    # OS motion preference disables the selected A transitions and line pseudo element.
    client.call('Emulation.setEmulatedMedia', features=[dict(name='prefers-reduced-motion', value='reduce')])
    assert js("getComputedStyle(document.querySelector('.conversation-step'),'::before').animationName") == 'none'
    assert js("getComputedStyle(document.querySelector('.conversation-steps-expansion')).transitionDuration") == '0s'
    # A hidden tab pauses the local step clock; unmount removes all cards.
    js("Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new Event('visibilitychange'))")
    stopped = js("document.querySelectorAll('.conversation-steps-time')[1].textContent")
    js('new Promise(r=>setTimeout(r,650))')
    assert js("document.querySelectorAll('.conversation-steps-time')[1].textContent") == stopped
    js("OrreryMobileTerminal.close()")
    assert js("document.querySelector('.conversation-steps')===null")
    assert not any(op in {'prepare', 'commit'} for op, _ in state['calls'])


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_stopped_unpaired_call_keeps_original_group_until_late_result(phone, theme, tmp_path):
    client, js, wait, state = phone
    if theme == 'light':
        js((ROOT/'dashboard/theme_core.js').read_text())
        js("document.documentElement.dataset.colorTheme='light';Object.entries(AgentStackColorThemeCore.deriveWarmPaperLightTheme().cssVariables).forEach(([k,v])=>document.documentElement.style.setProperty(k,v))")
    js("window.OrreryMobileAgent={forName:()=>({running:false,stale:false,state:'wait',workDisplay:''})}")
    sequence = fixtures('claude')
    state.update(provider='claude', events=project('claude', sequence[:2]))
    js("OrreryMobileTerminal.close();OrreryMobileTerminal.open('SampleAgent')")
    wait("document.querySelector('[data-id=call]')!==null")
    wait("document.querySelector('.conversation-tool-group')!==null")
    assert js("document.querySelectorAll('.conversation-step').length") == 0
    assert js("document.querySelectorAll('.conversation-steps:not([hidden])').length") == 0
    js("document.querySelector('.conversation-tool-group').click();window.pendingCall=document.querySelector('[data-id=call]')")
    assert js('pendingCall.hidden') is False
    assert js("pendingCall.querySelector('.conversation-tool-label').textContent") == 'Read'
    folder = Path(os.environ.get('MOBILE_STEPS_UNPAIRED_CAPTURE_DIR', str(tmp_path)))
    folder.mkdir(parents=True, exist_ok=True)
    def capture(stage):
        js('new Promise(r=>setTimeout(r,300))')
        data = client.call('Page.captureScreenshot', format='png', captureBeyondViewport=False)['data']
        (folder/('mobile-'+theme+'-'+stage+'.png')).write_bytes(base64.b64decode(data))
    capture('call-only')
    later = raw('later-prompt', 'text', 'user', step_boundary=True)
    later['text'] = 'Now inspect another file.'
    state['events'] = project('claude', sequence[:2]+[later])
    js("document.dispatchEvent(new Event('visibilitychange'))")
    wait("document.querySelector('[data-id=later-prompt]')!==null")
    assert js('pendingCall.hidden') is False
    assert js("document.querySelectorAll('.conversation-tool-group').length") == 1
    assert js("document.querySelectorAll('.conversation-step').length") == 0
    capture('later-prompt')
    state['events'] = project('claude', sequence[:2]+[later, sequence[2]])
    js("document.dispatchEvent(new Event('visibilitychange'))")
    wait("document.querySelectorAll('.conversation-step').length===1")
    assert js('pendingCall.hidden') is True
    assert js("document.querySelectorAll('.conversation-tool-group').length") == 0
    assert js("document.querySelectorAll('.conversation-step-icon.done').length") == 1
    js("document.querySelector('.conversation-steps:not([hidden]) .conversation-steps-toggle').click()")
    capture('matched')
    assert not any(op in {'prepare', 'commit'} for op, _ in state['calls'])
