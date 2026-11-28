"""Private legacy adapter: resolve once, re-admit actual target before effects."""
import json
import os
from pathlib import Path
import runpy
import stat
import sys
import time


class _NotificationHandoff:
    """Persist the watcher handoff before sending bytes to the editor."""

    def __init__(self, path, identity, key):
        self.signal = Path(path)
        self.identity = identity
        self.key = key
        runtime = Path(os.environ.get('AGENTSTACK_RUNTIME_DIR', str(Path.home()/'.agentstack/runtime')))
        self.state = runtime / 'notify-state.json'

    def current(self):
        try:
            st = self.signal.lstat()
        except FileNotFoundError:
            return False
        return (stat.S_ISREG(st.st_mode)
                and f'{st.st_dev}:{st.st_ino}:{st.st_mtime_ns}:{st.st_size}' == self.identity)

    def claim(self, *, resuming):
        import fcntl

        with open(str(self.state) + '.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not self.current():
                return False
            try:
                state = json.loads(self.state.read_text(encoding='utf-8'))
            except FileNotFoundError:
                state = {}
            if not isinstance(state, dict):
                raise ValueError('invalid watcher state')
            entry = state.setdefault(self.key, {})
            if not isinstance(entry, dict):
                raise ValueError('invalid watcher entry')
            if entry.get('last_result') == 'success' or entry.get('terminal'):
                return False
            if entry.get('editor_handoff_epoch'):
                return resuming
            old_failure = (entry.get('last_result') == 'submit_failed'
                           and entry.get('wait_reason') in {
                               'draft_not_confirmed', 'enter_not_confirmed', 'enter_failed', 'paste_failed',
                           })
            entry['editor_handoff_epoch'] = int(time.time())
            tmp = self.state.with_suffix('.json.tmp')
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
            tmp.replace(self.state)
            return resuming or not old_failure


def main(argv):
    api=runpy.run_path(str(Path(__file__).with_name('runtime_tmux.py')))
    try:
        home=Path(os.environ.get('AGENTSTACK_HOME',str(Path.home()/'.agentstack')))
        factory=runpy.run_path(str(Path(__file__).with_name('legacy_consumer.py')))['LegacyConsumer']
        consumer=factory(home,transport_api=api)
        if argv and argv[0]=='stop-group':consumer.stop_group(argv[1]);return 0
        if argv and argv[0]=='submit-input':
            notice = None
            prepare_mcp = (len(argv) == 4 and argv[3] == '--prepare-agy-mcp'
                           and argv[1] == os.environ.get('TMUX_PANE'))
            if len(argv) == 7 and argv[3] == '--notice':
                notice = _NotificationHandoff(*argv[4:])
            elif len(argv) != 3 and not prepare_mcp:
                return 2
            with Path(argv[2]).open(encoding='utf-8') as handle:
                text=handle.read(262145)
            if not text.strip() or len(text.encode('utf-8'))>262144:return 2
            result = consumer.submit_text(argv[1], text, on_wait=lambda reason: print(reason, file=sys.stderr),
                                          notice=notice, **({'prepare_mcp': True} if prepare_mcp else {}))
            return {'accepted':0,'refused':3,'unknown':4}[result]
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
