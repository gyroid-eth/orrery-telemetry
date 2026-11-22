#!/usr/bin/env python3
"""通常のMail配送を変えず、測定用端末への通知時刻を採取する。"""
from __future__ import annotations

import argparse
import builtins
from contextlib import contextmanager
import hashlib
import io
import json
import math
import os
from pathlib import Path
import random
import re
import selectors
import shlex
import stat
import statistics
import subprocess
import sys
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]
PROGRAM = 'mail-notice-measure'
PHASES = ('warmup', 'normal', 'burst', 'recovery')
NAME = re.compile(r'[A-Za-z][A-Za-z0-9-]{0,127}')
NONCE = r'notice-probe/([0-9a-f-]{36})/(warmup|normal|burst|recovery)/([1-9][0-9]*)'
NOTICE = re.compile(r'ORRERY Mail notification: message from ([A-Za-z][A-Za-z0-9-]*) '
                    r'\[(low|normal|high|urgent)\]: (' + NONCE + r')\. Body ')
BEGIN, END = b'\x1b[200~', b'\x1b[201~'
SCHEMA = 1
BASELINE_WATCHER_SHA256 = 'bc3f55df03c4a947cf267e2b224256315e3cb2e12dffbfcc6050ac50452cd103'


class MeasurementError(ValueError):
    """公開可能な固定reasonだけを持つ測定失敗。"""


def identifier(value):
    if str(uuid.UUID(value)) != value:
        raise MeasurementError('invalid_uuid')
    return value


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise MeasurementError('positive_number_required')
    return number


def tag(value):
    return hashlib.sha256(value.encode()).hexdigest()


def nonce(run_id, phase, sequence):
    identifier(run_id)
    if phase not in PHASES or type(sequence) is not int or sequence < 1:
        raise MeasurementError('invalid_sequence')
    return f'notice-probe/{run_id}/{phase}/{sequence}'


def clock_info():
    return time.get_clock_info('monotonic').implementation


def write_line(fd, value):
    raw = (json.dumps(value, separators=(',', ':'), ensure_ascii=True) + '\n').encode()
    if os.write(fd, raw) != len(raw):
        raise MeasurementError('event_write_incomplete')


@contextmanager
def output_file(path):
    # 既存fileを開かず、通知や資格情報を誤って上書きしない。
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        yield fd
    finally:
        os.close(fd)


def event(base, name, **values):
    return {'schema': SCHEMA, **base, 'event': name,
            'at_ns': time.monotonic_ns(), **values}


def base_record(run_id, clock_id, sender, recipient):
    identifier(run_id); identifier(clock_id)
    if not NAME.fullmatch(sender) or not NAME.fullmatch(recipient) or sender == recipient:
        raise MeasurementError('invalid_probe_names')
    return dict(run_id=run_id, clock_id=clock_id,
                sender_tag=tag(sender), recipient_tag=tag(recipient))


class PasteParser:
    """chunk境界を跨ぐpasteを蓄え、閉じたframeの外のCRだけで確定する。"""

    def __init__(self):
        self.buffer = bytearray()
        self.pasted = bytearray()
        self.pending = []
        self.inside = False
        self.errors = 0

    def feed(self, data):
        self.buffer.extend(data)
        submitted = []
        while self.buffer:
            marker = END if self.inside else BEGIN
            if self.buffer.startswith(marker):
                del self.buffer[:len(marker)]
                if self.inside:
                    self.pending.append(bytes(self.pasted))
                    self.pasted.clear()
                self.inside = not self.inside
                continue
            # delimiterの途中なら次のreadを待つ。
            if marker.startswith(self.buffer):
                break
            byte = self.buffer.pop(0)
            if self.inside:
                self.pasted.append(byte)
            elif byte == 13:
                if self.pending:
                    submitted.append(b''.join(self.pending))
                    self.pending.clear()
            else:
                self.errors += 1
        return submitted


def notice_events(payload, base, sender, *, at_ns):
    try:
        text = payload.decode('utf-8')
    except UnicodeError:
        raise MeasurementError('invalid_notification_encoding') from None
    matches = list(NOTICE.finditer(text))
    if not matches or text[:matches[0].start()].strip():
        raise MeasurementError('invalid_notification_frame')
    result = []
    for match in matches:
        actual_sender, _, _, run_id, phase, sequence = match.groups()
        if actual_sender != sender or run_id != base['run_id']:
            raise MeasurementError('notification_binding_mismatch')
        result.append(event(base, 'receiver_notice', phase=phase,
                            sequence=int(sequence), at_ns=at_ns))
    return result


def sink(args):
    import signal
    import termios
    import tty
    base = base_record(args.run_id, args.clock_id, args.sender, args.recipient)
    if not os.isatty(0):
        raise MeasurementError('sink_tty_required')
    if args.event_fd is not None:
        fd = args.event_fd
        if fd <= 2 or not stat.S_ISFIFO(os.fstat(fd).st_mode):
            raise MeasurementError('event_pipe_required')
        close_fd = False
    else:
        info = os.lstat(args.event_fifo)
        if not stat.S_ISFIFO(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise MeasurementError('private_event_fifo_required')
        fd = os.open(args.event_fifo, os.O_WRONLY | os.O_NONBLOCK)
        close_fd = True
    previous = termios.tcgetattr(0)
    parser = PasteParser()
    def stop(signum, frame):
        raise KeyboardInterrupt
    handlers = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        tty.setraw(0)
        os.write(1, b'\x1b[?2004hmeasurement-ready\r\n')
        write_line(fd, event(base, 'sink_ready', clock_impl=clock_info(), program=PROGRAM))
        while True:
            data = os.read(0, 4096)
            if not data:
                return
            for payload in parser.feed(data):
                received = time.monotonic_ns()
                try:
                    records = notice_events(payload, base, args.sender, at_ns=received)
                except MeasurementError as error:
                    write_line(fd, event(base, 'sink_rejected', reason=str(error)))
                else:
                    for record in records:
                        write_line(fd, record)
    except KeyboardInterrupt:
        return
    finally:
        termios.tcsetattr(0, termios.TCSANOW, previous)
        os.write(1, b'\x1b[?2004l')
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
        if close_fd:
            os.close(fd)


def collect(args):
    base = base_record(args.run_id, args.clock_id, args.sender, args.recipient)
    duration = positive(args.seconds)
    os.mkfifo(args.fifo, 0o600)
    read_fd = os.open(args.fifo, os.O_RDONLY | os.O_NONBLOCK)
    keep_fd = os.open(args.fifo, os.O_WRONLY | os.O_NONBLOCK)
    pending = bytearray()
    try:
        with output_file(args.out) as out, selectors.DefaultSelector() as selected:
            selected.register(read_fd, selectors.EVENT_READ)
            deadline = time.monotonic() + duration
            while time.monotonic() < deadline:
                if not selected.select(min(1, max(0, deadline - time.monotonic()))):
                    continue
                pending.extend(os.read(read_fd, 65536))
                while b'\n' in pending:
                    line, _, rest = pending.partition(b'\n')
                    pending = bytearray(rest)
                    value = json.loads(line)
                    if (any(value.get(k) != v for k, v in base.items())
                            or value.get('event') not in ('sink_ready', 'sink_rejected', 'receiver_notice')):
                        raise MeasurementError('collector_binding_mismatch')
                    # sink由来の限定fieldだけを保存する。
                    allowed = set(base) | {'schema', 'event', 'at_ns', 'clock_impl',
                                           'program', 'phase', 'sequence', 'reason'}
                    if set(value) - allowed:
                        raise MeasurementError('unexpected_event_fields')
                    write_line(out, value)
            if pending:
                raise MeasurementError('event_line_incomplete')
            write_line(out, event(base, 'collector_end', clock_impl=clock_info()))
    finally:
        os.close(read_fd); os.close(keep_fd)
        # FIFOはこの呼出しで作った測定用のものだけ。
        Path(args.fifo).unlink()


class CanonicalProxy:
    """既存のbound stdio MCPに接続する。Mail HTTPや登録は実装しない。"""

    def __init__(self, product_root=ROOT, *, timeout=30):
        runner = Path(product_root) / 'integrations/codex_app/plugin/scripts/run-mcp.sh'
        if not runner.is_file():
            raise MeasurementError('canonical_adapter_missing')
        self.process = subprocess.Popen(['/bin/bash', str(runner)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.timeout, self.serial, self.pending = positive(timeout), 0, bytearray()
        self.selected = selectors.DefaultSelector()
        self.selected.register(self.process.stdout, selectors.EVENT_READ)

    def rpc(self, method, params):
        self.serial += 1
        request = dict(jsonrpc='2.0', id=self.serial, method=method, params=params)
        write_line(self.process.stdin.fileno(), request)
        deadline = time.monotonic() + self.timeout
        while True:
            while b'\n' in self.pending:
                line, _, rest = self.pending.partition(b'\n')
                self.pending = bytearray(rest)
                value = json.loads(line)
                if value.get('id') != self.serial:
                    continue
                if 'error' in value or not isinstance(value.get('result'), dict):
                    raise MeasurementError('canonical_proxy_error')
                return value['result']
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.selected.select(remaining):
                raise MeasurementError('canonical_proxy_timeout')
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise MeasurementError('canonical_proxy_eof')
            self.pending.extend(chunk)

    def tool(self, name, arguments):
        result = self.rpc('tools/call', dict(name=name, arguments=arguments))
        if result.get('isError'):
            raise MeasurementError('canonical_tool_unconfirmed')
        data = result.get('structuredContent')
        if not isinstance(data, dict):
            raise MeasurementError('canonical_schema_unsupported')
        return data

    def bind(self, sender, recipient):
        self.rpc('initialize', dict(protocolVersion='2025-06-18', capabilities={},
                                   clientInfo=dict(name=PROGRAM, version='1')))
        write_line(self.process.stdin.fileno(), dict(jsonrpc='2.0', method='notifications/initialized'))
        # bootstrap・register・fetchは呼ばない。
        binding = self.tool('runtime_status', {})
        if (binding.get('agent_name') != sender or binding.get('program') != PROGRAM
                or binding.get('surface') != 'direct' or binding.get('state') != 'bound'):
            raise MeasurementError('canonical_probe_binding_required')
        target = self.tool('whois', {'name': recipient})
        if target.get('name') != recipient or target.get('program') != PROGRAM or target.get('retired_at'):
            raise MeasurementError('canonical_probe_recipient_required')
        return binding

    def close(self):
        self.selected.close()
        self.process.stdin.close()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill(); self.process.wait()
        self.process.stdout.close()


def accepted_message(value, *, sender, recipient, subject, project):
    deliveries = value.get('deliveries')
    if (value.get('verified_sender') is not True or value.get('count') != 1
            or not isinstance(deliveries, list) or len(deliveries) != 1):
        raise MeasurementError('send_acceptance_unconfirmed')
    delivery = deliveries[0]
    payload = delivery.get('payload', {})
    mid = payload.get('id')
    if (delivery.get('project') != project or type(mid) is not int or mid < 1
            or payload.get('from') != sender or payload.get('to') != [recipient]
            or payload.get('subject') != subject or payload.get('cc', []) or payload.get('bcc', [])):
        raise MeasurementError('send_binding_mismatch')
    return mid


def send_series(args, *, proxy_factory=CanonicalProxy, sleep=time.sleep):
    base = base_record(args.run_id, args.clock_id, args.sender, args.recipient)
    interval = positive(args.interval)
    if args.count < 1 or not math.isfinite(args.jitter) or not 0 <= args.jitter < interval:
        raise MeasurementError('invalid_schedule')
    readiness = [json.loads(line) for line in args.receiver_events.read_text().splitlines()]
    if not any(row.get('event') == 'sink_ready' and row.get('program') == PROGRAM
               and row.get('clock_impl') == clock_info()
               and all(row.get(k) == v for k, v in base.items()) for row in readiness):
        raise MeasurementError('matching_sink_ready_required')
    generator = random.Random(args.seed)
    with output_file(args.out) as out:
        write_line(out, event(base, 'sender_plan', phase=args.phase, count=args.count,
                             clock_impl=clock_info(), interval_s=interval, jitter_s=args.jitter,
                             seed=args.seed, program=PROGRAM))
        proxy = proxy_factory(timeout=args.rpc_timeout)
        try:
            binding = proxy.bind(args.sender, args.recipient)
            next_start = time.monotonic()
            for sequence in range(1, args.count + 1):
                sleep(max(0, next_start - time.monotonic()))
                started = time.monotonic()
                subject = nonce(args.run_id, args.phase, sequence)
                write_line(out, event(base, 'sender_call', phase=args.phase, sequence=sequence))
                try:
                    response = proxy.tool('send_message', dict(to=[args.recipient], subject=subject,
                        body_md='Notification measurement: ' + subject, importance='normal',
                        ack_required=False))
                    mid = accepted_message(response, sender=args.sender, recipient=args.recipient,
                                           subject=subject, project=binding['project_key'])
                except (MeasurementError, OSError, ValueError):
                    write_line(out, event(base, 'sender_unconfirmed', phase=args.phase, sequence=sequence))
                    return 1  # unknownを再送しない。未試行件数も集計に残る。
                write_line(out, event(base, 'sender_accepted', phase=args.phase,
                                     sequence=sequence, message_id=mid))
                # 遅れた呼出しをcatch-up burstにしない。
                next_start = max(time.monotonic(), started + interval + generator.uniform(-args.jitter, args.jitter))
        finally:
            proxy.close()
    return 0


def distribution(values):
    values = sorted(values)
    return dict(n=len(values), p50_ms=statistics.median(values) if values else None,
        p95_ms=values[math.ceil(.95 * len(values)) - 1] if values else None,
        max_ms=max(values) if values else None)


def summarize(records, *, deadline_seconds=45):
    deadline_ns = round(positive(deadline_seconds) * 1_000_000_000)
    bases = {tuple(row.get(k) for k in ('run_id', 'clock_id', 'sender_tag', 'recipient_tag'))
             for row in records}
    if len(bases) != 1 or any(row.get('schema') != SCHEMA for row in records):
        raise MeasurementError('event_context_mismatch')
    implementations = {row['clock_impl'] for row in records if 'clock_impl' in row}
    if len(implementations) != 1:
        raise MeasurementError('clock_implementation_mismatch')
    for row in records:
        if type(row.get('at_ns')) is not int or row['at_ns'] < 0:
            raise MeasurementError('invalid_event_time')
    endings = [r['at_ns'] for r in records if r['event'] == 'collector_end']
    ready = [r for r in records if r['event'] == 'sink_ready' and r.get('program') == PROGRAM]
    groups = {}
    for plan in (r for r in records if r['event'] == 'sender_plan'):
        phase = plan['phase']
        if phase not in PHASES or phase in groups or type(plan['count']) is not int or plan['count'] < 1:
            raise MeasurementError('invalid_sender_plan')
        calls, acks, notices, unconfirmed = {}, {}, {}, set()
        for row in (r for r in records if r.get('phase') == phase and 'sequence' in r):
            seq = row['sequence']
            if type(seq) is not int or not 1 <= seq <= plan['count']:
                raise MeasurementError('unexpected_sequence')
            if row['event'] == 'receiver_notice':
                notices.setdefault(seq, []).append(row['at_ns'])
            elif row['event'] in ('sender_call', 'sender_accepted'):
                store = calls if row['event'] == 'sender_call' else acks
                if seq in store:
                    raise MeasurementError('duplicate_sender_event')
                store[seq] = row
            elif row['event'] == 'sender_unconfirmed':
                unconfirmed.add(seq)
        if set(acks) - set(calls) or set(notices) - set(calls):
            raise MeasurementError('notice_without_call')
        mids = [r.get('message_id') for r in acks.values()]
        if any(type(mid) is not int or mid < 1 for mid in mids) or len(set(mids)) != len(mids):
            raise MeasurementError('invalid_message_binding')
        call_ms, ack_ms, before_ack, late, missing, pending = [], [], 0, 0, 0, 0
        for seq, call in calls.items():
            if seq in acks and acks[seq]['at_ns'] < call['at_ns']:
                raise MeasurementError('ack_before_call')
            if seq not in notices:
                if endings and max(endings) >= call['at_ns'] + deadline_ns:
                    missing += 1
                else:
                    pending += 1
                continue
            first = min(notices[seq])
            if first < call['at_ns']:
                raise MeasurementError('notice_before_call')
            late += first > call['at_ns'] + deadline_ns
            call_ms.append((first - call['at_ns']) / 1_000_000)
            if seq in acks:
                lag = (first - acks[seq]['at_ns']) / 1_000_000
                ack_ms.append(lag); before_ack += lag < 0
        ordered = sorted(r['at_ns'] for r in calls.values())
        duplicate = sum(max(0, len(times) - 1) for times in notices.values())
        invalid_sink = sum(r['event'] == 'sink_rejected' for r in records)
        if set(acks) & unconfirmed:
            raise MeasurementError('contradictory_sender_result')
        # 送信中の中断では明示の結果marker自体が残らないことがある。
        unconfirmed |= set(calls) - set(acks)
        latest = max((row['at_ns'] for row in records), default=0)
        observation_closed = bool(endings) and max(endings) == latest
        groups[phase] = dict(planned=plan['count'], attempted=len(calls), accepted=len(acks),
            unconfirmed=len(unconfirmed), not_attempted=plan['count'] - len(calls),
            notified=len(notices), notice_events=sum(map(len, notices.values())),
            duplicate=duplicate, late=late, missing_at_deadline=missing, observation_pending=pending,
            notice_before_ack=before_ack, sink_rejected=invalid_sink,
            ack_to_notice=distribution(ack_ms), call_to_notice=distribution(call_ms),
            actual_start_intervals=distribution([(b-a)/1_000_000 for a,b in zip(ordered,ordered[1:])]),
            complete=bool(ready) and observation_closed and not (duplicate or late or missing or pending or invalid_sink)
                and len(acks) == len(notices) == len(calls) == plan['count'])
    if not groups:
        raise MeasurementError('sender_plan_required')
    return dict(schema=SCHEMA, run_id=next(iter(bases))[0], groups=groups,
                complete=all(g['complete'] for g in groups.values()))


def seed_data(epoch, history_count):
    state, signals = {}, {}
    for group, count, result, age in (
            ('Due', 20, 'session_not_found', 60), ('Cooldown', 15, 'session_not_found', 5),
            ('Success', 15, 'success', 60), ('Unseen', 11, None, 60)):
        for index in range(1, count + 1):
            agent, mid = 'Measure' + group, len(signals) + 1
            relative = f'signals/projects/probe/agents/{agent}/{mid}.signal'
            signals[relative] = dict(agent=agent, message=dict(id=mid, **{'from': 'MeasureSender'},
                subject='synthetic backlog', importance='normal'))
            if result:
                state[f'{agent}:{mid}'] = state_entry(agent, str(mid), result, epoch-age)
    for index in range(history_count):
        key = f'{index:08d}'
        state['MeasureHistory:' + key] = state_entry('MeasureHistory', key, 'success', epoch-60)
    return state, signals


def state_entry(agent, key, result, epoch):
    stamp = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(epoch))
    row = dict(agent=agent, msg_key=key, last_result=result, last_attempt_epoch=epoch,
               last_attempt_ts=stamp, source='watcher')
    if result == 'success':
        row.update(last_success_epoch=epoch, last_success_ts=stamp)
    return row


def state_bytes(state):
    return json.dumps(state, indent=2, ensure_ascii=False).encode('utf-8')


def normalized_state(state, epoch):
    normalized = json.loads(json.dumps(state))
    for entry in normalized.values():
        for field in ('last_attempt', 'last_success'):
            if field + '_epoch' in entry:
                value = entry[field + '_epoch']
                if entry[field + '_ts'] != time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(value)):
                    raise MeasurementError('seed_timestamp_mismatch')
                entry[field + '_epoch'] = value - epoch
                entry[field + '_ts'] = 'relative:' + str(value - epoch)
    return normalized


def seed_fixture(root, *, epoch, target_bytes=4_900_000, empty=False):
    root = Path(root)
    if type(epoch) is not int or epoch < 60 or target_bytes < 0:
        raise MeasurementError('invalid_seed_parameters')
    if empty:
        state, signals, history = {}, {}, 0
    else:
        # 同じseedで、目標byteを超える最初のentry数を二分探索する。
        low, high = 0, 1
        while len(state_bytes(seed_data(epoch, high)[0])) < target_bytes:
            high *= 2
        while low < high:
            middle = (low + high) // 2
            if len(state_bytes(seed_data(epoch, middle)[0])) >= target_bytes:
                high = middle
            else:
                low = middle + 1
        history = low
        state, signals = seed_data(epoch, history)
    raw = state_bytes(state)
    digest = tag(json.dumps(dict(state=normalized_state(state, epoch), signals=signals),
                           sort_keys=True, separators=(',', ':')))
    root.mkdir(mode=0o700)  # 既存runtimeへseedを復元しない。
    (root / 'runtime/notify-locks').mkdir(parents=True, mode=0o700)
    (root / 'runtime/notify-state.json').write_bytes(raw)
    for relative, value in signals.items():
        path = root / relative
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')
        os.utime(path, (epoch-60, epoch-60))
    manifest = dict(kind='mail-notice-measure-seed-v1', epoch=epoch, empty=empty,
        target_bytes=target_bytes, state_bytes=len(raw), history_entries=history,
        signal_count=len(signals), normalized_sha256=digest)
    (root / 'seed.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


def seed_manifest(root, *, verify=False):
    root = Path(root).resolve()
    manifest = json.loads((root / 'seed.json').read_text())
    if manifest.get('kind') != 'mail-notice-measure-seed-v1':
        raise MeasurementError('isolated_seed_required')
    if verify:
        if manifest['empty']:
            expected, signals = {}, {}
        else:
            expected, signals = seed_data(manifest['epoch'], manifest['history_entries'])
        raw = (root / 'runtime/notify-state.json').read_bytes()
        actual = json.loads(raw)
        if actual != expected:
            raise MeasurementError('seed_state_changed')
        if raw != state_bytes(expected) or len(raw) != manifest['state_bytes']:
            raise MeasurementError('seed_encoding_changed')
        found = {str(p.relative_to(root)) for p in (root / 'signals').rglob('*.signal')}
        if found != set(signals):
            raise MeasurementError('seed_signal_set_changed')
        for relative, value in signals.items():
            path = root / relative
            if json.loads(path.read_text()) != value or int(path.stat().st_mtime) != manifest['epoch']-60:
                raise MeasurementError('seed_signal_changed')
        digest = tag(json.dumps(dict(state=normalized_state(actual, manifest['epoch']), signals=signals),
                               sort_keys=True, separators=(',', ':')))
        if digest != manifest['normalized_sha256']:
            raise MeasurementError('seed_digest_mismatch')
    return root, manifest


class JsonIO:
    """隔離stateだけの論理read/writeを数え、内容は記録しない。"""

    def __init__(self, state):
        self.state = Path(state).absolute()
        self.counts = dict(state_read_calls=0, state_read_bytes=0,
                           state_write_calls=0, state_write_bytes=0)

    def __enter__(self):
        self.original_open, self.original_io = builtins.open, io.open
        builtins.open = self.wrap(self.original_open)
        io.open = self.wrap(self.original_io)
        return self

    def __exit__(self, *args):
        builtins.open, io.open = self.original_open, self.original_io

    def wrap(self, opener):
        meter = self
        class Stream:
            def __init__(self, handle, reading, writing):
                self.handle, self.reading, self.writing = handle, reading, writing
            def __getattr__(self, name):
                return getattr(self.handle, name)
            def __enter__(self):
                self.handle.__enter__()
                return self
            def __exit__(self, *args):
                return self.handle.__exit__(*args)
            def read(self, *args):
                data = self.handle.read(*args)
                if self.reading:
                    meter.counts['state_read_calls'] += 1
                    meter.counts['state_read_bytes'] += len(data.encode('utf-8') if isinstance(data, str) else data)
                return data
            def write(self, data):
                count = self.handle.write(data)
                if self.writing:
                    meter.counts['state_write_calls'] += 1
                    part = data[:count]
                    meter.counts['state_write_bytes'] += len(part.encode('utf-8') if isinstance(part, str) else part)
                return count
        def opened(file, *args, **kwargs):
            handle = opener(file, *args, **kwargs)
            if not isinstance(file, (str, bytes, os.PathLike)):
                return handle
            path = Path(os.fsdecode(file)).absolute()
            reading = path == self.state
            writing = path == self.state.with_suffix(self.state.suffix + '.tmp')
            return Stream(handle, reading, writing) if reading or writing else handle
        return opened


def append_probe(root, phase, value):
    fd = os.open(Path(root) / f'io-{phase}.jsonl', os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        write_line(fd, value)
    finally:
        os.close(fd)


def instrument_python(args):
    """既存watcherのinline Pythonだけを、隔離fixtureで観測する。"""
    root, _ = seed_manifest(args.root)
    argv = args.arguments[1:] if args.arguments[:1] == ['--'] else args.arguments
    state = root / 'runtime/notify-state.json'
    target = Path(argv[1]).absolute() if len(argv) > 1 and argv[0] == '-' else None
    state_call = target == state
    metadata = target is not None and target.is_relative_to(root / 'signals') and target.suffix == '.signal'
    if not (state_call or metadata):
        os.execv(args.python, [args.python, *argv])
    if state_call:
        operation = 'state_check' if len(argv) == 5 else 'state_result'
        key_tag = tag(argv[2] + ':' + argv[3])
    else:
        operation, key_tag = 'metadata', tag(target.parent.name + ':' + target.stem)
    observation_id = str(uuid.uuid4())
    started, code = time.monotonic_ns(), 0
    append_probe(root, args.phase, dict(event='observation_started', observation_id=observation_id,
        operation=operation, key_tag=key_tag, at_ns=started))
    namespace = {'__name__': '__main__', '__file__': '<watcher-inline>'}
    previous, sys.argv = sys.argv, argv
    meter = JsonIO(state)
    try:
        source = sys.stdin.read()
        with meter:
            exec(compile(source, '<watcher-inline>', 'exec'), namespace)
    except SystemExit as error:
        code = error.code if isinstance(error.code, int) else 1 if error.code else 0
    except BaseException:
        code = 2
        raise
    finally:
        sys.argv = previous
        entry = namespace.get('entry') or {}
        reason = ('success' if entry.get('last_result') == 'success' else 'cooldown') if code == 1 else 'eligible' if code == 0 else 'error'
        append_probe(root, args.phase, dict(event=operation, observation_id=observation_id,
            key_tag=key_tag, at_ns=started,
            elapsed_ns=time.monotonic_ns()-started, exit_code=code, decision=reason, **meter.counts))
    return code


def io_wrappers(args):
    root, _ = seed_manifest(args.root, verify=args.phase == 'first')
    if hashlib.sha256((ROOT/'hooks/watch_agent_mail_signals.sh').read_bytes()).hexdigest() != BASELINE_WATCHER_SHA256:
        raise MeasurementError('io_coverage_requires_new_helper_probe')
    python = Path(args.python)
    if not python.is_absolute() or not os.access(python, os.X_OK):
        raise MeasurementError('explicit_python_required')
    directory = root / ('io-bin-' + args.phase)
    directory.mkdir(mode=0o700)
    invocation = [str(python), str(Path(__file__).resolve()), 'instrument-python',
                  '--root', str(root), '--phase', args.phase, '--python', str(python), '--']
    (directory / 'python3').write_text('#!/bin/sh\nexec ' + shlex.join(invocation) + ' "$@"\n')
    (directory / 'python3').chmod(0o700)
    worker = [str(python), str(Path(__file__).resolve()), 'io-worker',
              '--root', str(root), '--phase', args.phase, '--']
    (directory / 'worker').write_text('#!/bin/sh\nexec ' + shlex.join(worker) + ' "$@"\n')
    (directory / 'worker').chmod(0o700)
    return dict(path_prefix=str(directory), coverage='watcher-inline-state-and-metadata-only',
                persistent_deliver=str(directory / 'worker'),
                trace=str(root / f'io-{args.phase}.jsonl'))


def io_summary(root, phase):
    root, manifest = seed_manifest(root)
    rows = [json.loads(line) for line in (root / f'io-{phase}.jsonl').read_text().splitlines()]
    checks = {}
    for row in sorted(rows, key=lambda r: r['at_ns']):
        if row['event'] == 'state_check':
            checks.setdefault(row['key_tag'], row)
    fields = ('state_read_calls', 'state_read_bytes', 'state_write_calls', 'state_write_bytes')
    starts = [r['at_ns'] for r in rows if r['event'] == 'phase_begin']
    ends = [r['at_ns'] for r in rows if r['event'] == 'phase_end']
    candidates = len({r['key_tag'] for r in rows if r['event'] == 'metadata'})
    begun, finished, invalid = {}, {}, False
    for row in rows:
        if row['event'] not in ('observation_started', 'metadata', 'state_check', 'state_result'):
            continue
        observation_id = row.get('observation_id')
        store = begun if row['event'] == 'observation_started' else finished
        if not isinstance(observation_id, str) or not observation_id or observation_id in store:
            invalid = True
        else:
            store[observation_id] = row
    for observation_id, finish in finished.items():
        begin = begun.get(observation_id)
        if (begin is None or begin.get('operation') != finish['event']
                or begin.get('key_tag') != finish.get('key_tag')
                or begin['at_ns'] != finish['at_ns'] or finish.get('elapsed_ns', -1) < 0):
            invalid = True
    unfinished = len(set(begun) - set(finished))
    workers = sum(r['event'] == 'worker_started' for r in rows)
    # eligible判定からworkerの入口までの待ちも、worker終了の証拠ではない。
    dispatches = {r['key_tag'] for r in rows
                  if r['event'] == 'state_check' and r.get('decision') == 'eligible'}
    return dict(coverage='watcher-inline-state-and-metadata-only',
        candidate_count=candidates,
        excluded_success=sum(r['decision'] == 'success' for r in checks.values()),
        excluded_cooldown=sum(r['decision'] == 'cooldown' for r in checks.values()),
        excluded_confirmed_absent=0,  # この固定baselineには一括不在選別がない。
        worker_started=workers, worker_unconfirmed=workers,
        worker_dispatch_unconfirmed=len(dispatches),
        observations_unfinished=unfinished, observation_pair_invalid=invalid,
        scan_elapsed_ns=ends[0]-starts[0] if len(starts) == len(ends) == 1 else None,
        complete=len(starts) == len(ends) == 1
            and not (unfinished or invalid or workers or dispatches)
            and (phase != 'first' or candidates >= manifest['signal_count'])
            and starts[0] <= min(r['at_ns'] for r in rows)
            and ends[0] >= max(r['at_ns'] + r.get('elapsed_ns', 0) for r in rows)
            and not any(r.get('exit_code', 0) not in (0, 1) for r in rows),
        **{field: sum(r.get(field, 0) for r in rows) for field in fields})


def cli(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('sink', 'collect', 'send'):
        command = commands.add_parser(name)
        for flag in ('run-id', 'clock-id', 'sender', 'recipient'):
            command.add_argument('--' + flag, required=True)
        if name == 'sink':
            group = command.add_mutually_exclusive_group(required=True)
            group.add_argument('--event-fd', type=int)
            group.add_argument('--event-fifo', type=Path)
        elif name == 'collect':
            command.add_argument('--fifo', type=Path, required=True)
            command.add_argument('--out', type=Path, required=True)
            command.add_argument('--seconds', type=float, required=True)
        else:
            command.add_argument('--out', type=Path, required=True)
            command.add_argument('--receiver-events', type=Path, required=True)
            command.add_argument('--phase', choices=PHASES, default='normal')
            command.add_argument('--count', type=int, default=100)
            command.add_argument('--interval', type=float, default=3)
            command.add_argument('--jitter', type=float, default=.5)
            command.add_argument('--seed', type=int, default=0)
            command.add_argument('--rpc-timeout', type=float, default=30)
    analysis = commands.add_parser('analyze')
    analysis.add_argument('events', type=Path, nargs='+')
    analysis.add_argument('--deadline-seconds', type=float, default=45)
    seed = commands.add_parser('seed')
    seed.add_argument('--root', type=Path, required=True)
    seed.add_argument('--epoch', type=int, required=True)
    seed.add_argument('--target-bytes', type=int, default=4_900_000)
    seed.add_argument('--empty', action='store_true')
    verify = commands.add_parser('verify-seed')
    verify.add_argument('--root', type=Path, required=True)
    for name in ('io-wrappers', 'instrument-python', 'io-summary', 'io-worker', 'io-mark'):
        command = commands.add_parser(name)
        command.add_argument('--root', type=Path, required=True)
        command.add_argument('--phase', choices=('first', 'next'), required=True)
        if name in ('io-wrappers', 'instrument-python'):
            command.add_argument('--python', required=True)
        if name in ('instrument-python', 'io-worker'):
            command.add_argument('arguments', nargs=argparse.REMAINDER)
        if name == 'io-mark':
            command.add_argument('--edge', choices=('begin', 'end'), required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'sink':
            sink(args)
        elif args.command == 'collect':
            collect(args)
        elif args.command == 'send':
            return send_series(args)
        elif args.command == 'analyze':
            rows = [json.loads(line) for file in args.events for line in file.read_text().splitlines()]
            result = summarize(rows, deadline_seconds=args.deadline_seconds)
            print(json.dumps(result, indent=2))
            return 0 if result['complete'] else 1
        elif args.command == 'seed':
            print(json.dumps(seed_fixture(args.root, epoch=args.epoch,
                target_bytes=args.target_bytes, empty=args.empty), indent=2))
        elif args.command == 'verify-seed':
            print(json.dumps(seed_manifest(args.root, verify=True)[1], indent=2))
        elif args.command == 'io-wrappers':
            print(json.dumps(io_wrappers(args), indent=2))
        elif args.command == 'instrument-python':
            return instrument_python(args)
        elif args.command == 'io-worker':
            root, _ = seed_manifest(args.root)
            # この口ではpersistent helperの後に続く配送worker全体の終了は証明できない。
            append_probe(root, args.phase, dict(event='worker_started',
                observation_id=str(uuid.uuid4()), completion='unconfirmed', at_ns=time.monotonic_ns()))
            arguments = args.arguments[1:] if args.arguments[:1] == ['--'] else args.arguments
            helper = ROOT / 'bin/agentstack-persistent-deliver'
            os.execv(str(helper), [str(helper), *arguments])
        elif args.command == 'io-mark':
            root, _ = seed_manifest(args.root)
            append_probe(root, args.phase, dict(event='phase_' + args.edge, at_ns=time.monotonic_ns()))
        else:
            print(json.dumps(io_summary(args.root, args.phase), indent=2))
    except (MeasurementError, OSError, ValueError, KeyError, TypeError) as error:
        # raw MCP stderr・frame・path・環境値を表示しない。
        print(str(error) if isinstance(error, MeasurementError) else type(error).__name__, file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(cli())
