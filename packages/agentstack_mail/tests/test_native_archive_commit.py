"""macOS archive commits match GitPython without rewriting its Python index."""
from __future__ import annotations

import asyncio
import errno
from pathlib import Path
from types import SimpleNamespace

import pytest
from git import Actor, Git, Repo
from git.exc import GitCommandError, GitCommandNotFound, HookExecutionError

from agentstack_mail import storage

ACTOR = Actor('Archive Writer', 'archive@example.invalid')
SETTINGS = SimpleNamespace(storage=SimpleNamespace(
    git_author_name=ACTOR.name, git_author_email=ACTOR.email))


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    for name in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY',
                 'GIT_ALTERNATE_OBJECT_DIRECTORIES'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', '/dev/null')
    monkeypatch.setenv('GIT_AUTHOR_DATE', '2000-01-01T00:00:00+0000')
    monkeypatch.setenv('GIT_COMMITTER_DATE', '2000-01-01T00:00:00+0000')
    monkeypatch.setenv('GIT_AUTHOR_NAME', 'Ambient Author')
    monkeypatch.setenv('GIT_COMMITTER_NAME', 'Ambient Committer')
    # Keep the host platform visible to filelock and GitPython.
    monkeypatch.setattr(storage, 'sys', SimpleNamespace(platform='darwin'))
    return tmp_path


def make_repo(root):
    root.mkdir()
    repo = Repo.init(root)
    (root / 'tracked.md').write_text('before\n')
    repo.index.add(['tracked.md'])
    repo.index.commit('seed', author=ACTOR, committer=ACTOR)
    return repo


def direct(root, message, paths):
    asyncio.run(storage._commit_direct(root, SETTINGS, message, paths))


def snapshot(repo):
    return [(c.tree.hexsha, c.message, str(c.author), str(c.committer))
            for c in repo.iter_commits(reverse=True)]


def test_archive_content_identity_trailers_and_order_match_gitpython(isolated):
    before, after = (isolated / name for name in ('before', 'after'))
    with make_repo(before) as legacy, make_repo(after) as native:
        operations = [
            ('plain', 'plain'),
            (' space at end \n\n', ' space at end \n\n'),
            ('日本語\n', '日本語\n'),
            ('mail: Sender -> Parent | Result', 'mail: Sender -> Parent | Result\n\nAgent: Sender\n'),
            ('file_reservation: Editor acquired file', 'file_reservation: Editor acquired file\n\nAgent: Editor\n'),
            ('mail: Sender -> Parent | Result\n\nAgent: Existing\n',
             'mail: Sender -> Parent | Result\n\nAgent: Existing\n'),
            ('', ''),
        ]
        for repo, root in ((legacy, before), (native, after)):
            repo.git.config('commit.gpgSign', 'true')  # IndexFile.commit does not sign.
            (root / 'untracked.md').write_text('must not be staged\n')
            (root / 'staged.md').write_text('already staged\n')
            repo.index.add(['staged.md'])
        for n, (message, expected) in enumerate(operations):
            relative = f'projects/fixture/event-{n}.md'
            for root in (before, after):
                file = root / relative
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text(f'event {n}: 日本語\n')
            legacy.index.add([relative])
            legacy.index.commit(expected, author=ACTOR, committer=ACTOR)
            direct(after, message, [relative])
        # Working-tree-only dirtiness used to create a same-tree commit.
        for root in (before, after):
            (root / 'tracked.md').write_text('not staged\n')
        legacy.index.add([relative])
        legacy.index.commit('unchanged requested path', author=ACTOR, committer=ACTOR)
        direct(after, 'unchanged requested path', [relative])
        assert snapshot(native) == snapshot(legacy)
        assert 'untracked.md' not in native.head.commit.tree
        assert native.head.commit.tree['tracked.md'].data_stream.read() == b'before\n'
        assert (after / 'tracked.md').read_bytes() == (before / 'tracked.md').read_bytes()


def test_native_commit_runs_existing_commit_hooks(isolated):
    snapshots = []
    for backend in ('legacy', 'native'):
        root = isolated / backend
        with make_repo(root) as repo:
            for name, body in (
                ('pre-commit', "printf 'pre\\n' >> hooks-ran.txt\n"),
                ('commit-msg', "printf '\\nHook: checked\\n' >> \"$1\"\nprintf 'message\\n' >> hooks-ran.txt\n"),
                ('post-commit', "printf 'post\\n' >> hooks-ran.txt\n"),
            ):
                file = root / '.git/hooks' / name
                file.write_text('#!/bin/sh\n' + body)
                file.chmod(0o700)
            (root / 'tracked.md').write_text('after\n')
            if backend == 'legacy':
                repo.index.add(['tracked.md'])
                repo.index.commit('hook probe', author=ACTOR, committer=ACTOR)
            else:
                direct(root, 'hook probe', ['tracked.md'])
            snapshots.append((snapshot(repo), (root / 'hooks-ran.txt').read_text()))
    assert snapshots[0] == snapshots[1]
    assert snapshots[1][1] == 'pre\nmessage\npost\n'


def test_native_never_deserializes_or_commits_the_gitpython_index(isolated, monkeypatch):
    root = isolated / 'archive'
    with make_repo(root) as repo:
        def forbidden(*args, **kwargs):
            pytest.fail('native path entered a Python index operation')
        monkeypatch.setattr(type(repo.index), 'add', forbidden)
        monkeypatch.setattr(type(repo.index), 'commit', forbidden)
        (root / 'tracked.md').write_text('after\n')
        direct(root, 'native', ['tracked.md'])
        assert repo.head.commit.message == 'native'


def test_missing_path_still_fails_instead_of_staging_a_deletion(isolated):
    root = isolated / 'archive'
    with make_repo(root) as repo:
        before = repo.head.commit.hexsha
        (root / 'tracked.md').unlink()
        with pytest.raises(FileNotFoundError):
            direct(root, 'removed', ['tracked.md'])
        assert repo.head.commit.hexsha == before
        assert repo.head.commit.tree['tracked.md'].data_stream.read() == b'before\n'


def test_directory_add_does_not_stage_removed_files(isolated):
    root = isolated / 'archive'
    with make_repo(root) as repo:
        (root / 'tracked.md').unlink()
        (root / 'added.md').write_text('new\n')
        direct(root, 'directory', ['.'])
        assert repo.head.commit.tree['tracked.md'].data_stream.read() == b'before\n'
        assert repo.head.commit.tree['added.md'].data_stream.read() == b'new\n'


def test_hook_failure_is_nonzero_and_preserves_pending_archive(isolated):
    root = isolated / 'archive'
    with make_repo(root) as repo:
        before = repo.head.commit.hexsha
        hook = root / '.git/hooks/pre-commit'
        hook.write_text('#!/bin/sh\nexit 7\n');hook.chmod(0o700)
        (root / 'tracked.md').write_text('pending\n')
        with pytest.raises(HookExecutionError):
            direct(root, 'refused', ['tracked.md'])
        assert repo.head.commit.hexsha == before
        assert (root / 'tracked.md').read_text() == 'pending\n'
        assert repo.git.diff('--cached', '--name-only') == 'tracked.md'


def test_missing_git_is_not_reported_as_success(isolated, monkeypatch):
    root = isolated / 'archive'
    with make_repo(root) as repo:
        before = repo.head.commit.hexsha
        (root / 'tracked.md').write_text('pending\n')
        monkeypatch.setattr(Git, 'GIT_PYTHON_GIT_EXECUTABLE', str(isolated / 'missing-git'))
        with pytest.raises(GitCommandNotFound):
            direct(root, 'missing git', ['tracked.md'])
        monkeypatch.undo()
        assert repo.head.commit.hexsha == before
        assert (root / 'tracked.md').read_text() == 'pending\n'


def test_fresh_index_lock_retries_then_fails_without_removing_owner(isolated):
    root = isolated / 'archive'
    with make_repo(root) as repo:
        before = repo.head.commit.hexsha
        lock = root / '.git/index.lock';lock.write_text('another writer')
        (root / 'tracked.md').write_text('pending\n')
        with pytest.raises(storage.GitIndexLockError) as error:
            direct(root, 'locked', ['tracked.md'])
        assert error.value.attempts == 6
        assert lock.read_text() == 'another writer'
        assert repo.head.commit.hexsha == before
        assert (root / 'tracked.md').read_text() == 'pending\n'


def test_transient_native_index_lock_uses_existing_retry(isolated, monkeypatch):
    root = isolated / 'archive'
    with make_repo(root) as repo:
        lock = root / '.git/index.lock';lock.write_text('temporary writer')
        (root / 'tracked.md').write_text('after retry\n')
        original = storage._native_archive_git
        calls = []
        async def run():
            observed = asyncio.Event()
            loop = asyncio.get_running_loop()
            def audited(target, command, *args, **kwargs):
                calls.append(command)
                try:
                    return original(target, command, *args, **kwargs)
                except FileExistsError:
                    loop.call_soon_threadsafe(observed.set)
                    raise
            monkeypatch.setattr(storage, '_native_archive_git', audited)
            async def unlock():
                await observed.wait();lock.unlink()
            task = asyncio.create_task(unlock())
            try:
                await storage._commit_direct(root, SETTINGS, 'retried', ['tracked.md'])
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        asyncio.run(run())
        assert calls.count('update_index') == 2
        assert repo.head.commit.message == 'retried'


@pytest.mark.parametrize('platform', ['linux', 'win32'])
def test_non_mac_commit_branch_is_unchanged(tmp_path, monkeypatch, platform):
    calls = []
    fake = SimpleNamespace(
        index=SimpleNamespace(add=lambda paths: calls.append(('index.add', list(paths))),
                              commit=lambda *a, **k: calls.append(('index.commit', a, k))),
        git=SimpleNamespace(add=lambda *args: calls.append(('git.add', args))),
        is_dirty=lambda **kwargs: True, close=lambda: None)
    monkeypatch.setattr(storage, 'Repo', lambda *a: fake)
    monkeypatch.setattr(storage, 'sys', SimpleNamespace(platform=platform))
    direct(tmp_path, 'unchanged', ['file.md'])
    assert calls[0] == (('git.add', ('--', 'file.md')) if platform == 'win32'
                        else ('index.add', ['file.md']))
    assert calls[1][0:2] == ('index.commit', ('unchanged',))


@pytest.mark.parametrize("hook_name,committed", [("pre-commit", False), ("post-commit", True)])
def test_rejecting_hooks_preserve_legacy_failure_and_commit_state(isolated, hook_name, committed):
    results = []
    for backend in ("legacy", "native"):
        root = isolated / backend
        with make_repo(root) as repo:
            before = repo.head.commit.hexsha
            hook = root / ".git/hooks" / hook_name
            hook.write_text("#!/bin/sh\nexit 7\n")
            hook.chmod(0o700)
            (root / "tracked.md").write_text("pending\n")
            try:
                if backend == "legacy":
                    repo.index.add(["tracked.md"])
                    repo.index.commit("refused", author=ACTOR, committer=ACTOR)
                else:
                    direct(root, "refused", ["tracked.md"])
            except Exception as exc:
                error = type(exc)
            else:
                error = None
            results.append((error, repo.head.commit.hexsha != before))
    assert results == [(HookExecutionError, committed)] * 2


def test_prepare_commit_msg_is_not_newly_enabled(isolated):
    for backend in ("legacy", "native"):
        root = isolated / backend
        with make_repo(root) as repo:
            hook = root / ".git/hooks/prepare-commit-msg"
            hook.write_text("#!/bin/sh\nprintf touched > unexpected-hook\n")
            hook.chmod(0o700)
            (root / "tracked.md").write_text("after\n")
            if backend == "legacy":
                repo.index.add(["tracked.md"])
                repo.index.commit("probe", author=ACTOR, committer=ACTOR)
            else:
                direct(root, "probe", ["tracked.md"])
            assert not (root / "unexpected-hook").exists()


def test_native_emfile_recovers_with_existing_locks(isolated, monkeypatch):
    root = isolated / "archive"
    with make_repo(root) as repo:
        (root / "tracked.md").write_text("recovered\n")
        original = storage._native_archive_git
        attempts = []
        def run(target, command, *args, **kwargs):
            assert storage._GITPYTHON_INDEX_OPERATION_LOCK.locked()
            assert storage._commit_lock_path(root, ["tracked.md"]).exists()
            if command == "update_index":
                attempts.append(target)
                if len(attempts) == 1:
                    raise OSError(errno.EMFILE, "fixture exhausted descriptors")
            return original(target, command, *args, **kwargs)
        monkeypatch.setattr(storage, "_native_archive_git", run)
        direct(root, "recovered", ["tracked.md"])
        assert len(attempts) == 2 and attempts[0] is not attempts[1]
        assert repo.head.commit.message == "recovered"
        assert not storage._commit_lock_path(root, ["tracked.md"]).exists()
        assert not storage._GITPYTHON_INDEX_OPERATION_LOCK.locked()


@pytest.mark.parametrize("scope", ["file", "directory"])
@pytest.mark.parametrize("excludes", ["repository", "global"])
def test_ignored_audit_files_match_legacy_blobs_and_tree(isolated, scope, excludes):
    trees = []
    for backend in ("legacy", "native"):
        root = isolated / backend
        with make_repo(root) as repo:
            rules = root / (".gitignore" if excludes == "repository" else ".git/global-ignore")
            rules.write_text("events/*.json\n")
            if excludes == "global":
                repo.git.config("core.excludesFile", str(rules))
            (root / "events").mkdir()
            (root / "events/event.json").write_bytes(b'{"ignored":true}\n')
            (root / "events/visible.md").write_bytes(b"visible\n")
            paths = ["events/event.json"] if scope == "file" else ["events"]
            if backend == "legacy":
                repo.index.add(paths)
                repo.index.commit("ignored audit", author=ACTOR, committer=ACTOR)
            else:
                direct(root, "ignored audit", paths)
            blob = repo.head.commit.tree["events/event.json"]
            assert blob.data_stream.read() == b'{"ignored":true}\n'
            trees.append((repo.head.commit.tree.hexsha, blob.hexsha))
    assert trees[0] == trees[1]


@pytest.mark.parametrize("extension", ["md", "json"])
@pytest.mark.parametrize("content", [b"first\r\nsecond\r\n", b"first\rsecond\r",
                                     b"a\r\nb\rc\nd", b"no final newline", b""])
def test_audit_bytes_ignore_text_and_clean_filters(isolated, extension, content):
    results = []
    for backend in ("legacy", "native"):
        root = isolated / backend
        with make_repo(root) as repo:
            (root / ".gitattributes").write_text("*.json text filter=audit\n*.md text filter=audit\n")
            repo.git.config("filter.audit.clean", "tr a-z A-Z")
            target = f"nested/deep/event.{extension}"
            (root / target).parent.mkdir(parents=True)
            (root / target).write_bytes(content)
            if backend == "legacy":
                repo.index.add([target])
                repo.index.commit("raw audit", author=ACTOR, committer=ACTOR)
            else:
                direct(root, "raw audit", [target])
            blob = repo.head.commit.tree[target]
            assert blob.data_stream.read() == content
            results.append((repo.head.commit.tree.hexsha, blob.hexsha))
    assert results[0] == results[1]


@pytest.mark.parametrize("scope", ["files", "directory"])
def test_modes_symlinks_and_empty_files_match_legacy(isolated, scope):
    results = []
    for backend in ("legacy", "native"):
        root = isolated / backend
        with make_repo(root) as repo:
            directory = root / "events"
            directory.mkdir()
            for name, mode, content in (("run.sh", 0o755, b"echo audit\r\n"),
                                        ("group-exec", 0o654, b"group executable only"),
                                        ("empty", 0o644, b"")):
                file = directory / name
                file.write_bytes(content)
                file.chmod(mode)
            (directory / "link").symlink_to("run.sh")
            paths = ["events"] if scope == "directory" else ["events/"+n for n in ("run.sh", "group-exec", "empty", "link")]
            if backend == "legacy":
                repo.index.add(paths)
                repo.index.commit("modes", author=ACTOR, committer=ACTOR)
            else:
                direct(root, "modes", paths)
            tree = repo.head.commit.tree
            results.append((tree.hexsha, [(tree["events/"+name].mode, tree["events/"+name].hexsha)
                                         for name in ("run.sh", "group-exec", "empty", "link")]))
    assert results[0] == results[1]
