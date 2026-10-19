"""One Keychain read in the owned inference process; no credential environment."""
import logging
import os
from pathlib import Path
import shlex
import subprocess
import sys

from mail_lifecycle import read, save, stamp
from runner import base_env


def install(state, run=subprocess.run, platform=None):
    state = Path(state)
    config = read(state / 'run-config.json')
    command = read(Path(config['profile_home']) / 'config.yaml')['providers'][config['provider']]['key_cmd']
    argv = shlex.split(command)
    if (platform or sys.platform) != 'darwin' or argv[:2] != ['/usr/bin/security', 'find-generic-password']:
        raise ValueError('Owned credential mode requires the inspected Mac Keychain reference')
    flags = argv[2:]
    seen = set()
    while flags:
        flag = flags.pop(0)
        if flag not in ('-s', '-a', '-w') or flag in seen:
            raise ValueError('Unexpected Keychain reference option')
        seen.add(flag)
        if flag != '-w' and (not flags or not flags.pop(0)):
            raise ValueError('Keychain option value required')
    if not {'-s', '-w'} <= seen:
        raise ValueError('Keychain service and value-only output required')
    intent = state / 'credential-read.json'
    save(intent, {'attempt_id': read(state / 'attempt.json')['attempt_id'],
                  'status': 'started', 'at': stamp()}, exclusive=True)
    try:
        result = run(argv, stdin=subprocess.DEVNULL, env=base_env(config), capture_output=True, timeout=15)
        secret = result.stdout.rstrip(b'\r\n') if result.returncode == 0 else b''
        token = secret.decode('utf-8')
        if len(secret) < 8 or '\n' in token or '\r' in token:
            raise ValueError('Credential unavailable')
    except Exception:
        save(intent, {'attempt_id': read(state / 'attempt.json')['attempt_id'], 'status': 'failed', 'at': stamp()})
        raise ValueError('Owned credential lookup failed; do not retry') from None
    from agent import command_token_source

    class Provider:
        cache_identity = 'owned-keychain-once'

        def __call__(self):
            return token

    provider = Provider()

    def build(key_cmd, provider_label='custom'):
        if str(key_cmd).strip() != command.strip():
            raise ValueError('Auxiliary credential route differs from the current managed profile')
        return provider

    command_token_source.build_command_token_provider = build
    save(intent, {'attempt_id': read(state / 'attempt.json')['attempt_id'], 'status': 'available',
                  'process_id': os.getpid(), 'at': stamp()})
    scanned = []

    def scan():
        if not scanned:
            for stream in (sys.stdout, sys.stderr):
                stream.flush()
            logging.shutdown()
            from diagnostics import scan_value
            scanned.append(scan_value(state, secret, source='owned-process-prefix-scan'))
        return scanned[0]

    return scan


def wrap_exit(module, scan):
    original = module._exit_after_oneshot

    def exit_with_scan(rc):
        report = scan()
        if report.get('matches') != 0 or report.get('errors') != 0:
            rc = 1
        return original(rc)
    module._exit_after_oneshot = exit_with_scan
