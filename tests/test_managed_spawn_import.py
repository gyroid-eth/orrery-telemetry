"""Installed launchd layout, cwd=/, no PYTHONPATH or checkout import path."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class LaunchdImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.product = self.home / '.agentstack'
        self.dashboard = self.product / 'dashboard'
        self.dashboard.mkdir(parents=True)
        for file in ('managed_spawn.py', 'managed_catalog.py'):
            shutil.copyfile(ROOT / 'dashboard' / file, self.dashboard / file)
        for harness in ('dsh', 'hermes'):
            target = self.product / 'integrations' / harness
            target.mkdir(parents=True)
            shutil.copyfile(ROOT / 'integrations' / harness / 'managed_lifecycle.py', target / 'managed_lifecycle.py')
        # Sentinel only: import-path verification must not initialize services.
        (self.dashboard / 'server.py').write_text('''from pathlib import Path
INSTALLED_CORE = True
GLOBAL_MODE = False
_HOME = Path(__file__).resolve().parents[2]
MCP_HTTP_URL = 'http://127.0.0.1:1/mcp'
RUNTIME_DIR = str(_HOME / 'runtime')
MAIL_ENV_PATH = str(_HOME / 'mail.env')
def _project_key(): return str(_HOME / 'project')
''')
        self.manifest = self.home / 'manifest.json'
        self.manifest.write_text(json.dumps({'enabled': True,
            'backends': {'deepseek': {'model': 'fixture-deepseek'}, 'strata': {'model': 'fixture-strata'}},
            'harnesses': {}}))

    def child(self, code):
        # Keep Python's own stdlib; the sole application import path is the
        # installed dashboard directory. -I also excludes cwd and PYTHONPATH.
        boot = '''import sys, json
from pathlib import Path
stdlib = [p for p in sys.path if 'site-packages' not in p]
sys.path[:] = [sys.argv[1]] + stdlib
assert str(Path.cwd()) == '/'
assert sys.argv[2] not in sys.path
import managed_spawn as m
'''
        return subprocess.run([sys.executable, '-I', '-B', '-c', boot + code,
                               str(self.dashboard), str(self.product), str(self.manifest)],
            cwd='/', env={'HOME': str(self.home), 'PATH': '/usr/bin:/bin'},
            capture_output=True, text=True, timeout=10)

    def test_catalog_returns_real_provider_rows_in_launchd_layout(self):
        result = self.child("rows=m.catalog(sys.argv[3],selected_mode='legacy'); print(json.dumps([p['id'] for p in rows]))")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ['deepseek', 'strata'])
        self.assertEqual(result.stderr, '')

    def test_worker_imports_both_real_adapters_and_installed_core(self):
        result = self.child('''
from types import SimpleNamespace
home=Path(sys.argv[3]).parent
source=home/'source';source.mkdir()
loaded=[]
original=m._product_module
def product_module(name):
    module=original(name)
    assert Path(module.__file__).resolve().is_relative_to(Path(sys.argv[2]))
    loaded.append(name)
    if name.startswith('integrations.'):
        module.prepare=lambda **kwargs: Path(kwargs['state']).mkdir()
        module.start=lambda *args,**kwargs: SimpleNamespace(poll=lambda:0)
        module.status=lambda *args: {'ready':True,'terminal':True,'agent_name':'FixtureChild'}
        module.finalize=lambda *args,**kwargs: {'task_success':True,'runtime_success':True}
        module.clean=lambda *args,**kwargs: None
    return module
m._product_module=product_module
m.parent_connection=lambda *args: lambda *args: {'name':'FixtureParent','retired_at':None}
config={'project_key':str(home/'project'),'connection':{'mcp_url':'http://127.0.0.1:1/mcp',
        'runtime_dir':str(home/'runtime'),'mail_env':str(home/'mail.env')}}
m.trusted_selection=lambda *args: ({},config)
for harness in m.HARNESSES:
    job=home/harness;job.mkdir()
    request={'harness':harness,'provider':'strata','parent':'FixtureParent','task':'fixture',
             'dir':str(source),'allowed_paths':['result.txt'],'expected':[{'path':'result.txt','contains':'done'}]}
    m.save(job/'request.json',{'request':request,'config_file':sys.argv[3],'selected_mode':'legacy'})
    m.save(job/'status.json',{'phase':'queued','state':'pending','task_success':None,'runtime_success':None})
    result=m.run_job(job,run=object(),provider_loader=lambda *args,**kwargs: [])
    assert result['phase']=='cleaned' and result['task_success'] is True,result
assert loaded==['integrations.dsh.managed_lifecycle','dashboard.server','integrations.hermes.managed_lifecycle','dashboard.server'],loaded
print('installed imports resolved')
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'installed imports resolved')

    def test_missing_catalog_logs_one_line_instead_of_silent_empty(self):
        (self.dashboard / 'managed_catalog.py').unlink()
        result = self.child("assert m.catalog(sys.argv[3],selected_mode='legacy') == []")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertIn('managed catalog unavailable (ModuleNotFoundError)', result.stderr)

    def test_worker_import_failure_is_failed_and_diagnosed(self):
        result = self.child('''
job=Path(sys.argv[3]).parent/'job';job.mkdir()
m.save(job/'request.json',{'request':{'harness':'absent'},'config_file':sys.argv[3],'selected_mode':'legacy'})
m.save(job/'status.json',{'phase':'queued','task_success':None,'runtime_success':None})
m.trusted_selection=lambda *args: ({},{})
value=m.run_job(job)
assert value['state']=='failed' and value['finished'] is True
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertIn('managed worker failed during queued (ModuleNotFoundError)', result.stderr)


if __name__ == '__main__':
    unittest.main()
