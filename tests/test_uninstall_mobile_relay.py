"""uninstall.sh and the phone reader's launchd job.

There is one `org.agentstack.mobile-relay` plist per HOME, written by
`agentstack-mobile`, not by the installer. uninstall.sh stops and removes it
only when it belongs to the install being removed; uninstalling an install
somewhere else (`--install-dir`) leaves it alone. A test that ran the
uninstaller with the real HOME once removed a real job this way.

Every case runs with a temporary HOME and a fake `launchctl` that only logs.
"""
from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LABEL = 'org.agentstack.mobile-relay'


def _world(tmp_path: Path, install: Path, config: Path, **manifest) -> dict:
    home = tmp_path / 'home'
    plist = home / 'Library/LaunchAgents' / f'{LABEL}.plist'
    plist.parent.mkdir(parents=True)
    plist.write_bytes(plistlib.dumps({'Label': LABEL, 'ProgramArguments': [
        sys.executable, '/somewhere/mobile_relay.py', '--config', str(config)]}))
    install.mkdir(parents=True)
    state = {'install_dir': str(install), 'services': [], 'owned_files': [], 'owned_dirs': [], 'purge_paths': []}
    state.update(manifest)
    (install / 'install-state.json').write_text(json.dumps(state))
    fake = tmp_path / 'fake-bin'
    fake.mkdir()
    calls = tmp_path / 'launchctl-calls'
    (fake / 'launchctl').write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> {calls}\n')
    (fake / 'launchctl').chmod(0o755)
    env = {key: value for key, value in os.environ.items() if not key.startswith('AGENTSTACK_')}
    env.update(HOME=str(home), PATH=f'{fake}:{Path(sys.executable).parent}:/usr/bin:/bin')
    # the uninstaller must find the fake, never the real one
    assert shutil.which('launchctl', path=env['PATH']) == str(fake / 'launchctl')
    return {'home': home, 'plist': plist, 'calls': calls, 'env': env}


def _uninstall(world: dict, *args: str) -> subprocess.CompletedProcess:
    done = subprocess.run(['/bin/bash', str(ROOT / 'scripts/uninstall.sh'), *args], cwd=world['home'],
                          env=world['env'], stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr
    return done


def _calls(world: dict) -> list[str]:
    return world['calls'].read_text().splitlines() if world['calls'].exists() else []


def test_default_install_stops_and_removes_the_reader_job_as_before(tmp_path):
    home = tmp_path / 'home'
    world = _world(tmp_path, home / '.agentstack', home / '.agentstack/mobile.json')
    _uninstall(world)
    assert not world['plist'].exists()
    assert _calls(world) == [f'bootout gui/{os.getuid()}/{LABEL}']


def test_install_elsewhere_leaves_this_homes_reader_job_alone(tmp_path):
    home = tmp_path / 'home'
    world = _world(tmp_path, tmp_path / 'other-install', home / '.agentstack/mobile.json')
    before = world['plist'].read_bytes()
    _uninstall(world, '--install-dir', str(tmp_path / 'other-install'))
    assert world['plist'].read_bytes() == before
    assert _calls(world) == []


@pytest.mark.parametrize('owned', ['config', 'owned_files', 'services'])
def test_install_elsewhere_removes_a_reader_job_that_is_its_own(tmp_path, owned):
    install = tmp_path / 'other-install'
    home = tmp_path / 'home'
    plist = home / 'Library/LaunchAgents' / f'{LABEL}.plist'
    config = install / 'mobile.json' if owned == 'config' else home / '.agentstack/mobile.json'
    manifest = {'owned_files': [str(plist)]} if owned == 'owned_files' else {}
    world = _world(tmp_path, install, config, **manifest)
    if owned == 'services':
        state = json.loads((install / 'install-state.json').read_text())
        state['services'] = [{'kind': 'launchd', 'label': LABEL}]
        (install / 'install-state.json').write_text(json.dumps(state))
    _uninstall(world, '--install-dir', str(install))
    assert not world['plist'].exists()
    assert f'bootout gui/{os.getuid()}/{LABEL}' in _calls(world)


def test_dry_run_names_the_reader_job_and_changes_nothing(tmp_path):
    home = tmp_path / 'home'
    world = _world(tmp_path, home / '.agentstack', home / '.agentstack/mobile.json')
    done = _uninstall(world, '--dry-run')
    assert world['plist'].exists() and _calls(world) == []
    assert f'DRY-RUN would run: launchctl bootout gui/{os.getuid()}/{LABEL}' in done.stdout
