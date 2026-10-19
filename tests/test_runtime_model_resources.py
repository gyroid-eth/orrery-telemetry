"""Resource producers do not execute auth/CLI or adopt external source files."""
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bin/lib'))
resources = importlib.import_module('runtime_model_resources')


@pytest.fixture
def root(tmp_path):
    root = tmp_path/'install'
    root.mkdir(mode=0o700)
    return root


def executable(tmp_path):
    path = tmp_path/'approved-python'
    path.write_text('#!/bin/sh\necho MUST_NOT_EXECUTE > never-created\n')
    path.chmod(0o500)
    return path


def test_runner_plan_is_readonly_and_binds_absolute_isolated_interpreter(root,tmp_path):
    python = executable(tmp_path)
    before = list(root.iterdir())
    plan = resources.runner_output(root,python)
    assert list(root.iterdir()) == before
    assert not (tmp_path/'never-created').exists()
    output = plan['output']
    assert output['content'].startswith('#!'+str(python)+' -I\n')
    assert output['mode'] == 0o500
    digest = hashlib.sha256(output['content'].encode()).hexdigest()
    assert output['path'] == str(root/'libexec/local-model-auth'/digest/'runner')
    assert plan['runner']['sha256'] == digest
    assert plan['python']['sha256'] == hashlib.sha256(python.read_bytes()).hexdigest()


@pytest.mark.parametrize('mutation',['symlink','hardlink','writable','not-executable','relative','spaces'])
def test_runner_refuses_unsafe_or_unrepresentable_interpreter(root,tmp_path,mutation):
    python = executable(tmp_path)
    if mutation == 'symlink':
        link = tmp_path/'link'
        link.symlink_to(python)
        python = link
    elif mutation == 'hardlink':os.link(python,tmp_path/'alias')
    elif mutation == 'writable':python.chmod(0o777)
    elif mutation == 'not-executable':python.chmod(0o600)
    elif mutation == 'relative':python = Path('relative-python')
    else:
        renamed = tmp_path/'python with spaces'
        python.rename(renamed)
        python = renamed
    with pytest.raises(resources.LocalModelError,match='RESOURCE_INVALID'):
        resources.runner_output(root,python)
    assert not list(root.iterdir())


def test_skill_outputs_pin_original_body_and_only_own_new_links(root,tmp_path):
    source = tmp_path/'normal-skills/example'
    source.mkdir(parents=True)
    (source/'SKILL.md').write_text('ordinary fixture skill nonce\n')
    value = resources.skill_outputs([str(source)],root/'runtime/provider-home/codex/skills')
    assert value['outputs'] == [{'path':str(root/'runtime/provider-home/codex/skills/example'),
                                  'target':str(source),'kind':'skill-link'}]
    assert value['source_evidence'][str(source)]['skill_md_sha256'] == hashlib.sha256(
        (source/'SKILL.md').read_bytes()).hexdigest()
    assert not list(root.iterdir())
    assert (source/'SKILL.md').read_text() == 'ordinary fixture skill nonce\n'


def test_source_dependencies_are_hashes_only_never_install_owned_or_executed(tmp_path):
    source = executable(tmp_path)
    proof = resources.resource_digests([str(source)])
    assert proof == {str(source):hashlib.sha256(source.read_bytes()).hexdigest()}
    assert not (tmp_path/'never-created').exists()
    assert 'owned_files' not in json.dumps(proof)


@pytest.mark.parametrize('mutation',[None,'content','mode','symlink','missing'])
def test_deployed_runner_rechecks_approved_interpreter_without_execution(root,tmp_path,mutation):
    python = executable(tmp_path)
    value = resources.runner_output(root,python)
    runner = Path(value['output']['path'])
    runner.parent.mkdir(mode=0o700,parents=True)
    runner.write_text(value['output']['content'])
    runner.chmod(0o500)
    if mutation == 'content':
        python.chmod(0o600)
        python.write_text('# changed interpreter resource\n')
        python.chmod(0o500)
    elif mutation == 'mode':python.chmod(0o700)
    elif mutation == 'symlink':
        original = python.with_name('original-python')
        python.rename(original)
        python.symlink_to(original)
    elif mutation == 'missing':python.unlink()
    auth = importlib.import_module('local_model_auth')
    def check():auth.check_runner_resources(value['runner'],os.getuid())
    if mutation is None:check()
    else:
        with pytest.raises(auth.LocalModelError,match='AUTH_FAILED'):check()
    assert not (tmp_path/'never-created').exists()


def test_settings_source_records_absence_and_hashes_without_disclosing_contents(tmp_path):
    present = tmp_path/'config.toml'
    present.write_text('fixture_private_setting="must-not-appear-in-evidence"\n')
    present.chmod(0o600)
    absent = tmp_path/'not-created/config.toml'
    value = resources.settings_sources([str(present),str(absent)])
    assert value == {str(present):{'state':'present','uid':os.getuid(),'mode':0o600,
                                  'sha256':hashlib.sha256(present.read_bytes()).hexdigest()},
                     str(absent):{'state':'absent'}}
    assert 'must-not-appear-in-evidence' not in json.dumps(value)
    assert not absent.parent.exists(), 'readonly source admission created a directory'
    present.write_text('changed=true\n')
    assert resources.settings_sources([str(present),str(absent)]) != value
    absent.parent.mkdir(mode=0o700)
    absent.write_text('new-source=true\n')
    absent.chmod(0o600)
    assert resources.settings_sources([str(present),str(absent)])[str(absent)]['state'] == 'present'


@pytest.mark.parametrize('mutation',['symlink-file','symlink-parent','hardlink','writable','directory','relative','dotdot'])
def test_settings_source_rejects_unsafe_or_aliased_sources(tmp_path,mutation):
    path = tmp_path/'config.toml'
    path.write_text('fixture=true\n')
    path.chmod(0o600)
    if mutation == 'symlink-file':
        alias = tmp_path/'alias.toml';alias.symlink_to(path);path = alias
    elif mutation == 'symlink-parent':
        alias = tmp_path/'alias';alias.symlink_to(tmp_path,target_is_directory=True);path = alias/'missing.toml'
    elif mutation == 'hardlink':os.link(path,tmp_path/'other.toml')
    elif mutation == 'writable':path.chmod(0o666)
    elif mutation == 'directory':path = tmp_path
    elif mutation == 'relative':path = 'config.toml'
    else:path = str(tmp_path)+'/../config.toml'
    with pytest.raises(resources.LocalModelError,match='RESOURCE_INVALID'):
        resources.settings_sources([str(path)])


def test_auth_source_pins_helper_and_declared_dependencies_without_execution(tmp_path):
    command = executable(tmp_path)
    dependency = tmp_path/'dependency.txt'
    dependency.write_text('external resource\n');dependency.chmod(0o600)
    profile = {'codex':{'auth_command':[str(command),'fixture-reference']},'claude':{}}
    value = resources.auth_source(profile,'codex',[str(dependency)])
    assert value['executable']['path'] == str(command)
    assert value['dependency_digests'] == {str(dependency):hashlib.sha256(dependency.read_bytes()).hexdigest()}
    assert resources.auth_source(profile,'claude',[]) is None
    with pytest.raises(resources.LocalModelError,match='RESOURCE_INVALID'):
        resources.auth_source(profile,'claude',[str(dependency)])
    assert not (tmp_path/'never-created').exists()


@pytest.mark.parametrize('mutation',[None,'command-content','command-mode','command-symlink','dependency','argv'])
def test_auth_runner_rechecks_pinned_source_before_executor(tmp_path,mutation):
    command = executable(tmp_path)
    dependency = tmp_path/'dependency.txt'
    dependency.write_text('original\n');dependency.chmod(0o600)
    argv = [str(command),'fixture-reference']
    source = resources.auth_source({'codex':{'auth_command':argv}},'codex',[str(dependency)])
    if mutation == 'command-content':
        command.chmod(0o600);command.write_text('# changed\n');command.chmod(0o500)
    elif mutation == 'command-mode':command.chmod(0o700)
    elif mutation == 'command-symlink':
        old = command.with_name('previous');command.rename(old);command.symlink_to(old)
    elif mutation == 'dependency':dependency.write_text('changed\n')
    elif mutation == 'argv':argv[0] = '/missing/credential-source'
    auth = importlib.import_module('local_model_auth')
    plan = {'argv':argv,'source':source,'cwd':str(tmp_path),
            'env':{'PATH':'/usr/bin:/bin','LANG':'C','HOME':str(tmp_path),'USER':'fixture','LOGNAME':'fixture'},
            'timeout_ms':5000,'stdout_limit_bytes':8192,'refresh_interval_ms':300000}
    executed = []
    def executor(*args,**kwargs):
        from subprocess import CompletedProcess
        executed.append(args);return CompletedProcess(args[0],0,stdout=b'fixture-output')
    if mutation is None:
        assert auth.run_auth(plan,executor=executor) == b'fixture-output'
        assert len(executed) == 1
    else:
        with pytest.raises(auth.LocalModelError,match='AUTH_FAILED'):auth.run_auth(plan,executor=executor)
        assert not executed
    assert not (tmp_path/'never-created').exists()


@pytest.mark.parametrize('mutation',[None,'cli','settings','settings-mode','new-settings','renderer',
                                     'skill-body','skill-asset','new-skill-file','symlink-skill-file','native-claim'])
def test_configuration_resource_drift_is_checked_without_native_or_auth_execution(tmp_path,mutation):
    auth = importlib.import_module('local_model_auth')
    command = executable(tmp_path)
    settings = tmp_path/'settings.json';settings.write_text('{}');settings.chmod(0o600)
    absent = tmp_path/'absent.json'
    renderer = tmp_path/'renderer.py';renderer.write_text('# reviewed producer\n');renderer.chmod(0o600)
    skill = tmp_path/'skills/example';skill.mkdir(parents=True)
    body = skill/'SKILL.md';body.write_text('fixture skill\n')
    asset = skill/'asset.txt';asset.write_text('fixture asset\n')
    value = {'contract_version':1,
             'cli':{**resources.executable_evidence(command),'declared_version':'fixture-pinned'},
             'settings_sources':resources.settings_sources([str(settings),str(absent)]),
             'renderer_sources':resources.settings_sources([str(renderer)]),
             'skill_sources':auth.skill_source_evidence([str(skill)]),'requires_native_validation':True}
    if mutation == 'cli':command.chmod(0o600);command.write_text('# changed\n');command.chmod(0o500)
    elif mutation == 'settings':settings.write_text('{"changed":true}')
    elif mutation == 'settings-mode':settings.chmod(0o644)
    elif mutation == 'new-settings':absent.write_text('{}');absent.chmod(0o600)
    elif mutation == 'renderer':renderer.write_text('# changed\n')
    elif mutation == 'skill-body':body.write_text('changed skill\n')
    elif mutation == 'skill-asset':asset.write_text('changed asset\n')
    elif mutation == 'new-skill-file':(skill/'added.txt').write_text('extra source\n')
    elif mutation == 'symlink-skill-file':(skill/'alias.txt').symlink_to(asset)
    elif mutation == 'native-claim':value['requires_native_validation'] = False
    if mutation is None:auth.check_configuration_evidence(value)
    else:
        with pytest.raises(auth.LocalModelError,match='AUTH_FAILED'):auth.check_configuration_evidence(value)
    assert not (tmp_path/'never-created').exists()
