"""Offline API/job boundaries; fake adapters never invoke a product or provider."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

from dashboard import managed_spawn as m


class Adapter:
    def __init__(self, job, *, broken=False, finalize_error=False):
        self.job, self.calls, self.tick = job, [], 0
        self.broken, self.finalize_error = broken, finalize_error
    def prepare(self, **kwargs):
        self.calls.append('prepare')
        self.kwargs = kwargs
        Path(kwargs['state']).mkdir()
    def start(self, state, *, run):
        self.calls.append('start')
        return SimpleNamespace(poll=lambda: 0)  # Wrapper already finished!
    def status(self, state):
        self.tick += 1
        if self.tick == 2:
            assert m.read(self.job / 'status.json')['state'] == 'pending'
        return {'agent_name': 'TestChild', 'runtime_attempt_id': 'runtime-generated',
                'ready': self.tick > 1 and not self.broken,
                'terminal': self.tick > 2, 'task_success': None, 'runtime_success': None}
    def finalize(self, state, *, parent_call, run):
        self.calls.append('finalize')
        if self.finalize_error:
            raise RuntimeError('no proof')
        return {'task_success': not self.broken, 'runtime_success': True}
    def clean(self, state, *, run):
        self.calls.append('clean')


class ManagedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / 'source'; self.source.mkdir()
        (self.source / 'result.txt').write_text('original')
        self.backend = self.root / 'backend.json'
        m.save(self.backend, {'backends': {b: {'model': b + '-model', 'effort': 'low'} for b in m.PROVIDERS}})
        self.template = self.root / 'template.json'
        m.save(self.template, {'home': str(self.root / 'home'), 'project_key': str(self.root / 'project'),
                  'backend_file': str(self.backend), 'profiles': {b: {'model': b + '-model'} for b in m.PROVIDERS}})
        self.config = self.root / 'manifest.json'
        m.save(self.config, {'enabled': True, 'state_root': str(self.root / 'jobs'),
            'harnesses': {h: {'config_file': str(self.template), 'python': '/usr/bin/python3'} for h in m.HARNESSES}})
        self.providers = [{'id': b, 'efforts': ['off', 'low', 'high', 'max'],
            'models': [{'id': b + '-model', 'harnesses': [{'id': h, 'available': True} for h in m.HARNESSES]}]}
            for b in m.PROVIDERS]
        self.spawn_calls = []
        def spawn(*args, **kwargs):
            self.spawn_calls.append((args, kwargs)); return SimpleNamespace(pid=1234)
        self.loader = lambda *a, **k: self.providers
        self.manager = m.Manager(self.config, provider_loader=self.loader, spawn=spawn)
    def payload(self, provider='strata', harness='dsh'):
        return {'attempt_id': str(uuid.uuid4()), 'provider': provider, 'harness': harness,
                'model': provider + '-model', 'parent': 'TestParent', 'task': 'write scoped result',
                'dir': str(self.source), 'allowed_paths': ['result.txt'],
                'expected': [{'path': 'result.txt', 'contains': 'done'}]}
    def parent_call(self, tool, args):
        self.assertEqual(tool, 'whois'); return {'name': args['name'], 'retired_at': None}
    def test_four_combinations_cleanup_and_wrapper_not_ready(self):
        for provider in m.PROVIDERS:
            for harness in m.HARNESSES:
                with self.subTest(provider=provider, harness=harness):
                    request = self.payload(provider, harness)
                    accepted = self.manager.submit(request)
                    self.assertEqual(accepted['state'], 'pending')
                    job = self.root / 'jobs' / request['attempt_id']
                    adapter = Adapter(job)
                    final = m.run_job(job, adapter=adapter, parent_call=self.parent_call,
                                      run=object(), sleep=lambda _: None, provider_loader=self.loader)
                    self.assertEqual(adapter.calls, ['prepare', 'start', 'finalize', 'clean'])
                    self.assertTrue(final['task_success']); self.assertTrue(final['runtime_success'])
                    self.assertTrue(final['finished']); self.assertEqual(final['phase'], 'cleaned')
                    self.assertNotEqual(final['attempt_id'], final['runtime_attempt_id'])
                    self.assertEqual((job / 'work/result.txt').read_text(), 'original')
                    self.assertEqual((self.source / 'result.txt').read_text(), 'original')
    def test_retry_and_restart_reuse_job_not_identity(self):
        request = self.payload(); self.manager.submit(request)
        again = m.Manager(self.config, provider_loader=self.loader, spawn=lambda *a, **k: self.fail('second worker'))
        self.assertEqual(again.submit(request)['attempt_id'], request['attempt_id'])
        self.assertEqual(len(self.spawn_calls), 1)
        request['task'] = 'different'
        self.assertEqual(again.submit(request)['error_code'], 'attempt_conflict')
    def test_default_off_global_and_unavailable_do_not_spawn(self):
        self.assertEqual(m.catalog(None, selected_mode='legacy'), [])
        self.assertFalse(m.Manager(None).submit(self.payload())['ok'])
        global_manager = m.Manager(self.config, selected_mode='global', spawn=lambda *a, **k: self.fail('spawn'))
        self.assertEqual(global_manager.submit(self.payload())['error_code'], 'unsupported_mode')
        self.providers[1]['models'][0]['harnesses'][0]['available'] = False
        self.assertFalse(self.manager.submit(self.payload())['ok'])
        self.assertEqual(self.spawn_calls, [])
    def test_client_cannot_supply_commands_scope_escape_or_context(self):
        for extra in ({'node': '/tmp/node'}, {'context': 2}, {'standalone': True},
                      {'allowed_paths': ['../outside']}, {'allowed_paths': ['.git/config']},
                      {'allowed_paths': ['*']}, {'expected': [{'path': 'outside', 'contains': 'x'}]}):
            request = self.payload(); request.update(extra)
            self.assertFalse(self.manager.submit(request)['ok'])
        self.assertEqual(self.spawn_calls, [])
    def test_worker_environment_never_inherits_keys_or_loaders(self):
        with patch.dict('os.environ', {'DEEPSEEK_API_KEY': 'dummy', 'NODE_OPTIONS': '--import evil',
                                     'CHILD_REGISTRATION_TOKEN': 'dummy'}):
            self.manager.submit(self.payload())
        env = self.spawn_calls[0][1]['env']
        for key in ('DEEPSEEK_API_KEY', 'NODE_OPTIONS', 'CHILD_REGISTRATION_TOKEN'):
            self.assertNotIn(key, env)
    def test_parent_mismatch_before_prepare(self):
        request = self.payload(); self.manager.submit(request)
        job = self.root / 'jobs' / request['attempt_id']; adapter = Adapter(job)
        final = m.run_job(job, adapter=adapter, parent_call=lambda *a: {'name': 'OtherParent'},
                          run=object(), provider_loader=self.loader)
        self.assertEqual(adapter.calls, []); self.assertFalse(final['task_success']) if final['task_success'] is not None else None
        self.assertEqual(final['state'], 'failed')
    def test_cleanup_failure_is_not_success_and_not_retried(self):
        request = self.payload(); self.manager.submit(request)
        job = self.root / 'jobs' / request['attempt_id']; adapter = Adapter(job, finalize_error=True)
        final = m.run_job(job, adapter=adapter, parent_call=self.parent_call,
                          run=object(), sleep=lambda _: None, provider_loader=self.loader)
        self.assertEqual(adapter.calls.count('finalize'), 1)
        self.assertNotIn('clean', adapter.calls)
        self.assertIsNone(final['runtime_success']); self.assertEqual(final['state'], 'failed')
    def test_terminal_without_readiness_cannot_be_success(self):
        request = self.payload(); self.manager.submit(request)
        job = self.root / 'jobs' / request['attempt_id']; adapter = Adapter(job, broken=True)
        final = m.run_job(job, adapter=adapter, parent_call=self.parent_call,
                          run=object(), sleep=lambda _: None, provider_loader=self.loader)
        self.assertEqual(final['state'], 'failed'); self.assertFalse(final['task_success'])
    def test_stop_routes_same_name_to_persistent_job(self):
        request = self.payload(); self.manager.submit(request)
        job = self.root / 'jobs' / request['attempt_id']
        current = m.read(job / 'status.json'); current['name'] = 'TestChild'; m.save(job / 'status.json', current)
        self.assertTrue(self.manager.stop('TestChild')['pending'])
        self.assertTrue((job / 'stop-request.json').exists()); self.assertIsNone(self.manager.stop('NativeChild'))
    def test_disable_preserves_lookup_stop_and_same_attempt_without_new_worker(self):
        request = self.payload(); accepted = self.manager.submit(request)
        job = self.root / 'jobs' / request['attempt_id']
        status = m.read(job / 'status.json'); status['name'] = 'TestChild'; m.save(job / 'status.json', status)
        manifest = m.read(self.config); manifest['enabled'] = False; m.save(self.config, manifest)
        self.assertEqual(self.manager.lookup(request['attempt_id'])['attempt_id'], request['attempt_id'])
        self.assertEqual(self.manager.submit(request)['attempt_id'], request['attempt_id'])
        self.assertTrue(self.manager.stop('TestChild')['ok'])
        self.assertFalse(self.manager.submit(self.payload())['ok'])
        self.assertEqual(len(self.spawn_calls), 1)

    def test_selected_model_mismatch_before_worker(self):
        data = m.read(self.backend); data['backends']['strata']['model'] = 'other'; m.save(self.backend, data)
        self.assertEqual(self.manager.submit(self.payload())['error_code'], 'template_model_mismatch')
        self.assertEqual(self.spawn_calls, [])
    def test_effort_projection_changes_private_copy_only(self):
        for harness in m.HARNESSES:
            request = self.payload(harness=harness); request['effort'] = 'off'; self.manager.submit(request)
            job = self.root / 'jobs' / request['attempt_id']; adapter = Adapter(job)
            m.run_job(job, adapter=adapter, parent_call=self.parent_call, run=object(),
                      sleep=lambda _: None, provider_loader=self.loader)
            projected = m.read(job / 'run-template.json')
            if harness == 'dsh':
                self.assertEqual(m.read(projected['backend_file'])['backends']['strata']['effort'], 'off')
                self.assertEqual(m.read(self.backend)['backends']['strata']['effort'], 'low')
            else:
                self.assertEqual(projected['profiles']['strata']['reasoning_effort'], 'none')
                self.assertNotIn('reasoning_effort', m.read(self.template)['profiles']['strata'])


if __name__ == '__main__':
    unittest.main()

class ServerRoutingTests(unittest.TestCase):
    def setUp(self):
        from dashboard import server
        self.server = server
    def test_managed_dispatch_precedes_old_registration_path(self):
        response = {'ok': True, 'managed': True, 'pending': True, 'attempt_id': 'request'}
        manager = SimpleNamespace(submit=lambda payload: response)
        with patch.object(self.server, '_managed_spawn_manager', return_value=manager), \
             patch.object(self.server, '_spawn_request', side_effect=AssertionError('old path')):
            self.assertEqual(self.server.do_spawn({'provider': 'strata', 'harness': 'dsh'}), response)
    def test_native_harness_metadata_preserves_old_payload(self):
        for provider, harness in [('claude', 'claudecode'), ('codex', 'codex'), ('gemini', 'gemini')]:
            request = {'provider': provider, 'task': 'old task', 'parent': 'Parent'}
            with patch.object(self.server, '_spawn_unavailable_error', return_value=None), \
                 patch.object(self.server, '_spawn_request', side_effect=lambda p: (None, {'ok': False, 'snapshot': p})):
                old = self.server.do_spawn(request)
                new = self.server.do_spawn({**request, 'harness': harness, 'helper_model': 'same'})
                self.assertEqual(old, new)
        with patch.object(self.server, '_spawn_request', side_effect=AssertionError('fallback')):
            self.assertFalse(self.server.do_spawn({'provider': 'claude', 'harness': 'unknown'})['ok'])
    def test_catalog_same_consumer_off_on_off(self):
        import types
        module = types.ModuleType('dashboard.managed_catalog')
        module.load_catalog_inputs = lambda *a, **k: ({}, {}, True)
        added = [{'id': 'strata', 'managed': True}]
        module.catalog_providers = lambda *a, **k: added
        fake_models = SimpleNamespace(models=('sonnet',), source='fixture', error=None, overflow=())
        with patch.object(self.server.subprocess, 'run', return_value=SimpleNamespace(stdout='Bright\n\x1eCurie\n')), \
             patch.object(self.server, '_claude_catalog', return_value=fake_models), \
             patch.object(self.server, '_claude_default_model', return_value='sonnet'), \
             patch.object(self.server, '_spawn_scientist_statuses', return_value={}), \
             patch.object(self.server, '_portrait_file', return_value=None), \
             patch.object(self.server.codex_models, 'provider_catalog', return_value={'id': 'codex'}), \
             patch.dict('sys.modules', {'dashboard.managed_catalog': module}), \
             patch.dict('os.environ', {'AGENTSTACK_MANAGED_SPAWN_CONFIG': ''}):
            old = self.server.spawn_names_payload()
            with patch.dict('os.environ', {'AGENTSTACK_MANAGED_SPAWN_CONFIG': '/fixture/manifest'}):
                enabled = self.server.spawn_names_payload()
            off_again = self.server.spawn_names_payload()
        self.assertEqual(old, off_again)
        self.assertEqual(enabled['providers'], old['providers'] + added)
        for key in old.keys() - {'providers'}:
            self.assertEqual(old[key], enabled[key])

class ProfileBusyTests(unittest.TestCase):
    setUp = ManagedTests.setUp
    payload = ManagedTests.payload
    def test_hermes_same_profile_refused_but_other_backend_and_dsh_allowed(self):
        request = self.payload(harness='hermes'); self.assertTrue(self.manager.submit(request)['ok'])
        self.assertEqual(self.manager.submit(self.payload(harness='hermes'))['error_code'], 'profile_busy')
        self.assertTrue(self.manager.submit(self.payload(provider='deepseek', harness='hermes'))['ok'])
        self.assertTrue(self.manager.submit(self.payload(harness='dsh'))['ok'])
        self.assertEqual(self.manager.submit(request)['attempt_id'], request['attempt_id'])

class ManagedHttpTests(unittest.TestCase):
    def test_post_202_and_uuid_lookup_share_existing_routes(self):
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        from dashboard import server
        aid = str(uuid.uuid4())
        accepted = {'ok': True, 'managed': True, 'attempt_id': aid, 'state': 'pending', 'pending': True}
        manager = SimpleNamespace(submit=lambda p: accepted, lookup=lambda value: {**accepted, 'attempt_id': value})
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True); thread.start()
        try:
            with patch.object(server, '_managed_spawn_manager', return_value=manager):
                base = 'http://127.0.0.1:' + str(httpd.server_port)
                request = urllib.request.Request(base + '/api/spawn',
                    data=json.dumps({'provider': 'strata', 'harness': 'dsh'}).encode(),
                    headers={'Content-Type': 'application/json'}, method='POST')
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(response.status, 202)
                    self.assertEqual(json.load(response)['attempt_id'], aid)
                with urllib.request.urlopen(base + '/api/spawn-status?attempt_id=' + aid) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(json.load(response)['attempt_id'], aid)
        finally:
            httpd.shutdown(); thread.join(); httpd.server_close()

class ParentContextTests(unittest.TestCase):
    def test_whois_uses_existing_context_without_parent_owner_or_inbox(self):
        calls = []
        core = SimpleNamespace(_project_key=lambda: '/synthetic/project',
            _runtime_agent_token=lambda *a: self.fail('must not read parent owner'),
            _mcp_call=lambda tool, args: (calls.append((tool, args)) or {'ok': True, 'data': {'name': args['agent_name']}}))
        connection = m.parent_connection(core, 'Parent')
        self.assertEqual(connection('whois', {'name': 'Child'}), {'name': 'Child'})
        self.assertEqual(calls[0][0], 'whois')
        self.assertNotIn('registration_token', calls[0][1])
        self.assertFalse(calls[0][1]['include_recent_commits'])
        with self.assertRaises(ValueError):
            connection('fetch_inbox', {})
        self.assertEqual(len(calls), 1)
