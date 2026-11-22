"""Inactive distribution and ownership, using synthetic receipts and no services."""
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('example_distribution', ROOT / 'scripts/lib/install_examples.py')
examples = importlib.util.module_from_spec(spec)
spec.loader.exec_module(examples)


@pytest.fixture
def home(tmp_path):
    install = tmp_path.resolve() / 'install'
    install.mkdir()
    return install


def commit(home, **kwargs):
    examples.write_manifest({'install_dir': str(home), 'owned_files': [], 'purge_paths': []},
                            home / 'install-state.json', repo=ROOT, python=sys.executable, **kwargs)
    return examples.read(home / 'install-state.json')


def references(home, harnesses=('dsh', 'hermes')):
    dsh = {'node': str(home / 'node'), 'runtime': str(home / 'runtime'),
           'generation_receipt': str(home / 'dsh-receipt'), 'python': sys.executable}
    hermes = {'hermes_executable': str(home / 'pm/bin/hermes'),
              'hermes_python': str(home / 'pm/bin/python'), 'python': str(home / 'pm/bin/python'),
              'hermes_source': str(home / 'source'), 'managed_root': str(home / 'managed'),
              'generation_receipt': str(home / 'hermes-receipt')}
    return {h: {'dsh': dsh, 'hermes': hermes}[h] for h in harnesses}


def project(monkeypatch, home, *, platform='darwin', harnesses=('dsh', 'hermes')):
    monkeypatch.setattr(examples, 'runtime_references', lambda *args: references(home, harnesses))
    monkeypatch.setattr(examples.sys, 'platform', platform)
    state = commit(home)
    manifests = [Path(row['path']) for row in state['example_files']
                 if '/resolved/' in row['path'] and Path(row['path']).name == 'manifest.example.json']
    return state, manifests[-1].parent


def test_static_distribution_never_enables_or_adopts_existing_files(home):
    base = home / examples.RELATIVE
    base.mkdir(parents=True)
    existing = base / 'manifest.example.json'
    existing.write_text('user-owned bytes')
    state = commit(home)
    assert existing.read_text() == 'user-owned bytes'
    assert str(existing) not in [row['path'] for row in state['example_files']]
    assert len(state['example_files']) == 5
    assert state['owned_files'] == state['purge_paths'] == []
    assert not (base / 'resolved').exists()
    before = {row['path']: Path(row['path']).read_bytes() for row in state['example_files']}
    assert commit(home)['example_files'] == state['example_files']
    assert all(Path(path).read_bytes() == data for path, data in before.items())
    examples.remove_examples(state)
    assert existing.read_text() == 'user-owned bytes'


@pytest.mark.parametrize('platform', ['darwin', 'linux'])
@pytest.mark.parametrize('harnesses', [('dsh',), ('hermes',), ('dsh', 'hermes')])
def test_snapshot_has_exact_runtime_and_peer_refs_and_one_credential_scheme(monkeypatch, home, platform, harnesses):
    state, snapshot = project(monkeypatch, home, platform=platform, harnesses=harnesses)
    manifest = examples.read(snapshot / 'manifest.example.json')
    assert manifest['enabled'] is False and set(manifest['harnesses']) == set(harnesses)
    assert len(state['example_files']) == 10
    for harness in harnesses:
        runner = manifest['harnesses'][harness]
        assert runner['python'] == references(home)[harness]['python']
        assert Path(runner['config_file']).parent == snapshot
        config = examples.read(runner['config_file'])
        for key, value in references(home)[harness].items():
            if key != 'python':
                assert config[key] == value
    dsh = examples.read(snapshot / 'dsh-backends.example.json')['backends']['deepseek']
    profile = examples.read(snapshot / 'hermes-config.example.json')['profiles']['deepseek']
    assert Path(examples.read(snapshot / 'dsh-config.example.json')['backend_file']).parent == snapshot
    if platform == 'linux':
        assert manifest['backends']['deepseek']['credential_ref'] == {
            'store': 'secret-service', 'attributes': {'service': 'orrery-dsh-deepseek', 'account': 'api-key'}}
        assert 'keychain' not in dsh and 'key_cmd' not in profile
    else:
        assert 'credential_ref' not in manifest['backends']['deepseek']
        assert dsh['keychain'] == {'service': 'orrery-dsh-deepseek', 'account': 'api-key'}
        assert profile['key_cmd'].startswith('/usr/bin/security ')
    assert snapshot.stat().st_mode & 0o777 == 0o700
    assert all((snapshot / name).stat().st_mode & 0o777 == 0o600 for name in examples.JSON_FILES)
    assert not any(key in manifest for key in ('effort', 'limits', 'context_length'))


def test_reinstall_preserves_edits_and_unrecorded_snapshot(monkeypatch, home):
    state, snapshot = project(monkeypatch, home)
    edited = snapshot / 'dsh-config.example.json'
    edited.write_text('user edits')
    current_bytes = {p: p.read_bytes() for p in snapshot.iterdir()}
    state2 = commit(home)
    assert str(edited) not in [row['path'] for row in state2['example_files']]
    assert all(p.read_bytes() == data for p, data in current_bytes.items())
    # Erasing ownership must not cause a same-content pre-existing file to be adopted.
    (home / 'install-state.json').write_text(json.dumps({'install_dir': str(home)}))
    assert commit(home)['example_files'] == []


def test_generation_change_adds_snapshot_without_changing_source_or_old_copy(monkeypatch, home):
    state, old = project(monkeypatch, home)
    old_bytes = {p: p.read_bytes() for p in old.iterdir()}
    changed = references(home)
    changed['dsh']['runtime'] = str(home / 'new-runtime')
    monkeypatch.setattr(examples, 'runtime_references', lambda *args: changed)
    state2 = commit(home)
    snapshots = {Path(row['path']).parent for row in state2['example_files'] if '/resolved/' in row['path']}
    assert len(snapshots) == 2
    assert all(p.read_bytes() == data for p, data in old_bytes.items())
    assert examples.read(home / examples.RELATIVE / 'manifest.example.json')['enabled'] is False


def test_receipt_content_change_alone_has_a_new_snapshot_identity(monkeypatch, home):
    refs = references(home)
    refs['dsh']['_receipt_sha256'] = 'old-content'
    monkeypatch.setattr(examples, 'runtime_references', lambda *args: refs)
    first = commit(home)
    refs['dsh']['_receipt_sha256'] = 'new-content'
    second = commit(home)
    assert len(second['example_files']) == len(first['example_files']) + 4
    for row in second['example_files']:
        if row['path'].endswith('dsh-config.example.json') and '/resolved/' in row['path']:
            assert '_receipt_sha256' not in examples.read(row['path'])


@pytest.mark.parametrize('collision', ['parent-link', 'file-link', 'directory', 'file-parent'])
def test_collisions_never_write_through_or_adopt(monkeypatch, home, tmp_path, collision):
    outside = tmp_path / 'outside'
    outside.mkdir()
    sentinel = outside / 'manifest.example.json'
    sentinel.write_text('KEEP')
    base = home / examples.RELATIVE
    if collision == 'parent-link':
        (home / 'examples').symlink_to(outside, target_is_directory=True)
    elif collision == 'file-parent':
        (home / 'examples').write_text('KEEP')
    else:
        base.mkdir(parents=True)
        if collision == 'file-link':
            (base / 'manifest.example.json').symlink_to(sentinel)
        else:
            (base / 'manifest.example.json').mkdir()
    state = commit(home)
    assert sentinel.read_text() == 'KEEP'
    examples.remove_examples(state)
    assert sentinel.read_text() == 'KEEP'


def test_uninstall_keeps_edits_links_extra_files_and_private_copy(monkeypatch, home, tmp_path):
    state, snapshot = project(monkeypatch, home)
    private = tmp_path / 'private-copy'
    shutil.copytree(snapshot, private)
    edited = snapshot / 'dsh-config.example.json'
    edited.write_text('edited')
    linked = snapshot / 'dsh-backends.example.json'
    linked.unlink()
    linked.symlink_to(private / 'dsh-backends.example.json')
    extra = snapshot / 'user.txt'
    extra.write_text('extra')
    examples.remove_examples(state, dry_run=True)
    assert (snapshot / 'manifest.example.json').exists()
    examples.remove_examples(state)
    assert edited.read_text() == 'edited' and linked.is_symlink() and extra.read_text() == 'extra'
    assert (private / 'manifest.example.json').is_file()
    assert not (snapshot / 'manifest.example.json').exists()


@pytest.mark.parametrize('phase', ['static-publication', 'snapshot-publication', 'manifest-write'])
def test_precommit_failure_cleans_only_new_hash_owned_files_then_retry(monkeypatch, home, phase):
    base = home / examples.RELATIVE
    base.mkdir(parents=True)
    previous = base / 'README.md'
    previous.write_text('existing user README')
    old_state = {'install_dir': str(home), 'notes': ['old manifest']}
    (home / 'install-state.json').write_bytes(examples.encoded(old_state))
    monkeypatch.setattr(examples, 'runtime_references', lambda *args: references(home))
    original_prepare = examples.Examples.prepare
    original_publish = examples.Examples.publish_file
    original_replace = examples.os.replace
    if phase == 'static-publication':
        def fail(self, path, content):
            original_publish(self, path, content)
            if path.name == 'README.en.md':
                raise OSError('synthetic publication failure')
        monkeypatch.setattr(examples.Examples, 'publish_file', fail)
    elif phase == 'snapshot-publication':
        def fail(self):
            original_prepare(self)
            raise OSError('synthetic failure after snapshot publication')
        monkeypatch.setattr(examples.Examples, 'prepare', fail)
    else:
        monkeypatch.setattr(examples.os, 'replace', lambda *args: (_ for _ in ()).throw(OSError('synthetic disk full')))
    with pytest.raises(OSError):
        commit(home)
    assert examples.read(home / 'install-state.json') == old_state
    assert previous.read_text() == 'existing user README'
    assert list(base.iterdir()) == [previous]
    monkeypatch.setattr(examples.Examples, 'prepare', original_prepare)
    monkeypatch.setattr(examples.Examples, 'publish_file', original_publish)
    monkeypatch.setattr(examples.os, 'replace', original_replace)
    state = commit(home)
    assert len(state['example_files']) == 9
    examples.remove_examples(state)
    assert list(base.iterdir()) == [previous]


def test_rename_reported_failure_after_commit_retains_owned_examples(monkeypatch, home):
    original = examples.os.replace
    def replace_then_fail(source, target):
        original(source, target)
        raise OSError('synthetic failure after rename')
    monkeypatch.setattr(examples.os, 'replace', replace_then_fail)
    with pytest.raises(OSError):
        commit(home)
    state = examples.read(home / 'install-state.json')
    assert len(state['example_files']) == 6
    assert all(Path(row['path']).is_file() for row in state['example_files'])


def test_unreadable_commit_state_preserves_examples_and_reports_unknown(monkeypatch, home):
    original_replace, original_read = examples.os.replace, Path.read_bytes
    destination = home / 'install-state.json'
    def replace_then_fail(source, target):
        original_replace(source, target)
        raise OSError('synthetic lost rename acknowledgement')
    def unreadable(path):
        if path == destination:
            raise PermissionError('synthetic unreadable commit state')
        return original_read(path)
    monkeypatch.setattr(examples.os, 'replace', replace_then_fail)
    monkeypatch.setattr(Path, 'read_bytes', unreadable)
    with pytest.raises(ValueError, match='commit_unknown'):
        commit(home)
    assert all((home / examples.RELATIVE / name).is_file() for name in examples.FILES)


def test_failed_publication_keeps_a_changed_file_unowned_on_retry(monkeypatch, home):
    original = examples.Examples.prepare
    changed = home / examples.RELATIVE / 'manifest.example.json'
    def fail(self):
        original(self)
        changed.write_text('user edit after publication')
        raise OSError('synthetic failure')
    monkeypatch.setattr(examples.Examples, 'prepare', fail)
    with pytest.raises(ValueError, match='cleanup_incomplete'):
        commit(home)
    assert changed.read_text() == 'user edit after publication'
    assert not (home / 'install-state.json').exists()
    monkeypatch.setattr(examples.Examples, 'prepare', original)
    state = commit(home)
    assert str(changed) not in [row['path'] for row in state['example_files']]
    examples.remove_examples(state)
    assert changed.read_text() == 'user edit after publication'


def synthetic_dsh(home):
    generation = home / 'harnesses/dsh/synthetic-generation'
    runtime = generation / 'runtime'
    node = generation / 'node'
    node.parent.mkdir(parents=True)
    node.write_text('synthetic executable; never executed')
    node.chmod(0o700)
    proof = generation / 'evidence/node-runtime.json'
    proof.parent.mkdir()
    proof.write_bytes(examples.encoded({'sha256': examples.digest(node.read_bytes())}))
    pins = examples.read(ROOT / 'integrations/dsh/runtime-pins.json')
    for pin in pins:
        package = runtime / 'node_modules' / pin['name'] / 'package.json'
        package.parent.mkdir(parents=True, exist_ok=True)
        package.write_bytes(examples.encoded({'version': pin['version']}))
    source_file = runtime / 'node_modules/@deepseek-harness-tui/dsh-tui/synthetic-proof.js'
    source_file.write_text('synthetic proof; never imported')
    upstream = generation / 'evidence/upstream.json'
    upstream.write_bytes(examples.encoded({'variant': 'plain-datadir', 'source_patch': 'tui-datadir-v1',
        'recipe_sha256': examples.digest((ROOT / 'integrations/dsh/tui-datadir.patch.json').read_bytes()),
        'files': [{'file': source_file.name, 'sha256': examples.digest(source_file.read_bytes())}]}))
    owner = generation / examples.OWNER
    details = {'node': str(node), 'runtime': str(runtime), 'runtime_receipt': str(upstream),
               'generation_receipt': str(owner), 'variant': 'plain-datadir', 'thin_config_contract': 1}
    owner.write_bytes(examples.encoded({'complete': True, 'harness': 'dsh', 'variant': 'plain-datadir',
                                      'thin_config_contract': 1, 'details': details}))
    current = generation.parent / 'current.json'
    current.write_bytes(examples.encoded({'generation': generation.name, **details}))
    return node, current, details


def test_real_receipt_validator_is_used_and_current_tampering_is_rejected(home):
    node, current, details = synthetic_dsh(home)
    runtime = Path(details['runtime'])
    refs = examples.runtime_references(ROOT, home, sys.executable)
    assert refs['dsh']['runtime'] == str(runtime) and refs['dsh']['python'] == sys.executable
    node.write_text('changed')
    with pytest.raises(ValueError, match='Pinned Node executable changed'):
        examples.runtime_references(ROOT, home, sys.executable)
    current.write_bytes(examples.encoded({'generation': current.parent.joinpath('synthetic-generation').name,
                                        **details, 'node': '/wrong'}))
    with pytest.raises(ValueError, match='reference_changed'):
        examples.runtime_references(ROOT, home, sys.executable)


def test_hermes_python_comes_from_committed_pm_facts_after_validation(monkeypatch, home):
    root = home / 'harnesses/hermes/synthetic-v2'
    source = root / 'source'
    source.mkdir(parents=True)
    facts = root / 'installs' / examples.digest(str(source.resolve()).encode())[:16] / 'facts.json'
    environment = facts.parent / 'environments/committed'
    environment.mkdir(parents=True)
    facts.write_bytes(examples.encoded({'packages': {'venv': {'environment': str(environment)}}}))
    owner = root / examples.OWNER
    details = {'managed_root': str(root), 'hermes_source': str(source),
               'hermes_executable': str(environment / 'bin/hermes'), 'generation_receipt': str(owner)}
    owner.write_bytes(examples.encoded({'harness': 'hermes', 'complete': True, 'variant': 'plain',
                                      'thin_config_contract': 1, 'details': details}))
    (root.parent / 'current.json').write_bytes(examples.encoded({'generation': root.name, **details}))
    calls = []
    def validate(repo, harness, receipt):
        calls.append(harness)
        assert receipt['details'] == details
    monkeypatch.setattr(examples, 'runtime_validator', lambda repo: SimpleNamespace(validate=validate))
    refs = examples.runtime_references(ROOT, home, sys.executable)
    assert calls == ['hermes']
    assert refs['hermes']['python'] == refs['hermes']['hermes_python'] == str(environment / 'bin/python')
    assert refs['hermes']['_receipt_sha256'] == examples.digest(owner.read_bytes())
    def reject(*args):
        raise ValueError('synthetic invalid committed environment')
    monkeypatch.setattr(examples, 'runtime_validator', lambda repo: SimpleNamespace(validate=reject))
    with pytest.raises(ValueError, match='invalid committed'):
        examples.runtime_references(ROOT, home, sys.executable)


@pytest.mark.parametrize('with_runtime', [False, True])
def test_installed_copy_e1_and_actual_uninstall_keep_private_bytes(home, tmp_path, with_runtime):
    # Use only the private copy in cwd=/ and isolated Python. Static A needs
    # no runtime validator; B validates the synthetic owned dsh receipt.
    source = tmp_path / 'source-copy'
    shutil.copytree(ROOT / examples.RELATIVE, source / examples.RELATIVE)
    if with_runtime:
        synthetic_dsh(home)
        for relative in ('scripts/lib/managed_harness_install.py', 'integrations/dsh/runtime-pins.json',
                         'integrations/dsh/tui-datadir.patch.json'):
            target = source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, target)
    library = home / 'bin/lib/install_examples.py'
    library.parent.mkdir(parents=True)
    shutil.copy2(ROOT / 'scripts/lib/install_examples.py', library)
    script = ('import importlib.util,sys; '
              's=importlib.util.spec_from_file_location("examples",sys.argv[1]); '
              'm=importlib.util.module_from_spec(s);s.loader.exec_module(m); '
              'm.write_manifest({"install_dir":sys.argv[2],"owned_files":[],"services":[],"purge_paths":[]},'
              'sys.argv[3],repo=sys.argv[4],python=sys.executable)')
    env = {'HOME': str(tmp_path / 'empty-home'), 'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1'}
    done = subprocess.run([sys.executable, '-I', '-B', '-c', script, str(library), str(home),
                           str(home / 'install-state.json'), str(source)], cwd='/', env=env,
                          capture_output=True, text=True, timeout=20)
    assert done.returncode == 0, done.stderr
    state = examples.read(home / 'install-state.json')
    assert len(state['example_files']) == (10 if with_runtime else 6)
    if with_runtime:
        row = next(row for row in state['example_files']
                   if '/resolved/' in row['path'] and row['path'].endswith('manifest.example.json'))
        inactive = examples.read(row['path'])
        assert inactive['enabled'] is False and set(inactive['harnesses']) == {'dsh'}
        copied_config = examples.read(inactive['harnesses']['dsh']['config_file'])
        assert copied_config['node'] == str(home / 'harnesses/dsh/synthetic-generation/node')
    edited = home / examples.RELATIVE / 'manifest.example.json'
    edited.write_text('PRIVATE EDIT')
    uninstall = home / 'bin/agentstack-uninstall'
    shutil.copy2(ROOT / 'scripts/uninstall.sh', uninstall)
    removed = subprocess.run(['/bin/bash', str(uninstall), '--install-dir', str(home), '--purge-data'],
                             cwd='/', env=env, text=True, capture_output=True, timeout=20)
    assert removed.returncode == 0, removed.stderr
    assert edited.read_text() == 'PRIVATE EDIT'
    assert not (home / examples.RELATIVE / 'README.md').exists()


@pytest.mark.parametrize('dry_run', [True, False])
def test_actual_write_manifest_wires_distribution_and_dry_run(home, dry_run):
    text = (ROOT / 'scripts/install.sh').read_text()
    # A first-column } also occurs inside the Python heredoc. Use the next
    # shell function boundary rather than truncating at the manifest dict.
    function = 'write_manifest() {\n' + text.split('write_manifest() {\n', 1)[1].split('\nmain() {', 1)[0]
    variables = {name: '' for name in re.findall(r'\$([A-Za-z_][A-Za-z0-9_]*)', function)}
    variables.update(DRY_RUN=str(dry_run).lower(), MANIFEST=str(home / 'install-state.json'),
                     INSTALL_DIR=str(home), PYTHON_BIN=sys.executable, REPO_ROOT=str(ROOT),
                     SCRIPT_DIR=str(ROOT / 'scripts'), AGENT_MAIL_NAME_CAPABILITY_JSON='{}',
                     SAFE_MERGE_RESULT_FILE=str(home / 'no-settings-result'),
                     MCP_MERGE_RESULT_FILE=str(home / 'no-mcp-result'))
    assignments = [key + '=' + shlex.quote(value) for key, value in variables.items()]
    script = '\n'.join(['set -eu', 'plan() { :; }', *assignments, function,
                         'write_manifest manual "" "" ""'])
    done = subprocess.run(['/bin/bash', '-c', script], cwd='/', env={'PATH': '/usr/bin:/bin'},
                          capture_output=True, text=True, timeout=10)
    assert done.returncode == 0, done.stderr
    if dry_run:
        assert list(home.iterdir()) == []
    else:
        state = examples.read(home / 'install-state.json')
        assert len(state['example_files']) == 6
        assert not any('/examples/' in raw for raw in state['owned_files'])
        assert not any('/examples/' in raw for raw in state['purge_paths'])
