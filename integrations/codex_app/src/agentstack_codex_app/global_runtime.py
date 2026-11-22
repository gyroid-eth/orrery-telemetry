"""Load the installed common client; never keep another owner credential."""
from pathlib import Path
import os
import runpy
import subprocess


def selected_client():
    library = Path(__file__).resolve().parents[4] / 'bin/lib/runtime_client.py'
    if not library.exists():
        library = Path(os.environ.get('AGENTSTACK_HOME', str(Path.home()/'.agentstack'))) / 'bin/lib/runtime_client.py'
    loader = library.with_name('global-client-loader.sh')
    if not loader.is_file():
        loader = Path(__file__).with_name('_global-client-loader.sh')
    if not loader.is_file():
        raise RuntimeError('GLOBAL_ENTRY_UNAVAILABLE')
    result = subprocess.run(['/bin/bash', str(loader), 'admit', '--root', str(library.parents[2])],
                            capture_output=True, text=True, timeout=10)
    if result.stdout.strip() != 'orrery-client-admission-v1' or result.returncode not in (0,125):
        raise RuntimeError('GLOBAL_ENTRY_UNAVAILABLE')
    if result.returncode == 125:
        return None
    if not library.is_file():
        raise RuntimeError('GLOBAL_ENTRY_UNAVAILABLE')
    api = runpy.run_path(str(library))
    choice = api['selected']([])
    client = api['configured'](selection=choice)
    return (api, client) if client is not None else None
