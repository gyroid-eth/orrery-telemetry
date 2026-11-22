"""Run under the shared heavy lock: invokes spawn_child with fake dependencies."""
import os
from pathlib import Path
import shlex
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_foreground_resume_runs_cleanup_after_nonzero_native_exit(tmp_path):
    hooks = tmp_path / 'hooks'
    hooks.mkdir()
    cleaned = tmp_path / 'cleanup-called'
    unwanted = tmp_path / 'external-called'
    command = (shlex.join(['/bin/sh', '-c', 'exit 7']) + '; native_rc=$?; '
               + shlex.join(['/usr/bin/touch', str(cleaned)]) + '; exit "$native_rc"')
    (hooks / 'harness_child.py').write_text('print(' + repr(command) + ')\n')
    binary = tmp_path / 'bin'
    binary.mkdir()
    for name in ('tmux', 'security'):
        file = binary / name
        file.write_text('#!/bin/sh\n' + shlex.join(['/usr/bin/touch', str(unwanted)]) + '\nexit 99\n')
        file.chmod(0o755)
    environment = {
        'HOME': str(tmp_path), 'PATH': str(binary) + os.pathsep + '/usr/bin:/bin',
        'AGENTSTACK_HOOKS_DIR': str(hooks), 'AGENTSTACK_HOME': str(tmp_path),
        'AGENTSTACK_RUNTIME_DIR': str(tmp_path / 'runtime'),
        'AGENTSTACK_PROJECT_KEY': '/fixture/project', 'AGENTSTACK_PYTHON': sys.executable,
        'AGENTSTACK_RUNTIME_MODE': 'legacy',
        'AGENTSTACK_LABEL_PREFIX': 'org.agentstack.test-native-resume',
    }
    result = subprocess.run(['/bin/bash', str(ROOT / 'hooks/spawn_child.sh'),
                             '--resume-native', 'FixtureAgent'],
                            env=environment, cwd='/', stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 7, result.stderr
    assert cleaned.exists()
    assert not unwanted.exists(), 'foreground resume must not create a tmux session or acquire credentials'
