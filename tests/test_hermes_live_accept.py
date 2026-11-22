"""Private configuration fixtures for locale, consent and concurrent generation."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


defaults = load('fixture_telemetry_defaults', ROOT / 'integrations/hermes/telemetry_defaults.py')
fixtures = load('fixture_thin_config', ROOT / 'tests/test_hermes_thin_config.py')
try:
    from ruamel.yaml import YAML
    read_yaml = YAML(typ='safe').load
except ModuleNotFoundError:
    from yaml import safe_load as read_yaml


class TelemetryDefaults(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.profile, self.state = self.root / 'profile', self.root / 'state'
        self.profile.mkdir()
        self.path = self.profile / 'config.yaml'

    def settle(self, raw):
        self.path.write_bytes(raw.encode('utf-8'))
        self.assertTrue(defaults.settle_unanswered(self.profile, self.state))
        return self.path.read_bytes().decode('utf-8')

    def test_only_answer_values_change_in_yaml_and_json(self):
        for raw in [
                '# preserve comments\nmodel: "quoted model" # preserve spaces\ntelemetry:\n  other: 3 # preserve me\n  shared_metrics:\n    enabled: false # previous default\n    send: false\n    offer_version: 1 # old offer\nmemory: {enabled: true}\n',
                '{ "model" : "quoted model", "telemetry": {"other": 3, "shared_metrics": {"enabled": false, "send": false, "offer_version": 1}}, "memory": {"enabled": true} }\r\n']:
            with self.subTest(raw=raw):
                expected = raw.replace('offer_version: 1', 'offer_version: 2').replace('"offer_version": 1', '"offer_version": 2')
                self.assertEqual(self.settle(raw), expected)
        self.assertEqual(len((self.state / 'telemetry-defaults.log').read_text().splitlines()), 2)

    def test_missing_answer_is_added_without_reformatting_other_keys(self):
        for raw in ['', '# header\n', 'model: "keep" # comment\n', '{"model" : "keep"}',
                    'telemetry:\n  other: 7\nmodel: "keep"',
                    'telemetry:\n  shared_metrics: {}\nmodel: "keep"',
                    'telemetry:\n  shared_metrics:\n    enabled: false # keep\nmodel: "keep"\n']:
            with self.subTest(raw=raw):
                revised = self.settle(raw)
                data = read_yaml(revised)
                self.assertEqual(data['telemetry']['shared_metrics'], defaults.ANSWER)
                if '"keep"' in raw:
                    # The complete unrelated key token/comment survives byte for byte.
                    unchanged = next(line for line in raw.splitlines() if 'model:' in line) if 'model:' in raw else '"model" : "keep"'
                    self.assertIn(unchanged, revised)
                self.assertFalse(defaults.settle_unanswered(self.profile, self.state))

    def test_explicit_choices_are_byte_identical(self):
        for section in [dict(enabled=True, send=True), dict(enabled=True, send=False),
                        dict(enabled=False, send=True), dict(enabled=False, send=False, offer_version=2)]:
            with self.subTest(section=section):
                raw = json.dumps({'model': {'default': 'keep'}, 'telemetry': {'shared_metrics': section}}, indent=3)
                self.path.write_bytes(raw.encode())
                self.assertFalse(defaults.settle_unanswered(self.profile, self.state))
                self.assertEqual(self.path.read_bytes(), raw.encode())
        self.assertFalse(self.state.exists())

    def test_projected_mcp_changes_only_its_yaml_fields_and_is_idempotent(self):
        for raw in ['# keep header\nmodel: "keep" # comment\nmcp_servers:\n  old: {command: /fixture/old}\n# keep memory comment\nmemory: {enabled: true}\n',
                    '{ "model" : "keep", "mcp_servers": {"old": {"command": "/fixture/old"}}, "memory" : {"enabled": true} }\r\n']:
            with self.subTest(raw=raw):
                self.path.write_bytes(raw.encode())
                selected = {'old': {'command': '/fixture/new', 'enabled': True}}
                self.assertTrue(defaults.project_mcp(self.profile, selected))
                result = self.path.read_bytes()
                self.assertEqual(read_yaml(result)['mcp_servers'], selected)
                self.assertIn(b'"keep"', result)
                for literal in ('# keep header', '# comment', '# keep memory comment', 'memory: {enabled: true}', '"model" : "keep"', '"memory" : {"enabled": true}', '\r\n'):
                    if literal in raw:self.assertIn(literal.encode(), result)
                self.assertFalse(defaults.project_mcp(self.profile, selected))
                self.assertEqual(self.path.read_bytes(), result)

    def test_block_mcp_numeric_name_remains_string(self):
        raw = '# keep header\nmodel: "keep" # comment\nmcp_servers:\n  old: {command: /fixture/old}\nmemory: {enabled: true}\n'
        self.path.write_bytes(raw.encode())
        selected = {'old': {'command': '/fixture/old'}, '123': {'command': '/fixture/new', 'enabled': True}}
        self.assertTrue(defaults.project_mcp(self.profile, selected))
        result = self.path.read_bytes()
        self.assertEqual(read_yaml(result)['mcp_servers'], selected)
        self.assertIn(b'model: "keep" # comment\n', result)
        self.assertIn(b'memory: {enabled: true}\n', result)
        self.assertFalse(defaults.project_mcp(self.profile, selected))
        self.assertEqual(self.path.read_bytes(), result)

    def test_foreign_config_symlink_is_not_read_or_replaced(self):
        foreign = self.root / 'foreign.yaml'
        foreign.write_text('model: private\n')
        self.path.symlink_to(foreign)
        with self.assertRaisesRegex(ValueError, 'owned_link_conflict'):
            defaults.settle_unanswered(self.profile, self.state)
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(foreign.read_bytes(), b'model: private\n')


NATIVE_DATA_SCRIPT = r'''
import importlib.util,json,os,shutil,sys,tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
source=Path(sys.argv[1]); sys.path.insert(0,str(source))
def audit(event,args):
    if event in ('socket.connect','socket.getaddrinfo','subprocess.Popen'):
        raise AssertionError('native data fixture forbids network/children')
sys.addaudithook(audit)
with tempfile.TemporaryDirectory() as temporary:
    root=Path(temporary); profile=root/'profile';profile.mkdir()
    os.environ['HERMES_HOME']=str(profile)
    workspace=root/'workspace/agent';workspace.mkdir(parents=True)
    shutil.copyfile(source/'agent/i18n.py',workspace/'i18n.py')
    spec=importlib.util.spec_from_file_location('fixture_packaged_i18n',workspace/'i18n.py')
    i18n=importlib.util.module_from_spec(spec);spec.loader.exec_module(i18n)
    keys=['cli.chat.initializing_agent','cli.tui.placeholder_busy','cli.stream.tool_preparing',
          'cli.shared.duration_s','display.review.summary_cli']
    assert all(i18n.t(key,lang='en')==key for key in keys)
    os.environ['HERMES_BUNDLED_LOCALES']=str(source/'locales')
    i18n.reset_language_cache()
    assert all(i18n.t(key,lang='en')!=key for key in keys)
    from hermes_cli.observability import shared_metrics_consent as consent
    # Exercise the actual native offer predicate without a terminal UI or setup imports.
    import hermes_cli.config as config
    spec=importlib.util.spec_from_file_location('fixture_generator',str(Path(sys.argv[2]).with_name('thin_config.py')))
    generator=importlib.util.module_from_spec(spec);spec.loader.exec_module(generator)
    merge_cases=[({'url':'https://stale.invalid/mcp'},{'command':'/selected/server'}),
                 ({'command':'/old','args':['old']},{'command':'/selected','args':['selected']}),
                 ({'env':{'old':'keep'}},{'env':None})]
    for old,selected in merge_cases:
        effective=config._deep_merge({'server':old},{'server':selected})['server']
        assert generator._effective_mcp(old,selected)==effective
    setup=SimpleNamespace(is_noninteractive=lambda:False)
    cases=[{'enabled':False,'send':False},
           {'enabled':True,'send':True}, {'enabled':True,'send':False},
           {'enabled':False,'send':False,'offer_version':2}]
    for section in cases:
        before={'telemetry':{'shared_metrics':section}}
        (profile/'config.yaml').write_text(json.dumps(before))
        spec=importlib.util.spec_from_file_location('fixture_defaults',sys.argv[2])
        defaults=importlib.util.module_from_spec(spec);spec.loader.exec_module(defaults)
        defaults.settle_unanswered(profile,root/'state')
        after=json.loads((profile/'config.yaml').read_text())
        assert consent.consent_state(after)['decided'] is True
        calls=[]
        with patch.dict(sys.modules,{'hermes_cli.setup':setup}), \
             patch.object(config,'is_managed',return_value=False), \
             patch.object(consent,'_current_state',return_value=consent.consent_state(after)), \
             patch.object(consent,'offer_consent',side_effect=lambda **kwargs:calls.append(kwargs)), \
             patch.object(consent.sys,'stdin',SimpleNamespace(isatty=lambda:True)), \
             patch.object(consent.sys,'stdout',SimpleNamespace(isatty=lambda:True)):
            consent.offer_consent_before_chat(SimpleNamespace())
        assert not calls,calls
    print(json.dumps({'translated_keys':len(keys),'native_consent_cases':len(cases),'mcp_merge_cases':len(merge_cases),'network':0}))
'''


FAKE_MCP_SCRIPT = r'''
import json,sys,time
from pathlib import Path
trace=Path(sys.argv[1])
for line in sys.stdin:
    request=json.loads(line)
    method=request.get('method')
    with trace.open('a') as out:out.write(str(method)+'\n')
    if 'id' not in request:continue
    if method=='initialize':
        delay=trace.with_name('connect-delay.json')
        if delay.exists():time.sleep(json.loads(delay.read_text()))
        result={'protocolVersion':request['params']['protocolVersion'],'capabilities':{'tools':{}},'serverInfo':{'name':'offline-fixture','version':'1'}}
    elif method=='tools/list':
        result={'tools':[{'name':'fixture_ping','description':'Offline synthetic probe','inputSchema':{'type':'object','properties':{}}}]}
    elif method=='ping':result={}
    else:result={}
    print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}),flush=True)
'''

NATIVE_MCP_SCRIPT = r'''
import json,logging,os,sys,time
from pathlib import Path
source=Path(sys.argv[1]).resolve();root=Path(sys.argv[2]).resolve();plan=json.loads(Path(sys.argv[3]).read_text());expected=sys.argv[4]
os.environ.update(plan['env'])
blocked=[]
def audit(event,args):
    if event=='open' and args and isinstance(args[0],(str,bytes,os.PathLike)):
        path=Path(os.fsdecode(args[0]))
        if path.name in ('.env','.op.env') and not path.resolve().is_relative_to(root):
            blocked.append('external_dotenv');raise SystemExit('external dotenv forbidden')
    if event in ('socket.connect','socket.getaddrinfo'):
        blocked.append(event);raise AssertionError('No network in stdio fixture')
    if event=='subprocess.Popen':
        argv=args[1]
        if isinstance(argv,(list,tuple)) and list(argv)==[sys.executable,'-I','-B',str(root/'fake-mcp.py'),str(root/'methods.log')]:return
        if isinstance(argv,(list,tuple)) and list(argv)==[sys.executable,str(source/'tools/mcp_death_supervisor.py'),'--parent-pgid',str(os.getpgid(0))]:return
        if isinstance(argv,(list,tuple)) and Path(argv[0]).name=='git' and any(x in argv for x in ('rev-parse','describe','log','status','show','rev-list','branch','tag')):return
        blocked.append('unexpected_subprocess');raise AssertionError('Unexpected subprocess')
sys.addaudithook(audit)
sys.path.insert(0,str(source))
import httpx,mcp
from hermes_cli import config
from hermes_cli.mcp_startup import ensure_mcp_discovery_before_agent_build,_has_configured_mcp_servers
from tools.mcp_tool_discovery import get_mcp_status
from tools.mcp_tool_lifecycle import shutdown_mcp_servers
from tools.registry import registry
from tools.mcp_tool_schema import mcp_prefixed_tool_name
assert Path(config.__file__).resolve().is_relative_to(source)
assert all(Path(module.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()) for module in (httpx,mcp))
try:
    gate=_has_configured_mcp_servers()
    single_query=json.loads(sys.argv[5])
    started=time.monotonic()
    ensure_mcp_discovery_before_agent_build(logger=logging.getLogger('offline-fixture'),single_query=single_query)
    elapsed=time.monotonic()-started
    status=get_mcp_status()
    name=mcp_prefixed_tool_name('orrery-mail','fixture_ping')
    methods=(root/'methods.log').read_text().splitlines() if (root/'methods.log').exists() else []
    registered=registry.get_entry(name) is not None
    summary={'startup_gate':gate,'methods':methods,'registered':registered,'blocked':blocked,
             'single_query':single_query,'wait_seconds':elapsed,
             'connected':any(row['name']=='orrery-mail' and row['connected'] and row['tools']==1 for row in status),
             'imports':{module.__name__:str(Path(module.__file__).resolve()) for module in (config,httpx,mcp)}}
    print(json.dumps(summary),flush=True)
    if expected=='selected':
        assert gate and 'initialize' in methods and 'tools/list' in methods and registered and summary['connected'], 'Native MCP discovery did not register the selected stdio tool'
    else:
        assert not methods and not registered and not summary['connected'], 'Disabled or unselected MCP was started'
    assert not blocked
finally:
    shutdown_mcp_servers()
'''


class NativeAcceptanceData(unittest.TestCase):
    def native(self):
        source, python = os.environ.get('ORRERY_HERMES_TEST_SOURCE'), os.environ.get('ORRERY_HERMES_TEST_PYTHON')
        if not source or not python:
            self.skipTest('Explicit installed dependency interpreter/source required')
        return source, python

    def test_real_native_managed_mcp_discovery_reaches_stdio_tools(self):
        self.mcp_case('selected')

    def test_real_native_slow_mcp_is_in_first_interactive_and_single_query_snapshot(self):
        # Exceed both native defaults (1.5s interactive, 15s single query).
        # The TTY --query-file launch takes the interactive branch.
        for single_query in (False, True):
            with self.subTest(single_query=single_query):
                summary = self.mcp_case('selected', delay=17, single_query=single_query)
                self.assertGreaterEqual(summary['wait_seconds'], 17)
                self.assertLess(summary['wait_seconds'], 40)  # returns on discovery, not a fixed sleep

    def test_real_native_empty_disabled_and_unselected_mcp_never_spawns(self):
        for mode in ('empty', 'disabled', 'unselected'):
            with self.subTest(mode=mode):
                self.mcp_case(mode)

    def mcp_case(self, mode, *, delay=0, single_query=True):
        source, python = self.native()
        real_popen = subprocess.Popen
        fixture = fixtures.ThinConfig()
        fixture.setUp()
        try:
            (fixture.base / 'fake-mcp.py').write_text(FAKE_MCP_SCRIPT)
            fixture.save(fixture.base / 'connect-delay.json', delay)
            definition = {'command': python, 'args': ['-I', '-B', str(fixture.base / 'fake-mcp.py'), str(fixture.base / 'methods.log')]}
            servers = {'orrery-mail': definition} if mode == 'selected' else {'orrery-mail': {**definition, 'enabled': False}} if mode == 'disabled' else {}
            fixture.save(fixture.base / 'mcp.json', {'mcpServers': servers})
            if mode == 'unselected':
                fixture.save(fixture.profile / 'config.yaml', {'mcp_servers': {'orrery-mail': definition}})
            plan = fixture.generate()
            plan_file = fixture.base / 'plan.json'
            fixture.save(plan_file, plan)
            with patch.object(subprocess, 'Popen', real_popen):
                result = subprocess.run([python, '-I', '-B', '-c', NATIVE_MCP_SCRIPT,
                    source, str(fixture.base), str(plan_file), mode, json.dumps(single_query)], cwd=fixture.base,
                    env={'HOME': str(fixture.base), 'TMPDIR': str(fixture.base), 'PATH': os.defpath,
                         'PYTHONDONTWRITEBYTECODE': '1', 'HERMES_DISABLE_LAZY_INSTALLS': '1', 'UV_OFFLINE': '1'},
                    capture_output=True, text=True, timeout=40)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            summary = json.loads(result.stdout)
            print('MCP_DISCOVERY_EVIDENCE=' + json.dumps({'mode': mode, **summary}))
            if mode == 'selected':
                self.assertTrue(summary['startup_gate'] and summary['registered'] and summary['connected'])
                self.assertIn('tools/list', summary['methods'])
            else:
                self.assertFalse(summary['registered'] or summary['connected'])
                self.assertEqual(summary['methods'], [])
            self.assertEqual(summary['blocked'], [])
            return summary
        finally:
            fixture.doCleanups()

    def test_real_native_locale_fallback_and_consent_offer(self):
        source, python = self.native()
        with tempfile.TemporaryDirectory() as home:
            result = subprocess.run([python, '-I', '-B', '-c', NATIVE_DATA_SCRIPT, source,
                str(ROOT / 'integrations/hermes/telemetry_defaults.py')],
                cwd=home, env={'HOME':home,'PATH':os.defpath,'UV_OFFLINE':'1','HERMES_DISABLE_LAZY_INSTALLS':'1'},
                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)['translated_keys'], 5)

    def test_two_generations_keep_other_yaml_bytes_and_log_only_one_default(self):
        _, python = self.native()
        real_popen = subprocess.Popen
        fixture = fixtures.ThinConfig()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        profile = fixture.profile
        profile.mkdir(parents=True)
        raw = '# unchanged\nmodel: "preserve" # verbatim\ntelemetry:\n  shared_metrics:\n    enabled: false\n    send: false\n    offer_version: 1 # old\nmemory: {enabled: true}\n'
        (profile / 'config.yaml').write_text(raw)
        documents=[]
        for name in ('first','second'):
            document={**fixture.document,'state_dir':str(fixture.base/name)}
            path=fixture.base/(name+'.json');path.write_text(json.dumps(document));documents.append(path)
        worker='import json,sys;sys.path.insert(0,sys.argv[1]);from integrations.hermes.thin_config import generate;generate(json.load(open(sys.argv[2])))'
        processes=[]
        try:
            with patch.object(subprocess,'Popen',real_popen):
                for document in documents:
                    processes.append(subprocess.Popen([python,'-I','-B','-c',worker,str(ROOT),str(document)],
                        env={'HOME':str(fixture.base),'PATH':os.defpath,'UV_OFFLINE':'1','HERMES_DISABLE_LAZY_INSTALLS':'1'},
                        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True))
                for process in processes:
                    stdout,stderr=process.communicate(timeout=20)
                    self.assertEqual(process.returncode,0,stdout+stderr)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate();process.wait(timeout=5)
        expected = raw.replace('offer_version: 1','offer_version: 2')
        self.assertEqual((profile/'config.yaml').read_text(), expected + 'mcp_servers: ' + json.dumps({
            'orrery_mail': {'enabled': True, 'command': '/fixture/proxy',
                            'args': ['--bound-token-file', '/fixture/token-reference']}}) + '\n')
        logs=list(fixture.base.glob('*/telemetry-defaults.log'))
        self.assertEqual(len(logs),1)
        self.assertEqual(len(logs[0].read_text().splitlines()),1)


if __name__ == '__main__':
    unittest.main()
