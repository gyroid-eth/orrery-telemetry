"""Step diffs stay off the phone until a specific recorded operation is opened."""
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
STAT = {'added': 12, 'removed': 1, 'new_file': False}
PAYLOAD = {
    'ok': True, 'path': 'Fixture Key/report.txt', 'new_file': False, 'truncated': False,
    'hunks': [{'header': '@@ -1 +1,12 @@', 'lines': [
        {'op': '-', 'text': 'old line'},
        {'op': '+', 'text': HTML+' Fixture Key'},
        *[{'op': '+', 'text': f'line {n}'} for n in range(2, 13)],
    ]}],
}


def recorded_steps(provider, ref=COCKPIT_REF):
    step_id = 'b' * 32
    if provider == 'claude':
        return [
            dict(id='edit', kind='tool', role='assistant', text='Edit', ts='',
                 step=dict(id=step_id, phase='call', tool='Edit', detail='report.txt')),
            dict(id='result', kind='tool_result', role='assistant', text=HTML, ts='',
                 step=dict(id=step_id, phase='result', status='done', diff_stat=STAT, diff_ref=ref,
                           output=HTML), diff=PAYLOAD),
        ]
    return [dict(id='edit', kind='tool', role='assistant', text='FileChange', ts='',
                 step=dict(id=step_id, phase='complete', tool='ApplyPatch', status='done',
                           detail='report.txt', diff_stat=STAT, diff_ref=ref), diff=PAYLOAD)]


@pytest.mark.parametrize('provider', ['claude', 'codex'])
def test_route_requires_owner_and_issued_diff_id_and_loads_one_step(provider):
    raw = recorded_steps(provider)
    raw[0]['references'] = [dict(id=COCKPIT_REF, label='report.txt', kind='file')]

    def answer(query):
        if 'ref' in query:
            return 200, dict(PAYLOAD, private='not projected')
        return 200, dict(ok=True, provider=provider, events=raw)

    with cockpit(answer) as upstream:
        client = history.HistoryClient(upstream)
        with serving(reader(client=client, store=Store(('SampleAgent', 'OtherFixture')))) as port:
            status, body = get(port, '/view/conversation?name=SampleAgent')
            assert status == 200 and COCKPIT_REF.encode() not in body and b'<img' not in body
            rows = json.loads(body)['events']
            summary = rows[-1]['step']
            assert summary['diff_stat'] == STAT
            ref, file_ref = summary['diff_ref'], rows[0]['files'][0]['ref']
            assert ref != file_ref and history.TOKEN.fullmatch(ref)
            assert [urlsplit(p).path for p, _ in Cockpit.requests] == ['/telemetry/conversation']
            route = '/view/conversation/diff?name=SampleAgent&ref='
            assert get(port, route+ref, {**OWNER, 'Tailscale-User-Login': 'other@example.test'})[0] == 403
            assert get(port, route+ref+'&path=/tmp/report.txt')[0] == 400
            assert get(port, route+ref+'&thumbnail=1')[0] == 400
            assert get(port, route+ref+'&ref='+ref)[0] == 400
            assert get(port, route+file_ref)[0] == 410
            assert get(port, route.replace('SampleAgent', 'OtherFixture')+ref)[0] == 410
            assert get(port, route.replace('/diff', '/preview')+ref)[0] == 410
            assert len(Cockpit.requests) == 1
            status, body = get(port, route+ref)
            assert status == 200 and json.loads(body) == PAYLOAD
            assert len(Cockpit.requests) == 2
            path, headers = Cockpit.requests[-1]
            assert urlsplit(path).path == '/telemetry/conversation/diff'
            assert parse_qs(urlsplit(path).query) == {'ref': [COCKPIT_REF]}
            assert not {'Origin', 'Tailscale-User-Login', 'Cookie'} & set(headers)


def test_history_disable_gate_and_expired_diff():
    path = '/view/conversation/diff?name=SampleAgent&ref='
    with serving(reader(enabled=False)) as port:
        assert get(port, path+PHONE_REF)[0] == 404
    raw = recorded_steps('claude')
    with cockpit(lambda q: (400, {'ok': False, 'error': 'reference_expired'}) if 'ref' in q
                 else (200, {'ok': True, 'provider': 'claude', 'events': raw})) as upstream:
        client = history.HistoryClient(upstream)
        with serving(reader(client=client, store=Store(('SampleAgent',)))) as port:
            ref = client.page('SampleAgent')['events'][-1]['step']['diff_ref']
            status, body = get(port, path+ref)
            assert status == 410 and json.loads(body)['error'] == 'diff_expired'


@pytest.mark.parametrize('change', ['unsupported-provider', 'failed', 'bad-count', 'bad-id', 'call'])
def test_diff_metadata_is_only_forwarded_for_supported_completed_edits(change):
    raw = recorded_steps('codex')[0]
    step = raw['step']
    provider = 'dsh' if change == 'unsupported-provider' else 'codex'
    if change == 'failed':
        step['status'] = 'failed'
    elif change == 'bad-count':
        step['diff_stat'] = {**STAT, 'removed': True}
    elif change == 'bad-id':
        step['diff_ref'] = '/tmp/report.txt'
    elif change == 'call':
        step['phase'] = 'call'
    output = history.project_page(dict(ok=True, provider=provider, events=[raw]), issue_diff=lambda _: PHONE_REF)[0]
    assert 'diff_stat' not in output[0]['step'] and 'diff_ref' not in output[0]['step']
    assert 'diff' not in output[0]


@pytest.mark.parametrize('lines', [[{'op': '+', 'text': 'x'}]*401,
                                  [{'op': '+', 'text': '日\"'*400}]*60])
def test_decoded_diff_stays_within_line_and_utf8_wire_limits(lines):
    result = history.project_diff(dict(PAYLOAD, hunks=[dict(header='@@ edit @@', lines=lines)]))
    assert result['truncated']
    assert len(history.encode(result)) <= 64*1024
    assert 0 < len(result['hunks'][0]['lines']) <= 400
    assert result['hunks'][0]['lines'][0] == lines[0]


def test_invalid_diff_lines_are_refused():
    with pytest.raises(history.HistoryUnavailable, match='diff_unavailable'):
        history.project_diff(dict(PAYLOAD, hunks=[dict(header='', lines=[dict(op='=', text=HTML)])]))


def browser_setup(phone, provider, payload):
    _, js, wait, state = phone
    raw = recorded_steps(provider)
    state.update(provider=provider, events=history.project_page(
        dict(ok=True, provider=provider, events=raw), issue_diff=lambda _: PHONE_REF)[0],
        step_diffs={PHONE_REF: payload})
    js("OrreryMobileTerminal.close();OrreryMobileTerminal.open('SampleAgent')")
    wait("!!document.querySelector('.conversation-steps-toggle')")
    js("document.querySelector('.conversation-steps-toggle').click()")
    wait("!!document.querySelector('.conversation-step-diff-toggle:not([hidden])')")
    return js, wait, state


@pytest.mark.parametrize('provider,demo', [('claude', False), ('codex', True)])
def test_inline_diff_loads_on_click_expands_as_text_and_masks_before_render(phone, provider, demo):
    js, wait, state = browser_setup(phone, provider, PAYLOAD)
    if demo:
        if not os.environ.get('ORRERY_DEMO_BRIDGE_ROOT'):
            pytest.skip('paired Demo scripts required')
        state['demo'] = dict(version=1, supported=True, on=True, words=['Fixture Key'],
                             identity=dict(users=['fixture-user'], hosts=['fixture-host']))
        js('OrreryMobileDemo.refresh()')
        wait("Boolean(OrreryDemo?.on)")
    js("""
      window.firstDiffCopy=null;
      const replace=Element.prototype.replaceChildren;
      Element.prototype.replaceChildren=function(...nodes){
        if(this.matches('.conversation-step-diff')&&nodes.length&&firstDiffCopy===null)
          firstDiffCopy=nodes[0].textContent;
        return replace.apply(this,nodes);
      };
    """)
    assert not any('/diff?' in call for call in state['get_calls'])
    assert js("document.querySelector('.conversation-diff-added').textContent") == '+12'
    assert js("document.querySelector('.conversation-diff-removed').textContent") == '−1'
    js("document.querySelector('.conversation-step-diff-toggle').click()")
    wait("document.querySelectorAll('.conversation-step-diff-line').length===8")
    assert [x for x in state['get_calls'] if '/diff?' in x] == [
        '/view/conversation/diff?name=SampleAgent&ref='+PHONE_REF]
    assert js("document.querySelector('.conversation-step-diff-more').textContent") == 'Show 5 more lines'
    assert js("firstDiffCopy.includes('<img')&&!document.querySelector('.conversation-step-diff [data-fixture]')")
    assert js("firstDiffCopy.includes('Fixture Key')") is not demo
    js("document.querySelector('.conversation-step-diff-more').click()")
    assert js("document.querySelectorAll('.conversation-step-diff-line').length") == 13
    assert len([x for x in state['get_calls'] if '/diff?' in x]) == 1
    js("document.querySelector('.conversation-step-diff-toggle').click()")
    assert js("document.querySelector('.conversation-step-diff').hidden&&!document.querySelector('.conversation-step-diff').textContent")


def test_truncated_diff_can_close_and_invalid_history_discards_it(phone):
    js, wait, state = browser_setup(phone, 'claude', dict(PAYLOAD, truncated=True))
    js("document.querySelector('.conversation-step-diff-toggle').click()")
    wait("document.querySelector('.conversation-step-diff-note')?.textContent==='Too long to show here'")
    assert js("!document.querySelector('.conversation-step-diff-more')")
    state['error'] = 'history_unconfirmed'
    js("document.dispatchEvent(new Event('visibilitychange'))")
    wait("document.querySelector('.conversation-step-diff').hidden")
    assert js("document.querySelector('.conversation-step-diff').childElementCount") == 0


def test_closing_steps_discards_the_inflight_diff(phone):
    js, wait, state = browser_setup(phone, 'codex', PAYLOAD)
    gate = state['step_diff_gate'] = Event()
    try:
        js("document.querySelector('.conversation-step-diff-toggle').click()")
        wait("document.querySelector('.conversation-step-diff-note')?.textContent==='Loading diff…'")
        js("document.querySelector('.conversation-steps-toggle').click()")
        gate.set()
        js("document.querySelector('.conversation-steps-toggle').click()")
        assert js("document.querySelector('.conversation-step-diff').hidden")
        assert not js("!!document.querySelector('.conversation-step-diff-line')")
    finally:
        gate.set()
