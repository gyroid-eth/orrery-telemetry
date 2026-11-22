"""Small native CLI identity and backend metadata helpers."""
import json
import os
from pathlib import Path
import re
import runpy
import shlex
import stat
import sys

HARNESSES = ('dsh', 'hermes')
CONNECTIONS = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'hooks/native_connections.py'))


def matches_command(program, command):
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    if not argv:
        return False
    executable = os.path.basename(argv[0]).lower()
    if program == 'dsh':
        return (executable == 'node' and len(argv) > 1
                and argv[1].endswith('/node_modules/@deepseek-ai/dsh/lib/bin.js'))
    if program == 'hermes':
        if executable == 'hermes':
            return True
        if not re.fullmatch(r'python(?:\d+(?:\.\d+)*)?', executable):
            return False
        return (len(argv) > 2 and argv[1:3] == ['-m', 'hermes_cli.main']
                or len(argv) > 1 and (argv[1].endswith('/bin/hermes')
                                     or argv[1].endswith('/hermes_cli/main.py')))
    return False


def possible_process(program, name):
    executable = os.path.basename(name).lower()
    return (program == 'dsh' and (executable == 'node'
            or sys.platform == 'linux' and executable == 'mainthread')
            or program == 'hermes' and (executable == 'hermes' or executable.startswith('python')))


def metadata(runtime_dir, name, program):
    if program not in HARNESSES or not re.fullmatch(r'[A-Za-z0-9_.-]+', name):
        return {}
    result = {'harness': program, 'provider': '', 'locality': None}
    try:
        source = Path(runtime_dir) / 'child-agents' / (name + '.json')
        info = source.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
            return result
        record = json.loads(source.read_text())
        if (record.get('agent_name') != name or record.get('program') != program
                or record.get('harness') != program):
            return result
        backend = record.get('harness_backend')
        kind_args = {'kind': record['harness_backend_kind']} if 'harness_backend_kind' in record else {}
        kind = CONNECTIONS['connection_kind'](backend, **kind_args)
        result.update(provider=backend, locality='local' if kind == 'strata' else 'cloud')
    except (OSError, ValueError, AttributeError):
        pass
    return result
