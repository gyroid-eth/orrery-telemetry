"""Dashboard unit queries shared by the isolated installer fixtures."""

import shlex
import sys
from pathlib import Path


DASHBOARD_UNIT_SHOW = r'''
if [ "$2" = show ] && [ "$3" = --all ]; then
  for unit in "$@"; do :; done
  case "$unit" in
    *.agentdashboard.service)
      fragment="$HOME/.config/systemd/user/$unit"
      if [ -f "$fragment" ]; then
        pid=0
        if [ -f "${AGENTSTACK_TEST_SERVICE_PID:-}" ]; then
          pid=$(cat "$AGENTSTACK_TEST_SERVICE_PID")
        fi
        printf 'LoadState=loaded\nFragmentPath=%s\nMainPID=%s\n' "$fragment" "$pid"
        exit 0
      fi
      printf 'LoadState=not-found\nFragmentPath=\nMainPID=0\n'
      exit 1
      ;;
  esac
fi
'''


DASHBOARD_BUSCTL = f"#!/bin/sh\nexec {shlex.quote(sys.executable)} - \"$@\" <<'PY'\n" + '''
import json
import os
from pathlib import Path
import shlex
import sys

unit = os.environ["AGENTSTACK_LABEL_PREFIX"] + ".agentdashboard.service"
if sys.argv[3] == "call":
    assert sys.argv[-3:] == ["LoadUnit", "s", unit]
    print(json.dumps({"type": "o", "data": ["/test/agentdashboard"]}))
else:
    assert sys.argv[3] == "get-property" and sys.argv[-1] == "ExecStart"
    fragment = Path.home() / ".config/systemd/user" / unit
    (command,) = [line.split("=", 1)[1] for line in fragment.read_text().splitlines()
                  if line.startswith("ExecStart=")]
    argv = shlex.split(command)
    argv[0] = argv[0].removeprefix(":")
    print(json.dumps({"type": "a(sasbttttuii)",
                      "data": [[argv[0], argv, False, 0, 0, 0, 0, 0, 0, 0]]}))
PY
'''


def stub_dashboard_readiness(fake_bin: Path) -> str:
    """Let a recorder represent the dashboard while the installer runs normally."""
    helper = Path(__file__).resolve().parents[1] / "dashboard/systemd_dashboard.py"
    python = fake_bin / "dashboard-test-python"
    python.write_text(
        "#!/bin/sh\n"
        f'if [ "${{1:-}}" = {shlex.quote(str(helper))} ] && [ "${{2:-}}" = ready ]; then\n'
        '  test -f "$HOME/.config/systemd/user/$AGENTSTACK_LABEL_PREFIX.agentdashboard.service"\n'
        '  exit $?\n'
        'fi\n'
        f'exec {shlex.quote(sys.executable)} "$@"\n',
        encoding="utf-8",
    )
    python.chmod(0o755)
    return str(python)
