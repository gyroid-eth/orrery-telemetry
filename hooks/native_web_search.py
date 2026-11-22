"""共通の検索設定。生成時は参照だけを扱い、資格は harness 内で読む。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import runpy
import subprocess
import sys
from urllib.parse import urlsplit


def select(document, manifest):
    settings = manifest.get('web_search') or {}
    provider = document.get('web_search') or settings.get('provider')
    if provider is None:
        return None
    if provider not in ('exa', 'searxng'):
        raise ValueError('invalid_web_search')
    result = {'provider': provider}
    if provider == 'searxng':
        url = settings.get('searxng', {}).get('url', '')
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('invalid_web_search')
        result['url'] = url.rstrip('/')
    else:
        ref = settings.get('exa', {}).get('credential_ref')
        if ref is not None:
            if not isinstance(ref, dict):
                raise ValueError('invalid_credential_ref')
            if ref.get('store') == 'secret-service':
                runpy.run_path(str(Path(__file__).with_name('llm_secret_reader.py')))['validate_ref'](ref)
            elif (ref.get('store') != 'keychain' or set(ref) != {'store', 'service', 'account'}
                  or not all(isinstance(ref[k], str) and ref[k] and not ref[k].startswith('-')
                             and not any(ord(c) < 32 or ord(c) == 127 for c in ref[k])
                             for k in ('service', 'account'))):
                raise ValueError('invalid_credential_ref')
            result['credential_ref'] = ref
    return result


def command(product, config, python):
    return [str(python), '-I', '-B', str(Path(product) / 'hooks/native_web_search.py'), '--config', str(config)]


def definition(settings, argv):
    result = {'auth_command': argv}
    ref = settings.get('credential_ref', {})
    if ref.get('store') == 'keychain':
        result['keychain'] = {k: ref[k] for k in ('service', 'account')}
    elif ref:
        result['credential_ref'] = ref
    return result


def read_key(settings, *, runner=None):
    ref = settings.get('credential_ref', {})
    if ref.get('store') == 'secret-service':
        return runpy.run_path(str(Path(__file__).with_name('llm_secret_reader.py')))['read_ref'](ref, runner=runner)
    if ref.get('store') != 'keychain' or sys.platform != 'darwin':
        raise ValueError('credential_unavailable')
    try:
        result = (runner or subprocess.run)(['/usr/bin/security', 'find-generic-password',
            '-s', ref['service'], '-a', ref['account'], '-w'], stdin=subprocess.DEVNULL,
            capture_output=True, timeout=15)
        key = result.stdout.decode('utf-8').removesuffix('\n')
        if result.returncode or not key.strip() or len(key) > 65536 or any(ord(c) < 32 or ord(c) == 127 for c in key):
            raise ValueError('credential_unavailable')
        return key
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise ValueError('credential_unavailable') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--emit-env', action='store_true')
    args = parser.parse_args()
    try:
        settings = json.loads(Path(args.config).read_text())
        key = read_key(settings)
    except (ValueError, OSError, TypeError, KeyError):
        if not args.emit_env:
            print('Exa の資格を取得できません。web_search.exa.credential_ref を確認してください。', file=sys.stderr)
            return 1
        key = ''  # native secrets.command は失敗しても起動を止めない。
    print(('EXA_API_KEY=' if args.emit_env else '') + key)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
