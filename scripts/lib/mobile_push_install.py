"""Installer-only optional immutable Python environment for the mobile relay."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def install(root, python, requirements, *, dry_run=False, run=subprocess.run):
    root, requirements = Path(root), Path(requirements)
    version = hashlib.sha256(requirements.read_bytes()).hexdigest()[:20]
    candidate = root/'deps/mobile-push'/version
    executable = candidate/'bin/python'
    if dry_run:
        print('Mobile push: prepare optional pywebpush 2.5.0 environment; no keys, subscription or delivery.')
        return
    uv = shutil.which('uv')
    if not uv:raise ValueError('mobile_push_uv_missing')
    if not candidate.exists():
        candidate.parent.mkdir(parents=True, exist_ok=True)
        run([uv,'venv','--python',str(python),str(candidate)],check=True)
        run([uv,'pip','install','--python',str(executable),'-r',str(requirements)],check=True)
    # An incomplete/old candidate is not repaired or silently reused.
    run([str(executable),'-c',"import pywebpush,PIL,importlib.metadata; assert importlib.metadata.version('pywebpush')=='2.5.0'"],check=True)
    root.mkdir(parents=True,exist_ok=True)
    target=root/'mobile-push-runtime.json'
    if target.is_symlink():raise ValueError('mobile_push_runtime_unsafe')
    fd, temporary=tempfile.mkstemp(prefix='.mobile-push-',dir=root)
    try:
        with os.fdopen(fd,'w') as stream:
            os.fchmod(stream.fileno(),0o600)
            json.dump({'version':1,'python':str(executable),'candidate':version},stream)
            stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,target)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)
    print('Mobile push dependencies ready. Reapply phone access to select this Python. No notification permission or subscription was created.')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--install',required=True)
    parser.add_argument('--python',required=True)
    parser.add_argument('--requirements',required=True)
    parser.add_argument('--dry-run',action='store_true')
    args=parser.parse_args()
    install(args.install,args.python,args.requirements,dry_run=args.dry_run)


if __name__=='__main__':main()
