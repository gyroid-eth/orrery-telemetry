"""Exercise launch admission and desktop forwarding with ephemeral HTTP fixtures."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import threading

import pytest

from dashboard import mobile_relay as relay
from dashboard.mobile_spawn import MobileSpawn
from test_mobile_relay import Store, serving


CATALOG = {
    'providers':[
        {'id':'codex','label':'Codex','efforts':['low','high'],'effort_default':'low',
         'models':[{'id':'fixture-model','default':True,'url_index':2,'url_key':'catalog-key'}],
         'harnesses':[{'id':'codex-cli','available':True,'default':True},
                      {'id':'dsh','available':True},{'id':'hermes','available':False}]},
    ],
    'dirs':[{'path':'/workspace','label':'Workspace'},{'path':'/unavailable','exists':False}],
}
OWNER = 'owner@example.test'
ORIGIN = 'https://viewer.example.ts.net:9443'
HEADERS = {'Host':'viewer.example.ts.net:9443','Origin':ORIGIN,'Tailscale-User-Login':OWNER,
           'Content-Type':'application/json'}
RECEIPT = 'b'*32


class Input:
    enabled = True
    epoch = 'a'*32

    def __init__(self, api):
        self.api = api

    def call(self, operation):
        assert operation == 'capability'
        return {'contract':'orrery-mobile-pane-v1','enabled':self.enabled,'epoch':self.epoch,'api':self.api}


def request(port, method, path, body=None, headers=HEADERS):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=4)
    try:
        connection.request(method, path, json.dumps(body) if body is not None else None, headers=headers)
        response = connection.getresponse()
        return response.status, json.load(response)
    finally:
        connection.close()


def fixture(log, *, catalog=CATALOG, launch_result=None, status_result=None):
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, data, status=200):
            payload = json.dumps(data).encode()
            self.send_response(status)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            log.append(self.path)
            if self.path == '/telemetry/spawn-catalog':
                self.respond(catalog)
            elif self.path == '/api/spawn-status?name=FixtureCurie':
                self.respond(status_result or {'ok':True,'name':'FixtureCurie','state':'ready','detail':'internal'})
            else:
                self.respond({'ok':False}, 404)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            log.append((self.path, body))
            result = launch_result or {'ok':True,'child_name':'FixtureCurie','pending':True}
            self.respond(result, 202 if result['ok'] else 400)

    return ThreadingHTTPServer(('127.0.0.1', 0), Upstream)


def payload(catalog, *, configurable=True):
    return {'catalog':catalog['catalog'], 'receipt':RECEIPT, 'model':0, 'directory':0,
            'harness':'codex-cli', 'task':'Check the result.\nReport errors.',
            'role':'Review' if configurable else '', 'effort':'high',
            'browser':configurable, 'computer':configurable}


@pytest.mark.parametrize('api', [31,34])
def test_mobile_launch_reuses_catalog_and_dashboard_and_checks_receipt_once(api):
    calls = []
    entitlement = Input(api)
    configurable = api == 31
    with serving(fixture(calls)) as dashboard, serving(fixture(calls)) as desktop:
        config = relay.Config(OWNER, ORIGIN, upstream_port=dashboard, cockpit_port=desktop,
                              input_enabled=True, phone_policy_reviewed=True)
        server = relay.make_server(config,store=Store(),input_client=entitlement,test_port=0)
        with serving(server) as port:
            status, catalog = request(port,'GET','/view/spawn')
            assert status == 200 and catalog['configurable'] is configurable
            assert catalog['directories'] == [{'path':'/workspace','label':'Workspace'}]
            assert len(catalog['models'][0]['harnesses']) == 2
            assert 'url_key' not in catalog['models'][0]
            value = payload(catalog,configurable=configurable)
            assert request(port,'POST','/view/spawn',dict(value, directory=1))[0] == 400
            assert request(port,'POST','/view/spawn',dict(value, effort='invalid'))[0] == 400
            if not configurable:
                assert request(port,'POST','/view/spawn',dict(value, role='Refused'))[0] == 400
            assert request(port,'POST','/view/spawn',value,headers={**HEADERS,'Origin':'https://other.example.ts.net'})[0] == 403
            for _ in range(2):
                assert request(port,'POST','/view/spawn',value) == (200,{'state':'launching','name':'FixtureCurie'})
            expected = {'task':value['task'],'model':'fixture-model','provider':'codex','dir':'/workspace',
                        'standalone':True,'headless':True,'async':True,'effort':'high',
                        'url_index':2,'url_key':'catalog-key'}
            if configurable:
                expected.update(role='Review',tools={'mcp':['cua-driver'],'browser':{}})
            assert calls == ['/telemetry/spawn-catalog',('/api/spawn',expected)]
            query = '/view/spawn/status?receipt=' + RECEIPT
            assert request(port,'GET',query) == (200,{'state':'ready','name':'FixtureCurie'})
            assert calls[-1] == '/api/spawn-status?name=FixtureCurie'
            entitlement.epoch = 'c'*32
            assert request(port,'GET',query)[0] == 404
            assert request(port,'POST','/view/spawn',dict(value,receipt='c'*32))[0] == 409
            entitlement.enabled = False
            assert request(port,'GET','/view/spawn')[0] == 404
            assert request(port,'POST','/view/spawn',value)[0] == 404
            assert len(calls) == 3


def test_read_only_relay_denies_spawn_before_upstream():
    config = relay.Config(OWNER, ORIGIN)
    with serving(relay.make_server(config,store=Store(),test_port=0)) as port:
        assert request(port,'GET','/view/spawn')[0] == 404
        assert request(port,'POST','/view/spawn',{})[0] == 405


def test_antigravity_models_are_listed_without_forwarding_an_invalid_standalone_launch():
    catalog_source = {**CATALOG, 'providers': [*CATALOG['providers'],
        {'id': 'gemini', 'label': 'Antigravity',
         'capabilities': {'effort_required': True, 'standalone': False, 'resources_required': True},
         'models': [{'id': f'gemini-fixture-{i}'} for i in range(17)],
         'harnesses': [{'id': 'antigravity', 'available': True, 'default': True}], 'efforts': ['low', 'high']},
        {'id': 'deepseek', 'label': 'DeepSeek',
         'models': [{'id': 'deepseek-fixture', 'harnesses': [{'id': 'dsh', 'available': True},
                                                             {'id': 'hermes', 'available': True}]}]},
        {'id': 'local', 'label': 'Local', 'locality': 'local',
         'models': [{'id': 'local-fixture', 'url_index': 1, 'url_key': 'fixture-key',
                     'harnesses': [{'id': 'hermes', 'available': True}]}]},
    ]}
    submitted = []
    store = MobileSpawn(lambda: catalog_source, submitted.append, lambda _: {})
    catalog = store.catalog('a'*32, configurable=True)
    models = catalog['models']
    gemini = [model for model in models if model['provider'] == 'gemini']
    assert len(gemini) == 17
    assert all(model['harnesses'][0]['launch_blocked'] for model in gemini)
    assert [(m['provider'], [h['key'] for h in m['harnesses']]) for m in models[-2:]] == [
        ('deepseek', ['dsh', 'hermes']), ('local', ['hermes'])]
    assert models[-1]['locality'] == 'local' and 'url_key' not in models[-1]
    with pytest.raises(relay.mobile_spawn.SpawnError, match='invalid_launch'):
        store.launch('a'*32, {**payload(catalog), 'model': models.index(gemini[0]), 'harness': 'antigravity'})
    assert not submitted


def local_catalog():
    return {
        'providers': [{
            'id': 'strata-epyc', 'label': 'Fixture connection', 'locality': 'local',
            'models': [{'id': 'inkling-2-mini', 'url_index': 0, 'url_key': 'a' * 64}],
            'harnesses': [{'id': 'hermes', 'label': 'Hermes', 'available': True, 'default': True},
                          {'id': 'dsh', 'label': 'dsh', 'available': True}],
        }],
        'dirs': [{'path': '/workspace', 'label': 'Workspace'},
                 {'path': '/workspace/browser', 'label': 'Browser workspace'}],
        'native_browser_dirs': ['/workspace/browser'],
    }


@pytest.mark.parametrize('harness', ['dsh', 'hermes'])
@pytest.mark.parametrize('directory,desired,expected', [
    (0, None, ['cua-driver']),
    (0, True, ['cua-driver']),
    (1, True, ['cua-driver', 'chrome-devtools']),
    (1, False, ['cua-driver']),
])
def test_local_launch_forwards_only_tools_available_in_the_selected_directory(harness, directory, desired, expected):
    calls = []
    with serving(fixture(calls)) as dashboard, serving(fixture(calls, catalog=local_catalog())) as desktop:
        config = relay.Config(OWNER, ORIGIN, upstream_port=dashboard, cockpit_port=desktop,
                              input_enabled=True, phone_policy_reviewed=True)
        with serving(relay.make_server(config,store=Store(),input_client=Input(31),test_port=0)) as port:
            status, catalog = request(port, 'GET', '/view/spawn')
            assert status == 200 and catalog['models'][0]['provider'] == 'strata-epyc'
            assert [item.get('native_browser') for item in catalog['directories']] == [None, True]
            value = {**payload(catalog), 'directory': directory, 'harness': harness, 'effort': None}
            if desired is None:
                value.pop('browser')
            else:
                value['browser'] = desired
            assert request(port, 'POST', '/view/spawn', value) == (200, {'state':'launching','name':'FixtureCurie'})
            path, submitted = calls[-1]
            assert path == '/api/spawn' and submitted['harness'] == harness
            assert submitted['provider'] == 'strata-epyc' and submitted['model'] == 'inkling-2-mini'
            assert submitted['url_index'] == 0 and submitted['url_key'] == 'a' * 64
            assert submitted['tools'] == {'mcp': expected}
            assert (submitted.get('base') == 'mail-only') is ('chrome-devtools' not in expected)


@pytest.mark.parametrize('codex_browser', [False, True])
def test_catalog_probes_the_same_native_browser_copy_as_the_launcher(tmp_path, monkeypatch, codex_browser):
    from dashboard import server

    plain, allowed, blocked = [tmp_path / name for name in ('plain', 'allowed', 'blocked')]
    for directory in (plain, allowed, blocked):
        directory.mkdir()
    definition = {'command':'/usr/bin/true', 'args':[]}
    settings = tmp_path / 'harness-settings.json'
    settings.write_text(json.dumps({'projects': {
        str(allowed): {'mcpServers': {'chrome-devtools': definition}},
        str(blocked): {'mcpServers': {'chrome-devtools': {**definition, 'env': {'KEY':'private'}}}},
    }}))
    monkeypatch.setenv('AGENTSTACK_SPAWN_ROOTS', str(tmp_path))
    monkeypatch.setenv('AGENTSTACK_CLAUDE_JSON', str(settings))
    codex = tmp_path / 'codex'
    codex.mkdir()
    monkeypatch.setenv('CODEX_HOME', str(codex))
    if codex_browser:
        (codex / 'config.toml').write_text('''
[mcp_servers.chrome-devtools]
command = "npx"
args = ["-y", "chrome-devtools-mcp@latest", "--autoConnect"]
''')
    dirs = [str(plain), str(allowed), str(blocked)]
    assert server._native_browser_directories(dirs) == (dirs if codex_browser else [str(allowed)])


@pytest.mark.parametrize('asynchronous', [False, True])
def test_mobile_launch_returns_only_a_safe_failure_reason(asynchronous):
    calls = []
    failure = {'ok':False, 'error':'spawn launcher exited with status 1',
               'detail':'local log /fixture/private\nHarness prepare failed: unsupported_mcp_selection\n'}
    status_result = {'ok':True, 'name':'FixtureCurie', 'state':'failed', 'result':failure} if asynchronous else None
    dashboard_api = fixture(calls, launch_result=None if asynchronous else failure, status_result=status_result)
    with serving(dashboard_api) as dashboard, serving(fixture(calls)) as desktop:
        config = relay.Config(OWNER, ORIGIN, upstream_port=dashboard, cockpit_port=desktop,
                              input_enabled=True, phone_policy_reviewed=True)
        with serving(relay.make_server(config,store=Store(),input_client=Input(31),test_port=0)) as port:
            _, catalog = request(port, 'GET', '/view/spawn')
            expected = {'state':'failed','failure_reason':'unsupported_tools'}
            result = request(port, 'POST', '/view/spawn', payload(catalog))
            if asynchronous:
                assert result == (200, {'state':'launching','name':'FixtureCurie'})
                expected['name'] = 'FixtureCurie'
            else:
                assert result == (200, expected)
            for _ in range(2):
                assert request(port, 'GET', '/view/spawn/status?receipt=' + RECEIPT) == (200, expected)
            assert sum(call == '/api/spawn-status?name=FixtureCurie' for call in calls) == int(asynchronous)


def test_lost_acknowledgement_stays_unknown_even_if_confirmation_is_repeated():
    submitted = []
    submitted_event = threading.Event()
    let_finish = threading.Event()

    def launch(body):
        submitted.append(body)
        submitted_event.set()
        let_finish.wait(3)
        raise TimeoutError

    store = MobileSpawn(lambda: CATALOG, launch, lambda _: {})
    catalog = store.catalog('a'*32, configurable=True)
    values = payload(catalog)
    answer = []
    worker = threading.Thread(target=lambda: answer.append(store.launch('a'*32, values)))
    worker.start()
    try:
        assert submitted_event.wait(3)
        assert store.launch('a'*32, values) == {'state':'submitting'}
    finally:
        let_finish.set()
        worker.join(3)
    assert answer == [{'state':'unknown'}]
    assert store.launch('a'*32,values) == {'state':'unknown'}
    assert len(submitted) == 1
