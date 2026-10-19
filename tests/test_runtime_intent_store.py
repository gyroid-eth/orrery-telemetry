"""Private fixed-owner storage: races, pending and unsafe paths stay closed."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'bin/lib'))
from local_model_auth import LocalModelError, intent_digest
from runtime_intent_store import load_launch_intent, save_launch_intent


def intent(revision=1):
    value = {'schema_version':1, 'revision':revision, 'name':'FixtureChild',
             'provider':'codex', 'model':{'kind':'catalog', 'id':'gpt-fixture'}}
    value['intent_digest'] = intent_digest(value)
    return value


@pytest.fixture
def root(tmp_path):
    tmp_path.chmod(0o700)
    return tmp_path


@pytest.mark.parametrize('mode', ['global', 'legacy'])
def test_fixed_owner_roundtrip_preserves_existing_fields_and_identical_bytes(root, mode):
    owner = root/'profile.json' if mode == 'global' else root/'child-agents/FixtureChild.json'
    value = intent()
    save_launch_intent(owner, value, mode=mode)
    row = json.loads(owner.read_text())
    row['existing_sentinel'] = 'keep'
    owner.write_text(json.dumps(row))
    before = owner.read_bytes()
    assert save_launch_intent(owner, value, mode=mode) == value
    assert owner.read_bytes() == before
    assert load_launch_intent(owner, mode=mode) == value
    assert owner.stat().st_mode & 0o777 == 0o600
    assert json.loads(owner.read_text())['existing_sentinel'] == 'keep'


def test_global_store_initializes_the_existing_runtime_mutex_format(root):
    from runtime_client import check_operation_inode, operation_inode
    owner = root/'profile.json'
    save_launch_intent(owner, intent(), mode='global')
    lock = root/'runtime/child.lock'
    check_operation_inode(lock)
    inode = lock.stat().st_ino
    with operation_inode(lock):
        assert lock.stat().st_ino == inode


@pytest.mark.parametrize('role', ['credential.registration-pending.json', 'credential.enrollment-pending.json', 'runtime/mutation-pending.json'])
def test_different_revision_does_not_replace_any_pending_slot(root, role):
    owner = root/'profile.json'
    save_launch_intent(owner, intent(), mode='global')
    pending = root/role
    pending.parent.mkdir(mode=0o700, exist_ok=True)
    pending.write_text('{"request_id":"fixture-stable-id"}')
    before = owner.read_bytes(), pending.read_bytes()
    with pytest.raises(LocalModelError, match='PENDING'):
        save_launch_intent(owner, intent(2), mode='global')
    assert (owner.read_bytes(), pending.read_bytes()) == before


@pytest.mark.parametrize('phase', ['planned', 'gated', 'running', 'exec-unknown', 'abandoned'])
def test_receipt_nonterminal_prevents_revision(root, phase):
    owner = root/'profile.json'
    save_launch_intent(owner, intent(), mode='global')
    receipt = root/'runtime/launch.json'
    receipt.write_text(json.dumps({'phase':phase}))
    receipt.chmod(0o600)
    before = owner.read_bytes(), receipt.read_bytes()
    with pytest.raises(LocalModelError, match='PENDING'):
        save_launch_intent(owner, intent(2), mode='global')
    assert (owner.read_bytes(), receipt.read_bytes()) == before


@pytest.mark.parametrize('ending', ['report-required', 'report-pending', 'cleanup-pending'])
def test_ended_receipt_with_incomplete_report_or_cleanup_prevents_revision(root, ending):
    owner = root/'profile.json'
    save_launch_intent(owner, intent(), mode='global')
    receipt = root/'runtime/launch.json'
    receipt.write_text(json.dumps({'phase':'ended', 'termination':{'phase':ending}}))
    receipt.chmod(0o600)
    before = owner.read_bytes(), receipt.read_bytes()
    with pytest.raises(LocalModelError, match='PENDING'):
        save_launch_intent(owner, intent(2), mode='global')
    assert (owner.read_bytes(), receipt.read_bytes()) == before


@pytest.mark.parametrize('phase', ['ended', 'cancelled'])
def test_new_revision_after_known_terminal_preserves_receipt_and_other_fields(root, phase):
    owner = root/'profile.json'
    save_launch_intent(owner, intent(), mode='global')
    row = json.loads(owner.read_text())
    row['execution'] = {'argv':['/fixture/command']}
    owner.write_text(json.dumps(row))
    receipt = root/'runtime/launch.json'
    receipt.write_text(json.dumps({'phase':phase}))
    receipt.chmod(0o600)
    before = receipt.read_bytes()
    save_launch_intent(owner, intent(2), mode='global')
    assert receipt.read_bytes() == before
    assert json.loads(owner.read_text())['execution'] == row['execution']


@pytest.mark.parametrize('mutation', ['symlink', 'hardlink', 'mode', 'parent', 'digest', 'name'])
def test_unsafe_owner_or_modified_intent_is_never_written(root, mutation):
    owner = root/'client/profile.json'
    owner.parent.mkdir(mode=0o700)
    original = intent()
    save_launch_intent(owner, original, mode='global')
    candidate = intent(2)
    if mutation == 'symlink':
        target = root/'sentinel'
        owner.rename(target)
        owner.symlink_to(target)
    elif mutation == 'hardlink':
        os.link(owner, root/'sentinel')
    elif mutation == 'mode':
        owner.chmod(0o644)
    elif mutation == 'parent':
        owner.parent.chmod(0o755)
    elif mutation == 'digest':
        candidate['provider'] = 'claude'
    else:
        owner = root/'unexpected.json'
    before = {path: path.read_bytes() for path in root.rglob('*') if path.is_file()}
    with pytest.raises((LocalModelError, ValueError, OSError)):
        save_launch_intent(owner, candidate, mode='global')
    assert {path:path.read_bytes() for path in before} == before
