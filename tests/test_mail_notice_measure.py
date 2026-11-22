"""通知計測器の負例と、未変更watcherから実TTYまでの隔離正例。"""
import importlib.util
import json
import os
from pathlib import Path
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/measure_mail_notice.py'
spec = importlib.util.spec_from_file_location('notice_measure', SCRIPT)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
RUN, CLOCK = '11111111-1111-4111-8111-111111111111', '22222222-2222-4222-8222-222222222222'
BASE = m.base_record(RUN, CLOCK, 'ProbeSender', 'ProbeReceiver')


def prompt(sequence=1, *, sender='ProbeSender', phase='normal'):
    label = m.nonce(RUN, phase, sequence)
    return (f'ORRERY Mail notification: message from {sender} [normal]: {label}. '
            f'Body (complete; no inbox fetch needed): Notification measurement: {label}').encode()


def row(name, at, **fields):
    return dict(schema=1, **BASE, event=name, at_ns=at, **fields)


def records():
    return [row('sink_ready', 1, program=m.PROGRAM, clock_impl=m.clock_info()),
        row('sender_plan', 2, phase='normal', count=1, clock_impl=m.clock_info()),
        row('sender_call', 10_000_000, phase='normal', sequence=1),
        row('receiver_notice', 12_000_000, phase='normal', sequence=1),
        row('sender_accepted', 15_000_000, phase='normal', sequence=1, message_id=41),
        row('collector_end', 60_000_000_000, clock_impl=m.clock_info())]


@pytest.mark.parametrize('split', range(len(m.BEGIN) + len(m.END) + 1))
def test_paste_and_submit_survive_chunk_boundaries(split):
    parser = m.PasteParser()
    packet = m.BEGIN + prompt() + m.END
    assert parser.feed(packet[:split]) == []
    assert parser.feed(packet[split:]) == []
    assert parser.feed(b'\r') == [prompt()]
    assert parser.feed(b'\r') == []


def test_incomplete_paste_and_inner_cr_are_not_notices():
    parser = m.PasteParser()
    assert parser.feed(m.BEGIN + prompt() + b'\r') == []
    assert parser.feed(m.END) == []
    assert parser.feed(b'\r') == [prompt() + b'\r']


def test_one_submit_can_carry_two_envelopes_but_body_nonce_is_not_duplicate():
    parser = m.PasteParser()
    values = parser.feed(m.BEGIN + prompt() + m.END + m.BEGIN + prompt(2) + m.END + b'\r')
    events = m.notice_events(values[0], BASE, 'ProbeSender', at_ns=99)
    assert [e['sequence'] for e in events] == [1, 2]
    assert [e['at_ns'] for e in events] == [99, 99]
    assert len(m.notice_events(prompt() + prompt(), BASE, 'ProbeSender', at_ns=99)) == 2


@pytest.mark.parametrize('payload', [b'not a notification', prompt(sender='OtherSender'), b'\xff'])
def test_wrong_frame_or_sender_is_rejected(payload):
    with pytest.raises(m.MeasurementError):
        m.notice_events(payload, BASE, 'ProbeSender', at_ns=99)


def test_negative_ack_latency_is_preserved():
    summary = m.summarize(records())
    group = summary['groups']['normal']
    assert summary['complete'] and group['notice_before_ack'] == 1
    assert group['ack_to_notice'] == dict(n=1, p50_ms=-3, p95_ms=-3, max_ms=-3)
    assert group['call_to_notice']['p50_ms'] == 2


def test_missing_duplicate_late_and_unfinished_observation_are_not_green():
    missing = [r for r in records() if r['event'] != 'receiver_notice']
    assert m.summarize(missing)['groups']['normal']['missing_at_deadline'] == 1
    unfinished = [r for r in missing if r['event'] != 'collector_end']
    assert m.summarize(unfinished)['groups']['normal']['observation_pending'] == 1
    duplicate = records() + [row('receiver_notice', 20_000_000, phase='normal', sequence=1)]
    assert m.summarize(duplicate)['groups']['normal']['duplicate'] == 1
    late = records()
    late[3] = row('receiver_notice', 50_000_000_000, phase='normal', sequence=1)
    result = m.summarize(late)
    assert not result['complete'] and result['groups']['normal']['late'] == 1
    assert result['groups']['normal']['call_to_notice']['p95_ms'] == 49_990


@pytest.mark.parametrize('change', ['clock', 'before-call', 'before-ack', 'duplicate-id'])
def test_inconsistent_clock_or_sender_evidence_is_rejected(change):
    values = records()
    if change == 'clock':
        values[3]['clock_id'] = RUN
    elif change == 'before-call':
        values[3]['at_ns'] = 3
    elif change == 'before-ack':
        values[4]['at_ns'] = 3
    else:
        values.append(dict(values[4]))
    with pytest.raises(m.MeasurementError):
        m.summarize(values)


def test_unaccepted_sender_and_unattempted_messages_remain_visible():
    values = [r for r in records() if r['event'] not in ('sender_accepted', 'receiver_notice')]
    values[1]['count'] = 3
    values.append(row('sender_unconfirmed', 20_000_000, phase='normal', sequence=1))
    result = m.summarize(values)['groups']['normal']
    assert not result['complete']
    assert (result['planned'], result['unconfirmed'], result['not_attempted']) == (3, 1, 2)


@pytest.mark.parametrize('explicit', [False, True])
def test_call_without_acceptance_is_unconfirmed_with_or_without_result_marker(explicit):
    values = [r for r in records() if r['event'] != 'sender_accepted']
    values[1]['count'] = 3
    if explicit:
        values.append(row('sender_unconfirmed', 20_000_000, phase='normal', sequence=1))
    summary = m.summarize(values)
    result = summary['groups']['normal']
    assert (result['attempted'], result['accepted'], result['unconfirmed'], result['not_attempted']) == (1, 0, 1, 2)
    assert result['notified'] == 1 and not summary['complete']


def test_seed_equivalence_is_relative_but_changes_are_detected(tmp_path):
    first = m.seed_fixture(tmp_path / 'before', epoch=1_700_000_000, target_bytes=30_000)
    after = m.seed_fixture(tmp_path / 'after', epoch=1_700_000_100, target_bytes=30_000)
    assert first['normalized_sha256'] == after['normalized_sha256']
    assert first['signal_count'] == after['signal_count'] == 61
    assert first['state_bytes'] >= 30_000 and first['history_entries'] > 0
    smaller = m.state_bytes(m.seed_data(first['epoch'], first['history_entries']-1)[0])
    assert len(smaller) < 30_000
    root, _ = m.seed_manifest(tmp_path / 'before', verify=True)
    data = json.loads((root / 'runtime/notify-state.json').read_text())
    assert len([x for x in data.values() if x['last_result'] == 'session_not_found']) == 35
    data['MeasureDue:1']['last_result'] = 'success'
    (root / 'runtime/notify-state.json').write_text(json.dumps(data))
    with pytest.raises(m.MeasurementError, match='seed_state_changed'):
        m.seed_manifest(root, verify=True)
    with pytest.raises(FileExistsError):
        m.seed_fixture(root, epoch=first['epoch'])


def test_json_meter_counts_repeated_utf8_reads_and_atomic_temp_write(tmp_path):
    state = tmp_path / 'notify-state.json'
    initial = json.dumps({'例': '値'}, ensure_ascii=False)
    state.write_text(initial)
    replacement = json.dumps({'例': '新しい値'}, ensure_ascii=False)
    with m.JsonIO(state) as meter:
        with open(state, encoding='utf-8') as stream:
            assert json.load(stream) == {'例': '値'}
        assert state.read_text() == initial
        temporary = state.with_suffix('.json.tmp')
        temporary.write_text(replacement)
        temporary.replace(state)
    assert meter.counts == dict(state_read_calls=2, state_read_bytes=2*len(initial.encode()),
        state_write_calls=1, state_write_bytes=len(replacement.encode()))


def test_seed_verification_rejects_changed_initial_encoding(tmp_path):
    root = tmp_path / 'seed'
    m.seed_fixture(root, epoch=1_700_000_000, target_bytes=0)
    state = root / 'runtime/notify-state.json'
    state.write_text(json.dumps(json.loads(state.read_text()), separators=(',', ':')))
    with pytest.raises(m.MeasurementError, match='seed_encoding_changed'):
        m.seed_manifest(root, verify=True)


@pytest.mark.parametrize('end', [None, 120, 151])
@pytest.mark.parametrize('workers', [False, True])
def test_io_summary_counts_all_helpers_and_waits_for_completion(tmp_path, end, workers):
    root = tmp_path / 'seed'
    m.seed_fixture(root, epoch=1_700_000_000, empty=True)
    samples = [dict(event='phase_begin', at_ns=10),
        dict(event='metadata', key_tag='a', at_ns=11),
        dict(event='metadata', key_tag='a', at_ns=12),
        dict(event='metadata', key_tag='b', at_ns=13),
        dict(event='state_check', key_tag='a', at_ns=20, elapsed_ns=2,
             decision='success', exit_code=1, state_read_calls=1, state_read_bytes=100),
        dict(event='state_check', key_tag='a', at_ns=23, elapsed_ns=2,
             decision='success', exit_code=1, state_read_calls=1, state_read_bytes=100),
        dict(event='state_check', key_tag='b', at_ns=26, elapsed_ns=2,
             decision='cooldown', exit_code=1, state_read_calls=1, state_read_bytes=100),
        dict(event='worker_started', at_ns=30), dict(event='worker_started', at_ns=31),
        dict(event='state_result', key_tag='c', at_ns=110, elapsed_ns=40, exit_code=0,
             state_read_calls=1, state_read_bytes=100, state_write_calls=1, state_write_bytes=120)]
    if end is not None:
        samples.append(dict(event='phase_end', at_ns=end))
    for index, sample in enumerate(samples):
        if sample['event'] == 'worker_started' and not workers:
            continue
        if sample['event'] in ('metadata', 'state_check', 'state_result'):
            sample['observation_id'] = str(index)
            sample.setdefault('elapsed_ns', 0)
            m.append_probe(root, 'first', dict(event='observation_started', observation_id=str(index),
                operation=sample['event'], key_tag=sample['key_tag'], at_ns=sample['at_ns']))
        m.append_probe(root, 'first', sample)
    result = m.io_summary(root, 'first')
    assert result['candidate_count'] == 2
    assert (result['excluded_success'], result['excluded_cooldown']) == (1, 1)
    assert result['worker_started'] == result['worker_unconfirmed'] == (2 if workers else 0)
    assert (result['state_read_calls'], result['state_read_bytes']) == (4, 400)
    assert (result['state_write_calls'], result['state_write_bytes']) == (1, 120)
    assert result['observations_unfinished'] == 0 and not result['observation_pair_invalid']
    assert result['complete'] is (end == 151 and not workers)


@pytest.mark.parametrize('extra', [False, True])
def test_collector_preserves_events_and_rejects_extra_fields(tmp_path, extra):
    fifo, output = tmp_path / 'notice.fifo', tmp_path / 'receiver.jsonl'
    process = subprocess.Popen([sys.executable, '-I', '-B', str(SCRIPT), 'collect',
        '--run-id', RUN, '--clock-id', CLOCK, '--sender', 'ProbeSender',
        '--recipient', 'ProbeReceiver', '--fifo', str(fifo), '--out', str(output),
        '--seconds', '1'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 5
        fd = None
        while time.monotonic() < deadline and process.poll() is None:
            try:
                fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
                break
            except (FileNotFoundError, OSError):
                time.sleep(.01)
        assert fd is not None
        try:
            value = records()[0]
            if extra:
                value['body'] = 'synthetic-unrecordable-content'
            m.write_line(fd, value)
        finally:
            os.close(fd)
        _, stderr = process.communicate(timeout=5)
        assert not fifo.exists()
        saved = [json.loads(line) for line in output.read_text().splitlines()]
        if extra:
            assert process.returncode == 2 and 'unexpected_event_fields' in stderr
            assert saved == []
        else:
            assert process.returncode == 0, stderr
            assert saved[0] == value and saved[1]['event'] == 'collector_end'
            assert saved[1]['at_ns'] > saved[0]['at_ns']
        assert 'synthetic-unrecordable-content' not in output.read_text() + stderr
    finally:
        if process.poll() is None:
            process.terminate(); process.communicate(timeout=5)


def test_real_inline_state_helper_is_counted_without_changing_return(tmp_path):
    root = tmp_path / 'seed'
    m.seed_fixture(root, epoch=int(time.time()), target_bytes=0)
    source = (ROOT / 'hooks/watch_agent_mail_signals.sh').read_text()
    start = source.index("import json, sys, time\n", source.index('state_should_attempt()'))
    block = source[start:source.index('\nPY\n', start)]
    result = subprocess.run([sys.executable, '-I', '-B', str(SCRIPT), 'instrument-python',
        '--root', str(root), '--phase', 'first', '--python', sys.executable, '--', '-',
        str(root / 'runtime/notify-state.json'), 'MeasureSuccess', '36', '30'],
        input=block, text=True, capture_output=True, timeout=5)
    assert result.returncode == 1, result.stderr
    begin, output = [json.loads(line) for line in (root / 'io-first.jsonl').read_text().splitlines()]
    assert begin['event'] == 'observation_started'
    assert begin['observation_id'] == output['observation_id']
    assert begin['operation'] == output['event'] == 'state_check'
    assert output['decision'] == 'success'
    assert output['state_read_calls'] == 1 and output['state_read_bytes'] > 0
    assert 'MeasureSuccess' not in (root / 'io-first.jsonl').read_text()


def test_interrupted_inline_probe_remains_unfinished_after_phase_end(tmp_path):
    root = tmp_path / 'seed'
    m.seed_fixture(root, epoch=1_700_000_000, empty=True)
    m.append_probe(root, 'next', dict(event='phase_begin', at_ns=time.monotonic_ns()))
    source = tmp_path / 'inline.py'
    source.write_text('import time\ntime.sleep(60)\n')
    with source.open() as stdin:
        process = subprocess.Popen([sys.executable, '-I', '-B', str(SCRIPT), 'instrument-python',
            '--root', str(root), '--phase', 'next', '--python', sys.executable, '--', '-',
            str(root / 'runtime/notify-state.json'), 'MeasureSuccess', '36', '30'],
            stdin=stdin, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 5
        trace = root / 'io-next.jsonl'
        while time.monotonic() < deadline and process.poll() is None:
            if 'observation_started' in trace.read_text():
                break
            time.sleep(.01)
        assert process.poll() is None and 'observation_started' in trace.read_text()
        m.append_probe(root, 'next', dict(event='phase_end', at_ns=time.monotonic_ns()))
        active = m.io_summary(root, 'next')
        assert not active['complete'] and active['observations_unfinished'] == 1
        process.terminate(); process.wait(timeout=5)
        interrupted = m.io_summary(root, 'next')
        assert not interrupted['complete'] and interrupted['observations_unfinished'] == 1
        assert not interrupted['observation_pair_invalid']
    finally:
        if process.poll() is None:
            process.kill(); process.wait(timeout=5)


def test_worker_start_is_not_completed_by_persistent_helper_or_state_result(tmp_path):
    root = tmp_path / 'seed'
    m.seed_fixture(root, epoch=1_700_000_000, empty=True)
    for sample in [dict(event='phase_begin', at_ns=1),
        dict(event='worker_started', observation_id='worker-1', at_ns=2),
        dict(event='persistent_helper_finished', observation_id='worker-1', at_ns=3),
        dict(event='observation_started', observation_id='state-1', operation='state_result', key_tag='k', at_ns=4),
        dict(event='state_result', observation_id='state-1', key_tag='k', at_ns=4, elapsed_ns=1, exit_code=0),
        dict(event='phase_end', at_ns=6)]:
        m.append_probe(root, 'next', sample)
    result = m.io_summary(root, 'next')
    assert result['observations_unfinished'] == 0
    assert result['worker_unconfirmed'] == 1 and not result['complete']


def test_eligible_check_before_worker_entry_is_still_unconfirmed(tmp_path):
    root = tmp_path / 'seed'
    m.seed_fixture(root, epoch=1_700_000_000, empty=True)
    for sample in [dict(event='phase_begin', at_ns=1),
        dict(event='observation_started', observation_id='check-1', operation='state_check', key_tag='k', at_ns=2),
        dict(event='state_check', observation_id='check-1', key_tag='k', at_ns=2, elapsed_ns=1,
             exit_code=0, decision='eligible'), dict(event='phase_end', at_ns=4)]:
        m.append_probe(root, 'next', sample)
    result = m.io_summary(root, 'next')
    assert result['observations_unfinished'] == result['worker_started'] == 0
    assert result['worker_dispatch_unconfirmed'] == 1 and not result['complete']


@pytest.mark.parametrize('phase', ['first', 'next'])
@pytest.mark.parametrize('elapsed', [None, 25, 60])
def test_unfinished_span_is_detected_after_all_seed_metadata(tmp_path, phase, elapsed):
    root = tmp_path / 'seed'
    m.seed_fixture(root, epoch=1_700_000_000, target_bytes=0)
    m.append_probe(root, phase, dict(event='phase_begin', at_ns=1))
    for index in range(61):
        base = dict(observation_id=str(index), key_tag=str(index), at_ns=10+index*2)
        m.append_probe(root, phase, dict(event='observation_started', operation='metadata', **base))
        m.append_probe(root, phase, dict(event='metadata', elapsed_ns=1, exit_code=0, **base))
    pending = dict(observation_id='pending', key_tag='pending-key', at_ns=200)
    m.append_probe(root, phase, dict(event='observation_started', operation='state_result', **pending))
    m.append_probe(root, phase, dict(event='phase_end', at_ns=250))
    result = m.io_summary(root, phase)
    assert result['candidate_count'] == 61
    assert result['observations_unfinished'] == 1 and not result['complete']
    if elapsed is not None:
        m.append_probe(root, phase, dict(event='state_result', elapsed_ns=elapsed, exit_code=0, **pending))
        result = m.io_summary(root, phase)
        assert result['observations_unfinished'] == 0
        assert result['complete'] is (elapsed == 25)


def test_io_wrappers_refuse_uncovered_watcher_source(tmp_path, monkeypatch):
    root, product = tmp_path / 'seed', tmp_path / 'product'
    m.seed_fixture(root, epoch=1_700_000_000, empty=True)
    watcher = product / 'hooks/watch_agent_mail_signals.sh'
    watcher.parent.mkdir(parents=True)
    watcher.write_text('changed helper layout')
    monkeypatch.setattr(m, 'ROOT', product)
    with pytest.raises(m.MeasurementError, match='io_coverage_requires_new_helper_probe'):
        m.io_wrappers(SimpleNamespace(root=root, phase='first', python=sys.executable))
    assert not (root / 'io-bin-first').exists()


def test_first_io_phase_without_seed_candidates_is_not_complete(tmp_path):
    root = tmp_path / 'seed'
    m.seed_fixture(root, epoch=1_700_000_000, target_bytes=0)
    m.append_probe(root, 'first', dict(event='phase_begin', at_ns=1))
    m.append_probe(root, 'first', dict(event='phase_end', at_ns=2))
    result = m.io_summary(root, 'first')
    assert result['candidate_count'] == result['state_read_bytes'] == 0
    assert not result['complete']


def proxy_fixture(tmp_path, *, program=m.PROGRAM):
    product = tmp_path / 'proxy-product'
    runner = product / 'integrations/codex_app/plugin/scripts/run-mcp.sh'
    runner.parent.mkdir(parents=True)
    helper = tmp_path / 'bound-proxy.py'
    helper.write_text('''import sys
sys.path.insert(0, sys.argv[1])
from agentstack_codex_app.mcp_server import AgentStackProxy, StdioMcpServer
class Snapshots:
    def get(self, key): return None
class Mail:
    def whois(self, **kw):
        return dict(name=kw['agent_name'],program='mail-notice-measure',retired_at=None)
    def send_message(self, **kw):
        assert kw['registration_token']=='synthetic-owner'
        if kw['subject'].endswith('/2'): raise ValueError('synthetic-private-diagnostic')
        return dict(count=1,verified_sender=True,deliveries=[dict(project='fixture-project',payload={
            'id':41,'from':kw['agent_name'],'to':kw['to'],'subject':kw['subject'],'cc':[],'bcc':[]})])
proxy=AgentStackProxy(None,Snapshots(),Mail())
proxy.bind_direct(agent_name='ProbeSender',project_key='fixture-project',
    owner_token='synthetic-owner',program=sys.argv[2])
StdioMcpServer(proxy).serve_forever()
''')
    runner.write_text('#!/bin/sh\nexec ' + shlex.join([sys.executable, '-I', '-B', str(helper),
        str(ROOT/'integrations/codex_app/src'), program]) + '\n')
    return product


def test_existing_bound_stdio_server_and_no_retry_after_unconfirmed_send(tmp_path):
    product = proxy_fixture(tmp_path)
    ready = tmp_path / 'receiver.jsonl'
    ready.write_text(json.dumps(row('sink_ready', 1, program=m.PROGRAM, clock_impl=m.clock_info()))+'\n')
    out = tmp_path / 'sender.jsonl'
    args = SimpleNamespace(run_id=RUN,clock_id=CLOCK,sender='ProbeSender',recipient='ProbeReceiver',
        phase='normal',count=3,interval=.1,jitter=0,seed=0,out=out,receiver_events=ready,rpc_timeout=5)
    result = m.send_series(args, proxy_factory=lambda **kw:m.CanonicalProxy(product,**kw), sleep=lambda _:None)
    assert result == 1
    values = [json.loads(line) for line in out.read_text().splitlines()]
    assert [r['event'] for r in values] == ['sender_plan','sender_call','sender_accepted',
                                          'sender_call','sender_unconfirmed']
    assert values[2]['message_id'] == 41
    assert 'synthetic-private' not in out.read_text() and 'synthetic-owner' not in out.read_text()


def test_canonical_proxy_refuses_other_program_and_missing_adapter(tmp_path):
    product = proxy_fixture(tmp_path, program='claude-code')
    proxy = m.CanonicalProxy(product,timeout=5)
    try:
        with pytest.raises(m.MeasurementError,match='canonical_probe_binding_required'):
            proxy.bind('ProbeSender','ProbeReceiver')
    finally:
        proxy.close()
    with pytest.raises(m.MeasurementError,match='canonical_adapter_missing'):
        m.CanonicalProxy(tmp_path/'absent')


@pytest.mark.parametrize('mutation', ['id', 'verified', 'to', 'from', 'project'])
def test_wrong_send_acceptance_is_not_counted(mutation):
    response = dict(verified_sender=True,count=1,deliveries=[dict(project='fixture',payload={
        'id':41,'from':'ProbeSender','to':['ProbeReceiver'],'subject':'probe'})])
    delivery = response['deliveries'][0]
    if mutation == 'verified': response['verified_sender'] = False
    elif mutation == 'project': delivery['project'] = 'other'
    else: delivery['payload'][mutation] = {'id':None,'to':['OtherReceiver'],'from':'OtherSender'}[mutation]
    with pytest.raises(m.MeasurementError):
        m.accepted_message(response,sender='ProbeSender',recipient='ProbeReceiver',subject='probe',project='fixture')


def receive_line(fd, pending, timeout=5):
    deadline = time.monotonic() + timeout
    while b'\n' not in pending:
        remaining = deadline - time.monotonic()
        assert remaining > 0 and select.select([fd], [], [], remaining)[0], 'event timeout'
        pending.extend(os.read(fd, 65536))
    line, _, rest = pending.partition(b'\n')
    pending[:] = rest
    return json.loads(line)


def sink_arguments(*tail):
    return [sys.executable, '-I', '-B', str(SCRIPT), 'sink', '--run-id', RUN,
        '--clock-id', CLOCK, '--sender', 'ProbeSender', '--recipient', 'ProbeReceiver', *tail]


def test_real_tty_no_echo_and_no_notice_before_closed_paste_and_cr():
    import termios
    master, slave = os.openpty()
    read_fd, write_fd = os.pipe()
    previous = termios.tcgetattr(slave)
    process = subprocess.Popen(sink_arguments('--event-fd', str(write_fd)), stdin=slave,
        stdout=slave, stderr=subprocess.PIPE, pass_fds=(write_fd,))
    pending = bytearray()
    try:
        assert receive_line(read_fd, pending)['event'] == 'sink_ready'
        assert not termios.tcgetattr(slave)[3] & termios.ECHO
        os.write(master, m.BEGIN + prompt() + b'\r')
        assert not select.select([read_fd], [], [], .1)[0]
        os.write(master, m.END)
        assert not select.select([read_fd], [], [], .1)[0]
        os.write(master, b'\r')
        assert receive_line(read_fd, pending)['event'] == 'receiver_notice'
        screen = os.read(master, 65536)
        assert b'measurement-ready' in screen and b'ORRERY Mail' not in screen
    finally:
        process.terminate()
        process.wait(timeout=5)
        restored = termios.tcgetattr(slave)
        # macOSはdriverのEXTPROC bitを付けることがある。変更したTTY規則を確認する。
        mask = termios.ECHO | termios.ICANON | termios.ISIG | termios.IEXTEN
        assert restored[3] & mask == previous[3] & mask
        assert restored[0] == previous[0] and restored[6] == previous[6]
        process.stderr.close()
        for fd in (master, slave, read_fd, write_fd):
            os.close(fd)


def test_unchanged_watcher_delivers_into_real_tmux_tty_sink(tmp_path):
    if os.name != 'posix':
        pytest.skip('POSIX TTY required')
    tmux = shutil.which('tmux', path='/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin')
    if not tmux:
        pytest.skip('tmux required for unchanged watcher integration')
    # コードrepoのpayloadだけを、一時HOMEへ配置する。
    sys.path.insert(0, str(ROOT / 'tests'))
    from _installed_client_payload import copy_client_library
    home = tmp_path / 'home'
    install = home / '.agentstack'
    home.mkdir()
    copy_client_library(install)
    watcher = install / 'hooks/watch_agent_mail_signals.sh'
    shutil.copyfile(ROOT / 'hooks/watch_agent_mail_signals.sh', watcher)
    runtime, signals = tmp_path / 'runtime', tmp_path / 'signals'
    command_bin = tmp_path / 'bin'
    command_bin.mkdir()
    (command_bin / 'python3').write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' "$@"\n')
    (command_bin / 'python3').chmod(0o700)
    (command_bin / 'fswatch').write_text('#!/bin/sh\nexec /bin/sleep 60\n')
    (command_bin / 'fswatch').chmod(0o700)
    with tempfile.TemporaryDirectory(prefix='notice-tty-', dir='/private/tmp') as socket_root:
        env = dict(HOME=str(home), PATH=str(command_bin)+':'+str(Path(tmux).parent)+':/usr/bin:/bin',
            TMUX_TMPDIR=socket_root, TMPDIR=str(tmp_path), AGENTSTACK_HOME=str(install),
            AGENTSTACK_RUNTIME_MODE='legacy', AGENTSTACK_RUNTIME_DIR=str(runtime),
            AGENTSTACK_SIGNALS_DIR=str(signals), AGENTSTACK_MAIL_HOME=str(tmp_path/'mail'),
            # 既定と異なる明示lockにし、実際の旧共有lockを参照させない。
            AGENTSTACK_MAIL_WATCHER_LOCK_DIR=str(tmp_path/'watcher-singleton'),
            AGENTSTACK_PERSISTENT_DELIVER=str(ROOT/'bin/agentstack-persistent-deliver'))
        fifo = tmp_path / 'events.fifo'
        os.mkfifo(fifo, 0o600)
        read_fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        keep_fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        pending, process = bytearray(), None
        def terminal(*args):
            return subprocess.run([tmux, '-f', '/dev/null', *args], env=env,
                capture_output=True, text=True, timeout=10)
        try:
            result = terminal('new-session', '-d', '-P', '-F', '#{pane_id}', '-s', 'ProbeReceiver',
                              shlex.join(sink_arguments('--event-fifo', str(fifo))))
            assert result.returncode == 0, result.stderr
            pane_id = result.stdout.strip()
            assert pane_id.startswith('%') and pane_id[1:].isdigit(), result.stdout
            assert receive_line(read_fd, pending)['event'] == 'sink_ready'
            admission = subprocess.run([sys.executable, '-B', str(install/'bin/lib/runtime_tmux_consumer.py'),
                'has-session', '-t', 'ProbeReceiver'],env=env,capture_output=True,text=True,timeout=10)
            assert admission.returncode == 0, admission.stderr
            signal_file = signals / 'projects/probe/agents/ProbeReceiver/777.signal'
            signal_file.parent.mkdir(parents=True)
            signal_file.write_text(json.dumps(dict(message=dict(id=777, **{'from':'ProbeSender'},
                subject=m.nonce(RUN, 'normal', 1), importance='normal',
                body_snippet='synthetic measurement', body_truncated=False))))
            with (tmp_path/'watcher.log').open('wb') as log:
                process = subprocess.Popen(['/bin/bash', str(watcher)], env=env,
                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                notice = receive_line(read_fd, pending, timeout=30)
                assert notice['event'] == 'receiver_notice' and notice['sequence'] == 1
                deadline = time.monotonic() + 15
                while signal_file.exists() and time.monotonic() < deadline:
                    time.sleep(.1)
                assert not signal_file.exists(), (tmp_path/'watcher.log').read_text()
                state = json.loads((runtime/'notify-state.json').read_text())
                assert state['ProbeReceiver:777']['last_result'] == 'success'
                assert not select.select([read_fd], [], [], .2)[0], 'duplicate receiver event'
                screen = terminal('capture-pane', '-p', '-t', pane_id)
                assert screen.returncode == 0, screen.stderr
                assert 'measurement-ready' in screen.stdout and 'ORRERY Mail' not in screen.stdout
                assert process.poll() is None
        finally:
            if process is not None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL); process.wait()
            terminal('kill-server')  # 自分の一時socketだけ。
            os.close(read_fd); os.close(keep_fd)
