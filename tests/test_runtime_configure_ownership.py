"""Explicit configure joins install-state; it never adopts user artifacts."""
from copy import deepcopy
import importlib
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'bin/lib'))


@pytest.fixture
def owned(tmp_path):
    root = tmp_path/'install'
    root.mkdir(mode=0o700)
    manifest = root/'install-state.json'
    manifest.write_text(json.dumps({'schema_version':1, 'install_dir':str(root),
        'owned_files':[str(manifest)], 'owned_dirs':[str(root)], 'skill_links':[],
        'services':[], 'retained_paths':[str(root/'runtime')],
        'purge_paths':[str(root/'runtime')]}))
    manifest.chmod(0o600)
    return root


def snapshot(root):
    return {str(p.relative_to(root)):(p.lstat().st_mode, p.lstat().st_ino,
            p.lstat().st_mtime_ns, os.readlink(p) if p.is_symlink()
            else p.read_bytes() if p.is_file() else None) for p in root.rglob('*')}


def test_install_preflight_preserves_legacy_minimal_manifest_without_r3_claim(owned):
    manifest=owned/'install-state.json'
    manifest.write_text('{}\n')
    before=snapshot(owned)
    api().installation_preflight(owned)
    assert snapshot(owned)==before
    # Explicit local configure still requires real ownership metadata.
    with pytest.raises(api().LocalModelError,match='INSTALL_MANIFEST_REQUIRED'):plan(owned)
    assert snapshot(owned)==before


def test_installer_can_publish_over_legacy_manifest_and_inventories_its_mutex(owned):
    manifest=owned/'install-state.json'
    manifest.write_text('{}\n')
    incoming={'schema_version':1,'install_dir':str(owned),'owned_files':[str(manifest)],
              'owned_dirs':[str(owned)],'skill_links':[]}
    api().publish_install_manifest(owned,incoming)
    saved=json.loads(manifest.read_text())
    assert 'configure_owned' not in saved
    locks=list((owned/'backups/instruction-migration/locks').glob('*.lock'))
    assert len(locks)==1
    assert set(map(str,locks)) <= set(saved['owned_files'])
    for lock in locks:
        assert lock.parent != owned
        assert {str(lock.parent),str(lock.parent.parent),str(owned/'backups')} <= set(saved['owned_dirs'])


def test_configure_inventories_same_mutex_namespace_without_owning_other_backups(owned):
    backup=owned/'backups'
    backup.mkdir(mode=0o700)
    sentinel=backup/'operator-backup.txt';sentinel.write_text('retain unrelated backup')
    value=plan(owned)
    api().apply_outputs(owned,value,approved_id=value['plan_id'])
    saved=json.loads((owned/'install-state.json').read_text())
    locks=list((backup/'instruction-migration/locks').glob('*.lock'))
    assert len(locks)==1 and str(locks[0]) in saved['owned_files']
    assert str(sentinel) not in saved['owned_files']
    assert sentinel.read_text()=='retain unrelated backup'


def api():
    return importlib.import_module('runtime_configure_ownership')


def output(root, **overrides):
    return {'path':str(root/'libexec/local-model-auth/fixture/runner'),
            'content':'#!/approved/python -I\n# fixture generated runner\n',
            'mode':0o500, 'kind':'auth-runner', **overrides}


def plan(root, outputs=None):
    return api().plan_outputs(root, outputs or [output(root)], allowed_roots=[root/'libexec'])


def test_empty_outputs_are_an_explicit_readonly_plan_and_apply(owned):
    before = snapshot(owned)
    value = api().plan_outputs(owned,[],allowed_roots=[owned/'libexec'])
    assert value['outputs'] == []
    assert snapshot(owned) == before
    assert api().apply_outputs(owned,value,approved_id=value['plan_id'])['changed'] is False
    assert snapshot(owned) == before


def test_plan_and_unconfirmed_apply_do_not_create_files_or_locks(owned):
    before = snapshot(owned)
    value = plan(owned)
    assert snapshot(owned) == before
    with pytest.raises(api().LocalModelError, match='CONFIRMATION_REQUIRED'):
        api().apply_outputs(owned, value, approved_id=None)
    assert snapshot(owned) == before


def test_apply_records_exact_files_modes_digests_and_directories_in_existing_manifest(owned):
    value = plan(owned)
    api().apply_outputs(owned, value, approved_id=value['plan_id'])
    record = output(owned)
    target = Path(record['path'])
    manifest = json.loads((owned/'install-state.json').read_text())
    assert target.read_text() == record['content']
    assert target.stat().st_mode & 0o777 == 0o500
    evidence = manifest['configure_owned'][str(target)]
    assert evidence['uid'] == os.getuid() and evidence['kind'] == 'auth-runner'
    assert evidence['mode'] == 0o500 and len(evidence['sha256']) == 64
    assert str(target) in manifest['owned_files']
    assert str(target.parent) in manifest['owned_dirs']
    assert not manifest.get('configure_pending')
    assert not list(owned.rglob('*auth*ledger*'))
    before = snapshot(owned)
    again = plan(owned)
    api().apply_outputs(owned, again, approved_id=again['plan_id'])
    assert snapshot(owned) == before, 'identical explicit configure must be a no-op'


@pytest.mark.parametrize('mutation', ['unknown','changed','mode','symlink','hardlink'])
def test_unowned_or_changed_outputs_are_neither_adopted_nor_overwritten(owned, mutation):
    value = plan(owned)
    target = Path(output(owned)['path'])
    if mutation == 'unknown':
        target.parent.mkdir(parents=True, mode=0o700)
        target.write_text(output(owned)['content'])
        target.chmod(0o500)
    else:
        api().apply_outputs(owned, value, approved_id=value['plan_id'])
        if mutation == 'changed':
            target.chmod(0o600)
            target.write_text('operator modification')
            target.chmod(0o500)
        elif mutation == 'mode':
            target.chmod(0o700)
        elif mutation == 'symlink':
            target.rename(target.with_name('sentinel'))
            target.symlink_to(target.with_name('sentinel'))
        else:
            os.link(target,target.with_name('sentinel'))
    before = snapshot(owned)
    with pytest.raises(api().LocalModelError, match='OWNERSHIP_CONFLICT'):
        plan(owned)
    assert snapshot(owned) == before


@pytest.mark.parametrize('change', ['content','destination','mode','manifest'])
def test_apply_rechecks_exact_approved_plan_and_manifest(owned, change):
    value = plan(owned)
    if change == 'manifest':
        path = owned/'install-state.json'
        row = json.loads(path.read_text())
        path.write_text(json.dumps({**row,'external_change':True}))
    else:
        value = deepcopy(value)
        key,new = {'content':('content','changed'), 'destination':('path',str(owned/'foreign')),
                   'mode':('mode',0o700)}[change]
        value['outputs'][0][key] = new
    before = snapshot(owned)
    with pytest.raises(api().LocalModelError, match='PLAN_CHANGED'):
        api().apply_outputs(owned,value,approved_id=value['plan_id'])
    assert snapshot(owned) == before


def test_pending_manifest_is_the_only_interruption_record_and_requires_same_plan(owned):
    value = plan(owned)
    def stop(stage):
        if stage == 'output-written':
            raise OSError('fixture interruption')
    with pytest.raises(OSError,match='fixture interruption'):
        api().apply_outputs(owned,value,approved_id=value['plan_id'],checkpoint=stop)
    saved = json.loads((owned/'install-state.json').read_text())
    assert saved['configure_pending']['plan_id'] == value['plan_id']
    before = snapshot(owned)
    with pytest.raises(api().LocalModelError,match='PENDING_CONFLICT'):
        plan(owned,[output(owned,content='different plan')])
    assert snapshot(owned) == before
    api().apply_outputs(owned,value,approved_id=value['plan_id'])
    assert not json.loads((owned/'install-state.json').read_text()).get('configure_pending')


def test_directory_created_by_someone_after_plan_is_not_adopted(owned):
    value = plan(owned)
    (owned/'libexec').mkdir(mode=0o700)
    before = snapshot(owned)
    with pytest.raises(api().LocalModelError,match='OWNERSHIP_CONFLICT'):
        api().apply_outputs(owned,value,approved_id=value['plan_id'])
    assert snapshot(owned) == before


def test_directory_without_recorded_inode_after_interruption_stays_unowned(owned):
    value = plan(owned)
    def stop(stage):
        if stage == 'directory-created':raise OSError('fixture directory interruption')
    with pytest.raises(OSError,match='fixture directory interruption'):
        api().apply_outputs(owned,value,approved_id=value['plan_id'],checkpoint=stop)
    before = snapshot(owned)
    with pytest.raises(api().LocalModelError,match='OWNERSHIP_CONFLICT'):
        api().apply_outputs(owned,value,approved_id=value['plan_id'])
    assert snapshot(owned) == before
    assert str(owned/'libexec') not in json.loads((owned/'install-state.json').read_text())['owned_dirs']


def test_completed_plan_replay_after_lost_return_is_readonly(owned):
    value = plan(owned)
    def stop(stage):
        if stage == 'complete':raise OSError('fixture lost result')
    with pytest.raises(OSError,match='fixture lost result'):
        api().apply_outputs(owned,value,approved_id=value['plan_id'],checkpoint=stop)
    before = snapshot(owned)
    assert api().apply_outputs(owned,value,approved_id=value['plan_id'])['changed'] is False
    assert snapshot(owned) == before


def test_skill_link_ownership_is_exact_target_and_never_includes_source(owned,tmp_path):
    source = tmp_path/'ordinary-skills/example'
    source.mkdir(parents=True)
    (source/'SKILL.md').write_text('fixture skill body')
    destination = owned/'runtime/provider-home/codex/skills/example'
    link = {'path':str(destination),'target':str(source),'kind':'skill-link'}
    value = api().plan_outputs(owned,[link],allowed_roots=[owned/'runtime/provider-home'])
    api().apply_outputs(owned,value,approved_id=value['plan_id'])
    saved = json.loads((owned/'install-state.json').read_text())
    assert saved['skill_links'] == [{'path':str(destination),'target':str(source)}]
    assert str(source) not in saved['owned_files']
    destination.unlink()
    destination.symlink_to(tmp_path/'retargeted')
    before = snapshot(owned)
    with pytest.raises(api().LocalModelError,match='OWNERSHIP_CONFLICT'):
        api().plan_outputs(owned,[link],allowed_roots=[owned/'runtime/provider-home'])
    assert snapshot(owned) == before


@pytest.mark.parametrize('destination',['relative','/outside/config','/a/../file'])
def test_output_paths_cannot_escape_admitted_output_roots(owned,destination):
    before = snapshot(owned)
    with pytest.raises(api().LocalModelError,match='OUTPUT_INVALID'):
        plan(owned,[output(owned,path=destination)])
    assert snapshot(owned) == before


def test_installer_manifest_publisher_preserves_exact_configure_ownership(owned):
    value = plan(owned)
    api().apply_outputs(owned,value,approved_id=value['plan_id'])
    before = json.loads((owned/'install-state.json').read_text())
    incoming = {key:value for key,value in before.items() if not key.startswith('configure_')}
    incoming.update(owned_files=[str(owned/'install-state.json')],owned_dirs=[str(owned)],
                    skill_links=[],new_version='fixture-next')
    api().publish_install_manifest(owned,incoming)
    after = json.loads((owned/'install-state.json').read_text())
    assert after['configure_owned'] == before['configure_owned']
    assert after['configure_owned_dirs'] == before['configure_owned_dirs']
    assert set(before['configure_owned']) <= set(after['owned_files'])
    assert set(before['configure_owned_dirs']) <= set(after['owned_dirs'])
    assert after['new_version'] == 'fixture-next'


@pytest.mark.parametrize('mutation',['changed','symlink','directory-replaced','pending'])
def test_install_preflight_and_republish_refuse_unresolved_outputs_without_writes(owned,mutation):
    value = plan(owned)
    api().apply_outputs(owned,value,approved_id=value['plan_id'])
    path = Path(output(owned)['path'])
    if mutation == 'changed':
        path.chmod(0o600)
        path.write_text('operator change')
        path.chmod(0o500)
    elif mutation == 'symlink':
        path.rename(path.with_name('original'))
        path.symlink_to(path.with_name('original'))
    elif mutation == 'directory-replaced':
        path.parent.rename(path.parent.with_name('original-directory'))
        path.parent.mkdir(mode=0o700)
    else:
        manifest = owned/'install-state.json'
        row = json.loads(manifest.read_text())
        manifest.write_text(json.dumps({**row,'configure_pending':{'plan_id':'unresolved'}}))
    before = snapshot(owned)
    incoming = {'schema_version':1,'install_dir':str(owned),'owned_files':[],'owned_dirs':[]}
    for check in (lambda:api().installation_preflight(owned),
                  lambda:api().publish_install_manifest(owned,incoming)):
        with pytest.raises(api().LocalModelError):check()
        assert snapshot(owned) == before


def test_removal_preflight_separates_changed_outputs_for_ancestor_purge(owned):
    value = api().plan_outputs(owned,[output(owned),output(owned,path=str(owned/'runtime/derived.json'),
                                           mode=0o600,kind='provider-settings')],
                               allowed_roots=[owned/'libexec',owned/'runtime'])
    api().apply_outputs(owned,value,approved_id=value['plan_id'])
    runtime = owned/'runtime/derived.json'
    runtime.write_text('operator change inside purge ancestor')
    before = snapshot(owned)
    result = api().inspect_owned_outputs(owned)
    assert result['removable'] == [output(owned)['path']]
    assert result['protected'] == [str(runtime)]
    assert snapshot(owned) == before


def test_reconfigure_does_not_adopt_replaced_previously_owned_directory(owned):
    value = plan(owned)
    api().apply_outputs(owned,value,approved_id=value['plan_id'])
    target = Path(output(owned)['path'])
    target.parent.rename(target.parent.with_name('original-directory'))
    target.parent.mkdir(mode=0o700)
    before = snapshot(owned)
    with pytest.raises(api().LocalModelError,match='OWNERSHIP_CONFLICT'):
        plan(owned)
    assert snapshot(owned) == before
