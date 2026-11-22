"""The actual uninstaller must reject pending purge before touching services."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

from test_instruction_blocks import ROOT, env, engine, apply, m
from _installed_client_payload import copy_client_library


def installed(env):
    e=engine(env)
    copy_client_library(e.install)
    for provider,name in (('claude','CLAUDE.md'),('codex','AGENTS.md')):
        path=e.install/provider/name;path.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(ROOT/provider/name,path)
    return e


def test_purge_pending_preflight_precedes_service_changes(env,monkeypatch):
    e=installed(env);original=e.save
    def interrupted(value,stage=None):
        original(value,stage)
        raise OSError('interrupted after receipt')
    monkeypatch.setattr(e,'save',interrupted)
    p=e.plan()
    try:e.apply(p,m.sha(m.canonical(p)),assume_yes=True)
    except OSError:pass
    calls=Path(env['HOME'])/'service-calls'
    stub=Path(env['HOME'])/'bin';stub.mkdir()
    (stub/'systemctl').write_text('#!/bin/sh\nprintf called >> '+str(calls)+'\n')
    (stub/'systemctl').chmod(0o755)
    manifest={'install_dir':str(e.install),'services':[{'kind':'systemd-user','unit':'isolated-fixture'}],
              'owned_files':[],'owned_dirs':[],'purge_paths':[],'instruction_receipts':[str(e.receipt_path)]}
    (e.install/'install-state.json').write_text(json.dumps(manifest))
    result=subprocess.run(['/bin/bash',str(ROOT/'scripts/uninstall.sh'),'--purge-data'],
        env={**env,'PATH':str(stub)+':'+str(Path(sys.executable).parent)+':/usr/bin:/bin'},capture_output=True,text=True)
    assert result.returncode!=0 and 'INSTRUCTION_BACKUP_PURGE_BLOCKED' in result.stderr
    assert not calls.exists() and e.receipt_path.exists() and not e.target.exists()


def test_actual_uninstall_removes_only_recorded_marker_and_keeps_private_backups(env):
    e=installed(env);e.home.mkdir();e.target.write_text('personal before\n')
    apply(e)
    e.target.write_bytes(e.target.read_bytes()+b'personal after\n')
    (e.install/'install-state.json').write_text(json.dumps({'install_dir':str(e.install),
        'services':[],'owned_files':[],'owned_dirs':[],'purge_paths':[],
        'instruction_receipts':[str(e.receipt_path)]}))
    result=subprocess.run(['/bin/bash',str(ROOT/'scripts/uninstall.sh')],
        env={**env,'PATH':str(Path(sys.executable).parent)+':/usr/bin:/bin'},capture_output=True,text=True)
    assert result.returncode==0,result.stdout+result.stderr
    assert e.target.read_text()=='personal before\npersonal after\n'
    assert list(e.ledger.glob('backups/*/*.before')) and e.receipt()['stage']=='uninstalled'
