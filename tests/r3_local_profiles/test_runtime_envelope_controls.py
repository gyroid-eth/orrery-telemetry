"""Negative controls for the literal runtime envelope and fixed fixture command.

These controls are measurement tests, not production caller/native acceptance.
The fixed driver is not evidence that an actual Claude/Codex binary was started.
Its check-to-exec race is accepted only within the owned temporary fixture root
and 0500 capture scripts, never as a product security boundary.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from . import fixture_provider_driver
from .evidence import (assert_caller_capture, assert_resume_capture, assert_runtime_capture,
                       prepare_caller_invocation, read_fresh_capture)


def execute(workspace, entry='shell', *, before_execution=None):
    owner, binary, capture, nonce = prepare_caller_invocation(workspace, entry)
    receipt = owner.parent/'runtime/launch.json'
    receipt.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    row = {'launch_id':'a92493ad-1688-499b-898f-b1445c8a63b6',
           'receipt_nonce':'independent-receipt-'+nonce}
    receipt.write_text(json.dumps(row))
    env = {'HOME':str(owner.parent/'runtime/provider-home/codex'), 'PATH':'/usr/bin:/bin',
           'AGENTSTACK_LAUNCH_ID':row['launch_id'], 'AGENTSTACK_LAUNCH_NONCE':row['receipt_nonce']}
    expected_script_hash = hashlib.sha256(capture.with_suffix('.py').read_bytes()).hexdigest()
    if before_execution:before_execution(binary,capture)
    subprocess.run(['/bin/sh','-c','export AGENTSTACK_PROVIDER_PID=$$; exec "$@"',
                    'fixture',str(binary),'--model','fixture-glm-model'],
                   cwd=workspace.scratch.parent,env=env,check=True)
    observed = read_fresh_capture(capture,nonce)
    return owner,binary,capture,nonce,receipt,observed,expected_script_hash


@pytest.mark.parametrize('mutation',[None,'uuid','nonce','pid','missing-uuid','missing-nonce',
                                    'missing-pid','old-capture','driver-bypass'])
def test_exact_three_runtime_keys_need_independent_receipt_and_process_evidence(workspace,mutation):
    owner,binary,capture,nonce,receipt,observed,expected_hash = execute(workspace)
    if mutation in ('uuid','nonce','pid'):
        key = {'uuid':'AGENTSTACK_LAUNCH_ID','nonce':'AGENTSTACK_LAUNCH_NONCE',
               'pid':'AGENTSTACK_PROVIDER_PID'}[mutation]
        observed['env'][key] = 'wrong'
    elif mutation and mutation.startswith('missing-'):
        key = {'uuid':'AGENTSTACK_LAUNCH_ID','nonce':'AGENTSTACK_LAUNCH_NONCE',
               'pid':'AGENTSTACK_PROVIDER_PID'}[mutation[8:]]
        del observed['env'][key]
    elif mutation == 'old-capture':
        receipt.write_text(json.dumps({'launch_id':'b92493ad-1688-499b-898f-b1445c8a63b6',
                                       'receipt_nonce':'new-receipt'}))
    elif mutation == 'driver-bypass':
        capture.with_suffix('.driver.json').unlink()
    def check():
        assert_runtime_capture(observed,receipt,capture=capture,binary=binary,
                               expected_script_sha256=expected_hash)
    if mutation is None:
        check()
    else:
        with pytest.raises((AssertionError,KeyError,FileNotFoundError)):
            check()


@pytest.mark.parametrize('mutation',[None,'other-value','extra-key','missing-key'])
@pytest.mark.parametrize('resume',[False,True])
def test_no_other_environment_difference_is_excluded(workspace,mutation,resume):
    _,_,capture,_,_,observed,_ = execute(workspace)
    expected = deepcopy(observed)
    # All three are supposed to differ across genuine launches.
    observed['env'].update(AGENTSTACK_LAUNCH_ID='different-uuid',
                           AGENTSTACK_LAUNCH_NONCE='different-nonce',AGENTSTACK_PROVIDER_PID='2')
    if mutation == 'other-value':observed['env']['HOME'] = '/wrong-home'
    elif mutation == 'extra-key':observed['env']['UNREVIEWED_ENV'] = 'unexpected'
    elif mutation == 'missing-key':del observed['env']['PATH']
    if resume:
        expected['argv'].append('literal-startup')
        observed['argv'] = ['resume','literal-session',*observed['argv']]
    capture.write_text(json.dumps(observed))
    def check():
        if resume:
            assert_resume_capture(observed,expected,startup_prompt='literal-startup',
                                  envelope_index=0,session_id='literal-session')
        else:assert_caller_capture(capture,expected)
    if mutation is None:check()
    else:
        with pytest.raises(AssertionError,match='env'):check()


@pytest.mark.parametrize('mutation',[None,'traversal','outside-absolute','symlink','mode',
                                    'dispatch-symlink','dispatch-mode','hash','stale-script','driver-changed'])
def test_dispatch_cannot_escape_fresh_checked_capture(workspace,mutation):
    owner,binary,previous,_,_,_,_ = execute(workspace)
    approved_digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    owner,binary,capture,nonce = prepare_caller_invocation(workspace,'dashboard')
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == approved_digest
    dispatch_path = binary.with_name('fixture-provider-dispatch.json')
    dispatch = json.loads(dispatch_path.read_text())
    script = Path(dispatch['script'])
    if mutation in ('traversal','outside-absolute'):
        outside = workspace.tmp/'bin/outside.py'
        outside.write_bytes(script.read_bytes())
        outside.chmod(0o500)
        dispatch['script'] = (str(script.parent/'../bin/outside.py') if mutation == 'traversal'
                              else str(outside))
    elif mutation == 'symlink':
        other = script.with_suffix('.saved')
        script.rename(other)
        script.symlink_to(other)
    elif mutation == 'mode':script.chmod(0o700)
    elif mutation == 'hash':dispatch['sha256'] = '0'*64
    elif mutation == 'stale-script':
        dispatch['script'] = str(previous.with_suffix('.py'))
        dispatch['sha256'] = hashlib.sha256(previous.with_suffix('.py').read_bytes()).hexdigest()
    elif mutation == 'driver-changed':
        binary.chmod(0o600)
        binary.write_text('# changed approved source\n')
        binary.chmod(0o500)
        with pytest.raises(AssertionError,match='approved driver changed'):
            prepare_caller_invocation(workspace,'operator')
        assert not capture.exists()
        return
    dispatch_path.write_text(json.dumps(dispatch))
    if mutation == 'dispatch-symlink':
        original = dispatch_path.with_name('original-dispatch.json')
        dispatch_path.rename(original)
        dispatch_path.symlink_to(original)
    elif mutation == 'dispatch-mode':dispatch_path.chmod(0o700)
    result = subprocess.run([str(binary)],cwd=workspace.scratch.parent,
                            env={'HOME':str(owner.parent),'PATH':'/usr/bin:/bin'},capture_output=True)
    if mutation is None:
        assert result.returncode == 0
        read_fresh_capture(capture,nonce)
    else:
        assert not capture.exists()
        if mutation != 'stale-script':assert result.returncode != 0


def test_script_and_dispatch_correlated_change_is_rejected(workspace):
    def mutate(binary,capture):
        script = capture.with_suffix('.py')
        script.chmod(0o600)
        script.write_bytes(script.read_bytes()+b'\n# correlated fixture measurement change\n')
        script.chmod(0o500)
        dispatch = binary.with_name('fixture-provider-dispatch.json')
        row = json.loads(dispatch.read_text())
        row['sha256'] = hashlib.sha256(script.read_bytes()).hexdigest()
        dispatch.write_text(json.dumps(row))
    _,binary,capture,nonce,receipt,observed,expected_hash = execute(workspace,before_execution=mutate)
    # A real capture and witness exist with the right nonce/PID/UUID. The
    # pre-execution expected bytes, NOT current dispatch/witness, must reject it.
    assert read_fresh_capture(capture,nonce)['pid'] == observed['pid']
    assert capture.with_suffix('.driver.json').is_file()
    with pytest.raises(AssertionError,match='capture script content changed'):
        assert_runtime_capture(observed,receipt,capture=capture,binary=binary,
                               expected_script_sha256=expected_hash)


@pytest.mark.parametrize('target',['dispatch','script'])
def test_foreign_file_uid_reaches_exact_resource_guard(workspace,monkeypatch,target):
    _,binary,capture,_ = prepare_caller_invocation(workspace,'shell')
    path = (binary.with_name('fixture-provider-dispatch.json') if target == 'dispatch'
            else capture.with_suffix('.py'))
    inode = path.stat().st_ino
    real_fstat = os.fstat
    observed_fds, exec_attempts = [], []
    def foreign_uid(fd):
        info = real_fstat(fd)
        if info.st_ino != inode:return info
        observed_fds.append(fd)
        return SimpleNamespace(st_uid=os.getuid()+1,st_mode=info.st_mode,
                               st_nlink=info.st_nlink,st_size=info.st_size)
    def unexpected_exec(*args):
        exec_attempts.append(args)
        raise AssertionError('foreign resource reached exec')
    monkeypatch.setattr(fixture_provider_driver.os,'fstat',foreign_uid)
    monkeypatch.setattr(fixture_provider_driver.os,'execv',unexpected_exec)
    # Directory and every other file retain their actual valid UID. Failure
    # must name THIS resource, not a directory or generic early rejection.
    with pytest.raises(ValueError,match='^fixture '+target+' UID mismatch$'):
        fixture_provider_driver.run(binary)
    assert observed_fds and not exec_attempts
    assert not capture.exists() and not capture.with_suffix('.driver.json').exists()


@pytest.mark.parametrize('mutation',['mode','uid','symlink'])
def test_unsafe_capture_directory_reaches_directory_guard(workspace,monkeypatch,mutation):
    _,binary,capture,_ = prepare_caller_invocation(workspace,'shell')
    directory = capture.parent
    if mutation == 'mode':directory.chmod(0o755)
    elif mutation == 'symlink':
        actual = directory.with_name('captures-actual')
        directory.rename(actual)
        directory.symlink_to(actual,target_is_directory=True)
    else:
        real_lstat = Path.lstat
        def foreign_directory_uid(path,*args,**kwargs):
            info = real_lstat(path,*args,**kwargs)
            if path != directory:return info
            return SimpleNamespace(st_mode=info.st_mode,st_uid=os.getuid()+1)
        monkeypatch.setattr(Path,'lstat',foreign_directory_uid)
    attempts = []
    def unexpected_exec(*args):
        attempts.append(args)
        raise AssertionError('unsafe directory reached exec')
    monkeypatch.setattr(fixture_provider_driver.os,'execv',unexpected_exec)
    # Every file is valid. This reason must come from the directory guard,
    # independently of the dispatch/script file UID and mode controls above.
    with pytest.raises(ValueError,match='^unsafe fixture directory$'):
        fixture_provider_driver.run(binary)
    assert not attempts and not capture.exists()
    assert not capture.with_suffix('.driver.json').exists()
