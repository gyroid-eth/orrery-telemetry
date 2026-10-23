"""Identity-scoped Hermes HOME; synthetic memory only, no provider or credentials."""
import builtins
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_hermes_thin_config as thin_fixture

config, ROOT = thin_fixture.config, thin_fixture.ROOT


@contextmanager
def forbid_shared_open(directory):
    original, path_open = builtins.open, Path.open
    def check(path):
        if isinstance(path, (str, os.PathLike)) and Path(path).resolve().is_relative_to(directory.resolve()):
            raise AssertionError('Shared profile must not be opened')
    def opened(path, *args, **kwargs):
        check(path)
        return original(path, *args, **kwargs)
    def path_opened(path, *args, **kwargs):
        check(path)
        return path_open(path, *args, **kwargs)
    with patch.object(builtins, 'open', opened), patch.object(Path, 'open', path_opened):
        yield


class ChildMemory(unittest.TestCase):
    def setUp(self):
        self.fixture = thin_fixture.ThinConfig()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.document = self.fixture.document

    def test_common_memory_and_private_settings_are_not_opened_or_cloned(self):
        fixture = self.fixture
        shared = fixture.runtime / 'profiles/deepseek'
        files = {'memories/MEMORY.md': 'shared-memory-marker', 'memories/USER.md': 'shared-user-marker',
                 '.env': 'synthetic private data', 'auth.json': '{}',
                 'config.yaml': '{"agent":{"reasoning_effort":"max"},"active_profile":"private"}',
                 'skills/private/SKILL.md': 'private skill'}
        for name, content in files.items():
            path = shared / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        with forbid_shared_open(shared):
            first = fixture.generate()
            second = fixture.generate(agent_name='OtherAgent', state_dir=str(fixture.base / 'other'))
        for plan in (first, second):
            home = Path(plan['env']['HERMES_HOME'])
            self.assertNotEqual(home, shared)
            for name in files:
                if name == 'config.yaml':
                    self.assertNotIn('agent', config.read(home / name))
                    self.assertNotIn('active_profile', config.read(home / name))
                else:
                    self.assertFalse((home / name).exists())
            self.assertTrue((home / 'plugins/orrery-thin-guard/__init__.py').is_symlink())
            overlay = config.read(Path(plan['config_dir']) / 'config.yaml')
            self.assertFalse(overlay['auth']['adopt_external_logins'])
            self.assertNotIn('--reasoning', plan['argv'])
        self.assertNotEqual(first['profile_home'], second['profile_home'])
        for name, content in files.items():
            self.assertEqual((shared / name).read_text(), content)

    def test_same_identity_and_generation_external_profiles_are_stable(self):
        fixture = self.fixture
        persistent = fixture.base / 'persistent-profiles'
        persistent.mkdir()
        generation = fixture.base / 'generation-one'
        generation.mkdir()
        (generation / 'profiles').symlink_to(persistent, target_is_directory=True)
        second = fixture.base / 'generation-two'
        second.mkdir()
        (second / 'profiles').symlink_to(persistent, target_is_directory=True)
        expected = persistent / 'children' / hashlib.sha256(self.document['project_key'].encode()).hexdigest() / self.document['agent_name']
        for root in (generation, second):
            (root / 'installs').symlink_to(fixture.runtime / 'installs', target_is_directory=True)
        fixture.template_data['managed_root'] = str(generation)
        fixture.save(fixture.template, fixture.template_data)
        one = fixture.generate()
        marker = Path(one['profile_home']) / 'memories/MEMORY.md'
        marker.parent.mkdir()
        marker.write_text('synthetic generation-stable memory')
        fixture.template_data['managed_root'] = str(second)
        fixture.save(fixture.template, fixture.template_data)
        two = fixture.generate(state_dir=str(fixture.base / 'next-attempt'))
        self.assertEqual(Path(one['profile_home']), expected)
        self.assertEqual(one['profile_home'], two['profile_home'])
        self.assertEqual(marker.read_text(), 'synthetic generation-stable memory')
        other = fixture.generate(project_key='/fixture/other-project', state_dir=str(fixture.base / 'other-project'))
        self.assertNotEqual(one['profile_home'], other['profile_home'])
        self.assertFalse((Path(other['profile_home']) / 'memories/MEMORY.md').exists())

    def test_legacy_changed_missing_and_unreadable_resume_reject_before_writes(self):
        fixture = self.fixture
        home = fixture.profile
        shared = fixture.runtime / 'profiles/deepseek'
        fixture.save(shared / 'config.yaml', {'synthetic': 'unchanged'})
        original = (shared / 'config.yaml').read_bytes()
        cases = [({'resume_session_id': 'owned-session'}, 'native_resume_profile_missing'),
            ({'resume_session_id': 'owned-session', 'native_profile_env': {'HERMES_HOME': str(shared)}}, 'native_resume_legacy_profile'),
            ({'resume_session_id': 'owned-session', 'native_profile_env': {'HERMES_HOME': str(fixture.base / 'foreign')}}, 'native_resume_profile_changed'),
            ({'resume_session_id': 'owned-session', 'native_profile_env': {'HERMES_HOME': str(home)}}, 'native_resume_home_missing')]
        for extra, reason in cases:
            with self.subTest(reason=reason), forbid_shared_open(shared):
                with self.assertRaisesRegex(ValueError, '^' + reason + '$'):
                    fixture.generate(**extra)
                self.assertFalse((fixture.base / 'child').exists())
                self.assertFalse(home.exists())
        home.mkdir(parents=True)
        with patch.object(config.os, 'access', side_effect=lambda p, mode: mode != (os.R_OK | os.X_OK)):
            with self.assertRaisesRegex(ValueError, '^native_resume_home_missing$'):
                fixture.generate(resume_session_id='owned-session', native_profile_env={'HERMES_HOME': str(home)})
        self.assertFalse((fixture.base / 'child').exists())
        self.assertEqual((shared / 'config.yaml').read_bytes(), original)

    def test_resume_requires_existing_readable_history_but_fresh_spawn_does_not(self):
        fixture = self.fixture
        first = fixture.generate()
        home = Path(first['profile_home'])
        self.assertFalse((home / 'state.db').exists())
        # Existing HOME alone must not be rebuilt into an empty conversation.
        state = fixture.base / 'resume-attempt'
        args = {'resume_session_id': 'exact-owned-session', 'state_dir': str(state),
                'native_profile_env': {'HERMES_HOME': str(home)}}
        with self.assertRaisesRegex(ValueError, '^native_resume_history_missing$'):
            fixture.generate(**args)
        self.assertFalse(state.exists())
        import sqlite3
        with sqlite3.connect(home / 'state.db') as db:
            db.execute('CREATE TABLE synthetic_history (id TEXT)')
        original = (home / 'state.db').read_bytes()
        access = config.os.access
        with patch.object(config.os, 'access', side_effect=lambda p, mode:
                          False if Path(p) == home / 'state.db' else access(p, mode)):
            with self.assertRaisesRegex(ValueError, '^native_resume_history_missing$'):
                fixture.generate(**args)
        self.assertFalse(state.exists())
        plan = fixture.generate(**args)
        self.assertEqual(plan['env']['HERMES_HOME'], str(home))
        self.assertEqual(plan['argv'][plan['argv'].index('--resume') + 1], 'exact-owned-session')
        self.assertEqual((home / 'state.db').read_bytes(), original)

    def test_missing_project_invalid_name_and_foreign_home_link_do_not_generate(self):
        for changes in ({'project_key': None}, {'project_key': ''}, {'agent_name': '../OtherAgent'}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, '^invalid_input$'):
                self.fixture.generate(**changes)
        target = self.fixture.base / 'foreign'
        target.mkdir()
        home = self.fixture.profile
        home.parent.mkdir(parents=True)
        home.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, '^native_resume_profile_changed$'):
            self.fixture.generate()
        self.assertEqual(list(target.iterdir()), [])
        self.assertFalse((self.fixture.base / 'child').exists())

    def test_name_free_catalog_admission_never_derives_or_creates_child_home(self):
        subset = {key: self.document[key] for key in ('manifest_file', 'product_root', 'backend_id', 'model')}
        with patch.object(config, 'child_home', side_effect=AssertionError('No identity in catalog')):
            facts = config.validate_input(subset)
        self.assertIn('profiles_root', facts)
        self.assertFalse((self.fixture.runtime / 'profiles').exists())
        self.assertFalse((self.fixture.base / 'child').exists())

    def test_installed_generator_minimal_environment_has_stable_private_home(self):
        # setUp blocks Popen; allow only this installed-copy generator process.
        product = self.fixture.base / 'installed'
        for name in ('thin_config.py', 'thin_guard.py', 'telemetry_defaults.py'):
            target = product / 'integrations/hermes' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / 'integrations/hermes' / name, target)
        helper = product / 'hooks/native_connections.py'
        helper.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / 'hooks/native_connections.py', helper)
        source = self.fixture.base / 'installed-input.json'
        output = self.fixture.base / 'installed-plan.json'
        self.fixture.save(source, {**self.document, 'product_root': str(product)})
        with patch.object(subprocess, 'Popen', REAL_POPEN):
            result = subprocess.run([sys.executable, '-I', '-B', str(product / 'integrations/hermes/thin_config.py'),
                '--input', str(source), '--out', str(output)], cwd='/',
                env={'HOME': str(self.fixture.base), 'PATH': os.defpath},
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = config.read(output)
        self.assertEqual(plan['env']['HERMES_HOME'], str(self.fixture.profile))
        self.assertTrue(plan['env']['HERMES_MANAGED_DIR'].startswith(str(self.fixture.base / 'child')))


REAL_POPEN = subprocess.Popen

NATIVE_MEMORY = r'''
import json,os,sys
from pathlib import Path
source,home,shared,action=sys.argv[1:]
def audit(event,args):
    if event in ('subprocess.Popen','socket.connect','socket.getaddrinfo'):
        raise AssertionError('native memory fixture forbids helpers/network')
    if event=='open' and isinstance(args[0],str) and Path(args[0]).resolve().is_relative_to(Path(shared).resolve()):
        raise AssertionError('shared profile access forbidden')
sys.addaudithook(audit)
os.environ['HERMES_HOME']=home
sys.path.insert(0,source)
from tools.memory_tool import MemoryStore,get_memory_dir
assert get_memory_dir()==Path(home)/'memories'
store=MemoryStore()
store.load_from_disk()
if action=='write':
    assert store.add('memory','synthetic-memory-marker-A')['success']
    assert store.add('user','synthetic-user-marker-A')['success']
else:
    expected=action=='read-A'
    assert ('synthetic-memory-marker-A' in store._system_prompt_snapshot['memory'])==expected
    assert ('synthetic-user-marker-A' in store._system_prompt_snapshot['user'])==expected
    assert ('synthetic-memory-marker-A' in '\n'.join(store.memory_entries))==expected
print(json.dumps({'memory_snapshot_checked':action!='write','synthetic_only':True}))
'''


class NativeMemorySnapshot(unittest.TestCase):
    def test_native_memory_store_reloads_same_home_and_keeps_other_child_empty(self):
        source = os.environ.get('ORRERY_HERMES_TEST_SOURCE')
        python = os.environ.get('ORRERY_HERMES_TEST_PYTHON')
        if not source or not python:
            self.skipTest('Explicit isolated pinned native source/dependency interpreter required')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second, shared = root / 'A', root / 'B', root / 'shared'
            shared.mkdir()
            (shared / 'MEMORY.md').write_text('shared-marker')
            for home, action in ((first, 'write'), (first, 'read-A'), (second, 'read-B')):
                result = subprocess.run([python, '-I', '-B', '-c', NATIVE_MEMORY,
                    source, str(home), str(shared), action], cwd='/',
                    env={'HOME': str(root), 'PATH': os.defpath, 'HERMES_DISABLE_LAZY_INSTALLS': '1',
                         'PYTHONDONTWRITEBYTECODE': '1', 'UV_OFFLINE': '1'},
                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((shared / 'MEMORY.md').read_text(), 'shared-marker')


if __name__ == '__main__':
    unittest.main()
