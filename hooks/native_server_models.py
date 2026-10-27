"""Configured Local URLs and OpenAI model inventories.

Static functions never acquire credentials or communicate. Only fetch_models
and require_model perform HTTP; read_key uses existing protected selectors.
No provider imports, discovery metadata, or server control operations.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import runpy
import shlex
import subprocess
import time
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

DEFAULT_CONTEXT = 64000
DEFAULT_OUTPUT = 8192
MAX_RESPONSE_BYTES = 1024 * 1024
HTTP_TIMEOUT = 3
MAX_PARALLEL = 2


def dynamic(spec):
    return isinstance(spec, dict) and spec.get('model_source') == 'server'


def model_id(value):
    if (not isinstance(value, str) or not 0 < len(value) <= 256
            or value != value.strip() or value.startswith('-')
            or any(ord(c) <= 32 or ord(c) == 127 for c in value)):
        raise ValueError('invalid_model_catalog')
    return value


def configured_url(value):
    try:
        if not isinstance(value, str) or any(ord(c) <= 32 or ord(c) == 127 for c in value):
            raise ValueError()
        parts = urlsplit(value)
        if (parts.scheme not in ('http', 'https') or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.query or parts.fragment or parts.port == 0):
            raise ValueError()
        return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip('/'), '', ''))
    except (ValueError, TypeError):
        raise ValueError('invalid_server_url') from None


def api_base(value):
    value = configured_url(value)
    return value if urlsplit(value).path.endswith('/v1') else value + '/v1'


def url_key(value):
    return hashlib.sha256(configured_url(value).encode('utf-8')).hexdigest()


def urls(spec):
    values = spec.get('urls')
    if not isinstance(values, list) or not values:
        raise ValueError('invalid_server_urls')
    result = [configured_url(value) for value in values]
    if len(set(result)) != len(result):
        raise ValueError('invalid_server_urls')
    if 'context_override' in spec and (type(spec['context_override']) is not int
            or not 1 <= spec['context_override'] <= 10000000):
        raise ValueError('invalid_context_override')
    return result


def selection(spec, document):
    values = urls(spec)
    index = document.get('url_index')
    if type(index) is not int or not 0 <= index < len(values):
        raise ValueError('url_selection_required')
    model_id(document.get('model'))
    selected = values[index]
    if 'server_url' in document and document['server_url'] != selected:
        raise ValueError('server_url_changed')
    if 'url_key' in document and document['url_key'] != url_key(selected):
        raise ValueError('server_url_changed')
    if document.get('resume_session_id') is not None and 'server_url' not in document:
        raise ValueError('server_url_required_for_resume')
    return {'url_index': index, 'server_url': selected, 'url_key': url_key(selected)}


def context(spec, row=None):
    value = row.get('meta', {}).get('n_ctx') if isinstance(row, dict) and isinstance(row.get('meta'), dict) else None
    if type(value) is int and 1 <= value <= 10000000:
        return value, 'server'
    if 'context_override' in spec:
        if type(spec['context_override']) is not int or not 1 <= spec['context_override'] <= 10000000:
            raise ValueError('invalid_context_override')
        return spec['context_override'], 'override'
    return DEFAULT_CONTEXT, 'default'


def parse_models(payload):
    values = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(values, list) or not values:
        raise ValueError('invalid_model_catalog')
    result, seen = [], set()
    for value in values:
        if not isinstance(value, dict):
            raise ValueError('invalid_model_catalog')
        identity = model_id(value.get('id'))
        if identity in seen:
            raise ValueError('invalid_model_catalog')
        seen.add(identity)
        status = value.get('status')
        state = status.get('value') if isinstance(status, dict) else None
        display = value.get('display_name')
        if not isinstance(display, str) or not 0 < len(display) <= 256 or any(ord(c) < 32 or ord(c) == 127 for c in display):
            display = identity
        result.append({'id': identity, 'display_name': display,
            'available': state != 'unloaded',
            'model_state': 'unloaded' if state == 'unloaded' else 'loaded' if state == 'loaded' else 'available',
            **({'meta': {'n_ctx': value['meta']['n_ctx']}} if isinstance(value.get('meta'), dict)
                and type(value['meta'].get('n_ctx')) is int else {})})
    return result


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('model_discovery_failed')


def fetch_models(base, key):
    """One bounded GET to a configured authority; no redirect or env proxy."""
    if not isinstance(key, str) or not key or any(ord(c) < 32 or ord(c) == 127 for c in key):
        raise ValueError('credential_unavailable')
    request = Request(api_base(base) + '/models', headers={
        'Authorization': 'Bearer ' + key, 'Accept': 'application/json'})
    started = time.monotonic()
    try:
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=HTTP_TIMEOUT) as response:
            chunks, size = [], 0
            while True:
                chunk = response.read1(min(65536, MAX_RESPONSE_BYTES + 1 - size))
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES or time.monotonic() - started > HTTP_TIMEOUT:
                    raise ValueError('model_discovery_failed')
                if not chunk:
                    break
                chunks.append(chunk)
            raw = b''.join(chunks)
        payload = json.loads(raw)
    except Exception:
        # Never publish response bodies, URL errors, auth output or a key.
        raise ValueError('model_discovery_failed') from None
    return parse_models(payload)


def require_model(base, identity, key):
    identity = model_id(identity)
    rows = fetch_models(base, key)
    selected = next((row for row in rows if row['id'] == identity), None)
    if selected is None:
        raise ValueError('model_not_served')
    if not selected['available']:
        raise ValueError('model_not_loaded')
    return selected


def read_key(facts, harness):
    """Acquire through existing Secret Service or macOS Keychain selectors.

    The picker never runs an arbitrary profile key_cmd. The CLI keeps its
    existing native credential adapter, independently of this HTTP lookup.
    """
    if facts.get('credential_ref') is not None:
        reader = runpy.run_path(str(Path(__file__).with_name('llm_secret_reader.py')))
        return reader['read_ref'](facts['credential_ref'])
    if harness == 'dsh':
        selector = facts['backend']['keychain']
        service, account = selector['service'], selector['account']
    else:
        argv = shlex.split(facts['route']['key_cmd'])
        if (len(argv) != 7 or argv[0] not in ('security', '/usr/bin/security')
                or argv[1] != 'find-generic-password' or argv[-1] != '-w'
                or set(argv[2:6:2]) != {'-s', '-a'}):
            raise ValueError('unsupported_credential_selector')
        selector = dict(zip(argv[2:6:2], argv[3:6:2]))
        service, account = selector['-s'], selector['-a']
    if not all(isinstance(v, str) and v and not any(ord(c) < 32 or ord(c) == 127 for c in v)
               for v in (service, account)):
        raise ValueError('invalid_credential_selector')
    try:
        result = subprocess.run(['/usr/bin/security', 'find-generic-password', '-s', service,
            '-a', account, '-w'], stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
        key = result.stdout.decode('utf-8').removesuffix('\n')
        if result.returncode or not key.strip() or not 0 < len(key) <= 65536 or any(ord(c) < 32 or ord(c) == 127 for c in key):
            raise ValueError()
        return key
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
        raise ValueError('credential_unavailable') from None


def inventories(spec, key):
    """URL count is configurable; only concurrent requests are bounded."""
    values = urls(spec)
    def one(pair):
        index, base = pair
        try:
            return {'url_index': index, 'models': fetch_models(base, key), 'reason': None}
        except ValueError as error:
            return {'url_index': index, 'models': [], 'reason': str(error)}
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        return list(pool.map(one, enumerate(values)))
