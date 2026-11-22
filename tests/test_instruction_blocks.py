"""User files only, isolated HOME; failure injection covers each durable boundary."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('instruction_blocks',ROOT/'bin/lib/instruction_blocks.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

@pytest.fixture
def env(tmp_path,monkeypatch):
    home=tmp_path/'home';home.mkdir()
    install=home/'.agentstack';install.mkdir()
    project=home/'work';project.mkdir()
    monkeypatch.setenv('HOME',str(home))
    monkeypatch.setenv('AGENTSTACK_HOME',str(install))
    monkeypatch.setenv('AGENTSTACK_TEMPLATE_HOME',str(ROOT))
    monkeypatch.setenv('AGENTSTACK_PROJECT_KEY',str(project))
    monkeypatch.chdir(project)
    return dict(os.environ)


def engine(env,provider='claude'):
    return m.Engine(provider,env=env)


def legacy(e,path,provider='claude'):
    item=next(x for x in e.catalog if x['provider']==provider)
    body=m.render(provider,item['template'].encode(),str(e.install),str(path.parent),'legacy-sed-v1')
    path.write_bytes(b'\xef\xbb\xbfMy notes\r\n\r\n'+body.replace(b'\n',b'\r\n')+b'\r\nLast line')
    return path.read_bytes()


def apply(e,**kw):
    p=e.plan();return e.apply(p,m.sha(m.canonical(p)),assume_yes=True,**kw)


def tree(root):
    return {str(x.relative_to(root)):x.read_bytes() for x in root.rglob('*') if x.is_file()}


def test_plan_is_readonly_and_separate_process_cas(env):
    e=engine(env);source=Path(env['AGENTSTACK_PROJECT_KEY'])/'CLAUDE.md';legacy(e,source)
    before=tree(Path(env['HOME']))
    args=[sys.executable,str(ROOT/'bin/lib/instruction_blocks.py'),'--plan','--json']
    r=subprocess.run(args,env=env,capture_output=True,text=True,check=True)
    p=json.loads(r.stdout);assert tree(Path(env['HOME']))==before
    target=e.target;target.parent.mkdir();target.write_text('concurrent note')
    r=subprocess.run([*args[:2],'--apply','--plan-id',p['plan_id'],'--assume-yes','--json'],env=env,capture_output=True,text=True)
    assert r.returncode==2 and 'INSTRUCTION_PLAN_CHANGED' in r.stdout
    assert target.read_text()=='concurrent note' and not e.receipt_path.exists()

@pytest.mark.parametrize('provider',['claude','codex'])
def test_user_target_exact_outside_bytes_and_uninstall(env,provider):
    e=engine(env,provider);e.target.parent.mkdir();outside=b'\xef\xbb\xbfpersonal\r\n\r\nend'
    e.target.write_bytes(outside)
    r=apply(e);assert r['stage']=='complete'
    assert e.target.read_bytes().startswith(outside)
    e.target.write_bytes(e.target.read_bytes()+b'new outside text')
    e.uninstall()
    assert e.target.read_bytes()==outside+b'\nnew outside text'
    assert list(e.ledger.glob('backups/*/*.before'))
    e.uninstall();assert m.purge_admission(e.install)


def test_multiple_sources_unique_backups(env,tmp_path):
    e=engine(env);one=Path(env['AGENTSTACK_PROJECT_KEY'])/'CLAUDE.md';two=one.parent/'child/CLAUDE.md';two.parent.mkdir()
    a=legacy(e,one);b=legacy(e,two)
    p=e.plan([str(two)]);r=e.apply(p,m.sha(m.canonical(p)),assume_yes=True)
    assert r['stage']=='complete' and len(r['sources_progress'])==2
    backups=[Path(x['backup']) for x in r['sources_progress']]
    assert len(set(backups))==2 and {x.read_bytes() for x in backups}=={a,b}
    assert one.read_bytes()==b'\xef\xbb\xbfMy notes\r\n\r\n\r\nLast line'

@pytest.mark.parametrize('after',[False,True])
@pytest.mark.parametrize('boundary',range(1,12))
def test_resume_each_durable_receipt_boundary(env,monkeypatch,boundary,after):
    e=engine(env);source=Path(env['AGENTSTACK_PROJECT_KEY'])/'CLAUDE.md';legacy(e,source)
    p=e.plan();pid=m.sha(m.canonical(p));original=e.save;calls=0
    def fail(r,stage=None):
        nonlocal calls
        calls+=1
        if calls==boundary and not after:raise OSError('power loss')
        original(r,stage)
        if calls==boundary and after:raise OSError('power loss')
    monkeypatch.setattr(e,'save',fail)
    try:e.apply(p,pid,assume_yes=True)
    except OSError:pass
    monkeypatch.setattr(e,'save',original)
    # Before the first receipt there are no mutations; a new apply is safe.
    if not e.receipt_path.exists():e.apply(p,pid,assume_yes=True)
    else:e.resume(pid)
    assert e.receipt()['stage']=='complete' and m.block_bytes(e.target.read_bytes())==e.block
    assert m.block_bytes(source.read_bytes()) is None

@pytest.mark.parametrize('source_link',[False,True])
def test_hardlink_and_symlink_referent_refused_without_mutation(env,source_link):
    e=engine(env);target=e.target;target.parent.mkdir();target.write_text('mine')
    alias=target.parent/'alias';os.link(target,alias)
    if source_link:
        link=target.parent/'link';link.symlink_to(target);e.target=link
    before=tree(Path(env['HOME']))
    with pytest.raises(m.Refused,match='HARDLINK'):e.plan()
    assert tree(Path(env['HOME']))==before


def test_symlink_preserved_and_new_hardlink_race_rejected(env,monkeypatch):
    e=engine(env);real=e.home/'real';real.parent.mkdir();real.write_text('outside')
    e.target.symlink_to(real.name)
    p=e.plan();original=m.verify;count=0
    def raced(entry,expected,**kw):
        nonlocal count
        count+=1
        if count==3:os.link(real,real.parent/'alias')
        return original(entry,expected,**kw)
    monkeypatch.setattr(m,'verify',raced)
    with pytest.raises(m.Refused,match='HARDLINK'):e.apply(p,m.sha(m.canonical(p)),assume_yes=True)
    assert real.read_text()=='outside' and e.target.is_symlink()
    monkeypatch.setattr(m,'verify',original);(real.parent/'alias').unlink()
    e.resume(m.sha(m.canonical(p)))
    assert e.target.is_symlink() and real.read_bytes().startswith(b'outside')


def test_edited_sources_retained_and_manual_resolution_allows_purge(env):
    e=engine(env);source=Path(env['AGENTSTACK_PROJECT_KEY'])/'CLAUDE.md'
    source.write_bytes(m.BEGIN['claude']+b'\nEdited unknown\n'+m.END+b'\n')
    r=apply(e);assert r['stage']=='retained' and source.read_bytes()
    with pytest.raises(m.Refused,match='PURGE_BLOCKED'):m.purge_admission(e.install)
    source.write_text('manual reviewed outside')
    e.uninstall();assert m.purge_admission(e.install)


def test_tracked_sources_are_not_assume_yes_consent(env):
    source=Path(env['AGENTSTACK_PROJECT_KEY'])/'CLAUDE.md'
    subprocess.run(['git','init','-q',str(source.parent)],check=True)
    e=engine(env);old=legacy(e,source)
    subprocess.run(['git','-C',str(source.parent),'add',source.name],check=True)
    assert apply(e)['stage']=='retained' and source.read_bytes()==old


def test_next_generation_accepts_outside_edits_and_explicit_replan(env,monkeypatch):
    e=engine(env);apply(e);before=e.target.read_bytes();e.target.write_bytes(before+b'outside')
    # A genuine new installed template is a generation, not an unknown block.
    e.template+=b'\nnew version\n';e.block=m.render('claude',e.template,str(e.install),e.project)
    assert apply(e)['stage']=='complete'
    assert e.target.read_bytes().endswith(b'outside')
    assert list((e.ledger/'history').glob('*.json'))

@pytest.mark.parametrize('bad',[b'<!-- <<< claude-agent-stack -->\n',m.BEGIN['claude']+b'\n'+m.BEGIN['codex']+b'\n'+m.END+b'\n',m.BEGIN['claude']+b'\nunclosed'])
def test_parser_refuses_ambiguous_markers(bad):
    with pytest.raises(m.Refused,match='malformed'):m.parse(bad)

@pytest.mark.parametrize('value',['a&b','a\\b','a|b'])
def test_historical_sed_matches_actual_writer(value):
    template=b'__AGENTSTACK_HOME__ / __AGENTSTACK_PROJECT_KEY__\n'
    actual=subprocess.run(['sed','-e',f's|__AGENTSTACK_HOME__|{value}|g','-e','s|__AGENTSTACK_PROJECT_KEY__|project|g'],input=template,capture_output=True)
    if actual.returncode:
        with pytest.raises(m.Refused):m.render('claude',template,value,'project','legacy-sed-v1')
    else:assert m.render('claude',template,value,'project','legacy-sed-v1')==m.BEGIN['claude']+b'\n'+actual.stdout+m.END+b'\n'


def test_detector_parent_only_isolates_bad_source(env,monkeypatch):
    e=engine(env);project=Path(env['AGENTSTACK_PROJECT_KEY']);legacy(e,project/'CLAUDE.md')
    child=project/'sub';child.mkdir();(child/'CLAUDE.md').symlink_to('missing')
    sibling=project/'sibling';sibling.mkdir();(sibling/'CLAUDE.md').write_text('never open')
    e.cwd=child
    findings=e.detect();assert any(f['kind']=='unknown' for f in findings)
    assert any(f['kind']=='legacy' for f in findings)
    assert not any('sibling' in f['source_path'] for f in findings)


def test_provider_configure_is_marker_only_and_launch_readonly(env):
    e=engine(env);home=e.install/'runtime/provider-home/claude';home.mkdir(parents=True)
    target=home/'CLAUDE.md';target.write_text('provider personal note\n')
    with pytest.raises(m.Refused,match='CONFIG_REQUIRED'):m.provider_instructions(home,'claude',ROOT,'')
    before=target.read_bytes();m.provider_instructions(home,'claude',ROOT,'',configure=True)
    assert target.read_bytes().startswith(before)
    after=tree(home);m.provider_instructions(home,'claude',ROOT,'');assert tree(home)==after
    assert not e.receipt_path.exists()


def test_home_scope_and_broken_target(env):
    env['AGENTSTACK_CLAUDE_MD_SCOPE']='project'
    with pytest.raises(m.Refused,match='SCOPE_REQUIRES'):engine(env)
    env.pop('AGENTSTACK_CLAUDE_MD_SCOPE');env['CLAUDE_CONFIG_DIR']=str(Path(env['HOME'])/'custom')
    env['CLAUDE_HOME']=str(Path(env['HOME'])/'.claude')
    with pytest.raises(m.Refused,match='HOME_CONFLICT'):engine(env)
    env.pop('CLAUDE_HOME');e=engine(env);e.home.mkdir();e.target.symlink_to('missing')
    with pytest.raises(m.Refused,match='PATH_UNSAFE'):engine(env)


def test_wire_outputs_match_canonical_schema(env):
    import jsonschema
    e=engine(env);p=e.plan();pid=m.sha(m.canonical(p));r=e.apply(p,pid,assume_yes=True)
    schema=json.loads((ROOT/'bin/lib/instruction_contract.json').read_text())
    for name,value in [('plan',p),('receipt',r)]:jsonschema.validate(value,{'$ref':'#/$defs/'+name,**schema})


def test_metadata_binary_xattr_mode_and_symlink_survive(env):
    e=engine(env);e.home.mkdir();real=e.home/'referent';real.write_text('personal')
    real.chmod(0o640);m.set_attr(real,'user.orrery-test' if sys.platform!='darwin' else 'org.orrery.test',m.base64.b64encode(b'\x00\xffmetadata').decode())
    before=m.attrs(real);e.target.symlink_to(real.name)
    apply(e)
    assert e.target.is_symlink() and real.stat().st_mode&0o777==0o640
    assert m.attrs(real)==before


def test_replan_is_readonly_and_preserves_old_backups(env,monkeypatch):
    e=engine(env);p=e.plan();pid=m.sha(m.canonical(p));original=e._resume
    monkeypatch.setattr(e,'_resume',lambda r:(_ for _ in ()).throw(OSError('after planned')))
    with pytest.raises(OSError):e.apply(p,pid,assume_yes=True)
    e.home.mkdir(exist_ok=True);e.target.write_text('new outside edit')
    before=tree(Path(env['HOME']));new=e.plan(replan=True);assert tree(Path(env['HOME']))==before
    with pytest.raises(m.Refused,match='OPERATION_PENDING'):e.apply(new,m.sha(m.canonical(new)),assume_yes=True,supersede=pid)
    monkeypatch.setattr(e,'_resume',original);monkeypatch.setattr(e,'confirm',lambda _: 'interactive-remove')
    e.apply(new,m.sha(m.canonical(new)),supersede=pid)
    assert e.target.read_bytes().startswith(b'new outside edit')
    assert (e.ledger/'history'/(pid+'.json')).exists()


def test_home_changed_resume_finds_original_fixed_receipt(env,monkeypatch):
    e=engine(env);p=e.plan();pid=m.sha(m.canonical(p))
    monkeypatch.setattr(e,'_resume',lambda r:(_ for _ in ()).throw(OSError('planned')))
    with pytest.raises(OSError):e.apply(p,pid,assume_yes=True)
    other=engine({**env,'CLAUDE_CONFIG_DIR':str(Path(env['HOME'])/'another')})
    with pytest.raises(m.Refused,match='HOME_CHANGED'):other.resume(pid)
    assert not other.target.exists()


def test_malformed_source_is_retained_while_other_source_migrates(env):
    e=engine(env);project=Path(env['AGENTSTACK_PROJECT_KEY'])
    bad=project/'CLAUDE.md';bad.write_bytes(m.BEGIN['claude']+b'\nUnclosed old instructions')
    good=project/'child/CLAUDE.md';good.parent.mkdir();legacy(e,good)
    previous=bad.read_bytes()
    p=e.plan([str(good)])
    result=e.apply(p,m.sha(m.canonical(p)),assume_yes=True)
    assert result['stage']=='retained' and bad.read_bytes()==previous
    assert m.block_bytes(good.read_bytes()) is None
    with pytest.raises(m.Refused,match='BACKUP_PURGE_BLOCKED'):m.purge_admission(e.install)
    bad.write_text('reviewed outside note')
    e.uninstall();assert m.purge_admission(e.install)


def test_codex_does_not_automigrate_project_claude_file(env):
    e=engine(env,'codex');source=Path(env['AGENTSTACK_PROJECT_KEY'])/'CLAUDE.md'
    legacy(e,source);previous=source.read_bytes()
    assert e.plan()['sources']==[]
    apply(e)
    assert source.read_bytes()==previous
    assert any(f['source_path']==str(source) for f in e.detect())


def test_update_uses_old_project_for_source_proof_and_current_project_for_target(env):
    old=Path(env['HOME'])/'previous-project';old.mkdir()
    e=engine({**env,'AGENTSTACK_LEGACY_PROJECT_KEY':str(old)})
    item=next(x for x in e.catalog if x['provider']=='claude' and '__AGENTSTACK_PROJECT_KEY__' in x['template'])
    source=old/'CLAUDE.md'
    nested=old/'nested/CLAUDE.md';nested.parent.mkdir()
    original=m.render('claude',item['template'].encode(),str(e.install),str(old),'legacy-sed-v1')
    for path in (source,nested):path.write_bytes(original)
    p=e.plan([str(nested)])
    assert p['target']['render_inputs']['project_key']==env['AGENTSTACK_PROJECT_KEY']
    assert {x['requested_path'] for x in p['sources']}=={str(source),str(nested)}
    assert all(x['render_inputs']['project_key']==str(old) for x in p['sources'])
    assert all(x['render_inputs']['renderer_id']=='legacy-sed-v1' for x in p['sources'])
    # The real setup entry reads the newly published env.sh, while the
    # installer carries the prior key only for old-source discovery.
    import shlex
    (e.install/'env.sh').write_text('export AGENTSTACK_PROJECT_KEY='+shlex.quote(env['AGENTSTACK_PROJECT_KEY'])+'\n')
    response=subprocess.run(['/bin/bash',str(ROOT/'bin/agentstack-claude-setup'),'--apply',
        '--plan-id',m.sha(m.canonical(p)),'--assume-yes','--path',str(nested),'--json'],
        env=e.env,capture_output=True,text=True,check=True)
    result=json.loads(response.stdout)
    assert result['stage']=='complete'
    assert env['AGENTSTACK_PROJECT_KEY'].encode() in e.target.read_bytes()
    assert str(old).encode() not in e.target.read_bytes()
    assert all(m.block_bytes(path.read_bytes()) is None for path in (source,nested))

@pytest.mark.parametrize('provider',['claude','codex'])
def test_fresh_noninteractive_setup_needs_no_existing_file_consent(env,provider,monkeypatch):
    e=engine(env,provider)
    monkeypatch.setattr(m.sys.stdin,'isatty',lambda:False)
    p=e.plan();r=e.apply(p,m.sha(m.canonical(p)))
    assert r['stage']=='complete' and r['consent']=='not-required'
    assert e.target.read_bytes()==e.block
    # Repeating a correct target without a receipt also changes no user file.
    e.receipt_path.unlink();before=e.target.stat()
    p=e.plan();e.apply(p,m.sha(m.canonical(p)))
    assert e.target.stat().st_ino==before.st_ino
    assert e.target.stat().st_mtime_ns==before.st_mtime_ns

@pytest.mark.parametrize('existing',['target','source'])
def test_noninteractive_existing_file_change_requires_consent(env,existing,monkeypatch):
    e=engine(env)
    if existing=='target':
        e.target.parent.mkdir();e.target.write_text('personal instructions\n')
    else:
        legacy(e,Path(env['AGENTSTACK_PROJECT_KEY'])/'CLAUDE.md')
    before=tree(Path(env['HOME']))
    monkeypatch.setattr(m.sys.stdin,'isatty',lambda:False)
    p=e.plan()
    with pytest.raises(m.Refused,match='confirmation required'):
        e.apply(p,m.sha(m.canonical(p)))
    # Mutex files are coordination only; no user bytes or receipt may change.
    after=tree(Path(env['HOME']))
    assert all(after[k]==v for k,v in before.items())
    assert not e.receipt_path.exists()
    assert (existing=='target')==e.target.exists()


def test_explicit_claude_config_dir_installs_at_the_literal_user_path(env):
    home = Path(env['HOME'])
    env['CLAUDE_CONFIG_DIR'] = str(home / 'custom-config')
    e = engine(env)
    assert e.target == home / 'custom-config' / 'CLAUDE.md'
    apply(e)
    assert m.block_bytes((home / 'custom-config' / 'CLAUDE.md').read_bytes()) == e.block
    assert not (home / '.claude' / 'CLAUDE.md').exists()
    assert not (Path(env['AGENTSTACK_PROJECT_KEY']) / 'CLAUDE.md').exists()
