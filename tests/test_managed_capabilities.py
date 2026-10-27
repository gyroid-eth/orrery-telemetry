"""Model/harness metadata never falls back to trial arrays or saved overrides."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dashboard import managed_capabilities as c


class CapabilitiesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.template = {'node': '/fixture/node', 'runtime': str(self.root/'runtime'),
                         'backend_file': str(self.root/'backends.json')}
        modules = Path(self.template['runtime'])/'node_modules'
        for name in ('@deepseek-ai/dsh', '@deepseek-harness-tui/dsh-tui'):
            self.save(modules/name/'package.json', {'version': 'fixture'})
        for name in ('@deepseek-ai/dsh-llm', '@deepseek-ai/dsh-llm-deepseek'):
            p=modules/name/'lib/index.js';p.parent.mkdir(parents=True);p.write_text('synthetic pinned runtime')
        self.backends = {b: {'model': b+'-model', 'effort': 'low', 'keychain': {'account': 'private-account'}}
                         for b in ('deepseek','strata')}
        self.save(Path(self.template['backend_file']), {'backends': self.backends})
        self.save(self.root/'template.json', self.template)
        self.manifest = self.root/'manifest.json'
        self.save(self.manifest, {'enabled':True,'backends':{b:{'model':v['model'],'efforts':['low']} for b,v in self.backends.items()},
                                 'harnesses':{'dsh':{'config_file':str(self.root/'template.json')}}})

    def save(self, path, obj):
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(obj))

    def refresh(self, *, failing=False):
        def node(argv, **kwargs):
            self.assertEqual(set(kwargs['env']), {'HOME','PATH'})
            self.assertEqual(kwargs['cwd'], kwargs['env']['HOME'])
            payload=json.loads(kwargs['input'])
            self.assertNotIn('effort', payload['entries'][0])
            self.assertNotIn('private-account', kwargs['input'])
            self.assertNotIn('dummy-key', kwargs['input'])
            if failing: raise subprocess.CalledProcessError(1,argv)
            rows=[{k:r[k] for k in ('backend_id','model','model_fingerprint')} |
                  {'efforts':['off','low','high','max'],'default_effort':'high','source':'harness'} for r in payload['entries']]
            return SimpleNamespace(stdout=json.dumps({'entries':rows,'forwarded':0,'network_attempts':0}))
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY':'dummy-key','NODE_OPTIONS':'--import evil'}), patch.object(c.subprocess,'run',side_effect=node) as run:
            result=c.refresh_dsh_capabilities(ROOT,self.template,self.manifest)
        return result,run

    def load(self, **kw):
        return c.load_effort_capability(self.template,harness=kw.get('harness','dsh'),backend_id='deepseek',model='deepseek-model')

    def test_refresh_and_load_neutral_harness_default_without_credential_inheritance(self):
        result,run=self.refresh();self.assertEqual(run.call_count,1)
        self.assertEqual(self.load(), {'efforts':['off','low','high','max'],'default_effort':'high','effort_source':'harness'})
        self.assertEqual(result['extraction_status'],'known')
        self.assertNotIn('keychain',json.dumps(result))
        self.assertEqual(c.cache_file(self.template).stat().st_mode & 0o777,0o600)
        # A per-request/profile override is not a capability change.
        self.backends['deepseek']['effort']='max'
        self.save(Path(self.template['backend_file']),{'backends':self.backends})
        self.assertEqual(self.load()['default_effort'],'high')

    def test_same_model_capabilities_are_keyed_by_connection(self):
        self.backends['strata-epyc'] = {**self.backends['strata'], 'model_definition': {'contextWindow': 262144}}
        self.save(Path(self.template['backend_file']), {'backends': self.backends})
        manifest=c.read(self.manifest)
        manifest['backends']['strata-epyc']={'kind':'strata','model':'strata-model'}
        self.save(self.manifest,manifest)
        result,_=self.refresh()
        assert {r['backend_id'] for r in result['entries']} == {'deepseek','strata','strata-epyc'}
        rows=[c.load_effort_capability(self.template,harness='dsh',backend_id=key,model='strata-model')
              for key in ('strata','strata-epyc')]
        assert all(row['effort_source']=='harness' for row in rows)
        self.backends['strata']['model_definition']={'contextWindow':32768}
        self.save(Path(self.template['backend_file']), {'backends': self.backends})
        assert c.load_effort_capability(self.template,harness='dsh',backend_id='strata',model='strata-model') == c.UNKNOWN
        assert c.load_effort_capability(self.template,harness='dsh',backend_id='strata-epyc',model='strata-model')['effort_source']=='harness'

    def test_missing_changed_runtime_and_model_are_unknown(self):
        self.assertEqual(self.load(),c.UNKNOWN)
        self.refresh()
        file=Path(self.template['runtime'])/'node_modules/@deepseek-ai/dsh-llm-deepseek/lib/index.js'
        file.write_text('updated runtime')
        self.assertEqual(self.load(),c.UNKNOWN)
        self.refresh()
        self.backends['deepseek']['model_definition']={'id':'deepseek-model','name':'new model metadata'}
        self.save(Path(self.template['backend_file']),{'backends':self.backends})
        self.assertEqual(self.load(),c.UNKNOWN)

    def test_failed_refresh_replaces_old_known_and_hermes_is_not_inferred(self):
        self.refresh();result,_=self.refresh(failing=True)
        self.assertEqual(result['extraction_status'],'unknown');self.assertEqual(self.load(),c.UNKNOWN)
        self.assertEqual(self.load(harness='hermes'),c.UNKNOWN)

    def test_missing_manifest_and_disabled_manifest_do_not_start_extractor(self):
        with patch.object(c.subprocess,'run') as run:
            c.refresh_dsh_capabilities(ROOT,self.template,None)
            self.save(self.manifest,{'enabled':False})
            c.refresh_dsh_capabilities(ROOT,self.template,self.manifest)
            run.assert_not_called()
        self.assertEqual(self.load(),c.UNKNOWN)

    def test_bad_cache_cannot_advertise_duplicate_or_unknown_default(self):
        self.refresh();target=c.cache_file(self.template);original=c.read(target)
        for updates in ({'efforts':['low','low']},{'default_effort':'not-supported'},{'source':'server'}):
            obj=json.loads(json.dumps(original));obj['entries'][0].update(updates);self.save(target,obj)
            self.assertEqual(self.load(),c.UNKNOWN)

    def test_real_runtime_refresh_to_catalog_is_keyless_and_ignores_trial_arrays(self):
        runtime, node = os.environ.get('ORRERY_DSH_TEST_RUNTIME'), os.environ.get('ORRERY_DSH_TEST_NODE')
        if not runtime or not node:
            self.skipTest('explicit fixed Node/runtime required')
        import shutil
        from dashboard.managed_catalog import standard_catalog
        # Runtime code stays read-only; all produced evidence is in this HOME.
        shutil.rmtree(Path(self.template['runtime']))
        Path(self.template['runtime']).symlink_to(Path(runtime).resolve(), target_is_directory=True)
        for backend in self.backends.values():
            backend.update(base_url='https://fixture.invalid', keychain={'service':'fixture-service','account':'fixture-account'})
        self.save(Path(self.template['backend_file']), {'backends':self.backends})
        self.template.update(node=node, product_root=str(self.root/'product'))
        for name in ('bin/agentstack-preregister-child','hooks/child_tools.py',
                     'hooks/cleanup-child-agent.sh','integrations/codex_app/plugin/scripts/run-mcp.sh'):
            p=Path(self.template['product_root'])/name;p.parent.mkdir(parents=True,exist_ok=True)
            p.write_text('synthetic helper; never executed');p.chmod(0o700)
        self.save(self.root/'template.json', self.template)
        manifest=c.read(self.manifest)
        manifest['harnesses']['dsh']['python']=sys.executable
        self.save(self.manifest,manifest)
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY':'dummy-should-not-leave-parent'}):
            result=c.refresh_dsh_capabilities(ROOT,self.template,self.manifest)
        self.assertEqual(result['extraction_status'],'known')
        def selection(file, harness, backend, model):
            if harness != 'dsh':
                raise ValueError('runtime_not_prepared')
            return {'config_file': str(self.root/'template.json')}
        with patch('dashboard.managed_catalog.sys.platform','darwin'), patch(
                'dashboard.managed_catalog._thin_selection', side_effect=selection):
            providers=standard_catalog(self.manifest,mode='legacy')
        for provider in providers:
            dsh,hermes=provider['models'][0]['harnesses']
            self.assertTrue(dsh['available'])
            self.assertEqual(dsh['efforts'],['off','low','high','max'])
            self.assertEqual(dsh['default_effort'],'high')
            self.assertIsNone(hermes['efforts'])
        self.assertNotIn('dummy-',json.dumps(result))
        self.assertTrue(c.cache_file(self.template).is_relative_to(self.root))

    def test_installed_dashboard_only_import_from_root_directory(self):
        # The same fallback import used by direct launchd execution (cwd=/).
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'dashboard'
            p.mkdir()
            (p.parent/'hooks').mkdir()
            (p.parent/'hooks/native_connections.py').write_bytes((ROOT/'hooks/native_connections.py').read_bytes())
            for name in ('managed_capabilities.py','managed_catalog.py','harness_helpers.py'):
                (p/name).write_bytes((ROOT/'dashboard'/name).read_bytes())
            script='import sys;sys.path.insert(0,'+repr(str(p))+');import managed_catalog;print(managed_catalog.standard_catalog(None, mode="legacy"))'
            r=subprocess.run([sys.executable,'-I','-B','-c',script],cwd='/',env={'PATH':os.defpath},text=True,capture_output=True)
            self.assertEqual(r.returncode,0,r.stderr);self.assertEqual(r.stdout.strip(),'[]')


if __name__=='__main__': unittest.main()
