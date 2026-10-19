"""Offline product-bound lifecycle controls; no real registration or Mail."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
import sys


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
        lifecycle.save(self.root / 'release-receipt.json', {'agent_name': 'FixtureChild', 'attempt_id': 'attempt', 'confirmed': True, 'source':'product-bound-proxy'})
        receipt = lifecycle.retire(self.root, self.fake_product)
        self.assertEqual(receipt['cleanup_exit_code'], 0)
        self.assertIsNone(receipt['retired_at'])
        self.assertIs(receipt['confirmed'], False)
        lifecycle.save(self.root / 'runtime-exit.json', {'group_absent': False, 'group_check': 'unknown'})
        with self.assertRaises(ValueError):
            lifecycle.retire(self.root, self.fake_product)

    def test_partial_registration_can_retire_without_fabricated_reservation(self):
        lifecycle.register(self.root,self.fake_product)
        lifecycle.save(self.root/'attempt.json',{'agent_name':'FixtureChild','attempt_id':'attempt'})
        lifecycle.save(self.root/'runtime-exit.json',{'group_absent':True,'group_check':'observed','runtime_not_started':True})
        self.assertIs(lifecycle.retire(self.root,self.fake_product)['confirmed'],False)
        lifecycle.save(self.root/'reservation-intent.json',{'paths':['one.txt']})
        with self.assertRaises(ValueError):
            lifecycle.retire(self.root,self.fake_product)

    def test_unsupported_credential_adapter_never_invokes_helper(self):
        sys.path.insert(0,str(INTEGRATION))
        try:
            import launch
        finally:
            sys.path.remove(str(INTEGRATION))
        with mock.patch.object(launch.sys,'platform','linux'), mock.patch.object(launch.subprocess,'run') as helper:
            with self.assertRaisesRegex(ValueError,'not implemented'):
                launch.credential({})
            helper.assert_not_called()

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

    def test_keyless_full_preflight_with_real_tui_and_synthetic_stdio_server(self):
        node, runtime = os.environ.get('ORRERY_DSH_TEST_NODE'), os.environ.get('ORRERY_DSH_TEST_RUNTIME')
        if not node or not runtime:
            self.skipTest('explicit isolated fixed Node/runtime required')
        # This server is a test fixture, not a proxy: no sockets/HTTP/forwarding,
        # tokens, registration or actual Mail. The real SDK/TUI use its stdio.
        server = self.root / 'fixture-server.py'
        server.write_text('''import sys,json,os,datetime
names=['runtime_status','whois','send_message','reserve_files','release_reservations']
for line in sys.stdin:
 try:
  q=json.loads(line)
  if 'id' not in q: continue
  method=q['method']; params=q.get('params',{})
  if method=='initialize':
   r={'protocolVersion':params['protocolVersion'],'capabilities':{'tools':{}},'serverInfo':{'name':'explicit-offline-fixture','version':'1'}}
  elif method=='tools/list':
   r={'tools':[{'name':n,'description':'Offline synthetic fixture','inputSchema':{'type':'object','properties':{'to':{'type':'array','items':{'type':'string'}},'subject':{'type':'string'},'body_md':{'type':'string'}} if n=='send_message' else {}}} for n in names]}
  elif method=='tools/call':
   name=params['name']; args=params.get('arguments',{})
   if name=='whois' and ':1/' in os.environ['AGENTSTACK_MCP_URL']:
    r={'isError':True,'content':[{'type':'text','text':'offline backend-down fixture'}]}
   else:
    if name=='runtime_status': value={'agent_name':'FixtureChild','project_key':'/fixture/project','program':'claude-code','lineage':{'parent_agent':'FixtureParent'}}
    elif name=='whois': value={'name':'FixtureChild','program':'dsh','retired_at':None}
    elif name=='reserve_files': value={'granted':[{'id':i+1,'path_pattern':p,'expires_ts':(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(seconds=1800)).isoformat()} for i,p in enumerate(args['paths'])],'conflicts':[]}
    elif name=='release_reservations': value={'released':len(args['file_reservation_ids'])}
    else: value={'not_real_mail':True}
    r={'content':[{'type':'text','text':json.dumps(value)}],'structuredContent':value}
  else: r={}
  print(json.dumps({'jsonrpc':'2.0','id':q['id'],'result':r}),flush=True)
 except Exception:
  sys.exit(2)
''')
        home = self.root / 'home'
        home.mkdir()
        product = home / '.agentstack'
        proxy = product / 'integrations/codex_app/plugin/scripts/run-mcp.sh'
        proxy.parent.mkdir(parents=True)
        import shlex
        proxy.write_text('#!/bin/sh\nexec ' + shlex.join([sys.executable, str(server)]) + '\n')
        proxy.chmod(0o700)
        for file in ('hooks/child_tools.py', 'hooks/cleanup-child-agent.sh'):
            target = product / file
            target.parent.mkdir(exist_ok=True, parents=True)
            target.write_text('# Explicit offline fixture\n')
        backend = self.root / 'backend.json'
        spec = json.loads((INTEGRATION / 'backend.json').read_text())
        # Keep the actual template's custom model/definition/effort unchanged.
        # Only the address is synthetic; egress must stop before any socket.
        spec['backends']['strata']['base_url'] = 'https://strata.invalid'
        backend.write_text(json.dumps(spec))
        work = self.root / 'work'
        work.mkdir()
        receipt = Path(runtime).parent / 'evidence/tui-patch-applied.json'
        config = dict(node=node,runtime=runtime,worktree=str(work),backend_file=str(backend),backend_id='strata',
            patch_receipt=str(receipt),home=str(home),product_root=str(product),project_key='/fixture/project',
            proxy_python=sys.executable,parent='FixtureParent',connection={
                'mcp_url':'http://127.0.0.1:9999/mcp','runtime_dir':str(self.root/'runtime-state'),
                'mail_env':str(self.root/'never-open.env'),'bearer_mode':'auto'},
            task={'prompt':'Write strata-first-ok into trial-ok.txt and report to the parent.',
                  'allowed_paths':['trial-ok.txt'],'expected':[{'path':'trial-ok.txt','contains':'strata-first-ok'}]})
        config_file = self.root / 'init.json'
        config_file.write_text(json.dumps(config))
        sys.path.insert(0,str(INTEGRATION))
        self.addCleanup(lambda: sys.path.remove(str(INTEGRATION)))
        import launch
        import preflight
        state = self.root / 'attempt'
        with mock.patch.dict(os.environ,{'HOME':str(home)}):
            launch.init_run(config_file,state)
        identity = {'agent_name':'FixtureChild','program':'dsh','project_key':'/fixture/project','parent':'FixtureParent'}
        lifecycle.save(state/'mail-identity.json',identity)
        proxy_config = {'command':str(proxy),'args':[], 'env':{
            'AGENTSTACK_PROXY_PROGRAM':'claude-code','AGENTSTACK_PROXY_AGENT_NAME':'FixtureChild',
            'AGENTSTACK_PROJECT_KEY':'/fixture/project','AGENTSTACK_MCP_URL':'http://127.0.0.1:9999/mcp'}}
        lifecycle.save(state/'mcp.json',{'mcpServers':{'orrery-mail':proxy_config}})
        lifecycle.save(state/'mail.patch.json',[{'insert':[{'id':'orrery-mail','name':'@deepseek-ai/dsh-mcp-client',
            'config':{'serverName':'orrery_mail','transport':'stdio',**proxy_config,
                      'cwd':str(work),'failOnStartupError':True,'reconnect':{'enabled':False}}}]}])
        try:
            proof = preflight.full(state)
        except Exception:
            evidence = os.environ.get('ORRERY_DSH_TEST_EVIDENCE')
            if evidence:
                import shutil
                shutil.copytree(state, Path(evidence) / ('failed-preflight-' + self.root.name), symlinks=True)
            raise
        self.assertTrue(proof['ok'])
        self.assertEqual(proof['forwarded_requests'],0)
        evidence = os.environ.get('ORRERY_DSH_TEST_EVIDENCE')
        if evidence:
            import shutil
            destination = Path(evidence) / ('passed-preflight-' + self.root.name)
            shutil.copytree(state,destination,symlinks=True)
            lifecycle.save(Path(evidence)/'verification.json',{
                'fixture':'synthetic stdio server, no backend sockets or registration',
                'evidence_directory':str(destination),
                'actual_sdk':True,'actual_tui':True,'actual_tools':proof['actual_tools'],
                'model':spec['backends']['strata']['model'],
                'model_definition':spec['backends']['strata']['model_definition'],
                'forwarded_requests':0,'schema_and_backend_negative_controls':True,
                'dummy_value_file_hits':sum(b'offline-dummy' in p.read_bytes()
                    for p in state.rglob('*') if p.is_file() and not p.is_symlink()
                    and 'node_modules' not in p.parts),
                'real_mail':False,'real_credentials':False})
        preflight.verify(state)
        with mock.patch.object(launch,'credential',side_effect=AssertionError('Must not read a credential')):
            # Changed inputs are refused before the credential seam is reached.
            task = lifecycle.read(state/'task.json')
            task['prompt'] += ' changed'
            lifecycle.save(state/'task.json',task)
            with self.assertRaises(ValueError):
                launch.execute_run(state)
        task['prompt'] = config['task']['prompt']
        lifecycle.save(state/'task.json',task)
        preflight.verify(state)
        aid=lifecycle.read(state/'attempt.json')['attempt_id']
        lifecycle.save(state/'owned-pane.json',{'attempt_id':aid,'pane_id':'%fixture','session':'fixture'})
        with mock.patch.dict(os.environ,{'TMUX_PANE':'%other'}), \
             mock.patch.object(launch,'credential',side_effect=AssertionError('Wrong pane must not read key')):
            with self.assertRaisesRegex(ValueError,'owned pane'):
                launch.execute_run(state)
        self.assertFalse((state/'run-started.json').exists())
        # Exercise the final credential seam and env handoff with no real key
        # and no runtime spawn. The real SDK probe still uses synthetic stdio.
        from types import SimpleNamespace
        captured = []
        child = SimpleNamespace(pid=999999,returncode=0,poll=lambda:0)
        def fake_spawn(argv,**options):
            captured.append(dict(options['env']))
            return child
        with mock.patch.object(launch,'credential',return_value='fixture-only-key') as key, \
             mock.patch.object(launch.subprocess,'Popen',side_effect=fake_spawn), \
             mock.patch.object(launch,'stop_owned_group',return_value=True), \
             mock.patch.object(launch,'run_config',return_value=lifecycle.read(state/'run-config.json')), \
             mock.patch.dict(os.environ,{'TMUX_PANE':'%fixture'}):
            # Popen is shared by subprocess helpers: isolate only the final
            # runtime handoff after keyless gate/probe have already passed.
            import preflight as gate
            with mock.patch.object(gate,'run_probe'), \
                 mock.patch.object(launch.subprocess,'check_output',return_value='v24.3.0'):
                self.assertEqual(launch.execute_run(state),0)
            self.assertEqual(key.call_count,1)
        self.assertEqual(captured[0]['DEEPSEEK_API_KEY'],'fixture-only-key')
        self.assertEqual(captured[0]['ORRERY_DSH_OFFLINE'],'0')
        with mock.patch.object(launch,'credential',side_effect=AssertionError('No second lookup')):
            with self.assertRaises(ValueError):
                launch.execute_run(state)
        self.assertFalse((work/'trial-ok.txt').exists())


if __name__ == '__main__':
    unittest.main()
