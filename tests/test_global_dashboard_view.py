"""Global-only visual shell and instruction observation, no provider binary."""
import importlib.util
import json
from pathlib import Path
import pytest
from service_teardown import TEST_LABEL_PREFIX
from dashboard import global_page, server as dashboard
from test_global_dashboard_backend import ui, request
from test_global_runtime_delivery import launch, server

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('instruction_view_engine',ROOT/'bin/lib/instruction_blocks.py')
engine=importlib.util.module_from_spec(spec);spec.loader.exec_module(engine)


def test_global_shell_never_starts_legacy_driver_and_serves_legacy_template_verbatim(ui,monkeypatch):
    raw=(ROOT/'dashboard/index.html').read_bytes()
    code,body=request(ui,'/',auth=False)
    assert code==200 and b'global_ui.js' in body and b'mail_flow.js' in body
    assert b'const mailQueue' not in body and b'onclick=' not in body
    assert b'theme_controller.js' in body and b'application/json' in body
    assert b'<style id="agentstack-dashboard-styles">' in body
    assert body.index(b'</style>')<body.index(b'href="theme_light.css"')
    monkeypatch.setattr(dashboard,'GLOBAL_MODE',False)
    code,legacy=request(ui,'/',auth=False)
    assert code==200 and legacy==dashboard._render_dashboard_index(raw)
    assert b'global_ui.js' not in legacy
    for name in ('global_ui.js','global_ui.css','mail_flow.js','network_node.js','network_core.js'):
        code,body=request(ui,'/'+name,auth=False)
        assert code==200 and body==(ROOT/'dashboard'/name).read_bytes()
    for name in ('anthropic','openai','google'):
        code,body=request(ui,'/assets/'+name+'.svg',auth=False)
        assert code==200 and body==(ROOT/'dashboard/assets'/f'{name}.svg').read_bytes()


def test_instruction_unknown_requires_auth_and_never_operator_home(ui):
    code,body=request(ui,'/api/instruction-migration?agent_id=2',auth=False)
    assert code==403
    code,body=request(ui,'/api/instruction-migration?agent_id=2')
    assert code==200,body
    row=json.loads(body)['data'];engine.validate(row,'status')
    assert row['disk_state']=='unknown' and row['target_path'] is None
    assert row['evidence_kind']=='candidate-observation-not-effective-load'
    assert json.loads(request(ui,'/api/instruction-migration?agent_id=2&session_id=../x')[1])['reason']=='QUERY_INVALID'


def test_global_folders_keep_guard_and_deliverables_have_no_implicit_scope(ui,monkeypatch):
    state=ui[0];work=state['root']/'folders';work.mkdir();(work/'allowed').mkdir()
    monkeypatch.setenv('AGENTSTACK_SPAWN_ROOTS',str(work))
    monkeypatch.setenv('AGENTSTACK_PROJECT_KEY',str(work))
    monkeypatch.setattr(dashboard,'PROJECT_KEY',str(work))
    monkeypatch.setattr(dashboard,'VAULT',str(work))
    monkeypatch.delenv('AGENTSTACK_DELIVERABLE_ROOTS',raising=False)
    logs=work/'logs';logs.mkdir();(logs/'LOG_test.md').write_text('---\nagent: Beta\ntitle: Fixture\n---\nbody')
    assert dashboard._deliverable_roots()==[]
    code,body=request(ui,'/api/deliverables');row=json.loads(body)
    assert code==200 and row['items']=={} and row['configured'] is False
    code,body=request(ui,'/api/fs/dirs?path='+str(work));row=json.loads(body)
    assert code==200 and {x['name'] for x in row['dirs']}=={'allowed','logs'}
    (work/'outside').symlink_to(state['root'])
    code,body=request(ui,'/api/fs/dirs?path='+str(work/'outside'))
    assert code==200 and json.loads(body)['path'] is None
    outside=state['root']/'LOG_outside.md';outside.write_text('---\nagent: Beta\ntitle: Outside\n---')
    (logs/'LOG_escape.md').symlink_to(outside)
    monkeypatch.setenv('AGENTSTACK_DELIVERABLE_ROOTS',str(logs))
    code,body=request(ui,'/api/deliverables');row=json.loads(body)
    assert code==200 and row['configured'] is True and len(row['items']['Beta'])==1
    assert row['items']['Beta'][0]['vault']==''


@pytest.mark.parametrize('provider',['claude','codex'])
def test_explicit_provider_home_observation_is_readonly_and_cwd_is_not_reload(tmp_path,provider):
    home=tmp_path/'provider';home.mkdir();cwd=tmp_path/'work';cwd.mkdir()
    # This fixture is an old block in the working folder, not a real user file.
    target=cwd/('CLAUDE.md' if provider=='claude' else 'AGENTS.md')
    env={'HOME':str(tmp_path),'AGENTSTACK_HOME':str(ROOT),'AGENTSTACK_LABEL_PREFIX':TEST_LABEL_PREFIX,'AGENTSTACK_TEMPLATE_HOME':str(ROOT),
         'CLAUDE_CONFIG_DIR':str(home),'CODEX_HOME':str(home)}
    e=engine.Engine(provider,env=env,cwd=cwd)
    old=next(x for x in e.catalog if x['provider']==provider)
    target.write_bytes(engine.render(provider,old['template'].encode(),str(ROOT),str(cwd),'legacy-sed-v1'))
    before=target.read_bytes()
    first=engine.instruction_status(provider,home,ROOT,cwd)
    assert first['disk_state']=='update_required' and first['session_state']=='unknown'
    assert first['target_path']==str(home/('CLAUDE.md' if provider=='claude' else 'AGENTS.md'))
    observation=engine.instruction_observation(provider,home,ROOT,cwd)
    assert engine.instruction_status(provider,home,ROOT,cwd,observation)['session_state']=='candidates_unchanged'
    target.write_bytes(b'Personal note\n')
    assert engine.instruction_status(provider,home,ROOT,cwd,observation)['session_state']=='reload_required'
    other=tmp_path/'other';other.mkdir()
    assert engine.instruction_status(provider,home,ROOT,other,observation)['session_state']=='unknown'
    assert before!=target.read_bytes() and not (home/'CLAUDE.md').exists() and not (home/'AGENTS.md').exists()


def test_session_records_candidates_once_per_session(launch):
    c,delivery,row=launch
    observed={**c.call('whois'),**{k:c.identity[k] for k in ('window_row_id','window_uuid')}};payload={'session_id':'fixture-session','cwd':str(c.isolation),'tool_response':observed}
    first=c.record_session(payload)
    assert first['instruction_observation']['provider']=='claude'
    instruction=c.runtime_dir/'provider-home/claude/CLAUDE.md'
    instruction.write_bytes(instruction.read_bytes()+b'\nOutside note\n')
    second=c.record_session(payload)
    assert second['instruction_observation']==first['instruction_observation']
    third=c.record_session({**payload,'session_id':'another-session'})
    assert third['instruction_observation']['observed_at']!=first['instruction_observation']['observed_at']


def test_shared_reply_renderer_uses_literal_projection_and_dismissal():
    import shutil,subprocess
    node=shutil.which('node')
    if not node:pytest.skip('node unavailable')
    harness=r"""
const assert=require('node:assert/strict');
class El {
 constructor(tag){this.tag=tag;this.children=[];this.className='';this.textContent='';this.classList={add:x=>this.className+=' '+x};}
 append(...xs){for(const x of xs){if(x instanceof El)x.parent=this;this.children.push(x);}}
 replaceChildren(...xs){this.children=[];this.append(...xs);}
 remove(){if(this.parent)this.parent.children=this.parent.children.filter(x=>x!==this);}
}
global.window={};global.document={createElement:t=>new El(t)};
const saved=new Map();global.sessionStorage={getItem:k=>saved.get(k),setItem:(k,v)=>saved.set(k,v)};
global.navigator={clipboard:{writeText:async()=>{}}};
"""
    assertions=r"""
const F=window.OrreryMailFlow,root=new El('div');
const rows=Array.from({length:7},(_,i)=>({id:i+1,sender:{name:'<Sender>'},to:[{name:'<Recipient>'}],cc:[],bcc:[],created_ts:'2026-01-01T00:00:00+00:00',reply_to:i||null,subject:'long subject '.repeat(20),body_md:'<script>unsafe()</script>\n'.repeat(100),history_incomplete:i===0}));
const finding={provider:'claude',target_path:'/fixture/CLAUDE.md',disk_state:'legacy_present',session_state:'unknown',findings:[{source_path:'/fixture/work/CLAUDE.md',kind:'legacy',block_sha256:'abc',template_id:'v1',cleanup_argv:['setup','--path',"a'b"]}]};
let chosen;F.render(root,{messages:rows,selected:7,incomplete:true,instruction:finding,onSelect:x=>chosen=x});
const walk=(e)=>[e,...e.children.filter(x=>x instanceof El).flatMap(walk)];
const all=()=>walk(root),items=()=>all().filter(x=>x.tag==='article');
assert.deepEqual(F.participants([{sender:{agent_id:1,name:'Alpha'},to:[{agent_id:2,name:'Beta'}]}]),['Alpha','Beta']);
assert.deepEqual(F.participants([{sender:{agent_id:1,name:'Alpha'},to:[]}],{a:1,b:99}),['Alpha','Unavailable participant']);
assert.deepEqual(F.participants([]),['Unavailable participant','Unavailable participant']);
assert.equal(items().length,7);assert.equal(items().filter(x=>x.className.includes(' reply')).length,6);
assert.equal(all().filter(x=>x.className==='reply-body')[0].textContent,rows[0].body_md);
assert.equal(all().filter(x=>x.tag==='script').length,0);
assert(all().some(x=>x.textContent==='Earlier mail is unavailable in this view.'));
all().find(x=>x.textContent==='↳ Reply to #6').onclick();assert.equal(chosen,6);
assert.deepEqual(all().filter(x=>x.tag==='a').map(x=>x.textContent),['English','日本語']);
all().find(x=>x.textContent==='Dismiss for this page session').onclick();
assert(F.dismissed(finding));F.render(root,{messages:rows,instruction:finding});
assert(!all().some(x=>x.className==='instruction-hint'));
const changed={...finding,findings:[{...finding.findings[0],block_sha256:'changed'}]};
assert(!F.dismissed(changed));assert(F.needsNotice({...finding,disk_state:'clean',session_state:'reload_required'}));
assert(!F.needsNotice({...finding,disk_state:'unknown',reason:'PROVIDER_HOME_UNPROVEN'}));
assert(F.notice({...finding,disk_state:'unknown',reason:'PROVIDER_HOME_UNPROVEN'},{force:true}));
assert.equal(F.notice({...finding,disk_state:'unknown'}).children[0].textContent,'Instruction status could not be verified.');
console.log('reply renderer: 7 messages, literal body, bounded replies, unavailable history, bilingual links, dismissal and changed hash OK');
"""
    result=subprocess.run([node,'-e',harness+(ROOT/'dashboard/mail_flow.js').read_text()+assertions],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr


def test_instruction_http_uses_existing_session_and_fixed_provider_home(launch,ui):
    c,delivery,row=launch
    payload={'session_id':'known-session','cwd':str(c.isolation),
             'tool_response':{**c.call('whois'),**{k:c.identity[k] for k in ('window_row_id','window_uuid')}}}
    c.record_session(payload)
    code,raw=request(ui,'/api/instruction-migration?agent_id=2&session_id=known-session')
    result=json.loads(raw)['data']
    assert code==200 and result['disk_state']=='clean',raw
    assert result['target_path']==str(c.runtime_dir/'provider-home/claude/CLAUDE.md')
    assert result['session_state']=='candidates_unchanged'
    target=c.runtime_dir/'provider-home/claude/CLAUDE.md'
    old=target.read_bytes();target.write_bytes(b'Personal text\n')
    code,raw=request(ui,'/api/instruction-migration?agent_id=2&session_id=known-session')
    assert json.loads(raw)['data']['session_state']=='reload_required'
    assert target.read_bytes()==b'Personal text\n' and old!=target.read_bytes()
    code,raw=request(ui,'/api/instruction-migration?agent_id=2&session_id=other-session')
    assert json.loads(raw)['data']['disk_state']=='unknown'


@pytest.mark.parametrize('provider', ['claude', 'codex'])
def test_cleanup_command_preserves_detection_home_and_install(tmp_path, provider):
    import subprocess, os
    home=tmp_path/'provider home';home.mkdir();work=tmp_path/'work';work.mkdir()
    e=engine.Engine(provider,env={'HOME':str(tmp_path/'isolated home'),
        'AGENTSTACK_HOME':str(ROOT),'AGENTSTACK_LABEL_PREFIX':TEST_LABEL_PREFIX,'AGENTSTACK_TEMPLATE_HOME':str(ROOT),
        'CLAUDE_CONFIG_DIR':str(home),'CODEX_HOME':str(home)},cwd=work)
    old=next(x for x in e.catalog if x['provider']==provider)
    source=work/('CLAUDE.md' if provider=='claude' else 'AGENTS.md')
    source.write_bytes(engine.render(provider,old['template'].encode(),str(ROOT),str(work),'legacy-sed-v1'))
    argv=next(f['cleanup_argv'] for f in e.detect() if f['cleanup_argv'])
    assert argv[0]=='env'
    assignments=dict(x.split('=',1) for x in argv[1:] if '=' in x and not x.startswith('--'))
    assert assignments['HOME']==e.env['HOME']
    assert assignments['AGENTSTACK_HOME']==str(ROOT)
    assert assignments['CLAUDE_CONFIG_DIR']==str(home)
    assert assignments['CODEX_HOME']==str(home)
    # Execute the emitted command from an unrelated, synthetic operator HOME.
    # No isolated user block exists, so deletion must be refused there too.
    operator=tmp_path/'operator';operator.mkdir()
    own=engine.Engine(provider,env={'HOME':str(operator),'AGENTSTACK_HOME':str(ROOT),'AGENTSTACK_LABEL_PREFIX':TEST_LABEL_PREFIX,'AGENTSTACK_TEMPLATE_HOME':str(ROOT)},cwd=operator)
    own.target.parent.mkdir();own.target.write_bytes(own.block)
    env={**os.environ,'HOME':str(operator),'AGENTSTACK_HOME':str(ROOT),'AGENTSTACK_LABEL_PREFIX':TEST_LABEL_PREFIX,'AGENTSTACK_TEMPLATE_HOME':str(ROOT)}
    before=source.read_bytes()
    result=subprocess.run([*argv,'--assume-yes'],env=env,cwd=operator,capture_output=True,text=True)
    assert result.returncode!=0
    assert 'INSTRUCTION_TARGET_REQUIRES_SETUP' in result.stdout+result.stderr
    assert source.read_bytes()==before
    assert not (operator/'.agentstack').exists()


def test_global_shell_allows_only_declared_markup_and_assets():
    from html.parser import HTMLParser
    raw=(ROOT/'dashboard/index.html').read_text()
    raw=raw.replace('</header>', '<form id="future-legacy" onsubmit="oldSubmit()"><img onerror="oldError()"></form></header>')
    raw=raw.replace('</body>', '<script type="application/json" id="unapproved">{}</script><script src="new-legacy.js"></script></body>')
    class Inspect(HTMLParser):
        def __init__(self):super().__init__();self.handlers=[];self.scripts=[];self.ids=[]
        def handle_starttag(self,tag,attrs):
            attrs=dict(attrs);self.handlers += [k for k in attrs if k.startswith('on')]
            if tag=='script':self.scripts.append(attrs)
            if 'id' in attrs:self.ids.append(attrs['id'])
    p=Inspect();p.feed(global_page.render(raw.encode()).decode())
    assert not p.handlers
    assert 'future-legacy' not in p.ids and 'spawnmd' not in p.ids and 'term' not in p.ids
    assert not any(x.get('id')=='unapproved' or x.get('src')=='new-legacy.js' for x in p.scripts)


def test_instruction_matches_local_identity_without_full_lookup(launch,ui,monkeypatch):
    from dashboard import global_backend
    c,delivery,row=launch
    c.record_session({'session_id':'only-match','cwd':str(c.isolation),
        'tool_response':{**c.call('whois'),**{k:c.identity[k] for k in ('window_row_id','window_uuid')}}})
    op=global_backend.OPERATOR_API['RuntimeOperator'](ui[0]['config_path'])
    op.prepare('unrelated');op.create('unrelated',agent_name='UnrelatedRoot',program='daemon',model='fixture')
    def no_lookup(*a,**kw):raise AssertionError('all-slot lookup called')
    monkeypatch.setattr(type(op),'lookup',no_lookup)
    seen=[];original=type(op).client
    def only_matching(self,name):seen.append(name);return original(self,name)
    monkeypatch.setattr(type(op),'client',only_matching)
    # Checkout/module location is not instruction provenance.
    monkeypatch.setattr(global_backend,'ROOT',c.isolation/'wrong-wrapper')
    code,raw=request(ui,'/api/instruction-migration?agent_id=2&session_id=only-match')
    assert code==200 and json.loads(raw)['data']['disk_state']=='clean',raw
    assert seen==[c.path.parent.name]


def test_instruction_configured_uses_slot_wrapper(launch,monkeypatch):
    from dashboard.global_backend import OPERATOR_API
    c,delivery,row=launch
    op=OPERATOR_API['RuntimeOperator'](c.runtime/'server-config.json')
    c.save_metadata('claude-code','fixture')
    monkeypatch.setitem(op.configured.__globals__,'ROOT',c.isolation/'wrong-wrapper')
    assert op.configured(c)=='claude'


def test_session_observation_uses_slot_wrapper(launch,monkeypatch):
    c,delivery,row=launch
    helper=engine.__dict__.copy();calls=[]
    def observe(provider,home,wrapper,cwd):
        calls.append(str(wrapper));return engine.instruction_observation(provider,home,wrapper,cwd)
    helper['instruction_observation']=observe
    namespace=c.record_session.__globals__;loader=namespace['runpy'];original=loader.run_path
    def load(path,*a,**kw):
        return helper if str(path).endswith('/bin/lib/instruction_blocks.py') else original(path,*a,**kw)
    monkeypatch.setattr(loader,'run_path',load)
    monkeypatch.setitem(namespace,'ROOT',c.isolation/'other-checkout')
    c.record_session({'session_id':'slot-provenance','cwd':str(c.isolation),
        'tool_response':{**c.call('whois'),**{k:c.identity[k] for k in ('window_row_id','window_uuid')}}})
    assert calls==[c.config['wrapper_root']]
