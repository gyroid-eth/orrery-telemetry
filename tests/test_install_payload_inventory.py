"""The inventory is the real payload call graph, observed without writes."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

ROOT=Path(__file__).resolve().parents[1]
INSTALLER=ROOT/'scripts/install.sh'


def fixture_environment(tmp_path):
    home=tmp_path/'home';home.mkdir();project=tmp_path/'project';project.mkdir();stage=tmp_path/'stage';stage.mkdir()
    env={k:v for k,v in os.environ.items() if not k.startswith('AGENTSTACK_') and k not in {'HOME','CODEX_HOME','BASH_ENV','TMUX','TMUX_PANE'}}
    env.update(HOME=str(home),AGENTSTACK_PYTHON=sys.executable,AGENTSTACK_LABEL_PREFIX='org.agentstack.test.inventory',AGENTSTACK_PROJECT_KEY=str(project),AGENTSTACK_TERMINAL='none',PYTHONDONTWRITEBYTECODE='1')
    return env,stage/'payload with space'


def invoke(env,destination,*options):
    return subprocess.run(['/bin/bash',str(INSTALLER),'--place-only','--install-dir',str(destination),*options],env=env,cwd=ROOT,capture_output=True,text=True,timeout=45)


def snapshot(root):
    return {str(p.relative_to(root)):('link',os.readlink(p),stat.S_IMODE(p.lstat().st_mode)) if p.is_symlink() else ('file',hashlib.sha256(p.read_bytes()).hexdigest(),stat.S_IMODE(p.stat().st_mode)) if p.is_file() else ('directory',stat.S_IMODE(p.stat().st_mode)) for p in root.rglob('*')}


@pytest.mark.parametrize('tier,name',[([], 'default'),(['--scoped'],'scoped'),(['--dashboard-only'],'dashboard-only')])
def test_inventory_is_readonly_and_matches_real_placement(tmp_path,tier,name):
    env,destination=fixture_environment(tmp_path)
    # Shell writes are forbidden before execution; Python inventory adds its
    # own audit guard. The observer files are prepared before the comparison.
    guard=tmp_path/'read-only.sh'
    guard.write_text("set -T\ntrap 'case \"$BASH_COMMAND\" in cp\\ *|mkdir\\ *|rm\\ *|mv\\ *|chmod\\ *|ln\\ *|touch\\ *) printf readonly-write-attempt >&2; exit 97;; esac' DEBUG\n")
    guarded={**env,'BASH_ENV':str(guard)};before=snapshot(tmp_path)
    printed=invoke(guarded,destination,'--print-inventory',*tier)
    assert printed.returncode==0,printed.stderr
    assert snapshot(tmp_path)==before
    assert not destination.exists()
    inventory=json.loads(printed.stdout)
    assert inventory['schema']=='place-only-inventory-v1'
    assert inventory['tier']==name
    assert inventory['install_dir']==str(destination.resolve())
    assert [e['path'] for e in inventory['entries']]==sorted(e['path'] for e in inventory['entries'])
    repeated=invoke(guarded,destination,'--print-inventory',*tier)
    assert repeated.returncode==0,repeated.stderr
    assert repeated.stdout==printed.stdout
    placed=invoke(env,destination,*tier)
    assert placed.returncode==0,placed.stderr
    expected={e['path']:e for e in inventory['entries']}
    manifest=json.loads((destination/'install-state.json').read_text())
    owned={p['path'] if isinstance(p,dict) else p for p in manifest['owned_files']}
    owned={str(Path(p).relative_to(destination)) if Path(p).is_absolute() else p for p in owned}
    owned={p for p in owned if not p.startswith('backups/instruction-migration/locks/')}
    actual={str(p.relative_to(destination)) for p in destination.rglob('*') if p.is_file() or p.is_symlink()}
    # Placement also creates operational locks while discovering instruction
    # state. Payload inventory follows owned_files and excludes those locks.
    assert set(expected)==owned-{'env.sh','install-state.json'}
    assert set(expected)==(actual&owned)-{'env.sh','install-state.json'}
    for relative,entry in expected.items():
        path=destination/relative
        assert entry['mode']==format(stat.S_IMODE(path.lstat().st_mode),'04o'),relative
        if entry['type']=='link':assert path.is_symlink() and os.readlink(path)==entry['target']
        else:assert not path.is_symlink() and hashlib.sha256(path.read_bytes()).hexdigest()==entry['sha256'],relative
    assert ('hooks/settings.template.json' in expected)==(name!='dashboard-only')
    assert 'dashboard/provider_server.py' not in expected
    assert 'bin/agentstack-reregister' in expected
    assert 'VERSION' in expected
    assert not any(e in expected for e in ('env.sh','install-state.json','runtime/operation.lock'))


def test_inventory_requires_place_only_and_disallows_dry_run(tmp_path):
    env,destination=fixture_environment(tmp_path)
    result=subprocess.run(['/bin/bash',str(INSTALLER),'--print-inventory'],env=env,cwd=ROOT,capture_output=True,text=True)
    assert result.returncode==2
    assert 'PLACE_ONLY_INVENTORY_REQUIRES_PLACE_ONLY' in result.stderr
    result=invoke(env,destination,'--print-inventory','--dry-run')
    assert result.returncode==2 and not destination.exists()


def load_backend():
    spec=importlib.util.spec_from_file_location('payload_backend',ROOT/'scripts/lib/installer_payload.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def test_inventory_backend_audit_refuses_write_even_when_called_directly(tmp_path):
    target=tmp_path/'forbidden'
    code="import sys;sys.path.insert(0,sys.argv[1]);import installer_payload as p;sys.addaudithook(p.readonly_audit);open(sys.argv[2],'w')"
    result=subprocess.run([sys.executable,'-I','-S','-B','-c',code,str(ROOT/'scripts/lib'),str(target)],capture_output=True,text=True)
    assert result.returncode!=0
    assert 'PLACE_ONLY_INVENTORY_WRITE_ATTEMPT' in result.stderr
    assert not target.exists()


def test_backend_projects_links_and_common_template_renderer(tmp_path):
    backend=load_backend();source=tmp_path/'source';source.mkdir();(source/'plain').write_bytes(b'content');(source/'plain').chmod(0o644);(source/'link').symlink_to('plain')
    destination=tmp_path/'payload'
    args=['cp','-R',str(source)+'/.',str(destination)+'/']
    raw=b'\0'.join([str(len(args)).encode(),*(os.fsencode(p) for p in args)])+b'\0'
    output=backend.inventory(str(destination),'dashboard-only',0o077,str(destination/'hooks'),io.BytesIO(raw))
    entries={e['path']:e for e in output['entries']}
    assert entries['link']['type']=='link' and entries['link']['target']=='plain'
    assert entries['plain']['sha256']==hashlib.sha256(b'content').hexdigest()
    assert backend.template_bytes(b'x\r\n__AGENTSTACK_HOOKS_DIR__\r\n','/stage/hooks')==b'x\n/stage/hooks\n'


def test_readonly_inventory_cannot_return_partial_success_on_source_error(tmp_path):
    env,destination=fixture_environment(tmp_path)
    backend=load_backend();args=['cp',str(tmp_path/'missing'),str(destination/'target')]
    raw=b'\0'.join([str(len(args)).encode(),*(os.fsencode(p) for p in args)])+b'\0'
    with pytest.raises(OSError):backend.inventory(str(destination),'dashboard-only',0o077,str(destination/'hooks'),io.BytesIO(raw))
    assert not destination.exists()
