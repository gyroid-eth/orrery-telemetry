"""Mobile launch choices and receipts for the existing dashboard launcher."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import re
import secrets
import threading
import time

try:
    from dashboard.mobile_input import NAME
except ModuleNotFoundError:
    from mobile_input import NAME


RECEIPT = re.compile(r'[a-f0-9]{32}\Z')
MAX_TASK_CHARS = 4000
CATALOG_TTL = 300
RECEIPT_TTL = 1800
CATALOG_LIMIT = 24
RECEIPT_LIMIT = 128


class SpawnError(ValueError):
    def __init__(self, code, status):
        super().__init__(code)
        self.status = status


def _text(value, limit):
    return (isinstance(value, str) and 0 < len(value) <= limit
            and all(ord(char) >= 32 and ord(char) != 127 for char in value))


def _matches_name(value):
    return isinstance(value, str) and NAME.fullmatch(value) is not None


def _harness_key(value):
    key = value.strip().lower().rsplit('/', 1)[-1]
    return {'claude':'claude-code', 'claudecode':'claude-code',
            'codex-cli':'codex', 'dsh-tui':'dsh'}.get(key, key)


_TOOLS_HARNESSES = {'claude-code', 'codex', 'dsh', 'hermes'}


def _effort_policy(provider, model, harness):
    fallback = model.get('effort_default', provider.get('effort_default'))
    if 'efforts' in harness:
        levels, fallback = harness['efforts'], harness.get('default_effort')
    elif 'efforts' in model:
        levels = model['efforts']
    elif isinstance(provider.get('model_efforts'), dict) and model['id'] in provider['model_efforts']:
        levels = provider['model_efforts'][model['id']]
        defaults = provider.get('model_effort_defaults')
        if 'effort_default' not in model and isinstance(defaults, dict):
            fallback = defaults.get(model['id'], fallback)
    else:
        levels = provider.get('efforts')
    levels = [entry for entry in levels if _text(entry, 32)] if isinstance(levels, list) else []
    capabilities = provider.get('capabilities')
    required = isinstance(capabilities, dict) and capabilities.get('effort_required') is True
    return {'efforts':levels, 'default_effort':fallback if fallback in levels else None,
            'effort_required':required}


def _catalog_choices(value):
    if not isinstance(value.get('providers'), list) or not isinstance(value.get('dirs'), list):
        raise SpawnError('catalog_unavailable', 503)
    models = []
    for provider in value['providers']:
        if not isinstance(provider, dict) or not _text(provider.get('id'), 96):
            continue
        provider_id = provider['id']
        label = provider.get('label')
        label = label if _text(label, 96) else provider_id
        options = provider.get('models')
        if not isinstance(options, list):
            continue
        for model in options:
            if not isinstance(model, dict) or not _text(model.get('id'), 256):
                continue
            harnesses = model.get('harnesses', provider.get('harnesses'))
            if not isinstance(harnesses, list):
                continue
            routes = []
            for harness in harnesses:
                if (not isinstance(harness, dict) or harness.get('available') is not True
                        or not _text(harness.get('id'), 96)):
                    continue
                harness_id = harness['id']
                key = _harness_key(harness_id)
                # These are the tools-aware launch paths supported by the
                # mobile form. The desktop retains the other provider options.
                if key not in _TOOLS_HARNESSES:
                    continue
                effort = _effort_policy(provider, model, harness)
                if effort['effort_required'] and not effort['efforts']:
                    continue
                route_label = harness.get('label')
                routes.append({'id': harness_id, 'key': key,
                               'label': route_label if _text(route_label, 96) else harness_id,
                               'default': harness.get('default') is True, **effort})
            if not routes:
                continue
            index, url_key = model.get('url_index'), model.get('url_key')
            if index is not None and (type(index) is not int or index < 0):
                continue
            if url_key is not None and not _text(url_key, 256):
                continue
            models.append({'provider': provider_id, 'provider_label': label,
                           'model': model['id'], 'default': model.get('default') is True,
                           'harnesses': routes, 'url_index': index, 'url_key': url_key})
    dirs = []
    for directory in value['dirs']:
        if not isinstance(directory, dict) or directory.get('exists') is False:
            continue
        path, label = directory.get('path'), directory.get('label')
        if _text(path, 512) and (path.startswith('/') or path == '~' or path.startswith('~/')):
            dirs.append({'path': path, 'label': label if _text(label, 96) else path})
    return models, dirs


def _options(model, route, directory, task, role, effort, computer, browser, configurable):
    payload = {'task':task, 'model':model['model'], 'provider':model['provider'],
               'dir':directory['path'], 'standalone':True, 'headless':True, 'async':True}
    legacy = {'claude':'claude-code', 'codex':'codex'}
    if route['key'] != legacy.get(model['provider']):
        payload['harness'] = route['id']
    if model['url_index'] is not None:
        payload['url_index'] = model['url_index']
    if model['url_key'] is not None:
        payload['url_key'] = model['url_key']
    if effort:
        payload['effort'] = effort
    if not configurable:
        return payload
    if role:
        payload['role'] = role
    native = route['key'] in {'dsh', 'hermes'}
    mcp = ['cua-driver'] if computer else []
    if browser and native:
        mcp.append('chrome-devtools')
    if not browser or route['key'] == 'codex' and not computer:
        payload['base'] = 'mail-only'
    payload['tools'] = {}
    if mcp:
        payload['tools']['mcp'] = mcp
    if browser and not native:
        payload['tools']['browser'] = {}
    return payload


@dataclass
class Receipt:
    epoch: str
    expiry: float
    state: str = 'submitting'
    name: str | None = None
    status_until: float | None = None

    def public(self):
        return {'state':self.state, **({'name':self.name} if self.name else {})}


class MobileSpawn:
    def __init__(self, catalog, launch, status, *, clock=time.monotonic):
        self._fetch_catalog = catalog
        self._launch = launch
        self._status = status
        self._clock = clock
        self._catalogs = OrderedDict()
        self._receipts = {}
        self._lock = threading.Lock()

    def _prune(self):
        now = self._clock()
        for token, (expiry, *_) in list(self._catalogs.items()):
            if expiry <= now:
                del self._catalogs[token]
        for token, receipt in list(self._receipts.items()):
            if receipt.expiry <= now and receipt.state != 'submitting':
                del self._receipts[token]

    def catalog(self, epoch, *, configurable):
        models, directories = _catalog_choices(self._fetch_catalog())
        token = secrets.token_hex(16)
        with self._lock:
            self._prune()
            if len(self._catalogs) >= CATALOG_LIMIT:
                self._catalogs.popitem(last=False)
            self._catalogs[token] = (self._clock() + CATALOG_TTL, epoch, configurable, models, directories)
        return {
            'catalog':token,
            'configurable':configurable,
            'models':[{key:value for key, value in model.items()
                       if key in {'model','provider','provider_label','default','harnesses','url_index'}} for model in models],
            'directories':directories,
        }

    def launch(self, epoch, value):
        if set(value) != {'catalog','receipt','model','harness','directory','task','computer','browser','effort','role'}:
            raise SpawnError('invalid_launch', 400)
        receipt_id, catalog_id = value['receipt'], value['catalog']
        if (not isinstance(receipt_id, str) or not RECEIPT.fullmatch(receipt_id)
                or not isinstance(catalog_id, str) or not RECEIPT.fullmatch(catalog_id)):
            raise SpawnError('invalid_launch', 400)
        task = value['task']
        if (not isinstance(task, str) or not task.strip() or len(task) > MAX_TASK_CHARS
                or any(ord(char) < 32 and char not in '\n\t\r' or ord(char) == 127 for char in task)):
            raise SpawnError('invalid_task', 400)
        model_index, dir_index = value['model'], value['directory']
        role, effort = value['role'], value['effort']
        if (not _text(value['harness'],96) or type(model_index) is not int or type(dir_index) is not int
                or type(value['computer']) is not bool or type(value['browser']) is not bool
                or (role != '' and not _text(role, 40)) or (effort is not None and not _text(effort, 32))):
            raise SpawnError('invalid_launch', 400)

        with self._lock:
            self._prune()
            if previous := self._receipts.get(receipt_id):
                if previous.epoch != epoch:
                    raise SpawnError('launch_unavailable', 409)
                return previous.public()
            catalog = self._catalogs.get(catalog_id)
            if catalog is None or catalog[1] != epoch:
                raise SpawnError('catalog_expired', 409)
            _, _, configurable, models, directories = catalog
            if not 0 <= model_index < len(models) or not 0 <= dir_index < len(directories):
                raise SpawnError('invalid_launch', 400)
            model, directory = models[model_index], directories[dir_index]
            route = next((item for item in model['harnesses'] if item['id'] == value['harness']), None)
            if (route is None or (effort is None and route['effort_required'])
                    or (effort is not None and effort not in route['efforts'])
                    or (not configurable and (role or value['computer'] or value['browser']))):
                raise SpawnError('invalid_launch', 400)
            if len(self._receipts) >= RECEIPT_LIMIT:
                raise SpawnError('launch_unavailable', 503)
            receipt = Receipt(epoch, self._clock() + RECEIPT_TTL)
            self._receipts[receipt_id] = receipt

        status_until = None
        try:
            result = self._launch(_options(model, route, directory, task.strip(), role.strip(),
                                           effort or route['default_effort'],
                                           value['computer'], value['browser'], configurable))
            if result.get('ok') is False:
                state, name = 'failed', None
            elif result.get('ok') is True and _matches_name(result.get('child_name')):
                state, name = ('launching' if result.get('pending') is True else 'ready'), result['child_name']
                seconds = result.get('verdict_deadline_seconds')
                if type(seconds) is not int or not 0 < seconds <= 600:
                    seconds = 140
                status_until = self._clock() + seconds
            else:
                state, name = 'unknown', None
        except (ValueError, OSError, TypeError):
            state, name = 'unknown', None
        with self._lock:
            receipt.state, receipt.name, receipt.status_until = state, name, status_until
            return receipt.public()

    def status(self, epoch, receipt_id):
        if not RECEIPT.fullmatch(receipt_id):
            raise SpawnError('invalid_launch', 400)
        with self._lock:
            self._prune()
            receipt = self._receipts.get(receipt_id)
            if receipt is None or receipt.epoch != epoch:
                raise SpawnError('launch_unavailable', 404)
            name = receipt.name
            if receipt.state not in {'launching','unknown'} or name is None:
                return receipt.public()
        try:
            result = self._status(name)
        except (ValueError, OSError, TypeError):
            result = {}
        with self._lock:
            if receipt.state in {'launching','unknown'}:
                state = result.get('state')
                if result.get('ok') is True and result.get('name') == name and state in {'ready','failed'}:
                    receipt.state = state
                elif receipt.status_until is not None and self._clock() >= receipt.status_until:
                    receipt.state = 'unknown'
            return receipt.public()
