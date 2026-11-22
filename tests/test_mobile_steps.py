"""Bounded cockpit -> phone metadata and the selected A collector, no services."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from dashboard import mobile_history as history

ROOT = Path(__file__).resolve().parents[1]
STAMP = '2026-11-15T07:00:00Z'


def raw(event_id, kind='tool', role='assistant', **extra):
    return dict(id=event_id, kind=kind, role=role, text='Read', ts=STAMP, **extra)


def fixtures(provider):
    prompt = raw('prompt', 'text', 'user', step_boundary=True)
    prompt['text'] = 'Please inspect the report and chart.'
    if provider == 'claude':
        return [prompt,
                raw('call', step=dict(id='a'*32, phase='call', tool='Read', detail='report.md', started=STAMP)),
                raw('result', 'tool_result', step=dict(id='a'*32, phase='result', status='done', ended='2026-11-15T07:00:02Z')),
                raw('unknown'),
                raw('call2', step=dict(id='b'*32, phase='call', tool='Bash', detail='pytest', started=STAMP)),
                raw('result2', 'tool_result', step=dict(id='b'*32, phase='result', status='failed', ended='2026-11-15T07:00:03Z'))]
    return [raw('start', 'system', 'system', step_turn=dict(id='c'*32, phase='start', started=1794726000000)),
            raw('wrapper', step_wrapper=True),
            raw('complete', step=dict(id='a'*32, phase='complete', tool='Read', detail='report.md', status='done', turn='c'*32, started=1794726000000, ended=1794726002000)),
            raw('unknown'),
            raw('observed', step=dict(id='b'*32, phase='complete', tool='view_image', detail='chart.png', turn='c'*32)),
            raw('end', 'system', 'system', step_turn=dict(id='c'*32, phase='end', duration=3000))]


def collect(events, provider):
    node = shutil.which('node')
    if not node:
        pytest.skip('node is unavailable')
    code = (ROOT/'dashboard/mobile/conversation.js').read_text().split('/* Opt-in, read-only conversation history.')[0]
    script = code + '\nconst m=OrreryMobileSteps.collect(' + json.dumps(events) + ',' + json.dumps(provider.title()) + ");console.log(JSON.stringify({groups:m.groups,consumed:[...m.consumed]}));"
    result = subprocess.run([node, '-'], input=script, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize('provider', ['claude', 'codex'])
def test_native_projected_sequence_pairs_and_keeps_unknown_tools(provider):
    projected, _, _, _ = history.project_page(dict(ok=True, provider=provider, events=fixtures(provider)))
    model = collect(projected, provider)
    assert len(model['groups']) == 1
    assert len(model['groups'][0]['steps']) == 2
    assert 'unknown' not in model['consumed']
    assert model['groups'][0]['steps'][0]['status'] == 'done'
    assert model['groups'][0]['steps'][1]['status'] == ('failed' if provider == 'claude' else 'observed')
    assert ('wrapper' in model['consumed']) == (provider == 'codex')
    assert model['groups'][0]['closed'] == (provider == 'codex')


@pytest.mark.parametrize('provider', ['claude', 'codex'])
@pytest.mark.parametrize('separator', ['assistant', 'update', 'user', 'received', 'sent', 'system'])
def test_projected_visible_output_splits_steps_in_the_same_turn(provider, separator):
    sequence = fixtures(provider)
    first_tool = sequence[1 if provider == 'claude' else 2]
    if separator in {'assistant', 'update', 'user'}:
        output = raw('progress', 'text', 'user' if separator == 'user' else 'assistant')
        if separator == 'update':
            output['variant'] = 'update'
    elif separator == 'system':
        output = raw('notice', 'system', 'system')
    else:
        mail = [dict(direction=separator, subject='Check complete')]
        if separator == 'sent':
            first_tool['mail'] = mail
            output = None
        else:
            output = raw('notice', 'mail', 'user', mail=mail)
    if output is not None:
        # Claude can deliver the first result after the intervening output.
        sequence.insert(2 if provider == 'claude' else 3, output)
    projected, _, _, _ = history.project_page(dict(ok=True, provider=provider, events=sequence))
    model = collect(projected, provider)
    first, second = [g for g in model['groups'] if g['steps']]
    assert [g['anchor'] for g in (first, second)] == (
        ['call', 'call2'] if provider == 'claude' else ['complete', 'observed'])
    assert [len(g['steps']) for g in (first, second)] == [1, 1]
    assert first['closed']
    assert first['duration'] == 2
    assert second['duration'] == (3 if provider == 'claude' else None)


def test_codex_completion_update_keeps_its_original_segment():
    start, wrapper, first, unknown, second, end = fixtures('codex')
    update = raw('repeat', step={**first['step'], 'status': 'failed'})
    events = [start, wrapper, first, raw('progress', 'text'), update, unknown, second, end]
    model = collect([history.project_event(e) for e in events], 'codex')
    assert [[s['eventId'] for s in g['steps']] for g in model['groups']] == [['complete'], ['observed']]
    assert model['groups'][0]['steps'][0]['status'] == 'failed'
    assert 'repeat' in model['consumed']


def test_codex_partial_turn_starting_with_mail_has_distinct_segments():
    _, _, first, _, second, end = fixtures('codex')
    first['mail'] = [dict(direction='sent', subject='Check complete')]
    model = collect([history.project_event(e) for e in [first, second, end]], 'codex')
    first_group, second_group = model['groups']
    assert first_group['id'] != second_group['id']
    assert [g['anchor'] for g in (first_group, second_group)] == ['complete', 'observed']


def test_results_are_metadata_only_and_do_not_create_preview_grants():
    secret = '/private/credentials/hidden.txt SECRET_OUTPUT'
    event = raw('result', 'tool_result', step=dict(id='a'*32, phase='result', status='done', output=secret, arguments=secret, path=secret, references=[secret]))
    event.update(text=secret, output=secret, arguments=secret)
    result = history.project_event(event)
    assert result['text'] == ''
    assert result['step'] == dict(id='a'*32, phase='result', status='done')
    assert secret not in json.dumps(result)
    assert 'files' not in result
    assert history.project_event(raw('old-result', 'tool_result')) is None


@pytest.mark.parametrize('change', [dict(id='raw-call-id'), dict(id='A'*32), dict(phase='execute'), dict(phase=[]), dict(tool=''), dict(id=9)])
def test_invalid_metadata_leaves_original_tool_fallback(change):
    step = dict(id='a'*32, phase='complete', tool='Read', detail='report.md', status='done')
    step.update(change)
    result = history.project_event(raw('e', step=step))
    assert 'step' not in result
    assert result['kind'] == 'tool'


def test_allowlist_bounds_times_and_rejects_unverified_outcome():
    result = history.project_event(raw('e', step=dict(id='a'*32, phase='complete', tool='Read\n'+'r'*300, detail='x'*500, started=float('nan'), ended=True, status=[], turn='private-turn-id', arguments='SECRET')))
    assert len(result['step']['tool'].encode()) <= 160
    assert len(result['step']['detail'].encode()) <= 240
    assert result['step']['status'] == 'observed'
    assert not {'arguments','started','ended','turn'} & result['step'].keys()
    boundary = history.project_event(raw('b', 'system', 'system', step_turn=dict(id='b'*32, phase='end', duration=float('inf'), path='SECRET', started=STAMP)))
    assert boundary['step_turn'] == dict(id='b'*32, phase='end', started=STAMP)


def test_partial_result_is_unpaired_until_older_call_is_loaded():
    sequence = fixtures('claude')
    events = [history.project_event(x) for x in sequence]
    partial = collect(events[2:], 'claude')
    assert partial['groups'][0]['orphans'] == 1
    full = collect(events, 'claude')
    assert full['groups'][0]['orphans'] == 0
    assert len(full['groups'][0]['steps']) == 2


def test_wire_limits_stay_bounded_with_steps():
    events = [raw(str(i), step=dict(id=f'{i:032x}', phase='complete', tool='r'*160, detail='d'*240, status='observed')) for i in range(100)]
    page, _, limited, _ = history.project_page(dict(ok=True, events=events))
    assert limited and len(page) == history.PAGE_EVENTS
    assert len(history.encode(page)) <= history.MAX_PAGE_BYTES
    assert history.MAX_WIRE_BYTES == 640*1024


def test_large_numeric_times_and_display_only_step_fields():
    for phase in ('call', 'complete'):
        result = history.project_event(raw('e', step=dict(id='a'*32, phase=phase, tool='Read', detail='report.md', started=10**1000, ended=-1, arguments='SECRET', output='SECRET', references=['SECRET'], path='SECRET')))
        assert 'SECRET' not in json.dumps(result)
        assert not {'started','ended'} & result['step'].keys()
    boundary = history.project_event(raw('t', 'system', 'system', step_turn=dict(id='b'*32, phase='end', duration=10**1000)))
    assert boundary['step_turn'] == dict(id='b'*32, phase='end')


@pytest.mark.parametrize('later_prompt', [False, True])
def test_unpaired_claude_call_is_consumed_only_after_its_matching_result(later_prompt):
    sequence = fixtures('claude')
    events = [history.project_event(x) for x in sequence[:2]]
    if later_prompt:
        events.append(history.project_event(raw('later-prompt', 'text', 'user', step_boundary=True)))
    pending = collect(events, 'claude')
    assert 'call' not in pending['consumed']
    assert sum(len(g['steps']) for g in pending['groups']) == 0
    assert sum(g['orphans'] for g in pending['groups']) == 0
    events.append(history.project_event(sequence[2]))
    paired = collect(events, 'claude')
    assert {'call', 'result'} <= set(paired['consumed'])
    assert len(paired['groups'][0]['steps']) == 1
    assert sum(len(g['steps']) for g in paired['groups']) == 1
