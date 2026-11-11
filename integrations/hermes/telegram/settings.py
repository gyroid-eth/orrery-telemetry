"""Non-secret Telegram settings; credential values stay inside the native host."""
import os
from pathlib import Path
import runpy
import subprocess
import sys


def reader():
    return runpy.run_path(str(Path(__file__).resolve().parents[3] / 'hooks/llm_secret_reader.py'))


def validate_ref(ref):
    if isinstance(ref, dict) and ref.get('store') == 'secret-service':
        return reader()['validate_ref'](ref)
    if (not isinstance(ref, dict) or set(ref) != {'store', 'service', 'account'}
            or ref.get('store') != 'keychain'
            or any(not isinstance(ref[key], str) or not 0 < len(ref[key]) <= 256
                   or any(ord(c) < 32 or ord(c) == 127 for c in ref[key])
                   for key in ('service', 'account'))):
        raise ValueError('invalid_telegram_reference')
    return ref


def validate(value):
    if not isinstance(value, dict) or set(value) != {'token_ref', 'allow_from'}:
        raise ValueError('invalid_telegram_settings')
    validate_ref(value['token_ref'])
    allow = value['allow_from']
    if (not isinstance(allow, list) or not 1 <= len(allow) <= 256
            or any(type(item) is not int or item <= 0 for item in allow)):
        raise ValueError('invalid_telegram_allowlist')
    return value


def read_token(ref):
    try:
        validate_ref(ref)
        if ref['store'] == 'secret-service':
            return reader()['read_ref'](ref)
        if sys.platform != 'darwin':return ''
        result = subprocess.run(['/usr/bin/security', 'find-generic-password', '-a', ref['account'],
            '-s', ref['service'], '-w'], stdin=subprocess.DEVNULL, capture_output=True,
            text=True, timeout=30)
        value = result.stdout.rstrip('\r\n')
        if result.returncode or not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
            return ''
        return value
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return ''
