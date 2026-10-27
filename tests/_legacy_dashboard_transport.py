"""Keep dashboard unit doubles above the real consumer's transport boundary.

Real default-endpoint compatibility is separately checked with native tmux.
These fixtures supply synthetic OS evidence; they do not disable admission.
"""
from pathlib import Path
import runpy
import socket
import subprocess
import tempfile
import weakref

ROOT = Path(__file__).resolve().parents[1]


def install_legacy_transport(monkeypatch, tmp_path, server, invoke, *, sessions=None):
    api = runpy.run_path(str(ROOT / 'bin/lib/runtime_tmux.py'))
    directory = tempfile.TemporaryDirectory(prefix='r263unit-', dir='/private/tmp' if Path('/private/tmp').is_dir() else None)
    path = Path(directory.name).resolve() / 's'
    stream = socket.socket(socket.AF_UNIX)
    stream.bind(str(path))
    path.chmod(0o600)

    def evidence(argv, **kwargs):
        args = argv[3:]
        if '#{socket_path}|#{pid}' in args:
            return subprocess.CompletedProcess(argv, 0, str(path) + '|300\n', '')
        if args[0] == 'list-sessions' and args[-1] == '#{session_name}' and sessions is not None:
            rows = sessions() if callable(sessions) else sessions
            return subprocess.CompletedProcess(argv, 0, '\n'.join(rows.values()), '')
        if '#{session_id}\t#{session_name}\t#{pane_id}\t#{pane_pid}' in args:
            target = args[args.index('-t') + 1]
            rows = sessions() if callable(sessions) else sessions
            if rows is None:
                sid, name = ('$1', target.lstrip('=')) if not target.startswith(('$', '%', '@')) else ('$1', 'FixtureSession')
            else:
                match = next(((sid, name) for sid, name in rows.items() if target in (sid, '=' + name, '%' + sid[1:])), None)
                if match is None:
                    return subprocess.CompletedProcess(argv, 1, '', "can't find session")
                sid, name = match
            return subprocess.CompletedProcess(argv, 0, f'{sid}\t{name}\t%{sid[1:]}\t400\n', '')
        return invoke(['tmux', *args], **kwargs)

    transport = api['Transport'](path, binary='tmux', run=evidence)
    transport.server_pid = 300
    consumer = api['LegacyConsumer'](tmp_path or Path(directory.name).resolve(), transport=transport, api={'process_start': lambda *a, **kw: 'fixture-start'}, proof_reader=lambda *a, **kw: [])
    monkeypatch.setattr(server, '_legacy_tmux_consumer', lambda: consumer)
    # Restoring the server factory releases this consumer even on failure.
    def cleanup():
        stream.close()
        directory.cleanup()

    weakref.finalize(consumer, cleanup)
    return consumer
