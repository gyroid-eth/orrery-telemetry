"""Private legacy adapter: resolve once, re-admit actual target before effects."""
import os
from pathlib import Path
import runpy
import sys


def main(argv):
    api=runpy.run_path(str(Path(__file__).with_name('runtime_tmux.py')))
    try:
        home=Path(os.environ.get('AGENTSTACK_HOME',str(Path.home()/'.agentstack')))
        factory=runpy.run_path(str(Path(__file__).with_name('legacy_consumer.py')))['LegacyConsumer']
        consumer=factory(home,transport_api=api)
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
