"""Command bodies pass the owner gate and stay lazy, bounded and literal."""
import json
import os
from threading import Event
from urllib.parse import parse_qs, urlsplit

import pytest

from dashboard import mobile_history as history
from mobile_reader_browser import phone
from test_mobile_history import Cockpit, OWNER, Store, cockpit, get, reader, serving


COCKPIT_REF = 'Opaque' + '_' * 26
PHONE_REF = 'c' * 32
HTML = '<img data-fixture src=x onerror="alert(1)">'
COMMAND = 'printf "%s\\n" "Fixture Key"\npython check.py'
OUTPUT = '\n'.join([HTML + ' Fixture Key', *[f'line {i}' for i in range(2, 12)]]) + '\n'
PAYLOAD = dict(ok=True, kind='command', command=COMMAND, output=OUTPUT, exit_code=7, truncated=False)
ROUTE = '/view/conversation/detail?name=SampleAgent&ref='


def recorded_steps(provider, *, code=7, status='failed'):
    step_id = 'a' * 32
    summary = dict(detail_ref=COCKPIT_REF, exit_code=code, output=OUTPUT, command=COMMAND, status=status)
    if provider == 'claude':
        return [
            dict(id='call', kind='tool', role='assistant', text='Bash',
                 step=dict(id=step_id, phase='call', tool='Bash', detail='python')),
            dict(id='result', kind='tool_result', role='assistant', text=OUTPUT,
                 step=dict(id=step_id, phase='result', **summary), output=OUTPUT),
        ]
    return [dict(id='result', kind='tool', role='assistant', text='Shell',
                 step=dict(id=step_id, phase='complete', tool='Shell', detail='python', **summary))]


@pytest.mark.parametrize('provider', ['claude', 'codex'])
def test_route_requires_owner_and_issued_command_id(provider):
    raw = recorded_steps(provider)
    raw[0]['references'] = [dict(id=COCKPIT_REF, label='report.txt', kind='file')]

    def answer(query):
        if 'ref' in query:
            return 200, dict(PAYLOAD, private='never forwarded')
        return 200, dict(ok=True, provider=provider, events=raw)

    with cockpit(answer) as upstream:
        client = history.HistoryClient(upstream)
        with serving(reader(client=client, store=Store(('SampleAgent', 'OtherFixture')))) as port:
            status, body = get(port, '/view/conversation?name=SampleAgent')
            assert status == 200
            assert all(secret not in body for secret in (COCKPIT_REF.encode(), b'Fixture Key', b'<img'))
            rows = json.loads(body)['events']
            file_ref = rows[0]['files'][0]['ref']
            ref = rows[-1]['step']['detail_ref']
            assert history.TOKEN.fullmatch(ref) and ref != file_ref
            assert rows[-1]['step']['exit_code'] == 7
            assert [urlsplit(p).path for p, _ in Cockpit.requests] == ['/telemetry/conversation']
            assert get(port, ROUTE+ref, {**OWNER, 'Tailscale-User-Login': 'other@example.test'})[0] == 403
            assert get(port, ROUTE+ref+'&path=/tmp/report.txt')[0] == 400
            assert get(port, ROUTE+ref+'&ref='+ref)[0] == 400
            assert get(port, ROUTE+file_ref)[0] == 410
            assert get(port, ROUTE.replace('SampleAgent', 'OtherFixture')+ref)[0] == 410
            assert get(port, ROUTE.replace('/detail', '/diff')+ref)[0] == 410
            assert get(port, ROUTE.replace('/detail', '/preview')+ref)[0] == 410
            assert len(Cockpit.requests) == 1
            status, body = get(port, ROUTE+ref)
            assert status == 200 and json.loads(body) == PAYLOAD
            assert len(Cockpit.requests) == 2
            path, headers = Cockpit.requests[-1]
            assert urlsplit(path).path == '/telemetry/conversation/detail'
            assert parse_qs(urlsplit(path).query) == {'ref': [COCKPIT_REF]}
            assert not {'Origin', 'Tailscale-User-Login', 'Cookie'} & set(headers)


def test_history_disable_gate_and_expired_command():
    with serving(reader(enabled=False)) as port:
        assert get(port, ROUTE+PHONE_REF)[0] == 404
    raw = recorded_steps('claude')
    with cockpit(lambda q: (400, {'ok': False, 'error': 'reference_expired'}) if 'ref' in q
                 else (200, {'ok': True, 'provider': 'claude', 'events': raw})) as upstream:
        client = history.HistoryClient(upstream)
        with serving(reader(client=client, store=Store(('SampleAgent',)))) as port:
            ref = client.page('SampleAgent')['events'][-1]['step']['detail_ref']
            status, body = get(port, ROUTE+ref)
            assert status == 410 and json.loads(body)['error'] == 'detail_expired'


@pytest.mark.parametrize(('provider', 'tool', 'phase', 'code', 'ref', 'code_visible', 'ref_visible'), [
    ('codex', 'Shell', 'complete', 0, COCKPIT_REF, True, True),
    ('codex', 'Shell', 'complete', 7, COCKPIT_REF, True, True),
    ('codex', 'Shell', 'complete', True, COCKPIT_REF, False, True),
    ('codex', 'Shell', 'complete', -9, '/tmp/report.txt', True, False),
    ('codex', 'Read', 'complete', 1, COCKPIT_REF, False, False),
    ('codex', 'Shell', 'call', 1, COCKPIT_REF, False, False),
    ('dsh', 'Shell', 'complete', 7, COCKPIT_REF, False, False),
    ('hermes', 'Shell', 'complete', 7, COCKPIT_REF, False, False),
])
def test_only_supported_command_metadata_is_projected(provider, tool, phase, code, ref, code_visible, ref_visible):
    event = recorded_steps('codex')[0]
    event['step'].update(tool=tool, phase=phase, exit_code=code, detail_ref=ref)
    rows, *_ = history.project_page(dict(ok=True, provider=provider, events=[event]),
                                    issue_detail=lambda _: PHONE_REF)
    step = rows[0]['step']
    assert ('exit_code' in step) is code_visible
    assert ('detail_ref' in step) is ref_visible
    assert not {'output', 'command'} & set(step)
    if ref_visible:
        assert step['detail_ref'] == PHONE_REF


@pytest.mark.parametrize(('code', 'status'), [(0, 'done'), (2, 'failed'), (None, 'failed')])
def test_claude_result_keeps_command_reference_with_or_without_known_exit(code, status):
    calls = recorded_steps('claude', code=code, status=status)
    rows, *_ = history.project_page(dict(ok=True, provider='claude', events=calls),
                                    issue_detail=lambda _: PHONE_REF)
    assert 'detail_ref' not in rows[0]['step']
    step = rows[1]['step']
    assert step['detail_ref'] == PHONE_REF and step['status'] == status
    assert step.get('exit_code') == code and ('exit_code' in step) is (code is not None)
    assert rows[1]['text'] == ''


def test_command_projection_strips_ansi_and_keeps_literal_text():
    value = history.project_command(dict(PAYLOAD, command='\x1b[32m'+COMMAND+'\x1b[0m',
        output='\x1b]8;;https://example.test\x1b\\'+HTML+'\x1b]8;;\x07\r\n'
               '\x9b31mred\x9b0m\x1bPprivate\x1b\\\x00\tline\n'))
    assert value == dict(PAYLOAD, output=HTML+'\nred\tline\n')


@pytest.mark.parametrize('output', ['日' * 26000, 'one line\n' * 401])
def test_command_limits_count_utf8_output_bytes_and_lines(output):
    value = history.project_command(dict(PAYLOAD, command='日'*4000, output=output))
    assert value['truncated']
    assert len(value['command'].encode()) <= 8*1024
    assert len(value['output'].encode()) <= 64*1024
    assert len(value['output'].splitlines()) <= 400
    assert '\ufffd' not in value['output'] + value['command']
    assert value['output'] and output.startswith(value['output'])


@pytest.mark.parametrize('invalid', [dict(kind='text'), dict(command=[]), dict(output={}), dict(exit_code=True)])
def test_malformed_command_details_are_refused(invalid):
    with pytest.raises(history.HistoryUnavailable, match='detail_unavailable'):
        history.project_command({**PAYLOAD, **invalid})


def browser_setup(phone, provider, payload, *, code=7, status='failed'):
    _, js, wait, state = phone
    raw = recorded_steps(provider, code=code, status=status)
    events = history.project_page(dict(ok=True, provider=provider, events=raw),
                                  issue_detail=lambda _: PHONE_REF)[0]
    state.update(provider=provider, events=events, step_details={PHONE_REF: payload})
    js("OrreryMobileTerminal.close();OrreryMobileTerminal.open('SampleAgent')")
    wait("!!document.querySelector('.conversation-steps-toggle')")
    js("document.querySelector('.conversation-steps-toggle').click()")
    wait("!!document.querySelector('.conversation-step-command-toggle:not([hidden])')")
    return js, wait, state


@pytest.mark.parametrize(('provider', 'demo'), [('claude', False), ('codex', True)])
def test_inline_command_is_lazy_literal_and_masked_before_insert(phone, provider, demo):
    js, wait, state = browser_setup(phone, provider, PAYLOAD)
    if demo:
        if not os.environ.get('ORRERY_DEMO_BRIDGE_ROOT'):
            pytest.skip('paired Demo scripts required')
        state['demo'] = dict(version=1, supported=True, on=True, words=['Fixture Key'],
                             identity=dict(users=['fixture-user'], hosts=['fixture-host']))
        js('OrreryMobileDemo.refresh()')
        wait("Boolean(OrreryDemo?.on)")
    js("""
      window.firstDetailCopy=null;
      const replace=Element.prototype.replaceChildren;
      Element.prototype.replaceChildren=function(...nodes){
        if(this.matches('.conversation-step-detail')&&nodes.length&&firstDetailCopy===null)
          firstDetailCopy=[...nodes[0].querySelectorAll('pre')].map(e=>e.textContent);
        return replace.apply(this,nodes);
      };
    """)
    assert not any('/detail?' in call for call in state['get_calls'])
    assert js("document.querySelector('.conversation-step-exit:not([hidden])').textContent") == 'exit 7'
    assert js("document.querySelector('.conversation-step-target').textContent") == 'python'
    js("document.querySelector('.conversation-step-command-toggle').click()")
    wait("window.firstDetailCopy!==null")
    assert [call for call in state['get_calls'] if '/detail?' in call] == [ROUTE+PHONE_REF]
    assert js("firstDetailCopy[0].startsWith('$ printf') && firstDetailCopy[1].includes('<img')")
    assert js("!document.querySelector('.conversation-step-detail [data-fixture]')")
    assert js("firstDetailCopy.map(value=>value.includes('Fixture Key'))") == [not demo, not demo]
    assert js(r"firstDetailCopy[1].split('\n').length") == 8
    assert js("document.querySelector('.conversation-step-detail-more').textContent") == 'Show 3 more lines'
    js("document.querySelector('.conversation-step-detail-more').click()")
    assert js(r"document.querySelector('.conversation-step-command-output').textContent.split('\n').length") == 11
    assert len([call for call in state['get_calls'] if '/detail?' in call]) == 1
    js("document.querySelector('.conversation-step-command-toggle').click()")
    assert js("document.querySelector('.conversation-step-detail').hidden&&!document.querySelector('.conversation-step-detail').textContent")


@pytest.mark.parametrize(('payload', 'notice'), [
    (dict(PAYLOAD, output='', exit_code=0), 'No output'),
    (dict(PAYLOAD, truncated=True, exit_code=0), 'Too long to show here'),
    (None, 'Command link expired. Reopen the conversation and try again.'),
])
def test_command_detail_notices(phone, payload, notice):
    js, wait, _ = browser_setup(phone, 'codex', payload, code=0, status='done')
    assert js("!document.querySelector('.conversation-step-exit:not([hidden])')")
    js("document.querySelector('.conversation-step-command-toggle').click()")
    wait("document.querySelector('.conversation-step-detail-note')?.textContent==="+json.dumps(notice))


def test_closing_steps_discards_inflight_command(phone):
    js, wait, state = browser_setup(phone, 'claude', PAYLOAD)
    gate = state['step_detail_gate'] = Event()
    try:
        js("document.querySelector('.conversation-step-command-toggle').click()")
        wait("document.querySelector('.conversation-step-detail-note')?.textContent==='Loading command…'")
        js("document.querySelector('.conversation-steps-toggle').click()")
        gate.set()
        js("document.querySelector('.conversation-steps-toggle').click()")
        assert js("document.querySelector('.conversation-step-detail').hidden")
        assert js("!document.querySelector('.conversation-step-command-code')")
    finally:
        gate.set()
