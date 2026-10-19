"""Offline product-bound lifecycle controls; no real registration or Mail."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


INTEGRATION = Path(__file__).resolve().parents[1] / 'integrations/dsh'
spec = importlib.util.spec_from_file_location('dsh_mail_lifecycle', INTEGRATION / 'mail_lifecycle.py')
lifecycle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lifecycle)


class ProductLifecycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='dsh-run-connection-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.product = self.root / 'product'
        for file in ['bin/agentstack-runtime-client', 'bin/agentstack-preregister-child',
                     'hooks/child_tools.py', 'integrations/codex_app/plugin/scripts/run-mcp.sh']:
            target = self.product / file
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch(mode=0o700)
        self.config = dict(product_root=str(self.product), project_key='/fixture/project',
            home=str(self.root), node='/fixture/node', proxy_python='/fixture/python',
            parent='FixtureParent', model='fixture-model', worktree=str(self.root),
            connection=dict(runtime_dir=str(self.root / 'runtime'), mcp_url='http://127.0.0.1:1/mcp',
                            mail_env=str(self.root / 'not-read.env'), bearer_mode='auto'))
        lifecycle.save(self.root / 'run-config.json', self.config)
        self.calls = []

    def fake_product(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        self.assertNotIn('DEEPSEEK_API_KEY', kwargs['env'])
        if argv[0].endswith('/agentstack-runtime-client'):
            return subprocess.CompletedProcess(argv, 0, 'legacy\n', '')
        if argv[0].endswith('/agentstack-preregister-child'):
            handoff = Path(argv[argv.index('--token-file-out') + 1])
            handoff.touch(mode=0o600)
            return subprocess.CompletedProcess(argv, 0, 'FixtureChild\n', '')
        if 'claude-config' in argv:
            def arg(name):
                return argv[argv.index(name) + 1]
            proxy = dict(command=arg('--runner'), args=[], env={
                'AGENTSTACK_PROXY_AGENT_NAME': arg('--child'),
                'AGENTSTACK_PROXY_TOKEN_FILE': arg('--token-file'),
                'AGENTSTACK_PROXY_PROGRAM': 'claude-code',
                'AGENTSTACK_PROJECT_KEY': arg('--project-key')})
            lifecycle.save(Path(arg('--out')), {'mcpServers': {'orrery-mail': proxy}})
        return subprocess.CompletedProcess(argv, 0, '', '')

    def test_registration_uses_generated_proxy_unchanged_and_no_retry(self):
        self.assertEqual(lifecycle.register(self.root, self.fake_product), 'FixtureChild')
        config = lifecycle.read(self.root / 'mcp.json')['mcpServers']['orrery-mail']
        actual = lifecycle.read(self.root / 'mail.patch.json')[0]['insert'][0]['config']
        for key in ('command', 'args', 'env'):
            self.assertEqual(actual[key], config[key])
        self.assertEqual(actual['env']['AGENTSTACK_PROXY_PROGRAM'], 'claude-code')
        with self.assertRaises(ValueError):
            lifecycle.register(self.root, self.fake_product)
        self.assertEqual(sum('/agentstack-preregister-child' in call[0][0] for call in self.calls), 1)

    def test_failed_helper_preserves_intent_and_blocks_second_registration(self):
        def failed(argv, **kwargs):
            if argv[0].endswith('/agentstack-preregister-child'):
                return subprocess.CompletedProcess(argv, 1, '', '')
            return self.fake_product(argv, **kwargs)
        with self.assertRaises(ValueError):
            lifecycle.register(self.root, failed)
        self.assertTrue((self.root / 'registration-intent.json').exists())
        with self.assertRaises(FileExistsError):
            lifecycle.register(self.root, self.fake_product)

    def test_unexpected_mode_stops_before_registration_intent(self):
        def global_mode(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, 'global\n', '')
        with self.assertRaises(ValueError):
            lifecycle.register(self.root, global_mode)
        self.assertFalse((self.root / 'registration-intent.json').exists())

    def test_cleanup_exit_zero_is_still_unconfirmed(self):
        lifecycle.register(self.root, self.fake_product)
        lifecycle.save(self.root / 'attempt.json', {'agent_name': 'FixtureChild', 'attempt_id': 'attempt'})
        lifecycle.save(self.root / 'runtime-exit.json', {'group_absent': True, 'group_check': 'observed'})
        lifecycle.save(self.root / 'release-receipt.json', {'agent_name': 'FixtureChild', 'attempt_id': 'attempt', 'confirmed': True})
        receipt = lifecycle.retire(self.root, self.fake_product)
        self.assertEqual(receipt['cleanup_exit_code'], 0)
        self.assertIsNone(receipt['retired_at'])
        self.assertIs(receipt['confirmed'], False)
        lifecycle.save(self.root / 'runtime-exit.json', {'group_absent': False, 'group_check': 'unknown'})
        with self.assertRaises(ValueError):
            lifecycle.retire(self.root, self.fake_product)

    def test_mail_receipt_controls(self):
        node = os.environ.get('ORRERY_DSH_TEST_NODE')
        if not node:
            self.skipTest('explicit fixed Node required')
        code = r'''
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
const {checkBinding,normalizeReservations,releaseReceipt}=await import(pathToFileURL(process.argv[1]));
const identity={agent_name:'Child',project_key:'/fixture',program:'dsh',parent:'Parent'};
const proxy={env:{AGENTSTACK_PROXY_PROGRAM:'claude-code'}};
const status={agent_name:'Child',project_key:'/fixture',program:'claude-code',lineage:{parent_agent:'Parent'}};
const who={name:'Child',program:'dsh',retired_at:null};
checkBinding(status,who,identity,proxy);
assert.throws(()=>checkBinding({...status,agent_name:'Other'},who,identity,proxy));
assert.throws(()=>checkBinding(status,{...who,retired_at:'now'},identity,proxy));
const row={id:1,path_pattern:'one.txt',expires_ts:new Date(Date.now()+1800000).toISOString()};
assert.equal(normalizeReservations({granted:[row]},['one.txt'],'Child')[0].owner,'Child');
assert.throws(()=>normalizeReservations({granted:[row],conflicts:[{}]},['one.txt'],'Child'));
assert.throws(()=>normalizeReservations({granted:[]},['one.txt'],'Child'));
assert.throws(()=>normalizeReservations({granted:[{...row,expires_ts:'2000-01-01'}]},['one.txt'],'Child'));
const attempt={attempt_id:'attempt',agent_name:'Child'};
assert.equal(releaseReceipt({attempt,ids:[1],result:{released:1}}).confirmed,true);
assert.equal(releaseReceipt({attempt,ids:[1],result:{released:0}}).confirmed,false);
assert.equal(releaseReceipt({attempt,ids:[],result:{released:0}}).confirmed,false);
'''
        result = subprocess.run([node, '--input-type=module', '-e', code, str(INTEGRATION / 'mail_probe.mjs')],
                                text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
