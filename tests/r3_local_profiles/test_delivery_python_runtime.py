"""Selected interpreter portability, not evidence of native CLI capability."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import sysconfig

import pytest

from . import caller_binding as binding
from .caller_payload import copy_installer_payload


def test_selected_base_starts_private_python_and_records_actual_branch(workspace):
    install = workspace.tmp/'install'
    copy_installer_payload(install)
    shared = (sys.platform=='darwin' and not sysconfig.get_config_var('PYTHONFRAMEWORK')
              and bool(sysconfig.get_config_var('Py_ENABLE_SHARED')))
    source = (Path(sysconfig.get_config_var('LIBDIR')) /
              sysconfig.get_config_var('LDLIBRARY')).resolve(strict=True) if shared else None
    source_bytes = source.read_bytes() if source else None
    python,trace = binding.make_delivery_python(workspace,install)
    root = workspace.tmp/'delivery-python'
    assert python==root/'bin/python' and python.resolve()==python and not python.is_symlink()
    assert python.read_bytes()==Path(sys.executable).resolve(strict=True).read_bytes()
    configs = list((root/'lib').glob('python*/site-packages/delivery-fixture.json'))
    assert len(configs)==1
    manifest = json.loads(configs[0].read_text())['interpreter_resources']
    if shared:
        target=root/'lib'/sysconfig.get_config_var('LDLIBRARY')
        assert manifest==[{'path':str(target),'source':str(source),
                          'sha256':hashlib.sha256(source_bytes).hexdigest()}]
        assert target.resolve()==target and not target.is_symlink()
        assert target.read_bytes()==source_bytes and target.stat().st_mode & 0o777==0o500
        assert source.read_bytes()==source_bytes
        branch='darwin-shared-copy'
    else:
        assert manifest==[] and not list((root/'lib').glob('libpython*.dylib'))
        branch='framework-no-copy' if sysconfig.get_config_var('PYTHONFRAMEWORK') else 'no-darwin-shared-library'
    result=subprocess.run([str(python),'-c',
        'import json,ssl,sys; print(json.dumps({"executable":sys.executable,"prefix":sys.prefix}))'],
        env={'HOME':str(workspace.tmp/'home'),'PATH':'/usr/bin:/bin'},
        capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)=={'executable':str(python),'prefix':str(root)}
    binding.assert_single_replacement(binding.read_binding_trace(trace))
    # A dedicated serial CI step runs this test with -s; do not claim CI ran
    # from a local framework/uv result or infer its build from a version number.
    print('R3_DELIVERY_PYTHON_BRANCH='+branch+' version='+sys.version.split()[0],flush=True)


@pytest.mark.parametrize('fault',['missing-name','traversal-name','relative-dir','missing-source'])
def test_declared_shared_library_metadata_fails_explicitly(workspace,monkeypatch,fault):
    values={'PYTHONFRAMEWORK':'','Py_ENABLE_SHARED':1,'LIBDIR':str(workspace.tmp/'missing'),
            'LDLIBRARY':'libpython-fixture.dylib'}
    if fault=='missing-name':values['LDLIBRARY']=None
    elif fault=='traversal-name':values['LDLIBRARY']='../libpython-fixture.dylib'
    elif fault=='relative-dir':values['LIBDIR']='relative'
    monkeypatch.setattr(binding.sys,'platform','darwin')
    monkeypatch.setattr(binding.sysconfig,'get_config_var',values.get)
    with pytest.raises(RuntimeError,match='R3_DELIVERY_PYTHON_UNSUPPORTED: library-'):
        binding._delivery_library_source()


@pytest.mark.parametrize('fault',['missing','symlink','mode','directory-symlink','bytes','both-bytes'])
def test_dependency_validation_pins_original_bytes_and_regular_resource(workspace,monkeypatch,fault):
    # Non-executable data exercises the resource validator only, NEVER native
    # permissions, Python startup or a simulated successful model invocation.
    source=workspace.tmp/'base/libpython-fixture.dylib'
    source.parent.mkdir(mode=0o700)
    source.write_bytes(b'original dependency, never executed\n')
    root=workspace.tmp/'dependency-check'
    root.mkdir(mode=0o700)
    monkeypatch.setattr(binding,'_delivery_library_source',lambda:source)
    manifest=binding._copy_delivery_library(root)
    binding._verify_delivery_library(root,manifest)
    target=root/'lib'/source.name
    if fault=='missing':target.unlink()
    elif fault=='symlink':target.unlink();target.symlink_to(source)
    elif fault=='mode':target.chmod(0o700)
    elif fault=='directory-symlink':
        (root/'lib').rename(root/'elsewhere')
        (root/'lib').symlink_to(root/'elsewhere',target_is_directory=True)
    else:
        target.chmod(0o600);target.write_bytes(b'changed dependency\n');target.chmod(0o500)
        if fault=='both-bytes':source.write_bytes(target.read_bytes())
    with pytest.raises(RuntimeError,match='R3_DELIVERY_PYTHON_LIBRARY_(INVALID|CHANGED)'):
        binding._verify_delivery_library(root,manifest)


@pytest.mark.parametrize('fault',['exec-error','timeout','nonzero','invalid-json'])
def test_private_probe_has_named_failure_and_never_selects_another_python(workspace,monkeypatch,fault):
    python=workspace.tmp/'exact-python'
    calls=[]
    def fail(argv,**kwargs):
        calls.append(argv)
        if fault=='exec-error':raise OSError('fixture exec refused')
        if fault=='timeout':raise subprocess.TimeoutExpired(argv,20)
        if fault=='nonzero':raise subprocess.CalledProcessError(-6,argv,stderr='fixture loader failed')
        return subprocess.CompletedProcess(argv,0,stdout='not JSON',stderr='')
    monkeypatch.setattr(binding.subprocess,'run',fail)
    with pytest.raises(RuntimeError,match='R3_DELIVERY_PYTHON_UNSUPPORTED: probe-'):
        binding._private_python_sites(python)
    assert len(calls)==1 and calls[0][0]==str(python)


def test_private_bootstrap_rejects_changed_dependency_manifest(workspace):
    install=workspace.tmp/'install'
    copy_installer_payload(install)
    python,trace=binding.make_delivery_python(workspace,install)
    paths=list((workspace.tmp/'delivery-python/lib').glob('python*/site-packages/delivery-fixture.json'))
    assert len(paths)==1
    config=paths[0]
    value=json.loads(config.read_text())
    value['interpreter_resources'].append({'unexpected':'resource'})
    config.chmod(0o600);config.write_text(json.dumps(value));config.chmod(0o400)
    result=subprocess.run([str(python),'-c','raise AssertionError("must not execute")'],
        env={'HOME':str(workspace.tmp/'home'),'PATH':'/usr/bin:/bin'},
        capture_output=True,text=True,timeout=30)
    assert result.returncode==86
    assert 'R3_DELIVERY_PYTHON_LIBRARY_INVALID' in result.stderr
    assert not trace.exists(), 'bootstrap admitted changed resource manifest'


def test_library_copy_preserves_declared_loader_name_not_resolved_basename(workspace,monkeypatch):
    source=workspace.tmp/'base/libpython-real.dylib'
    source.parent.mkdir(mode=0o700);source.write_bytes(b'dependency data, never executed\n')
    declared=source.with_name('libpython3.14.dylib')
    declared.symlink_to(source)
    root=workspace.tmp/'declared-library'
    root.mkdir(mode=0o700)
    values={'PYTHONFRAMEWORK':'','Py_ENABLE_SHARED':1,
            'LIBDIR':str(source.parent),'LDLIBRARY':declared.name}
    monkeypatch.setattr(binding.sys,'platform','darwin')
    monkeypatch.setattr(binding.sysconfig,'get_config_var',values.get)
    manifest=binding._copy_delivery_library(root)
    target=root/'lib/libpython3.14.dylib'
    assert target.is_file() and not target.is_symlink() and target.read_bytes()==source.read_bytes()
    assert manifest==[{'path':str(target),'source':str(source),
                      'sha256':hashlib.sha256(source.read_bytes()).hexdigest()}]
    assert not (root/'lib/libpython-real.dylib').exists()


def test_venv_creation_failure_is_named_without_another_interpreter(workspace,monkeypatch):
    calls=[]
    def fail(self,root):
        calls.append(root)
        assert self.symlinks is False and self.with_pip is False
        raise OSError('fixture cannot create venv')
    monkeypatch.setattr(binding.venv.EnvBuilder,'create',fail)
    with pytest.raises(RuntimeError,match='R3_DELIVERY_PYTHON_UNSUPPORTED: venv-create'):
        binding.make_delivery_python(workspace,workspace.tmp/'unused-install')
    assert calls==[workspace.tmp/'delivery-python']


def test_library_copy_io_failure_is_named(workspace,monkeypatch):
    source=workspace.tmp/'libpython-fixture.dylib'
    source.write_bytes(b'not executable')
    root=workspace.tmp/'cannot-copy'
    root.mkdir(mode=0o700)
    monkeypatch.setattr(binding,'_delivery_library_source',lambda:source)
    def fail(*args,**kwargs):raise OSError('fixture cannot copy library')
    monkeypatch.setattr(binding.shutil,'copyfile',fail)
    with pytest.raises(RuntimeError,match='R3_DELIVERY_PYTHON_UNSUPPORTED: library-copy'):
        binding._copy_delivery_library(root)
