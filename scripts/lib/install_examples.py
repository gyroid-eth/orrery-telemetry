"""Inactive native-harness examples: publication and hash-owned removal.

Internal installer library. Never loads a user's provider configuration or
credentials, enables a harness, or performs a provider request.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile

RELATIVE = Path('examples/native-harnesses')
JSON_FILES = ('manifest.example.json', 'dsh-config.example.json',
              'dsh-backends.example.json', 'hermes-config.example.json')
FILES = ('README.md', 'README.en.md', *JSON_FILES)
OWNER = '.orrery-managed-install.json'


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read(path):
    return json.loads(Path(path).read_bytes())


def no_links(path):
    """Check the spelling, including existing ancestors, before following it."""
    return path.is_absolute() and not any(p.is_symlink() for p in (path, *path.parents))


def regular(path):
    return no_links(path) and path.exists() and stat.S_ISREG(path.stat().st_mode)


def example_path(install, raw):
    if not isinstance(raw, str):
        return None
    path = Path(raw)
    if not path.is_absolute() or '..' in path.parts:
        return None
    try:
        path.relative_to(install / RELATIVE)
    except ValueError:
        return None
    return path if no_links(path) else None


def retained_files(install, records):
    result = []
    for row in records:
        if not isinstance(row, dict):
            continue
        path = example_path(install, row.get('path'))
        if path is not None and regular(path) and digest(path.read_bytes()) == row.get('sha256'):
            result.append({'path': str(path), 'sha256': row['sha256']})
    return result


def example_directory(install, raw):
    path = Path(raw) if isinstance(raw, str) else None
    if path == install / 'examples' and no_links(path):
        return path
    return example_path(install, raw)


def runtime_validator(repo):
    spec = importlib.util.spec_from_file_location('_example_runtime_validator',
                                                 repo / 'scripts/lib/managed_harness_install.py')
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    return validator


def runtime_references(repo, install, python):
    """Only exact installer current/owner references; no discovery or key reads."""
    validator = None
    refs = {}
    for harness in ('dsh', 'hermes'):
        base = install / 'harnesses' / harness
        current = base / 'current.json'
        if not os.path.lexists(current):
            continue
        if not regular(current):
            raise ValueError('example_runtime_reference_invalid: ' + harness)
        data = read(current)
        generation = data.get('generation')
        if not isinstance(generation, str) or Path(generation).name != generation or generation in ('', '.', '..'):
            raise ValueError('example_runtime_reference_invalid: ' + harness)
        owner = base / generation / OWNER
        if not regular(owner) or data.get('generation_receipt') != str(owner):
            raise ValueError('example_runtime_reference_invalid: ' + harness)
        receipt = read(owner)
        expected = 'plain-datadir' if harness == 'dsh' else 'plain'
        if receipt.get('variant') != expected or receipt.get('thin_config_contract') != 1:
            raise ValueError('example_plain_generation_required: ' + harness)
        details = receipt['details']
        if any(data.get(key) != value for key, value in details.items()):
            raise ValueError('example_runtime_reference_changed: ' + harness)
        if validator is None:
            validator = runtime_validator(repo)
        validator.validate(repo, harness, receipt)
        if harness == 'dsh':
            if receipt.get('variant') != 'plain-datadir':
                raise ValueError('example_plain_datadir_required')
            if not Path(python).is_absolute() or not os.access(python, os.X_OK):
                raise ValueError('example_generator_python_invalid')
            refs[harness] = {key: details[key] for key in ('node', 'runtime', 'generation_receipt')}
            refs[harness]['python'] = str(python)
        else:
            source = Path(details['hermes_source']).resolve()
            key = digest(str(source).encode())[:16]
            facts = Path(details['managed_root']) / 'installs' / key / 'facts.json'
            environment = Path(read(facts)['packages']['venv']['environment']).resolve()
            refs[harness] = {key: details[key] for key in
                             ('hermes_executable', 'hermes_source', 'managed_root', 'generation_receipt')}
            # validate() has checked this exact committed PM environment.
            refs[harness]['hermes_python'] = str(environment / 'bin/python')
            refs[harness]['python'] = str(environment / 'bin/python')
        refs[harness]['_receipt_sha256'] = digest(owner.read_bytes())
    return refs


class Examples:
    def __init__(self, repo, install, previous, python, *, platform=None):
        self.repo, self.install = Path(repo).resolve(), Path(install)
        self.base = self.install / RELATIVE
        self.previous, self.python = previous, python
        self.platform = sys.platform if platform is None else platform
        self.new_files, self.new_dirs, self.messages = [], [], []
        self.files = retained_files(self.install, previous.get('example_files', []))
        self.dirs = [str(p) for raw in previous.get('example_dirs', [])
                     if (p := example_directory(self.install, raw)) is not None and p.is_dir()]

    def mkdir(self, path):
        if not no_links(path):
            return False
        if os.path.lexists(path):
            return path.is_dir()
        if path == self.install or not self.mkdir(path.parent):
            return False
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            return no_links(path) and path.is_dir()
        self.new_dirs.append(path)
        return True

    def record(self, path, content):
        row = {'path': str(path), 'sha256': digest(content)}
        self.new_files.append(row)
        self.files.append(row)

    def publish_file(self, path, content):
        if not self.mkdir(path.parent) or os.path.lexists(path):
            self.messages.append('kept existing example (not revalidated): ' + str(path))
            return
        # A completed staging file is linked exclusively: never open an
        # existing destination for writing, even when it contains the same bytes.
        fd, raw = tempfile.mkstemp(prefix='.example-', dir=path.parent)
        stage = Path(raw)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(content)
            try:
                os.link(stage, path)
            except FileExistsError:
                self.messages.append('kept existing example (not revalidated): ' + str(path))
            else:
                self.record(path, content)
        finally:
            stage.unlink()

    def prepare(self):
        templates = {}
        for name in FILES:
            source = self.repo / RELATIVE / name
            if not regular(source):
                raise ValueError('example_source_missing: ' + name)
            templates[name] = source.read_bytes()
        # Read and validate known runtime references before publishing examples.
        refs = runtime_references(self.repo, self.install, self.python)
        for name, content in templates.items():
            self.publish_file(self.base / name, content)
        if not refs or self.platform not in ('darwin', 'linux'):
            self.messages.append('static inactive examples only; runtime setup is still required')
            return
        identity = {'platform': self.platform, 'references': refs,
                    'templates': {name: digest(content) for name, content in templates.items()}}
        snapshot = self.base / 'resolved' / digest(encoded(identity))[:24]
        if not self.mkdir(snapshot.parent) or os.path.lexists(snapshot):
            self.messages.append('kept snapshot (not revalidated): ' + str(snapshot))
            return
        documents = {name: json.loads(templates[name]) for name in JSON_FILES}
        manifest = documents['manifest.example.json']
        manifest['enabled'] = False
        manifest['harnesses'] = {}
        for harness, config_name in (('dsh', 'dsh-config.example.json'), ('hermes', 'hermes-config.example.json')):
            if harness not in refs:
                continue
            config = documents[config_name]
            config.update({key: value for key, value in refs[harness].items()
                           if key != 'python' and not key.startswith('_')})
            manifest['harnesses'][harness] = {'python': refs[harness]['python'],
                                             'config_file': str(snapshot / config_name)}
        documents['dsh-config.example.json']['backend_file'] = str(snapshot / 'dsh-backends.example.json')
        if self.platform == 'linux':
            manifest['backends']['deepseek']['credential_ref'] = {
                'store': 'secret-service', 'attributes': {'service': 'orrery-dsh-deepseek', 'account': 'api-key'}}
            documents['dsh-backends.example.json']['backends']['deepseek'].pop('keychain', None)
            documents['hermes-config.example.json']['profiles']['deepseek'].pop('key_cmd', None)
        stage = Path(tempfile.mkdtemp(prefix='.snapshot-', dir=snapshot.parent))
        try:
            contents = {name: encoded(document) for name, document in documents.items()}
            for name, content in contents.items():
                target = stage / name
                target.write_bytes(content)
                target.chmod(0o600)
            # Normal installer execution is serialized. Arbitrary concurrent
            # namespace replacement and SIGKILL recovery are not promised here.
            if os.path.lexists(snapshot):
                self.messages.append('kept snapshot (not revalidated): ' + str(snapshot))
                return
            stage.rename(snapshot)
            self.new_dirs.append(snapshot)
            for name, content in contents.items():
                self.record(snapshot / name, content)
            self.messages.append('inactive snapshot (copy and finish setup): ' + str(snapshot))
        finally:
            if stage.exists():
                shutil.rmtree(stage)

    def records(self):
        return {'example_files': list({row['path']: row for row in self.files}.values()),
                'example_dirs': sorted(set(self.dirs + [str(path) for path in self.new_dirs]))}

    def rollback(self):
        failures = []
        for row in reversed(self.new_files):
            path = example_path(self.install, row['path'])
            if path is None or not regular(path) or digest(path.read_bytes()) != row['sha256']:
                failures.append(row['path'])
                continue
            try:
                path.unlink()
            except OSError:
                failures.append(str(path))
        for path in reversed(self.new_dirs):
            if no_links(path):
                try:
                    path.rmdir()
                except OSError:
                    pass
        if failures:
            raise ValueError('example_cleanup_incomplete; retained: ' + ', '.join(failures))


def write_manifest(manifest, destination, *, repo, python):
    """Publish examples immediately before atomic ownership commit.

    This wraps only normal failures in the example publication interval. It
    does not replace service traps or roll back any other installer changes.
    """
    destination = Path(destination)
    if destination.is_symlink():
        raise ValueError('example_manifest_symlink')
    previous = read(destination) if destination.exists() else {}
    transaction = Examples(repo, manifest['install_dir'], previous, python)
    temporary = None
    attempted, committed = False, False
    candidate = None
    try:
        transaction.prepare()
        manifest.update(transaction.records())
        candidate = encoded(manifest)
        fd, raw = tempfile.mkstemp(prefix=destination.name + '.', dir=destination.parent)
        temporary = Path(raw)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(candidate)
        json.loads(temporary.read_bytes())
        attempted = True
        os.replace(temporary, destination)
        committed = True
        if destination.read_bytes() != candidate:
            raise ValueError('example_ownership_commit_changed')
    except Exception:
        if attempted and not committed:
            # A wrapper can report failure after rename. Unknown commit state
            # preserves files rather than deleting potentially owned examples.
            try:
                committed = destination.read_bytes() == candidate
            except FileNotFoundError:
                committed = False
            except OSError:
                raise ValueError('example_ownership_commit_unknown; examples retained') from None
        if not committed:
            transaction.rollback()
        raise
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    for message in transaction.messages:
        print(message)


def remove_examples(manifest, *, dry_run=False):
    """Called before ordinary payload removal by the installed uninstaller."""
    install = Path(manifest['install_dir'])
    for row in retained_files(install, manifest.get('example_files', [])):
        path = Path(row['path'])
        print(('DRY-RUN would remove example: ' if dry_run else 'remove example: ') + str(path))
        if not dry_run:
            path.unlink()
    if not dry_run:
        for raw in sorted(manifest.get('example_dirs', []), key=lambda value: len(str(value)), reverse=True):
            path = example_directory(install, raw)
            if path is not None:
                try:
                    path.rmdir()
                except OSError:
                    pass
