"""Generated skill-link drift: fake skill resources and OS, no native CLI."""
import os
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest

from test_runtime_legacy_auth_binding import bound, context_fixture, observation, read
from test_runtime_legacy_configure import prepared as base_configure_fixture
import local_model_auth as auth


@pytest.fixture
def source_count(request):
    return getattr(request, 'param', 1)


@pytest.fixture
def configure_fixture(base_configure_fixture, source_count):
    client, ws, args, configure, calls, api = base_configure_fixture
    source = ws.tmp / 'selected-fixture-skill'
    source.mkdir(mode=0o700)
    (source / 'SKILL.md').write_text('---\nname: selected-fixture-skill\n---\nFixture instructions.\n')
    args['skill_sources'] = [str(source)] if source_count else []
    return client, ws, args, configure, calls, api


def change_skill(bound, mutation):
    home = bound.store.owner.with_suffix('.preview-home')
    directory = home / 'skills'
    path = directory / 'selected-fixture-skill'
    if mutation == 'missing': path.unlink()
    elif mutation == 'target': path.unlink(); path.symlink_to(bound.ws.tmp)
    elif mutation == 'file': path.unlink(); path.write_text('not a link')
    elif mutation == 'extra': (directory / 'extra').symlink_to(bound.ws.tmp)
    elif mutation == 'hardlink': os.link(path, home / 'skill-link-alias', follow_symlinks=False)
    elif mutation == 'directory-mode': directory.chmod(0o755)
    elif mutation == 'directory-symlink':
        moved = home / 'moved-skills'
        directory.rename(moved); directory.symlink_to(moved)
    else: raise AssertionError(mutation)


@pytest.mark.parametrize('moment', ['before', 'during-observation'])
@pytest.mark.parametrize('mutation', ['missing', 'target', 'file', 'extra', 'hardlink', 'directory-mode', 'directory-symlink'])
def test_generated_skill_link_drift_never_releases_auth(bound, monkeypatch, moment, mutation):
    calls = observation(bound, monkeypatch)
    assert read(bound)['argv'] == [str(bound.key)]
    previous = len(calls)
    changed = []
    if moment == 'before': change_skill(bound, mutation)
    else:
        observed_run = auth.subprocess.run
        def during(argv, **options):
            if not changed:
                changed.append(True)
                change_skill(bound, mutation)
            return observed_run(argv, **options)
        monkeypatch.setattr(auth.subprocess, 'run', during)
    with pytest.raises(auth.LocalModelError, match='^LOCAL_MODEL_AUTH_FAILED$'): read(bound)
    if moment == 'before': assert len(calls) == previous
    else: assert changed == [True] and len(calls) > previous
    assert not bound.ws.tmp.joinpath('key-executed').exists()


@pytest.mark.parametrize('failure', [PermissionError, OSError])
def test_skill_enumeration_failure_is_not_an_empty_success(bound, monkeypatch, failure):
    calls = observation(bound, monkeypatch)
    assert read(bound)['argv'] == [str(bound.key)]
    directory = bound.store.owner.with_suffix('.preview-home') / 'skills'
    original = os.listdir
    def unreadable(path):
        # The canonical directory is opened without following symlinks and
        # enumerated through its fd; unrelated source checks stay real.
        if isinstance(path, int):
            info = os.fstat(path)
            if (info.st_dev, info.st_ino) == (directory.stat().st_dev, directory.stat().st_ino):
                raise failure('fixture directory read failure')
        return original(path)
    monkeypatch.setattr(os, 'listdir', unreadable)
    previous = len(calls)
    with pytest.raises(auth.LocalModelError, match='^LOCAL_MODEL_AUTH_FAILED$'): read(bound)
    assert len(calls) == previous


@pytest.mark.parametrize('mutation', ['missing', 'target', 'file', 'extra', 'hardlink', 'directory-mode', 'directory-symlink'])
def test_registration_rechecks_generated_skill_link(bound, mutation):
    from test_runtime_configure_ownership import snapshot
    script = Path(bound.intent['legacy_context']['install_root']) / 'bin/lib/runtime_legacy_mail.py'
    projection = runpy.run_path(str(script))['_registration_projection']
    assert projection(bound.intent, bound.ws.caller) == bound.intent['legacy_context']
    change_skill(bound, mutation)
    before = snapshot(bound.ws.tmp)
    with pytest.raises(auth.LocalModelError): projection(bound.intent, bound.ws.caller)
    assert snapshot(bound.ws.tmp) == before


@pytest.mark.parametrize('kind', ['directory', 'link'])
def test_generated_skill_uid_must_match_even_when_target_is_safe(bound, monkeypatch, kind):
    calls = observation(bound, monkeypatch)
    assert read(bound)['argv'] == [str(bound.key)]
    path = bound.store.owner.with_suffix('.preview-home') / 'skills'
    if kind == 'link': path = path / 'selected-fixture-skill'
    pinned = path.lstat()
    def changed(info):
        if (info.st_dev, info.st_ino) != (pinned.st_dev, pinned.st_ino): return info
        values = {key: getattr(info, key) for key in dir(info) if key.startswith('st_')}
        return SimpleNamespace(**{**values, 'st_uid': os.getuid() + 1})
    if kind == 'link':
        original = os.stat
        monkeypatch.setattr(os, 'stat', lambda *a, **k: changed(original(*a, **k)))
    else:
        original = os.fstat
        monkeypatch.setattr(os, 'fstat', lambda *a, **k: changed(original(*a, **k)))
    previous = len(calls)
    with pytest.raises(auth.LocalModelError, match='^LOCAL_MODEL_AUTH_FAILED$'): read(bound)
    assert len(calls) == previous


@pytest.mark.parametrize('source_count', [0], indirect=True)
@pytest.mark.parametrize('artifact', ['link', 'file', 'directory-symlink'])
def test_empty_selection_does_not_allow_unselected_generated_entries(bound, monkeypatch, artifact):
    calls = observation(bound, monkeypatch)
    assert read(bound)['argv'] == [str(bound.key)]
    directory = bound.store.owner.with_suffix('.preview-home') / 'skills'
    if artifact == 'directory-symlink': directory.symlink_to(bound.ws.tmp)
    else:
        directory.mkdir(mode=0o700)
        if artifact == 'file': (directory / 'extra').write_text('unselected')
        else: (directory / 'extra').symlink_to(bound.ws.tmp)
    previous = len(calls)
    with pytest.raises(auth.LocalModelError, match='^LOCAL_MODEL_AUTH_FAILED$'): read(bound)
    assert len(calls) == previous
