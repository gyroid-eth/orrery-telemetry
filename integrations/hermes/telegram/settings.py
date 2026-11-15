"""Non-secret Telegram settings; credential values stay inside the native host."""
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


def validate_configuration(value):
    """Check the plugin settings shape before attempting a credential read."""
    if not isinstance(value, dict) or set(value) != {'token_ref', 'allow_from'}:
        raise ValueError('invalid_telegram_settings')
    allow = value['allow_from']
    if (not isinstance(allow, list) or not 1 <= len(allow) <= 256
            or any(type(item) is not int or item <= 0 for item in allow)):
        raise ValueError('invalid_telegram_allowlist')
    return value


def validate(value):
    validate_configuration(value)
    validate_ref(value['token_ref'])
    return value


def read_token(ref, *, on_failure=None):
    """Keep values in-process; report only fixed failure codes to the host."""
    def unavailable(reason, returncode=None):
        if on_failure is not None:
            on_failure(reason, returncode)
        return ''

    if not isinstance(ref, dict):
        return unavailable('reference-invalid')
    if ref.get('store') not in ('keychain', 'secret-service'):
        return unavailable('unsupported-store')
    try:
        validate_ref(ref)
    except (ValueError, OSError):
        return unavailable('reference-invalid')
    if ref['store'] == 'secret-service':
        try:
            value = reader()['read_ref'](ref)
        except ValueError as exc:
            reason = {'unsupported_credential_store': 'unsupported-store',
                      'credential_timeout': 'secret-service-timeout'}.get(str(exc), 'secret-service-unavailable')
            return unavailable(reason)
        except subprocess.TimeoutExpired:
            return unavailable('secret-service-timeout')
        except OSError:
            return unavailable('secret-service-unavailable')
    else:
        if sys.platform != 'darwin':return unavailable('unsupported-store')
        try:
            result = subprocess.run(['/usr/bin/security', 'find-generic-password', '-a', ref['account'],
                '-s', ref['service'], '-w'], stdin=subprocess.DEVNULL, capture_output=True,
                text=True, timeout=30)
        except subprocess.TimeoutExpired:
            return unavailable('security-timeout')
        except UnicodeError:
            return unavailable('empty-or-invalid-value')
        except OSError:
            return unavailable('security-unavailable')
        if result.returncode:
            return unavailable('security-nonzero', result.returncode)
        value = result.stdout.rstrip('\r\n')
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
        return unavailable('empty-or-invalid-value')
    return value
