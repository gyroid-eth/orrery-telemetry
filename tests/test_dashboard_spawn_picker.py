"""Run the actual standalone picker projection and payload with synthetic controls."""
import json
import re
import subprocess
from pathlib import Path

import pytest

HTML = (Path(__file__).resolve().parents[1] / 'dashboard/index.html').read_text()


def function(name):
    return re.search(r'function '+name+r'\([^\n]*\)\{.*?\n\}', HTML, re.S).group()


def run(body):
    picker = re.search(r'/\* Catalog-only model choices.*?\n\}\);', HTML, re.S).group()
    functions = ['normalizeSpawnProviders', 'spawnSelectedModel', 'spawnSelectedHarness',
                 'spawnEffortInfo', 'resolveSpawnEffort', 'spawnEffectiveEffort',
                 'spawnSelectedProviderCapabilities', 'spawnProviderRequirementsMissing',
                 'spawnProviderAcceptsResources', 'buildSpawnPayload']
    script = picker + '\nconst SpawnPicker=module.exports;\n' + '\n'.join(function(n) for n in functions)
    script += """
let spmSelectedProvider='deepseek',spmSelectedModel='fixture',spmSelectedHarness='hermes';
let spmSelectedEffort='',spmEffortExplicit=false,spmTerminalExplicit=false;
let spmSelectedName='',spmSuggestedName='',spmIdentityState='auto',spmNameStatus=new Map();
const controls={};const SPM=id=>controls[id]||(controls[id]={value:'',checked:false,disabled:false});
controls['spm-task']={value:'Fixture task'};controls['spm-dir']={value:'/fixture'};
controls['spm-helper']={value:'same',disabled:true};
let spmProviders=normalizeSpawnProviders({providers:[{id:'deepseek',models:[{id:'fixture',harnesses:[
{id:'hermes',available:true,default:true,efforts:['none','high'],default_effort:'high',helper_models:[{id:'same'},{id:'small'}]}]}]}]});
"""
    result = subprocess.run(['node', '-e', script + '\n' + body], capture_output=True, text=True, timeout=10, check=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize('default,value,explicit,expected', [
    ('high','',False,'high'), (None,'',False,None), ('invented','',False,None),
    ('high','none',True,'none'), ('high','invented',True,None),
    ('none','',False,'none'), ('high','high',False,'high'),
])
def test_pair_effort_default_explicit_and_reset(default,value,explicit,expected):
    result=run('''
const h=spmProviders[0].models[0].harnesses[0];h.default_effort=DEFAULT;
spmSelectedEffort=VALUE;spmEffortExplicit=EXPLICIT;
console.log(JSON.stringify(buildSpawnPayload()));
'''.replace('DEFAULT',json.dumps(default)).replace('VALUE',json.dumps(value)).replace('EXPLICIT',json.dumps(explicit)))
    assert result.get('effort') == expected
    assert ('effort' in result) == (expected is not None)
    assert result['harness'] == 'hermes' and result['standalone'] is True
    assert 'helper_model' not in result and 'headless' not in result


def test_same_model_on_two_local_connections_keeps_exact_payload_selector():
    result=run('''
spmProviders=normalizeSpawnProviders({providers:['strata','strata-epyc'].map(id=>({
 id,kind:'strata',label:id==='strata'?'workstation':'EPYC',locality:'local',models:[
 {id:'same-wire-model',harnesses:[{id:'dsh',available:true},{id:'hermes',available:true}]}]}))});
const payloads=[];
for(const p of spmProviders)for(const h of ['dsh','hermes']){
 spmSelectedProvider=p.id;spmSelectedModel=p.models[0].id;spmSelectedHarness=h;
 payloads.push(buildSpawnPayload());
}
console.log(JSON.stringify(payloads));
''')
    assert [(p['provider'],p['model'],p['harness']) for p in result] == [
        (connection,'same-wire-model',harness)
        for connection in ('strata','strata-epyc') for harness in ('dsh','hermes')]


def test_dynamic_same_model_three_urls_keeps_url_index_and_display_name():
    result=run('''
let spmSelectedUrlIndex=2,spmSelectedUrlKey='2'.repeat(64);
spmSelectedProvider='local-workstation';spmSelectedModel='same-wire-model';spmSelectedHarness='hermes';
spmProviders=normalizeSpawnProviders({providers:[{id:'local-workstation',kind:'strata',dynamic_models:true,label:'Workstation',
 models:[0,1,2].map(url_index=>({id:'same-wire-model',display_name:'Readable model',url_index,url_key:String(url_index).repeat(64),
  harnesses:[{id:'hermes',available:true,efforts:null,effort_source:'unknown'}]}))}]});
const p=spmProviders[0];
console.log(JSON.stringify({payload:buildSpawnPayload(),keys:SpawnPicker.entries({providers:spmProviders}).map(e=>e.key),
 selected:spawnSelectedModel(p)}));
''')
    assert result['payload']['provider']=='local-workstation'
    assert result['payload']['model']=='same-wire-model' and result['payload']['url_index']==2
    assert result['payload']['url_key']=='2'*64
    assert 'effort' not in result['payload']
    assert len(set(result['keys']))==3 and result['selected']['display_name']=='Readable model'


def test_required_effort_needs_explicit_selection_even_with_default():
    assert run('''
spmProviders[0].capabilities={effort_required:true};
const untouched=spawnProviderRequirementsMissing();const initial=buildSpawnPayload();
spmSelectedEffort='none';spmEffortExplicit=true;
console.log(JSON.stringify([untouched,initial,spawnProviderRequirementsMissing(),buildSpawnPayload()]));
''') == ['select an effort for this provider',
        dict(task='Fixture task',dir='/fixture',provider='deepseek',model='fixture',role='',group='',standalone=True,harness='hermes'),
        '',dict(task='Fixture task',dir='/fixture',provider='deepseek',model='fixture',role='',group='',standalone=True,harness='hermes',effort='none')]


def test_invalid_pair_unknown_effort_and_explicit_options_do_not_leak_previous_choice():
    result=run('''
const h=spmProviders[0].models[0].harnesses[0];h.efforts=null;h.default_effort=null;
const unknown=buildSpawnPayload();h.available=false;const unavailable=spawnProviderRequirementsMissing();
h.available=true;SPM('spm-parent').value='FixtureParent';controls['spm-helper']={value:'small',disabled:false};
spmTerminalExplicit=true;SPM('spm-terminal').checked=true;
SPM('spm-worktree').checked=true;SPM('spm-worktree-base').value='HEAD';
console.log(JSON.stringify([unknown,unavailable,buildSpawnPayload()]));
''')
    assert 'effort' not in result[0]
    assert result[1] == 'Choose an available model and Harness'
    assert result[2]['parent']=='FixtureParent' and 'standalone' not in result[2]
    assert result[2]['helper_model']=='small' and result[2]['headless'] is False
    assert result[2]['worktree'] is True and result[2]['worktree_base']=='HEAD'


@pytest.mark.parametrize('provider,program', [('claude','claude-code'),('codex','codex-cli')])
def test_native_payload_keeps_legacy_harness_omission_and_chrome_boundary(provider,program):
    result=run('''
spmSelectedProvider=PROVIDER;spmSelectedHarness=PROGRAM;
spmProviders=normalizeSpawnProviders({providers:[{id:PROVIDER,program:PROGRAM,models:['fixture'],efforts:['high'],effort_default:'high'}]});
SPM('spm-chrome').checked=true;SPM('spm-chrome-device').value='fixture-device';
console.log(JSON.stringify(buildSpawnPayload()));
'''.replace('PROVIDER',json.dumps(provider)).replace('PROGRAM',json.dumps(program)))
    assert result['effort']=='high' and 'harness' not in result and 'helper_model' not in result
    assert ('claude_chrome' in result) == (provider=='claude')
    assert ('claude_chrome_device' in result) == (provider=='claude')


def test_model_own_default_is_respected_independently_of_level_source():
    result=run('''
spmSelectedProvider='codex';spmSelectedHarness='codex-cli';
spmProviders=normalizeSpawnProviders({providers:[{id:'codex',program:'codex-cli',
  models:[{id:'fixture',effort_default:null}],efforts:['low','high'],effort_default:'high'}]});
const p=spmProviders[0],m=p.models[0];
const initial=buildSpawnPayload();
delete m.effort_default;const inherited=buildSpawnPayload();
m.effort_default='low';const concrete=buildSpawnPayload();
m.effort_default=null;spmSelectedEffort='low';spmEffortExplicit=true;const explicit=buildSpawnPayload();
spmEffortExplicit=false;p.modelEfforts={fixture:['low','high']};p.modelEffortDefaults={fixture:'high'};
const mapped=buildSpawnPayload();
p.capabilities.effort_required=true;const required=spawnProviderRequirementsMissing();const requiredPayload=buildSpawnPayload();
spmEffortExplicit=true;const chosen=spawnProviderRequirementsMissing();const chosenPayload=buildSpawnPayload();
p.capabilities.effort_required=false;spmEffortExplicit=false;
m.harnesses=[{id:'codex-cli',available:true,efforts:['low','high'],default_effort:'high'}];
const harness=buildSpawnPayload();
console.log(JSON.stringify({initial,inherited,concrete,explicit,mapped,required,requiredPayload,chosen,chosenPayload,harness}));
''')
    for name in ('initial','mapped','requiredPayload'):
        assert 'effort' not in result[name]
    assert result['inherited']['effort']=='high'
    assert result['concrete']['effort']=='low'
    assert result['explicit']['effort']=='low'
    assert result['required']=='select an effort for this provider'
    assert result['chosen']=='' and result['chosenPayload']['effort']=='low'
    assert result['harness']['effort']=='high'
