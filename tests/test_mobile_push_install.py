"""Optional installer rehearsals: no production install, pip or service changes."""
import importlib.util
import json
from pathlib import Path

import pytest

from dashboard import mobile_push as push, mobile_settings as settings

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('mobile_push_install',ROOT/'scripts/lib/mobile_push_install.py')
installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)


def test_optional_dry_run_has_no_side_effects(tmp_path):
    target=tmp_path/'install'
    installer.install(target,'/python',ROOT/'scripts/mobile-push-requirements.txt',dry_run=True,run=lambda *a,**k:pytest.fail('executed'))
    assert not target.exists()


def test_prepares_checks_and_selects_one_immutable_environment(tmp_path,monkeypatch):
    monkeypatch.setattr(installer.shutil,'which',lambda _: '/fake/uv')
    calls=[]
    def run(argv,**kwargs):
        calls.append(argv)
        if argv[1]=='venv':
            candidate=Path(argv[-1]);(candidate/'bin').mkdir(parents=True)
            python=candidate/'bin/python';python.write_text('# fixture');python.chmod(0o755)
    installer.install(tmp_path,'/python',ROOT/'scripts/mobile-push-requirements.txt',run=run)
    python=push.runtime_python(tmp_path)
    assert python and 'deps/mobile-push' in python and len(calls)==3
    assert (tmp_path/'mobile-push-runtime.json').stat().st_mode&0o777==0o600
    installer.install(tmp_path,'/python',ROOT/'scripts/mobile-push-requirements.txt',run=run)
    assert len(calls)==4 and calls[-1][0]==python
    monkeypatch.setenv('AGENTSTACK_HOME',str(tmp_path))
    assert settings.push_python()==python
    assert settings.relay_config(dict(settings.DEFAULTS,owner_login='o@example.test',public_origin='https://a.example.ts.net',policy_reviewed_at='2026-11-13T00:00:00Z')).push_enabled


@pytest.mark.parametrize('damage',['path','mode','symlink'])
def test_runtime_pointer_cannot_select_arbitrary_python(tmp_path,damage):
    path=tmp_path/'mobile-push-runtime.json';path.write_text(json.dumps(dict(version=1,python='/usr/bin/python3',candidate='a'*20)));path.chmod(0o600)
    if damage=='mode':path.chmod(0o644)
    if damage=='symlink':
        path.rename(tmp_path/'other');path.symlink_to(tmp_path/'other')
    with pytest.raises(push.PushError):push.runtime_python(tmp_path)


def test_partial_dependency_setup_does_not_select_runtime(tmp_path,monkeypatch):
    monkeypatch.setattr(installer.shutil,'which',lambda _: '/fake/uv')
    def failed(*a,**k):raise RuntimeError('dependency failure')
    with pytest.raises(RuntimeError):installer.install(tmp_path,'/python',ROOT/'scripts/mobile-push-requirements.txt',run=failed)
    assert push.runtime_python(tmp_path) is None


def test_core_install_keeps_push_optional():
    source=(ROOT/'scripts/install.sh').read_text()
    assert 'WITH_MOBILE_PUSH=false' in source and '--with-mobile-push)' in source
    assert 'if [[ "$WITH_MOBILE_PUSH" == true ]]' in source
    assert 'pywebpush' not in (ROOT/'packages/agentstack_mail/pyproject.toml').read_text()
