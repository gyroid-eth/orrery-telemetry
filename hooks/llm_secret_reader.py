"""非秘密参照から選択した資格だけを読む内部入口。設定や鍵を書き込まない。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import runpy
import subprocess
import sys

SECRET_TOOL = '/usr/bin/secret-tool'
CONTEXT_KEYS = ('DBUS_SESSION_BUS_ADDRESS', 'XDG_RUNTIME_DIR')
CONNECTIONS = runpy.run_path(str(Path(__file__).with_name('native_connections.py')))
REASONS = {'invalid_credential_ref', 'credential_reference_conflict',
           'unsupported_credential_store', 'credential_reader_unavailable',
           'credential_unavailable', 'credential_timeout'}


def validate_ref(ref):
    if (not isinstance(ref, dict) or set(ref) != {'store', 'attributes'}
            or ref.get('store') != 'secret-service'):
        raise ValueError('invalid_credential_ref')
    attributes = ref['attributes']
    if (not isinstance(attributes, dict) or not 1 <= len(attributes) <= 16
            or not all(isinstance(v, str) and 0 < len(v) <= 256
                       and not any(ord(c) < 32 or ord(c) == 127 for c in v)
                       for pair in attributes.items() for v in pair)):
        raise ValueError('invalid_credential_ref')
    if not all(re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]*', key) and not value.startswith('-')
               for key, value in attributes.items()):
        raise ValueError('invalid_credential_ref')
    return ref


def validate_environment():
    # Admission checks only executable references, never D-Bus or stored keys.
    if sys.platform != 'linux':
        raise ValueError('unsupported_credential_store')
    if not Path(SECRET_TOOL).is_file() or not os.access(SECRET_TOOL, os.X_OK):
        raise ValueError('credential_reader_unavailable')


def command(product_root, manifest_file, backend, python):
    reader = Path(product_root) / 'hooks/llm_secret_reader.py'
    if not reader.is_file() or not Path(python).is_absolute():
        raise ValueError('credential_reader_unavailable')
    return [str(python), '-I', '-B', str(reader), '--manifest',
            str(Path(manifest_file).resolve()), '--backend', backend]


def context(environ=None):
    environ = os.environ if environ is None else environ
    return {key: environ[key] for key in CONTEXT_KEYS if environ.get(key)}


def read_ref(ref, *, runner=None):
    validate_ref(ref)
    validate_environment()
    argv = [SECRET_TOOL, 'lookup']
    for key, value in sorted(ref['attributes'].items()):
        argv.extend([key, value])
    env = {'PATH': '/usr/bin:/bin', 'HOME': os.path.expanduser('~'),
           'LANG': 'C.UTF-8', **context()}
    try:
        result = (runner or subprocess.run)(argv, env=env, stdin=subprocess.DEVNULL,
            capture_output=True, timeout=5)
    except subprocess.TimeoutExpired:
        raise ValueError('credential_timeout') from None
    except OSError:
        raise ValueError('credential_unavailable') from None
    try:
        if result.returncode or not 0 < len(result.stdout) <= 65536:
            raise ValueError('credential_unavailable')
        key = result.stdout.decode('utf-8').removesuffix('\n')
        if not key.strip() or any(ord(c) < 32 or ord(c) == 127 for c in key):
            raise ValueError('credential_unavailable')
    except (UnicodeError, AttributeError, TypeError):
        raise ValueError('credential_unavailable') from None
    return key


def manifest_ref(manifest_file, backend):
    try:
        data = CONNECTIONS['read_manifest'](manifest_file)
        _, spec = CONNECTIONS['resolve_connection'](data, backend)
        return validate_ref(spec['credential_ref'])
    except (OSError, UnicodeError, KeyError, TypeError, ValueError):
        raise ValueError('invalid_credential_ref') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--backend', required=True)
    args = parser.parse_args()
    try:
        key = read_ref(manifest_ref(args.manifest, args.backend))
    except ValueError as error:
        reason = str(error)
        print(reason if reason in REASONS else 'credential_unavailable', file=sys.stderr)
        return 1
    print(key)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
