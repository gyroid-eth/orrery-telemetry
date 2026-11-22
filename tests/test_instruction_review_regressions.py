"""Migration boundaries reported by independent review, isolated user files only."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from test_instruction_blocks import ROOT, env, engine, apply, legacy, tree, m
from test_instruction_migration_uninstall import installed


@pytest.mark.parametrize('explicit',[False,True])
def test_old_hardlinked_source_refuses_before_any_write(env,explicit):
    e=engine(env);source=Path(env['AGENTSTACK_PROJECT_KEY'])/'CLAUDE.md';legacy(e,source)
    os.link(source,source.with_name('alias'));before=tree(Path(env['HOME']))
    with pytest.raises(m.Refused,match='INSTRUCTION_HARDLINK_REQUIRES_MANUAL_COPY'):
        e.plan([str(source)] if explicit else [])
    assert tree(Path(env['HOME']))==before and not e.target.exists()


def test_purge_checks_retained_sources_in_all_receipt_generations(env):
    e=engine(env);source=Path(env['AGENTSTACK_PROJECT_KEY'])/'CLAUDE.md'
    source.write_bytes(m.BEGIN['claude']+b'\nEdited notes\n'+m.END+b'\n')
    assert apply(e)['stage']=='retained';previous=source.parent
    for name in ('B','C'):
        current=previous.parent/name;current.mkdir()
        e=m.Engine('claude',env={**env,'AGENTSTACK_PROJECT_KEY':str(current),'AGENTSTACK_LEGACY_PROJECT_KEY':str(previous)},cwd=current)
        apply(e);previous=current
    assert len(list((e.ledger/'history').glob('*.json')))==2
    before=tree(Path(env['HOME']))
    with pytest.raises(m.Refused,match='INSTRUCTION_BACKUP_PURGE_BLOCKED'):m.purge_admission(e.install)
    assert tree(Path(env['HOME']))==before
    source.write_text('Reviewed notes without markers\n')
    assert m.purge_admission(e.install)


@pytest.mark.parametrize('target_ready',[False,True])
def test_explicit_cleanup_changes_only_selected_source(env,monkeypatch,target_ready):
    e=engine(env);first=Path(env['AGENTSTACK_PROJECT_KEY'])/'CLAUDE.md';legacy(e,first)
    other=first.parent/'other';other.mkdir();selected=other/'CLAUDE.md';legacy(e,selected)
    if target_ready:e.home.mkdir();e.target.write_bytes(e.block)
    before=tree(Path(env['HOME']));monkeypatch.setattr(e,'confirm',lambda _: 'interactive-remove')
    if target_ready:
        result=e.remove_source(selected)
        assert [x['requested_path'] for x in result['plan']['sources']]==[str(selected)]
        assert m.block_bytes(selected.read_bytes()) is None
    else:
        with pytest.raises(m.Refused,match='INSTRUCTION_TARGET_REQUIRES_SETUP'):e.remove_source(selected)
    after=tree(Path(env['HOME']))
    for name,data in before.items():
        if target_ready and name==str(selected.relative_to(Path(env['HOME']))):continue
        assert after[name]==data
    assert e.target.exists()==target_ready


@pytest.mark.parametrize('change',['symlink','hardlink'])
def test_uninstall_skips_one_unsafe_target_and_continues_other_provider(env,change):
    e=installed(env);apply(e);codex=engine(env,'codex');apply(codex)
    old=e.target.read_bytes();alias=Path(env['HOME'])/'dotfiles';alias.write_bytes(old);alias.chmod(0o600)
    e.target.unlink()
    if change=='symlink':e.target.symlink_to(alias)
    else:os.link(alias,e.target)
    manifest={'install_dir':str(e.install),'services':[],'owned_files':[],'owned_dirs':[], 'purge_paths':[],
              'instruction_receipts':[str(e.receipt_path),str(codex.receipt_path)]}
    (e.install/'install-state.json').write_text(json.dumps(manifest))
    result=subprocess.run(['/bin/bash',str(ROOT/'scripts/uninstall.sh')],env={**env,'PATH':str(Path(sys.executable).parent)+':/usr/bin:/bin'},capture_output=True,text=True)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'warn: kept user instruction block:' in result.stderr
    assert alias.read_bytes()==old and e.target.read_bytes()==old
    assert m.block_bytes(codex.target.read_bytes()) is None


def test_edited_source_has_manual_review_instead_of_ineffective_cleanup(env):
    e=engine(env);source=Path(env['AGENTSTACK_PROJECT_KEY'])/'CLAUDE.md'
    source.write_bytes(m.BEGIN['claude']+b'\npersonal edits\n'+m.END+b'\n')
    f=next(x for x in e.detect() if x['source_path']==str(source))
    assert f['kind']=='edited' and f['cleanup_argv']==[] and 'compare locally' in f['reason']


@pytest.mark.parametrize('provider',['claude','codex'])
def test_explicit_project_key_wins_over_installed_env_for_setup(env,provider):
    install=Path(env['AGENTSTACK_HOME']);(install/'env.sh').write_text('export AGENTSTACK_PROJECT_KEY=/old/installed-project\n')
    result=subprocess.run(['/bin/bash',str(ROOT/f'bin/agentstack-{provider}-setup'),'--print'],env=env,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert env['AGENTSTACK_PROJECT_KEY'] in result.stdout and '/old/installed-project' not in result.stdout


@pytest.mark.parametrize('participant',[False,True])
def test_session_start_migration_notices_only_for_participants(env,participant):
    e=engine(env);source=Path(env['AGENTSTACK_PROJECT_KEY'])/'CLAUDE.md';legacy(e,source)
    values={**env,'AGENTSTACK_HOOKS_DIR':str(ROOT/'hooks'),'AGENTSTACK_RUNTIME_DIR':str(e.install/'runtime'),
            'AGENTSTACK_MCP_URL':'http://127.0.0.1:9/mcp','AGENTSTACK_PYTHON':sys.executable,'PATH':str(Path(sys.executable).parent)+':/usr/bin:/bin:/usr/sbin:/sbin'}
    for key in ('AGENT_NAME','TMUX','TMUX_PANE','PARENT_AGENT','CHILD_REGISTRATION_TOKEN'):values.pop(key,None)
    if participant:values['AGENT_NAME']='ExistingIdentity'
    result=subprocess.run(['/bin/bash',str(ROOT/'hooks/session-start-reminder.sh')],input=json.dumps({'session_id':'instruction-review-session'}),env=values,capture_output=True,text=True,timeout=60)
    assert result.returncode==0,result.stderr
    assert ('ORRERY instruction migration:' in result.stdout)==participant


@pytest.mark.parametrize('generation',['current','history'])
def test_corrupt_receipt_hash_blocks_purge_before_any_uninstall_effect(env,generation):
    e=installed(env);e.home.mkdir();e.target.write_text('Personal notes to retain in backup\n');apply(e)
    prior=e.receipt_path.read_bytes()
    if generation=='history':
        history=e.ledger/'history';history.mkdir();path=history/'previous.json'
        path.write_bytes(prior);path.chmod(0o600)
    else:path=e.receipt_path
    receipt=json.loads(path.read_bytes())
    receipt['plan']['target']['render_inputs']['project_key']='/changed-without-updating-hash'
    path.write_bytes(m.canonical(receipt)+b'\n')
    (e.install/'install-state.json').write_text(json.dumps({'install_dir':str(e.install),
        'services':[],'owned_files':[],'owned_dirs':[],'purge_paths':[],
        'instruction_receipts':[str(e.receipt_path)]}))
    before=tree(Path(env['HOME']))
    with pytest.raises(m.Refused,match='INSTRUCTION_PLAN_CHANGED'):m.purge_admission(e.install)
    result=subprocess.run(['/bin/bash',str(ROOT/'scripts/uninstall.sh'),'--purge-data'],
        env={**env,'PATH':str(Path(sys.executable).parent)+':/usr/bin:/bin'},capture_output=True,text=True)
    assert result.returncode!=0 and 'INSTRUCTION_PLAN_CHANGED' in result.stderr
    assert tree(Path(env['HOME']))==before
    assert e.ledger.exists() and list(e.ledger.glob('backups/*/*.before'))


@pytest.mark.parametrize('entry',['setup','installer'])
def test_unrelated_hardlink_ancestor_does_not_block_setup_or_installer(env,entry):
    import shutil
    e=installed(env);source=Path(env['HOME'])/'CLAUDE.md'
    source.write_text('Personal instructions without ORRERY markers\n')
    alias=source.with_name('personal-alias');os.link(source,alias)
    before=source.read_bytes();inode=source.stat().st_ino
    setup=e.install/'bin/agentstack-claude-setup'
    shutil.copy2(ROOT/'bin/agentstack-claude-setup',setup)
    values={**env,'AGENTSTACK_PYTHON':sys.executable}
    if entry=='setup':command=['/bin/bash',str(setup),'--assume-yes']
    else:
        # Run the installer's actual managed-setup function, without services.
        script=(ROOT/'scripts/install.sh').read_text()
        function=script[script.index('run_managed_setup() {'):script.index('safe_managed_doc_setups() {')]
        command=['/bin/bash','-euc',function+'''\nDRY_RUN=false
REPO_ROOT="$AGENTSTACK_TEMPLATE_HOME"
INSTALL_DIR="$AGENTSTACK_HOME"
BIN_DIR="$INSTALL_DIR/bin"
PROJECT_KEY="$AGENTSTACK_PROJECT_KEY"
ASSUME_YES=1
say() { printf '%s\\n' "$*"; }
warn() { printf '%s\\n' "$*" >&2; }
run_managed_setup "Claude CLAUDE.md" agentstack-claude-setup
''']
    result=subprocess.run(command,cwd=env['AGENTSTACK_PROJECT_KEY'],env=values,capture_output=True,text=True)
    assert result.returncode==0,result.stdout+result.stderr
    assert m.block_bytes(e.target.read_bytes())==e.block
    assert source.read_bytes()==alias.read_bytes()==before and source.stat().st_ino==inode
    assert not any(f['source_path']==str(source) for f in e.detect())
    with pytest.raises(m.Refused,match='INSTRUCTION_HARDLINK_REQUIRES_MANUAL_COPY'):e.plan([str(source)])
