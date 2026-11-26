"""Selected A on the real mobile page, synthetic APIs and shared opt-in CDP."""
import base64
import io
import json
import os
from pathlib import Path

import pytest
from PIL import Image

from mobile_reader_browser import phone  # noqa: F401
from test_mobile_steps import fixtures, raw
from dashboard.mobile_history import project_page

ROOT = Path(__file__).resolve().parents[1]


def project(provider, events):
    return project_page(dict(ok=True, provider=provider, events=events))[0]


def test_mobile_image_step_opens_the_file_preview(phone):
    client, js, wait, state = phone
    events = fixtures('codex')
    seen = next(event for event in events if event['id']=='observed')
    seen['step']['detail'] = 'diagram.png'
    seen['step']['image'] = dict(id='r'*32, kind='image', label='diagram.png')
    picture = io.BytesIO()
    Image.new('RGB', (640, 400), '#548eae').save(picture, 'PNG')
    state['file_preview'] = dict(ok=True, kind='image', width=640, height=400,
                                 data=base64.b64encode(picture.getvalue()).decode())
    state.update(provider='codex', events=project_page(dict(ok=True, provider='codex', events=events),
                                                      issue=lambda _: 'a'*32)[0])
    js("OrreryMobileTerminal.close();OrreryMobileTerminal.open('SampleAgent')")
    wait("document.querySelector('.conversation-step-image:not([hidden])')!==null")
    js("document.querySelector('.conversation-steps-toggle').click()")
    assert js("document.querySelectorAll('.conversation-step-image:not([hidden])').length") == 1
    assert js("document.querySelector('.conversation-step-image:not([hidden])').ariaLabel") == 'Preview diagram.png'
    capture = os.environ.get('ORRERY_STEP_IMAGE_SCREENSHOTS')
    if capture:
        folder = Path(capture)
        folder.mkdir(parents=True, exist_ok=True)
        client.call('Page.bringToFront')
        wait("document.querySelector('.conversation-step-image:not([hidden])').getBoundingClientRect().height>0")
        (folder/'phone-step.png').write_bytes(base64.b64decode(client.call('Page.captureScreenshot', format='png')['data']))
    js("document.querySelector('.conversation-step-image:not([hidden])').click()")
    wait("document.querySelector('.conversation-preview-image')?.naturalWidth===640")
    assert '/view/conversation/preview?name=SampleAgent&ref='+'a'*32 in state['get_calls']
    assert js("document.querySelector('.conversation-preview').ariaLabel") == 'Preview of diagram.png'
    if capture:
        (folder/'phone-preview.png').write_bytes(base64.b64decode(client.call('Page.captureScreenshot', format='png')['data']))


@pytest.mark.parametrize('provider', ['claude', 'codex'])
@pytest.mark.parametrize('theme', ['light', 'dark', 'demo'])
def test_steps_add_finish_toggle_demo_and_keep_reference(phone, provider, theme):
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
    first[1 if provider == 'claude' else 2]['references'] = [dict(id='r'*32, label='report.md', kind='file')]
    state.update(provider=provider, events=project(provider, first))
    js("OrreryMobileTerminal.close();OrreryMobileTerminal.open('SampleAgent')")
    wait("document.querySelectorAll('.conversation-step').length===1")
    wait("document.querySelector('.conversation-steps-heading').textContent==='Working…'")
    assert js("document.querySelector('.conversation-steps-toggle').getAttribute('aria-expanded')") == 'false'
    assert js("document.querySelector('.conversation-steps-toggle').getAttribute('aria-label').startsWith('Working… 1 step, ')")
    js("document.querySelector('.conversation-steps-toggle').click()")
    assert js("document.querySelector('.conversation-steps-toggle').getAttribute('aria-expanded')") == 'true'
    before = js("document.querySelector('.conversation-steps-time').textContent")
    wait("document.querySelector('.conversation-steps-time').textContent!=="+json.dumps(before), timeout=3)
    def capture(stage):
        destination = os.environ.get('MOBILE_STEPS_CAPTURE_DIR')
        if not destination:
            return
        folder = Path(destination)
        folder.mkdir(parents=True, exist_ok=True)
        js('new Promise(r=>setTimeout(r,300))')
        js("document.querySelector('.conversation-list').scrollTop=0")
        data = client.call('Page.captureScreenshot', format='png', captureBeyondViewport=False)['data']
        (folder/(provider+'-'+theme+'-'+stage+'.png')).write_bytes(base64.b64decode(data))
    capture('one-step')
    # Native pairing/IDs arrive through project_event, not an alternative renderer.
    state['events'] = project(provider, ongoing)
    js("document.dispatchEvent(new Event('visibilitychange'))")
    wait("document.querySelectorAll('.conversation-step').length===3")
    assert js("document.querySelectorAll('.conversation-steps').length") == 1
    assert js("document.querySelectorAll('.conversation-step-record:not([hidden]) .conversation-chip').length") == 1
    assert js("getComputedStyle(document.querySelector('.conversation-step-record'),'::before').display") == 'none'
    assert js("document.querySelectorAll('.conversation-tool-group').length") == 0
    assert js("document.querySelector('[data-id=unknown]').hidden")
    assert js("[...document.querySelectorAll('.conversation-step-outcome')].at(-1).textContent") == ('Failed' if provider == 'claude' else 'Observed')
    if theme == 'demo':
        wait("!document.querySelector('.conversation-step-target').textContent.includes('report')")
        assert js("document.querySelector('.conversation-step-tool').textContent") == 'Read'
    else:
        assert js("document.querySelector('.conversation-step-target').textContent") == 'report.md'
    assert js("getComputedStyle(document.querySelector('.conversation-step-target')).overflowWrap") == 'anywhere'
    assert js("""(() => {
        const style=getComputedStyle(document.querySelector('.conversation-steps'));
        return style.borderTopWidth==='0px'&&style.backgroundColor==='rgba(0, 0, 0, 0)';
    })()""")
    assert js("document.querySelector('.conversation-steps').scrollWidth<=document.querySelector('.conversation-list').clientWidth")
    capture('working')
    if provider == 'codex':
        state['events'] = project(provider, sequence)
    js("stepsActivity.state='wait';OrreryMobileConversation.refreshActivity();document.dispatchEvent(new Event('visibilitychange'))")
    wait("document.querySelector('.conversation-steps-toggle').getAttribute('aria-expanded')==='false'")
    assert js("document.querySelector('.conversation-steps-toggle').getAttribute('aria-label').startsWith('2 tool calls, ')")
    capture('collapsed')
    js("document.querySelector('.conversation-steps-toggle').click()")
    wait("document.querySelector('.conversation-steps-toggle').getAttribute('aria-expanded')==='true'")
    capture('opened')
    # The later active turn also starts collapsed.
    second = (raw('next', 'text', 'user', step_boundary=True) if provider == 'claude' else
              raw('next', 'system', 'system', step_turn=dict(id='d'*32, phase='start')))
    state['events'] = project(provider, sequence+[second])
    js("stepsActivity.state='work';document.dispatchEvent(new Event('visibilitychange'))")
    wait("document.querySelectorAll('.conversation-steps').length===2")
    assert js("document.querySelectorAll('.conversation-steps-toggle')[1].getAttribute('aria-expanded')") == 'false'
    # The prior manually opened card remains user-controlled; remount defaults to collapsed.
    js("OrreryMobileTerminal.close();OrreryMobileTerminal.open('SampleAgent')")
    wait("document.querySelectorAll('.conversation-steps').length===2")
    assert js("document.querySelector('.conversation-steps-toggle').getAttribute('aria-expanded')") == 'false'
    # OS motion preference disables the disclosure transition.
    client.call('Emulation.setEmulatedMedia', features=[dict(name='prefers-reduced-motion', value='reduce')])
    assert js("getComputedStyle(document.querySelector('.conversation-steps-expansion')).transitionDuration") == '0s'
    # A hidden tab pauses the local step clock; unmount removes all cards.
    js("Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new Event('visibilitychange'))")
    stopped = js("document.querySelectorAll('.conversation-steps-time')[1].textContent")
    js('new Promise(r=>setTimeout(r,650))')
    assert js("document.querySelectorAll('.conversation-steps-time')[1].textContent") == stopped
    js("OrreryMobileTerminal.close()")
    assert js("document.querySelector('.conversation-steps')===null")
    assert not any(op in {'prepare', 'commit'} for op, _ in state['calls'])


@pytest.mark.parametrize('provider', ['claude', 'codex'])
def test_active_request_stays_in_the_same_inline_step(phone, provider):
    _, js, wait, state = phone
    js("window.stepsActivity={running:true,stale:false,state:'work',workDisplay:'2s'};window.OrreryMobileAgent={forName:()=>stepsActivity}")
    sequence = fixtures(provider)
    if provider == 'claude':
        start = sequence[:2]
    else:
        start = [sequence[0], raw('call', tool_call_id='a'*32)]
    state.update(provider=provider, events=project(provider, start))
    js("OrreryMobileTerminal.close();OrreryMobileTerminal.open('SampleAgent')")
    wait("document.querySelector('.conversation-steps-count')?.textContent==='1 step'")
    js("window.firstStep=document.querySelector('.conversation-step');document.querySelector('.conversation-steps-toggle').click()")
    assert js("document.querySelector('.conversation-tool-group')===null")
    if provider == 'claude':
        js("stepsActivity.state='wait';OrreryMobileConversation.refreshActivity()")
        assert js("firstStep.querySelector('.conversation-step-outcome').textContent") == 'Observed'
        assert js("document.querySelector('.conversation-tool-group')===null")
    state['events'] = project(provider, start+[sequence[2]])
    js("document.dispatchEvent(new Event('visibilitychange'))")
    wait("firstStep.querySelector('.conversation-step-icon').classList.contains('done')")
    assert js("document.querySelectorAll('.conversation-step').length===1&&document.querySelector('.conversation-step')===firstStep")
    assert js("document.querySelector('.conversation-tool-group')===null")


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_stopped_unpaired_call_keeps_original_group_until_late_result(phone, theme):
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
    def capture(stage):
        destination = os.environ.get('MOBILE_STEPS_UNPAIRED_CAPTURE_DIR')
        if not destination:
            return
        folder = Path(destination)
        folder.mkdir(parents=True, exist_ok=True)
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


@pytest.mark.parametrize('provider', ['claude', 'codex'])
def test_visible_step_order_matches_on_live_append_and_reload(phone, provider):
    _, js, wait, state = phone
    js("window.OrreryMobileAgent={forName:()=>({running:false,stale:false,state:'wait'})}")
    sequence = fixtures(provider)
    first = sequence[:3]
    notice = raw('progress', 'text', variant='update')
    notice['text'] = 'The first check passed.'
    # A sent Mail card remains visible on the second tool row.
    sequence[4]['mail'] = [dict(direction='sent', subject='Second check complete')]
    state.update(provider=provider, events=project(provider, first))
    js("OrreryMobileTerminal.close();OrreryMobileTerminal.open('SampleAgent')")
    wait("document.querySelectorAll('.conversation-steps:not([hidden])').length===1")
    js("window.firstStepCard=document.querySelector('.conversation-steps:not([hidden])')")
    state['events'] = project(provider, first+[notice]+sequence[3:])
    js("document.dispatchEvent(new Event('visibilitychange'))")
    wait("document.querySelectorAll('.conversation-step').length===2")
    measure = """(() => {
        const rows = [...document.querySelector('.conversation-list').children].filter(el => !el.hidden);
        const cards = rows.filter(el => el.matches('.conversation-steps'));
        return {
            cards: cards.length,
            steps: cards.map(el => el.querySelectorAll('.conversation-step').length),
            order: rows.flatMap(el => el.matches('.conversation-steps') ? ['steps']
                : el.querySelector('.conversation-mail') ? ['mail']
                : el.dataset.id === 'progress' ? ['assistant'] : [])
        };
    })()"""
    live = js(measure)
    assert live == dict(cards=2, steps=[1, 1], order=['steps', 'assistant', 'steps', 'mail'])
    assert js("document.querySelector('.conversation-steps:not([hidden])')===firstStepCard")
    js("OrreryMobileTerminal.close();OrreryMobileTerminal.open('SampleAgent')")
    wait("document.querySelectorAll('.conversation-step').length===2")
    reloaded = js(measure)
    assert reloaded == live
    print(json.dumps(dict(provider=provider, viewport=js('[innerWidth,innerHeight]'),
                          live=live, reloaded=reloaded)))
