"""Upgrade shell-only tmux stubs with the explicit transport's literal evidence."""
import atexit
from pathlib import Path
import os
import socket
import tempfile
import sys
import shlex

_RESOURCES=[]

def _cleanup():
    for stream,base in reversed(_RESOURCES):
        stream.close();base.cleanup()
    _RESOURCES.clear()
atexit.register(_cleanup)


def add_transport_evidence(bindir,env,*,inherited=True):
    binary=Path(bindir)/'tmux';old=binary.read_text()
    base=tempfile.TemporaryDirectory(prefix='ttx-',dir='/private/tmp' if Path('/private/tmp').is_dir() else None)
    directory=Path(base.name).resolve()
    if inherited:
        path=directory/'stub.sock'
    else:
        parent=directory/('tmux-'+str(os.getuid()));parent.mkdir(mode=0o700)
        path=parent/'default';env['TMUX_TMPDIR']=str(directory)
    stream=socket.socket(socket.AF_UNIX);stream.bind(str(path));path.chmod(0o600)
    _RESOURCES.append((stream,base))
    # This is a fixture socket, not a service; every byte of PID evidence below
    # is synthetic. The real OS-process admission has its own outer test.
    if inherited:env['TMUX']=str(path)+',300,0'
    else:env.pop('TMUX',None)
    env.pop('TMUX_PANE',None)
    env['FAKE_TMUX_SOCKET']=str(path)
    env['FAKE_TMUX_SESSION_FILE']=str(directory/'session')
    prefix='''
if [[ "${1:-}" == -S ]]; then shift 2; fi
if [[ "${1:-}" == new-session ]]; then
  previous=""
  for argument in "$@"; do
    if [[ "$previous" == -s ]]; then printf '%s' "$argument" > "$FAKE_TMUX_SESSION_FILE"; fi
    previous="$argument"
  done
fi
if [[ "${1:-}" == display-message && "$*" == *'#{socket_path}'* ]]; then
  printf '%s|300\\n' "$FAKE_TMUX_SOCKET"; exit 0
fi
if [[ "$*" == *'#{session_id}'* && "$*" == *'#{pane_pid}'* ]]; then
  name="$(cat "$FAKE_TMUX_SESSION_FILE" 2>/dev/null)"
  [[ -n "$name" ]] || exit 1
  printf '$1\\t%s\\t%%1\\t400\\n' "$name"; exit 0
fi
if [[ "${1:-}" == display-message && " $* " == *' -t '* ]]; then
  case "${!#}" in
    '#{pane_id}') printf '%s\\n' '%1'; exit 0 ;;
    '#{session_name}'|'#S')
      name="$(cat "$FAKE_TMUX_SESSION_FILE" 2>/dev/null)"
      printf '%s\\n' "${FAKE_WARM_IDENTITY_OVERRIDE:-$name}"; exit 0 ;;
  esac
fi
'''
    lines=old.splitlines(keepends=True);binary.write_text(lines[0]+prefix+''.join(lines[1:]));binary.chmod(0o755)
    ps=Path(bindir)/'ps'
    if not ps.exists():
        ps.write_text('#!/bin/sh\n[ "$*" = "-p 400 -o lstart=" ] || exit 2\nprintf "%s\\n" "fixture-start"\n');ps.chmod(0o755)
    # The caller may put its interpreter directory ahead of the stub directory;
    # its Python executable must never be mistaken for a provider.
    assert 'exec codex' not in old and 'exec claude' not in old
