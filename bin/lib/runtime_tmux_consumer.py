"""Private legacy adapter: resolve once, re-admit actual target before effects."""
import hashlib
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import time


class NotificationReceipt:
    """Prevent a restarted worker from repeating an uncertain pane effect."""

    def __init__(self, path, text):
        self.path = Path(path)
        self.digest = hashlib.sha256(text.encode()).hexdigest()
        self.identity = None
        self.phase = None
        try:
            record = json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return
        if not isinstance(record, dict):
            raise ValueError('invalid notification receipt')
        if record.get('version') == 2:
            self.phase = record['phase']
        elif isinstance(record.get('confirmed'), bool):
            # Older receipts inferred success from an empty editor on retry.
            # Keep the signal pending unless notify-state already recorded it.
            self.phase = 'enter_pending'
        if self.phase not in ('paste_pending', 'enter_pending', 'confirmed'):
            raise ValueError('invalid notification receipt')
        if self.phase != 'confirmed' and record['digest'] != self.digest:
            raise ValueError('notification changed during submission')
        socket, server, pane, process = record['pane']
        self.identity = Path(socket), server, pane, process

    def before_paste(self, identity):
        self._save(identity, 'paste_pending')

    def before_enter(self, identity):
        if self.phase != 'enter_pending':
            self._save(identity, 'enter_pending')

    def confirm(self):
        self._save(self.identity, 'confirmed')

    def _save(self, identity, phase):
        socket, server, pane, process = identity
        entry = {'version': 2, 'phase': phase, 'pane': [str(socket), server, pane, process],
                 'digest': self.digest}
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent,
                                         prefix='.notify-receipt-', delete=False) as handle:
            temporary = Path(handle.name)
            try:
                json.dump(entry, handle)
                handle.flush()
                temporary.replace(self.path)
            finally:
                temporary.unlink(missing_ok=True)
        self.identity = identity
        self.phase = phase


def submit_input(argv, make_consumer):
    if len(argv) != 2:
        return 2
    pane, filename = argv
    with Path(filename).open(encoding='utf-8') as handle:
        text = handle.read(262145)
    if not text.strip() or len(text.encode('utf-8')) > 262144:
        return 2
    statuses = {'accepted': 0, 'refused': 3, 'unknown': 4}
    path = os.environ.get('AGENTSTACK_NOTIFY_RECEIPT')
    if not path:
        return statuses[make_consumer().submit_text(pane, text)]
    try:
        receipt = NotificationReceipt(path, text)
        if receipt.phase == 'confirmed':
            return 0
        if receipt.phase is not None:
            print('notification_' + receipt.phase, file=sys.stderr)
            return 4
        budget = int(os.environ.get('AGENTSTACK_NOTIFY_TIMEOUT', '5'))
        if budget <= 0:
            raise ValueError('invalid notification timeout')
        deadline = time.monotonic() + budget
        session = os.environ.get('AGENTSTACK_NOTIFY_SESSION')
        result = make_consumer().submit_text(
            pane, text, deadline=deadline, session=session, receipt=receipt,
            on_wait=lambda reason: print(reason, file=sys.stderr))
        if result == 'accepted':
            receipt.confirm()
        elif result == 'refused' and receipt.phase == 'paste_pending':
            # This invocation proved it did not start a paste.
            receipt.path.unlink(missing_ok=True)
        return statuses[result]
    except (OSError, ValueError, KeyError, TypeError):
        print('submission_state_unavailable', file=sys.stderr)
        return 2


def main(argv):
    api=runpy.run_path(str(Path(__file__).with_name('runtime_tmux.py')))
    try:
        home=Path(os.environ.get('AGENTSTACK_HOME',str(Path.home()/'.agentstack')))
        factory=runpy.run_path(str(Path(__file__).with_name('legacy_consumer.py')))['LegacyConsumer']
        def make_consumer():
            return factory(home,transport_api=api)
        if argv and argv[0]=='submit-input':
            return submit_input(argv[1:], make_consumer)
        consumer=make_consumer()
        if argv and argv[0]=='stop-group':consumer.stop_group(argv[1]);return 0
        if argv and (argv[0] in ('attach','attach-session') or (argv[0]=='new-session' and '-A' in argv)):
            if argv[0]=='new-session' and consumer.transport.identity is None:
                api['private_directory'](consumer.transport.path.parent,create=True)
            admitted=consumer.admitted_argv(argv)
            checked=consumer.admitted_argv(argv)
            if admitted!=checked:raise api['TargetError']('TMUX_TARGET_CHANGED')
            env={k:v for k,v in os.environ.items() if k not in ('TMUX','TMUX_PANE')}
            os.execvpe(admitted[0],admitted,env)
        result=consumer.command(argv)
        sys.stdout.write(result.stdout or '');sys.stderr.write(result.stderr or '')
        return result.returncode
    except api['TargetError'] as exc:
        print(api['target_reason'](exc),file=sys.stderr)
        return 2
    except (OSError,RuntimeError,ValueError,KeyError,TypeError):
        # Do not print the command/input or credentials in exception diagnostics.
        print('GLOBAL_TARGET_OWNERSHIP_UNKNOWN',file=sys.stderr)
        return 2

if __name__=='__main__':raise SystemExit(main(sys.argv[1:]))
