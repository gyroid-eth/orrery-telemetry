"""Reserved-task fixtures. No real registration, Keychain, API or Mail."""
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace
import unittest

import test_hermes_managed as pilot

ADAPTER = pilot.ADAPTER
sys.path.insert(0, str(ADAPTER))
import completion
import diagnostics
import mail_lifecycle
import mail_probe
import managed_entry
import provider_audit
import profile_projection
import runner
import task_evidence
import task_plugin


class FileTask(unittest.TestCase):
    def setUp(self):
        self.fixture = pilot.PilotFixture('runTest')
        self.fixture.setUp()
        self.root, self.product = self.fixture.root, self.fixture.product
        self.state = self.root / 'reserved-state'
        inputs = runner.read(self.fixture.config_path)
        inputs['profiles']['strata'].update(context_length=131072)
        runner.save(self.fixture.config_path, inputs)
        runner.initialize(self.fixture.config_path, self.state, 'strata', self.product.run, file_task=True)
        mail_lifecycle.register(self.state, self.product.run, generate_config=False)
        runner.configure(self.state, self.product.run)
        self.config = runner.read(self.state / 'run-config.json')
        self.task = runner.read(self.state / 'task.json')
        self.attempt = runner.read(self.state / 'attempt.json')
        self.target = Path(self.task['worktree']) / 'trial-ok.txt'

    def tearDown(self):
        self.fixture.tearDown()

    def reservations(self):
        rows = [{'id': 71, 'owner': 'FixtureChild', 'path_pattern': 'trial-ok.txt',
                 'expires_ts': (datetime.now(timezone.utc) + timedelta(seconds=1800)).isoformat()}]
        runner.save(self.state / 'reservations.json', {'attempt_id': self.attempt['attempt_id'],
            'agent_name': 'FixtureChild', 'source': 'product-bound-proxy', 'receipts': rows})
        runner.save(self.state / 'mail-probe.json', {'schema': True, 'backend': True, 'ok': True,
                                                  'agent_name': 'FixtureChild', 'fixture': ''})
        runner.apply_reservations(self.state)
        self.task = runner.read(self.state / 'task.json')
        return rows

    def plugin(self):
        callbacks = {}
        class Context:
            def register_hook(self, name, callback):
                callbacks[name] = callback
        prior = os.environ.get('HERMES_HOME')
        os.environ['HERMES_HOME'] = self.config['profile_home']
        try:
            task_plugin.install(Context(), self.state)
        finally:
            if prior is None:
                os.environ.pop('HERMES_HOME', None)
            else:
                os.environ['HERMES_HOME'] = prior
        return callbacks

    def completed_fixture(self):
        self.reservations()
        callbacks = self.plugin()
        before, after = callbacks['pre_tool_call'], callbacks['post_tool_call']
        args = {'path': str(self.target)}
        self.assertIsNone(before('read_file', args))
        after('read_file', '{}', args=args, status='ok')
        self.assertIsNone(before('write_file', args))
        self.target.write_text('hermes-first-ok\n')
        after('write_file', '{}', args=args, status='ok')
        self.assertIsNone(before(task_plugin.MAIL_TOOL, runner.read(self.state / 'message.json')))
        after(task_plugin.MAIL_TOOL, {'verified_sender': True, 'deliveries': [{'payload': {
            'id': 82, 'from': 'FixtureChild', 'to': ['FixtureParent']}}]}, status='ok')
        managed_entry.turn_writer(self.state)(None, {'completed': True})
        task_evidence.audit(self.state, 'provider-request-start', seq=1)

    def test_sync_denials_private_memory_and_loaded_callback_gate(self):
        unsafe = runner.read(self.fixture.config_path)
        unsafe['worktree'] = unsafe['project_key']
        unsafe_path = self.root / 'unsafe-input.json'
        runner.save(unsafe_path, unsafe)
        with self.assertRaisesRegex(ValueError, 'dedicated worktree'):
            runner.initialize(unsafe_path, self.root / 'unsafe-state', 'strata', self.product.run, file_task=True)
        self.assertFalse(Path(unsafe['worktree']).exists())
        self.reservations()
        callbacks = self.plugin()
        before = callbacks['pre_tool_call']
        self.assertEqual(before('write_file', {'path': str(self.root / 'outside')})['message'], 'outside-worktree')
        self.assertEqual(before('write_file', {'path': str(self.target.parent / 'other')})['message'], 'not-reserved')
        self.assertIsNone(before('write_file', {'path': str(self.target)}))
        self.assertIsNone(before('memory', {'action': 'add', 'content': 'private fixture'}))
        self.assertIsNone(before('write_file', {'path': str(Path(self.config['profile_home']) / 'skills/new/SKILL.md')}))
        self.assertEqual(before('session_search', {'profile': 'another'})['action'], 'block')
        rows = self.task['reservation_receipts']
        rows[0]['expires_ts'] = '2000-01-01T00:00:00Z'
        runner.save(self.state / 'task.json', self.task)
        expired = self.plugin()['pre_tool_call']
        self.assertEqual(expired('write_file', {'path': str(self.target)})['message'], 'reservation-expired')
        manager = SimpleNamespace(iter_hook_callbacks=lambda _: [before])
        self.assertIs(task_plugin.assert_registered(manager, self.state), before)
        for found in ([], [before, lambda **_: None]):
            with self.assertRaises(RuntimeError):
                task_plugin.assert_registered(SimpleNamespace(iter_hook_callbacks=lambda _: found), self.state)

    def test_five_checks_accept_server_send_and_match_optional_parent_receipt(self):
        self.completed_fixture()
        result = runner.evaluate(self.state)
        self.assertTrue(result['success'])
        self.assertEqual(result['missing'], [])
        capture = self.root / 'parent-inbox.json'
        for row in ({'id': 82, 'from': 'FixtureChild', 'kind': 'cc'},
                    {'id': 82, 'from': 'FixtureChild', 'kind': 'to', 'to': ['FixtureParent']},
                    {'id': 83, 'from': 'FixtureChild', 'kind': 'to'}):
            runner.save(capture, [row])
            with self.assertRaises(ValueError):
                mail_lifecycle.record_mail(self.state, capture)
        runner.save(capture, {'structuredContent': [{'id': 82, 'from': 'FixtureChild', 'kind': 'to'}]})
        mail_lifecycle.record_mail(self.state, capture)
        self.assertTrue(runner.evaluate(self.state)['success'])
        parent_receipt = runner.read(self.state / 'parent-mail-receipt.json')
        for change in ({'message_id': 83}, {'from': 'OtherChild'}, {'to': ['OtherParent']}):
            runner.save(self.state / 'parent-mail-receipt.json', {**parent_receipt, **change})
            self.assertFalse(runner.evaluate(self.state)['success'])
        runner.save(self.state / 'parent-mail-receipt.json', parent_receipt)
        self.assertTrue(runner.evaluate(self.state)['success'])
        runner.save(self.state / 'runtime-completion.json', {'attempt_id': self.attempt['attempt_id'], 'success': True})
        scan = {'attempt_id': self.attempt['attempt_id'], 'source': 'operator-private-prefix-scan', 'matches': 0, 'errors': 0}
        runner.save(self.state / 'diagnostic-scan.json', scan)
        self.assertTrue(runner.status(self.state)['full_managed_task_accepted'])
        runner.save(self.state / 'runtime-completion.json', {'attempt_id': self.attempt['attempt_id'], 'success': 'true'})
        self.assertIsNone(runner.status(self.state)['full_managed_task_accepted'])
        runner.save(self.state / 'runtime-completion.json', {'attempt_id': self.attempt['attempt_id'], 'success': True})
        runner.save(self.state / 'diagnostic-scan.json', {**scan, 'matches': 1})
        self.assertFalse(runner.status(self.state)['full_managed_task_accepted'])
        runner.save(self.state / 'diagnostic-scan.json', {**scan, 'attempt_id': 'other-attempt'})
        self.assertIsNone(runner.status(self.state)['full_managed_task_accepted'])
        task_evidence.audit(self.state, 'blocked-network', reason='fixture-denial')
        self.assertFalse(runner.evaluate(self.state)['success'])
        (self.state / 'audit.jsonl').unlink()
        self.assertIsNone(runner.evaluate(self.state)['success'])
        task_evidence.event(self.state, 'turn-end', reason_kind='interrupted')
        runner.save(self.state / 'runtime-exit.json', {'exit_code': 0})
        self.assertFalse(runner.evaluate(self.state)['success'])

    def test_server_send_failure_wrong_attempt_and_missing_delivery_are_not_success(self):
        self.completed_fixture()
        path = self.state / 'events.jsonl'
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        for change in ({'ok': False}, {'verified_sender': False}, {'attempt_id': 'other'},
                       {'deliveries': 0}, {'deliveries': None}, {'from': 'OtherChild'},
                       {'to': ['OtherParent']}, {'message_id': 0}):
            changed = [{**row, **change} if row.get('phase') == 'mail-sent' else row for row in rows]
            path.write_text(''.join(json.dumps(row) + '\n' for row in changed))
            self.assertFalse(runner.evaluate(self.state)['success'], change)
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        (self.state / 'parent-mail-receipt.json').write_text('invalid-json')
        self.assertFalse(runner.evaluate(self.state)['success'])

    def test_product_reserve_projection_and_exact_release_count(self):
        outer = self
        class Session:
            released = 0
            async def list_tools(self):
                return SimpleNamespace(tools=[SimpleNamespace(name=n) for n in mail_probe.REQUIRED])
            async def call_tool(self, name, args):
                if name == 'runtime_status':
                    value = {'agent_name': 'FixtureChild', 'project_key': outer.config['project_key'],
                             'program': 'claude-code', 'lineage': {'parent_agent': 'FixtureParent'}}
                elif name == 'whois':
                    value = {'name': 'FixtureChild', 'program': 'hermes'}
                elif name == 'reserve_files':
                    outer.assertGreaterEqual(args['ttl_seconds'], 600)
                    value = {'granted': [{'id': 71, 'owner': 'FixtureChild', 'path_pattern': 'trial-ok.txt',
                        'expires_ts': (datetime.now(timezone.utc) + timedelta(seconds=1800)).isoformat()}]}
                else:
                    outer.assertEqual(name, 'release_reservations')
                    outer.assertEqual(args['file_reservation_ids'], [71])
                    value = {'released': self.released}
                return pilot.reply(value)
        session = Session()
        asyncio.run(mail_probe.exercise(session, self.state, 'reserve', {}))
        runner.save(self.state / 'mail-probe.json', {'schema': True, 'backend': True, 'ok': True,
                                                  'agent_name': 'FixtureChild'})
        runner.apply_reservations(self.state)
        self.assertEqual(runner.read(self.state / 'task.json')['reservation_receipts'][0]['id'], 71)
        runner.save(self.state / 'runtime-exit.json', {'group_absent': True, 'group_check': 'observed'})
        with self.assertRaisesRegex(ValueError, 'Release not confirmed'):
            asyncio.run(mail_probe.exercise(session, self.state, 'release', {}))
        self.assertFalse(runner.read(self.state / 'release-receipt.json')['confirmed'])
        session.released = 1
        asyncio.run(mail_probe.exercise(session, self.state, 'release', {}))
        self.assertTrue(runner.read(self.state / 'release-receipt.json')['confirmed'])
        receipt = mail_lifecycle.retire(self.state, self.product.run, require_release=True)
        self.assertFalse(receipt['confirmed'])
        self.assertTrue(completion.evaluate_runtime(self.state)['reservations_released'])

    def test_transport_policy_counts_and_diagnostics_never_print_prefix(self):
        policy = provider_audit.Policy(self.state, limit=1)
        self.assertEqual(policy.endpoint, ('https', 'provider.invalid', '/v1/messages'))
        profile_path = Path(self.config['profile_home']) / 'config.yaml'
        original = runner.read(profile_path)
        changed = json.loads(json.dumps(original))
        changed['providers']['fixture']['base_url'] = 'https://provider.invalid/v1'
        runner.save(profile_path, changed)
        self.assertEqual(provider_audit.Policy(self.state).endpoint[2], '/v1/messages')
        changed['providers']['fixture']['api_mode'] = 'chat_completions'
        runner.save(profile_path, changed)
        self.assertEqual(provider_audit.Policy(self.state).endpoint[2], '/v1/chat/completions')
        runner.save(profile_path, original)
        valid = SimpleNamespace(method='POST', url='https://provider.invalid/v1/messages')
        self.assertEqual(policy.before(valid), 1)
        with self.assertRaises(RuntimeError):
            policy.before(valid)
        with self.assertRaises(RuntimeError):
            policy.before(SimpleNamespace(method='GET', url='https://user:offline-password@other.invalid/models?key=private-key-in-query#hidden'))
        audit = (self.state / 'audit.jsonl').read_text()
        for secret in ('private-key-in-query', 'offline-password', 'hidden'):
            self.assertNotIn(secret, audit)
        denied = [json.loads(row) for row in audit.splitlines() if json.loads(row)['phase'] == 'blocked-network'][0]
        self.assertEqual((denied['method'], denied['host'], denied['port'], denied['path']),
                         ('GET', 'other.invalid', 443, '/models'))
        self.assertTrue(denied['callers'])
        dummy = b'offline-canary-value-1234'
        (self.state / 'stdout.log').write_bytes(dummy)
        result = diagnostics.scan(self.state, lambda *a, **k: SimpleNamespace(returncode=0, stdout=dummy))
        self.assertEqual(result['matches'], 1)
        self.assertNotIn(dummy[:8].decode(), json.dumps(result))

    def test_explicit_context_projection_does_not_invent_output_limit(self):
        profile = runner.read(Path(self.config['profile_home']) / 'config.yaml')
        self.assertEqual(profile['model']['context_length'], 131072)
        self.assertEqual(profile['providers']['fixture']['context_length'], 131072)
        self.assertNotIn('max_tokens', profile['model'])
        self.assertEqual(profile['model_overrides']['fixture']['fixture-model']['context_window'], 131072)
        spec = runner.read(self.fixture.config_path)['profiles']['strata']
        for invalid in (False, 0, -1, '131072', 1.5):
            with self.assertRaises(ValueError):
                profile_projection.project({**spec, 'context_length': invalid}, self.root,
                                           ['title_generation', 'background_review'])

    def test_configured_request_cap_denies_ninth_and_rejects_invalid_limits(self):
        runner.save(self.state / 'run-config.json', {**self.config, 'provider_request_limit': 8})
        policy = provider_audit.Policy(self.state)
        request = SimpleNamespace(method='POST', url='https://provider.invalid/v1/messages')
        for seq in range(1, 9):
            self.assertEqual(policy.before(request), seq)
        with self.assertRaisesRegex(RuntimeError, 'request limit'):
            policy.before(request)
        rows = [json.loads(row) for row in (self.state / 'audit.jsonl').read_text().splitlines()]
        self.assertEqual(sum(row['phase'] == 'provider-request-start' for row in rows), 8)
        self.assertEqual(sum(row['phase'] == 'limit-reached' for row in rows), 1)
        inputs = runner.read(self.fixture.config_path)
        for value in (0, -1, True, '8', None, 1.5):
            bad = {**inputs, 'provider_request_limit': value}
            path = self.root / 'invalid-limit.json'
            runner.save(path, bad)
            with self.assertRaisesRegex(ValueError, 'request limit'):
                runner.initialize(path, self.root / 'invalid-limit-state', 'deepseek', self.product.run, file_task=True)
            runner.save(self.state / 'run-config.json', {**self.config, 'provider_request_limit': value})
            with self.assertRaises(ValueError):
                provider_audit.Policy(self.state)

    def test_request_limit_is_unbounded_by_default_and_accepts_larger_trial_cap(self):
        request = SimpleNamespace(method='POST', url='https://provider.invalid/v1/messages')
        policy = provider_audit.Policy(self.state)
        self.assertIsNone(policy.limit)
        for seq in range(1, 66):
            self.assertEqual(policy.before(request), seq)
        self.assertNotIn('limit-reached', (self.state / 'audit.jsonl').read_text())
        inputs = {**runner.read(self.fixture.config_path), 'provider_request_limit': 100}
        path = self.root / 'larger-trial-input.json'
        runner.save(path, inputs)
        state = self.root / 'larger-trial-state'
        runner.initialize(path, state, 'deepseek', self.product.run, file_task=True)
        self.assertEqual(runner.read(state / 'run-config.json')['provider_request_limit'], 100)
        # Runtime policy reads the same value without requiring another profile.
        runner.save(self.state / 'run-config.json', {**self.config, 'provider_request_limit': 100})
        self.assertEqual(provider_audit.Policy(self.state).limit, 100)

    def test_installed_deepseek_catalog_pricing_and_messages_route_without_context_pin(self):
        python, source = os.environ.get('ORRERY_HERMES_TEST_PYTHON'), os.environ.get('ORRERY_HERMES_TEST_SOURCE')
        if not python or not source:
            self.skipTest('Explicit installed dependency interpreter/source required')
        profile_home = self.root / 'deepseek-profile'
        spec = {'provider': 'deepseek-api', 'model': 'deepseek-flash',
                'base_url': 'https://api.deepseek.com/anthropic', 'api_mode': 'anthropic_messages',
                'key_cmd': 'fixture-keychain-reference'}
        projected = profile_projection.project(spec, profile_home,
                         ['title_generation', 'background_review', 'compression'])['profile_config']
        self.assertNotIn('context_length', projected['model'])
        self.assertNotIn('model_overrides', projected)
        self.assertNotIn('max_tokens', projected['model'])
        profile_home.mkdir()
        runner.save(profile_home / 'config.yaml', projected)
        config = {**self.config, 'provider': 'deepseek-api', 'model': 'deepseek-flash',
                  'profile_name': 'deepseek', 'profile_home': str(profile_home),
                  'hermes_source': source, 'provider_request_limit': 8}
        runner.save(self.state / 'run-config.json', config)
        script = self.root / 'deepseek_catalog_fixture.py'
        script.write_text('''
import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1]);sys.path.insert(0,sys.argv[3])
state=Path(sys.argv[2])
def no_network(event,args):
    if event in ('socket.connect','socket.getaddrinfo'):
        raise AssertionError('offline fixture forbids sockets')
sys.addaudithook(no_network)
from managed_entry import deny_dotenv,skip_absent_dotenv,disable_live_metadata
deny_dotenv(state);skip_absent_dotenv(state)
import httpx,requests
original_send=httpx.Client._send_single_request
attempts=[]
def reject(*args,**kwargs):
    attempts.append(True)
    raise AssertionError('catalog or pricing attempted transport')
httpx.Client._send_single_request=reject
requests.Session.request=reject
from agent import model_metadata as metadata
from agent import usage_pricing as pricing
from agent.auxiliary_client import _candidate_context_window
route='https://api.deepseek.com/anthropic'
model='deepseek-flash'
def key_must_not_run():
    raise AssertionError('offline metadata resolved a real key')
usage=pricing.CanonicalUsage(input_tokens=12,output_tokens=3)
before_context=metadata.get_model_context_length(model,provider='custom',base_url=route,api_key=key_must_not_run)
# The bundled rate table is reachable with the native billing identity.
native_cost=pricing.estimate_usage_cost(model,usage,provider='deepseek',base_url=route,api_key=key_must_not_run)
assert native_cost.status=='estimated' and native_cost.source=='official_docs_snapshot',native_cost
assert not attempts,attempts
disable_live_metadata(state)
for provider in ('deepseek-api','custom',''):
    for key in ('offline-fixture',key_must_not_run):
        kwargs={'provider':provider,'base_url':route,'api_key':key}
        context=metadata.get_model_context_length(model,**kwargs)
        assert context==before_context and context>0,context
        assert _candidate_context_window(provider,model,route,key)==context
        cost=pricing.estimate_usage_cost(model,usage,**kwargs)
        # Named routes resolve to custom, which the pinned billing resolver
        # does not map back to DeepSeek. Preserve that explicit unknown.
        assert cost.status=='unknown' and cost.amount_usd is None,cost
assert pricing.estimate_usage_cost(model,usage,provider='deepseek',base_url=route,api_key=key_must_not_run)==native_cost
assert not attempts,attempts
# Restore HTTPX, then exercise the real SDK with an in-memory transport only.
httpx.Client._send_single_request=original_send
from provider_audit import install
policy=install(state)
assert policy.endpoint==('https','api.deepseek.com','/anthropic/v1/messages') and policy.limit==8
import anthropic
def response(request):
    assert request.method=='POST' and request.url.path=='/anthropic/v1/messages',request.url
    return httpx.Response(200,json={'id':'msg_fixture','type':'message','role':'assistant','model':model,
        'content':[{'type':'text','text':'fixture'}],'stop_reason':'end_turn','stop_sequence':None,
        'usage':{'input_tokens':1,'output_tokens':1}})
with httpx.Client(transport=httpx.MockTransport(response)) as client:
    sdk=anthropic.Anthropic(api_key='offline-fixture',base_url=route,http_client=client,max_retries=0)
    assert sdk.messages.create(model=model,max_tokens=8,messages=[{'role':'user','content':'fixture'}]).model==model
rows=[json.loads(row) for row in (state/'audit.jsonl').read_text().splitlines()]
assert not [row for row in rows if row['phase'].startswith('blocked-')],rows
print(json.dumps({'context':context,'context_source':'installed-catalog','pricing_source':cost.source,
                  'external_transports':len(attempts),'request_limit':policy.limit,'endpoint':policy.endpoint[2]}))
''')
        env = runner.managed_env(config, runner.read(self.state / 'mail-identity.json'), self.state)
        result = subprocess.run([python, '-I', '-B', str(script), str(ADAPTER), str(self.state), source],
                                 env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout.strip().splitlines()[-1])['external_transports'], 0)

    def test_installed_context_and_absent_dotenv_need_no_transport(self):
        python, source = os.environ.get('ORRERY_HERMES_TEST_PYTHON'), os.environ.get('ORRERY_HERMES_TEST_SOURCE')
        if not python or not source:
            self.skipTest('Explicit installed dependency interpreter/source required')
        config = {**self.config, 'hermes_source': source}
        runner.save(self.state / 'run-config.json', config)
        script = self.root / 'startup_sources_fixture.py'
        script.write_text('''
import json,os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1]);sys.path.insert(0,sys.argv[3])
state=Path(sys.argv[2])
def no_network(event,args):
    if event in ('socket.connect','socket.getaddrinfo'):
        raise AssertionError('offline fixture forbids sockets')
sys.addaudithook(no_network)
from managed_entry import deny_dotenv,skip_absent_dotenv
deny_dotenv(state)
skip_absent_dotenv(state)
import httpx,requests
attempts=[]
def reject(*args,**kwargs):
    attempts.append(True)
    raise AssertionError('context attempted a transport')
httpx.Client._send_single_request=reject
requests.Session.request=reject
from agent.model_metadata import get_model_context_length
for provider in ('fixture','custom','custom:fixture',''):
    for key in ('offline-fixture',lambda:'offline-fixture'):
        for explicit in ({},{'config_context_length':131072}):
            assert get_model_context_length('fixture-model',base_url='https://provider.invalid',
                provider=provider,api_key=key,**explicit)==131072
from agent.auxiliary_client import _candidate_context_window
assert _candidate_context_window('fixture','fixture-model','https://provider.invalid','offline-fixture')==131072
assert _candidate_context_window('custom','fixture-model','https://provider.invalid','offline-fixture')==131072
from hermes_cli.config import load_env
from agent.credential_pool import get_env_prefer_dotenv
assert load_env()=={}
assert get_env_prefer_dotenv('ORRERY_OFFLINE_NONEXISTENT_KEY')==''
assert not attempts,attempts
rows=[json.loads(row) for row in (state/'audit.jsonl').read_text().splitlines()]
assert not [row for row in rows if row['phase'].startswith('blocked-')],rows
absent=[row for row in rows if row['phase']=='dotenv-absent']
assert absent and all(row['path']==str(Path(os.environ['HERMES_HOME'])/'.env') for row in absent),absent
assert any(any('router/' in frame['file'] for frame in row['callers']) for row in absent),absent
assert any(any('credential_pool.py' in frame['file'] for frame in row['callers']) for row in absent),absent
canary=Path(os.environ['HERMES_HOME'])/'.env'
# The early missing-file path must not enable creation of a dotenv file.
try:
    canary.write_text('never-write-this-value')
except PermissionError:
    pass
else:
    raise AssertionError('dotenv write bypassed the guard')
print(json.dumps({'context':131072,'transports':len(attempts),'absent_dotenv_calls':len(absent)}))
''')
        env = runner.managed_env(config, runner.read(self.state / 'mail-identity.json'), self.state)
        result = subprocess.run([python, '-I', '-B', str(script), str(ADAPTER), str(self.state), source],
                                 env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout.strip().splitlines()[-1])['transports'], 0)

    def test_actual_installed_guard_preflight_and_tools_without_provider(self):
        python, source = os.environ.get('ORRERY_HERMES_TEST_PYTHON'), os.environ.get('ORRERY_HERMES_TEST_SOURCE')
        if not python or not source:
            self.skipTest('Explicit installed Hermes dependency interpreter/source required')
        self.reservations()
        config = self.config
        config['hermes_source'] = source
        key = hashlib.sha256(str(Path(source).resolve()).encode()).hexdigest()[:16]
        install = Path(config['managed_root']) / 'installs' / key
        venv = install / 'environments/fixture/venv'
        (venv / 'bin').mkdir(parents=True)
        (venv / 'pyvenv.cfg').write_text('# static fixture\n')
        (venv / 'bin/python').symlink_to(python)
        runner.save(install / 'facts.json', {'packages': {'venv': {'environment': str(venv)}}})
        runner.save(self.state / 'run-config.json', config)
        server = self.root / 'fake_task_mail.py'
        server.write_text('''
import sys
sys.addaudithook(lambda event,args: (_ for _ in ()).throw(RuntimeError('offline fixture'))
                 if event in ('socket.connect','socket.getaddrinfo') else None)
from mcp.server import MCPServer
s=MCPServer('offline-task-mail')
@s.tool()
def send_message(to:list[str],subject:str,body_md:str)->dict:
    return {'verified_sender':True,'deliveries':[{'payload':{'id':71,'from':'FixtureChild','to':to}}]}
s.run(transport='stdio')
''')
        proxy = Path(self.config['product_root']) / 'integrations/codex_app/plugin/scripts/run-mcp.sh'
        proxy.write_text('#!/bin/sh\nexec ' + shlex.join([python, '-I', '-B', str(server)]) + '\n')
        proxy.chmod(0o700)
        script = self.root / 'actual_guard_fixture.py'
        script.write_text('''
import json,logging,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
state=Path(sys.argv[2])
from managed_entry import load_guard,turn_writer
config,identity=load_guard(state)
from hermes_cli import plugins
from task_plugin import assert_registered
assert_registered(plugins.get_plugin_manager(),state)
from hermes_cli.oneshot import _validate_explicit_toolsets
from runner import pilot_toolsets
profile=json.loads((Path(config['profile_home'])/'config.yaml').read_text())
selected,error=_validate_explicit_toolsets(pilot_toolsets(profile,file_task=True))
assert error is None and 'orrery_file' in selected
from hermes_cli.mcp_startup import ensure_mcp_discovery_before_agent_build
from tools.mcp_tool_lifecycle import shutdown_mcp_servers
from provider_audit import install
install(state)
try:
    ensure_mcp_discovery_before_agent_build(logger=logging.getLogger('fixture'),single_query=True)
    import model_tools
    tools=model_tools.get_tool_definitions(enabled_toolsets=selected,quiet_mode=True,skip_tool_search_assembly=True)
    names={item['function']['name'] for item in tools}
    assert {'read_file','write_file','mcp__orrery_mail__send_message'}<=names,names
    assert 'terminal' not in names and 'patch' not in names,names
    target=Path(config['worktree'])/'trial-ok.txt'
    for tool,args in [('read_file',{'path':str(target)}),
                      ('write_file',{'path':str(target),'content':'hermes-first-ok\\n'})]:
        value=json.loads(model_tools.handle_function_call(tool,args,task_id='fixture-task'))
        assert 'error' not in value,value
    message=json.loads((state/'message.json').read_text())
    result=model_tools.handle_function_call('mcp__orrery_mail__send_message',message,task_id='fixture-task')
    receipt=json.loads((state/'mail-send-receipt.json').read_text())
    assert receipt['ok'] is True,result
    import httpx
    with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={}))) as client:
        client.post('https://provider.invalid/v1/messages',json={'model':'fixture-model'})
    turn_writer(state)(None,{'completed':True})
    print(json.dumps({'installed_guard_tools_fixture':True}))
finally:
    shutdown_mcp_servers()
''')
        env = runner.managed_env(config, runner.read(self.state / 'mail-identity.json'), self.state)
        result = subprocess.run([python, '-I', '-B', str(script), str(ADAPTER), str(self.state)],
                                 env=env, capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout.strip().splitlines()[-1])['installed_guard_tools_fixture'])
        # The actual entry's check path loads no provider client and requires the
        # current callback, rather than trusting a stale ready file.
        result = subprocess.run([python, '-I', '-B', str(ADAPTER / 'managed_entry.py'), '--state', str(self.state), '--check'],
                                 env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(runner.read(self.state / 'preflight-complete.json')['guard_loaded'])
        plugin = Path(config['profile_home']) / 'plugins/orrery-mail-receipt/__init__.py'
        plugin.write_text('def register(ctx): pass\n')
        result = subprocess.run([python, '-I', '-B', str(ADAPTER / 'managed_entry.py'), '--state', str(self.state), '--check'],
                                 env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)

    def test_installed_pricing_and_shared_metadata_stay_offline_and_unknown(self):
        python, source = os.environ.get('ORRERY_HERMES_TEST_PYTHON'), os.environ.get('ORRERY_HERMES_TEST_SOURCE')
        if not python or not source:
            self.skipTest('Explicit installed dependency interpreter/source required')
        config = {**self.config, 'hermes_source': source}
        runner.save(self.state / 'run-config.json', config)
        script = self.root / 'pricing_metadata_fixture.py'
        script.write_text('''
import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1]);sys.path.insert(0,sys.argv[3])
state=Path(sys.argv[2])
def no_network(event,args):
    if event in ('socket.connect','socket.getaddrinfo'):
        raise AssertionError('offline fixture forbids sockets')
sys.addaudithook(no_network)
from managed_entry import deny_dotenv,skip_absent_dotenv,disable_live_metadata
deny_dotenv(state);skip_absent_dotenv(state)
import httpx,requests
attempts=[]
def reject(*args,**kwargs):
    attempts.append(True)
    raise AssertionError('pricing attempted transport')
httpx.Client._send_single_request=reject
requests.Session.request=reject
from agent import model_metadata as metadata
# The real pricing consumer already bound both imports before policy install.
from agent import usage_pricing as pricing
before=pricing.fetch_endpoint_model_metadata
disable_live_metadata(state)
assert pricing.fetch_endpoint_model_metadata is metadata.fetch_endpoint_model_metadata
assert pricing.fetch_endpoint_model_metadata is not before
assert pricing.fetch_model_metadata is metadata.fetch_model_metadata
def key_must_not_run():
    raise AssertionError('metadata tried to resolve a key')
for provider in ('fixture','custom','custom:fixture'):
    kwargs={'provider':provider,'base_url':'https://provider.invalid','api_key':key_must_not_run}
    assert pricing.get_pricing_entry('fixture-model',**kwargs) is None
    assert pricing.has_known_pricing('fixture-model',**kwargs) is False
    cost=pricing.estimate_usage_cost('fixture-model',pricing.CanonicalUsage(input_tokens=12,output_tokens=3),**kwargs)
    assert cost.status=='unknown' and cost.amount_usd is None and cost.label=='n/a',cost
    assert metadata.get_model_context_length('fixture-model',**kwargs)==131072
assert metadata.fetch_endpoint_model_metadata('https://provider.invalid',api_key=key_must_not_run,force_refresh=True)=={}
assert metadata.fetch_model_metadata(force_refresh=True)=={}
assert metadata._resolve_endpoint_context_length('fixture-model','https://provider.invalid',api_key=key_must_not_run) is None
assert metadata._resolve_nous_context_length('fixture-model','https://provider.invalid',api_key=key_must_not_run)==(None,'')
assert pricing._openrouter_pricing_entry(pricing.BillingRoute('openrouter','fixture-model')) is None
assert not attempts,attempts
rows=[json.loads(row) for row in (state/'audit.jsonl').read_text().splitlines()]
assert not [row for row in rows if row['phase'].startswith('blocked-')],rows
assert {row['catalog'] for row in rows if row['phase']=='metadata-suppressed'}=={
    'fetch_endpoint_model_metadata','fetch_model_metadata'}
print(json.dumps({'transports':len(attempts),'pricing':'unknown','context':131072}))
''')
        env = runner.managed_env(config, runner.read(self.state / 'mail-identity.json'), self.state)
        result = subprocess.run([python, '-I', '-B', str(script), str(ADAPTER), str(self.state), source],
                                 env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout.strip().splitlines()[-1])['pricing'], 'unknown')

    def test_actual_httpx_async_redirect_and_other_dotenv_are_blocked(self):
        python, source = os.environ.get('ORRERY_HERMES_TEST_PYTHON'), os.environ.get('ORRERY_HERMES_TEST_SOURCE')
        if not python or not source:
            self.skipTest('Explicit installed dependency interpreter/source required')
        other = self.root / 'outside-profile'
        other.mkdir()
        (other / '.env').write_text('ORRERY_OFFLINE_CANARY=do-not-load-this-value\n')
        script = self.root / 'transport_fixture.py'
        script.write_text('''
import asyncio,json,os,socket,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1]);sys.path.insert(0,sys.argv[3])
from managed_entry import deny_dotenv,skip_absent_dotenv
from provider_audit import install
state=Path(sys.argv[2])
deny_dotenv(state)
skip_absent_dotenv(state)
install(state)
from hermes_cli.env_loader import load_hermes_dotenv
try:
    load_hermes_dotenv(hermes_home=sys.argv[4],load_external_secrets=False)
except PermissionError:
    pass
assert 'ORRERY_OFFLINE_CANARY' not in os.environ
import httpx
async def check_async():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={}))) as client:
        await client.post('https://provider.invalid/v1/messages',json={'fixture':True})
asyncio.run(check_async())
import anthropic
def message_reply(request):
    assert request.url.path=='/v1/messages'
    return httpx.Response(200,json={'id':'msg_fixture','type':'message','role':'assistant','model':'fixture-model',
        'content':[{'type':'text','text':'fixture'}],'stop_reason':'end_turn','stop_sequence':None,
        'usage':{'input_tokens':1,'output_tokens':1}})
with httpx.Client(transport=httpx.MockTransport(message_reply)) as client:
    sdk=anthropic.Anthropic(api_key='offline-fixture-credential',base_url='https://provider.invalid',
                            http_client=client,max_retries=0)
    response=sdk.messages.create(model='fixture-model',max_tokens=8,messages=[{'role':'user','content':'fixture'}])
    assert response.content[0].text=='fixture'
with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(302,headers={'Location':'https://other.invalid/'})),
                  follow_redirects=True) as client:
    try:
        client.post('https://provider.invalid/v1/messages')
    except RuntimeError:
        pass
    else:
        raise AssertionError('redirect escaped the transport boundary')
try:
    socket.getaddrinfo('never-resolve.invalid',443)
except RuntimeError:
    pass
else:
    raise AssertionError('unmanaged DNS was allowed')
rows=(state/'audit.jsonl').read_text()
assert 'blocked-dotenv' in rows and 'blocked-network' in rows and 'provider-request-start' in rows
assert 'do-not-load-this-value' not in rows
records=[json.loads(row) for row in rows.splitlines()]
dotenv=next(row for row in records if row['phase']=='blocked-dotenv')
assert dotenv['path']==str(Path(sys.argv[4])/'.env') and dotenv['method']=='open',dotenv
assert dotenv['callers'] and all(set(frame)=={'file','line','function'} for frame in dotenv['callers'])
dns=next(row for row in records if row.get('host')=='never-resolve.invalid')
assert dns['port']==443 and dns['path'] is None and dns['method'] is None,dns
print('offline-transport-fixture-ok')
''')
        env = runner.managed_env(self.config, runner.read(self.state / 'mail-identity.json'), self.state)
        result = subprocess.run([python, '-I', '-B', str(script), str(ADAPTER), str(self.state), source, str(other)],
                                 env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
