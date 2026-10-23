"""Private reads across real atomic replacement, without live client state."""
import errno
import json
import os
from pathlib import Path
import runpy

import pytest

ROOT = Path(__file__).resolve().parents[1]
API = runpy.run_path(str(ROOT / 'bin/lib/runtime_client.py'))


def private(path, raw):
    path.write_bytes(raw)
    path.chmod(0o600)
    return path


def assert_closed(fds, fstat):
    for fd in fds:
        with pytest.raises(OSError) as error:
            fstat(fd)
        assert error.value.errno == errno.EBADF


@pytest.mark.parametrize('boundary', ['open', 'fstat'])
def test_safe_atomic_replacement_returns_one_safe_snapshot(tmp_path, monkeypatch, boundary):
    path = private(tmp_path / 'private.json', b'{"version":"old"}')
    staged = private(tmp_path / 'replacement.json', b'{"version":"new"}')
    real_open, real_fstat = os.open, os.fstat
    fds, evidence = [], {'boundary': boundary, 'replacements': []}

    def replace(fd):
        before = real_fstat(fd)
        os.replace(staged, path)
        after, current = real_fstat(fd), path.lstat()
        evidence['replacements'].append({
            'old_inode': before.st_ino, 'old_nlink_before': before.st_nlink,
            'old_nlink_after': after.st_nlink, 'current_inode': current.st_ino,
            'current_uid': current.st_uid, 'current_mode': oct(current.st_mode & 0o777),
        })

    def opened(value, *args, **kwargs):
        fd = real_open(value, *args, **kwargs)
        if os.fspath(value) == str(path):
            fds.append(fd)
            if boundary == 'open' and not evidence['replacements']:
                replace(fd)
        return fd

    def observed(fd):
        info = real_fstat(fd)
        if boundary == 'fstat' and fd in fds and not evidence['replacements']:
            replace(fd)
        return info

    monkeypatch.setattr(os, 'open', opened)
    monkeypatch.setattr(os, 'fstat', observed)
    try:
        try:
            raw = API['read_private'](path)
        except API['ClientError'] as error:
            evidence['error'] = str(error)
            raise
        evidence['result'] = raw.decode()
        assert raw == b'{"version":"new"}'
        assert len(evidence['replacements']) == 1
    finally:
        assert_closed(fds, real_fstat)
        evidence['opened_fds'] = len(fds)
        evidence['all_fds_closed'] = True
        print('PRIVATE_REPLACE_EVIDENCE=' + json.dumps(evidence))


@pytest.fixture
def probe(monkeypatch):
    """Count actual opens and check every owned target fd after each test."""
    real_open, real_fstat = os.open, os.fstat
    probes = []

    def watch(path, after_open=None):
        assert not probes, 'one private target per test'
        state = {'fds': [], 'flags': [], 'attempts': 0}
        probes.append(state)

        def opened(value, flags, *args, **kwargs):
            target = os.fspath(value) == str(path)
            if target:
                state['attempts'] += 1
                state['flags'].append(flags)
            fd = real_open(value, flags, *args, **kwargs)
            if target:
                state['fds'].append(fd)
                if after_open is not None:
                    after_open(fd, state['attempts'])
            return fd

        monkeypatch.setattr(os, 'open', opened)
        return state, real_fstat

    yield watch
    for state in probes:
        assert_closed(state['fds'], real_fstat)
        for flags in state['flags']:
            assert flags & os.O_NOFOLLOW
            assert flags & os.O_NONBLOCK
            assert flags & os.O_ACCMODE == os.O_RDONLY


@pytest.mark.parametrize('size', [0, 9, 1048576])
def test_ordinary_private_bytes_are_unchanged(tmp_path, probe, size):
    raw = (b'\x00\xffpayload' * (size // 9 + 1))[:size]
    path = private(tmp_path / 'snapshot', raw)
    state, _ = probe(path)
    assert API['read_private'](path) == raw
    assert state['attempts'] == 1


@pytest.mark.parametrize('missing', [False, True])
def test_oversize_and_missing_keep_existing_errors(tmp_path, probe, missing):
    path = tmp_path / 'snapshot'
    if not missing:
        private(path, b'x' * 1048577)
    state, _ = probe(path)
    reason = 'PRIVATE_FILE_UNAVAILABLE' if missing else 'PRIVATE_FILE_TOO_LARGE'
    with pytest.raises(API['ClientError'], match='^' + reason + '$'):
        API['read_private'](path)
    assert state['attempts'] == 1


@pytest.mark.parametrize('kind', ['mode', 'hardlink', 'fifo', 'directory', 'symlink'])
@pytest.mark.timeout(5)
def test_unsafe_initial_file_is_not_retried(tmp_path, probe, kind):
    path = tmp_path / 'snapshot'
    if kind == 'fifo':
        os.mkfifo(path, 0o600)
    elif kind == 'directory':
        path.mkdir(mode=0o700)
    elif kind == 'symlink':
        path.symlink_to(private(tmp_path / 'real', b'private'))
    else:
        private(path, b'private')
        if kind == 'mode':
            path.chmod(0o640)
        else:
            os.link(path, tmp_path / 'other-link')
    state, _ = probe(path)
    reason = 'PRIVATE_FILE_UNAVAILABLE' if kind == 'symlink' else 'PRIVATE_FILE_UNSAFE'
    with pytest.raises(API['ClientError'], match='^' + reason + '$'):
        API['read_private'](path)
    assert state['attempts'] == 1


@pytest.mark.parametrize('kind', ['mode', 'hardlink'])
def test_unsafe_old_fd_cannot_be_retried_into_safe_content(tmp_path, monkeypatch, probe, kind):
    path = private(tmp_path / 'snapshot', b'unsafe old')
    staged = private(tmp_path / 'replacement', b'safe new')
    if kind == 'mode':
        path.chmod(0o640)
    else:
        os.link(path, tmp_path / 'old-link')
    state, real_fstat = probe(path)
    replaced = []

    def observed(fd):
        info = real_fstat(fd)
        if fd in state['fds'] and not replaced:
            os.replace(staged, path)
            replaced.append(True)
        return info

    monkeypatch.setattr(os, 'fstat', observed)
    with pytest.raises(API['ClientError'], match='^PRIVATE_FILE_UNSAFE$'):
        API['read_private'](path)
    assert replaced == [True]
    assert path.read_bytes() == b'safe new'
    assert state['attempts'] == 1


@pytest.mark.parametrize('kind', ['mode', 'symlink', 'hardlink', 'fifo'])
@pytest.mark.timeout(5)
def test_unsafe_replacement_is_rejected_before_another_open(tmp_path, probe, kind):
    path = private(tmp_path / 'snapshot', b'safe old')
    staged = tmp_path / 'replacement'
    if kind == 'fifo':
        os.mkfifo(staged, 0o600)
    elif kind == 'symlink':
        staged.symlink_to(private(tmp_path / 'real', b'must not be read'))
    else:
        private(staged, b'must not be read')
        if kind == 'mode':
            staged.chmod(0o644)
        else:
            os.link(staged, tmp_path / 'other-link')
    state, _ = probe(path, lambda fd, attempt: os.replace(staged, path))
    with pytest.raises(API['ClientError'], match='^PRIVATE_FILE_UNSAFE$'):
        API['read_private'](path)
    assert state['attempts'] == 1


@pytest.mark.parametrize('stop_after', [2, None])
def test_repeated_real_replacements_have_a_fixed_bound(tmp_path, probe, stop_after):
    path = private(tmp_path / 'snapshot', b'initial')

    def replace(fd, attempt):
        if stop_after is None or attempt <= stop_after:
            staged = private(tmp_path / ('replacement-' + str(attempt)),
                             ('snapshot-' + str(attempt)).encode())
            os.replace(staged, path)

    state, _ = probe(path, replace)
    if stop_after is None:
        with pytest.raises(API['ClientError'], match='^PRIVATE_FILE_UNSAFE$'):
            API['read_private'](path)
    else:
        assert API['read_private'](path) == b'snapshot-2'
    assert state['attempts'] == 3


def test_unlinked_fd_without_a_current_file_is_not_a_safe_replacement(tmp_path, probe):
    path = private(tmp_path / 'snapshot', b'old')
    state, _ = probe(path, lambda fd, attempt: path.unlink())
    with pytest.raises(API['ClientError'], match='^PRIVATE_FILE_UNAVAILABLE$'):
        API['read_private'](path)
    assert state['attempts'] == 1


@pytest.mark.parametrize('boundary', ['open', 'fstat', 'lstat', 'fdopen'])
@pytest.mark.parametrize('number', [errno.EACCES, errno.EIO])
def test_permission_and_io_failures_are_not_transient(tmp_path, monkeypatch, probe, boundary, number):
    path = private(tmp_path / 'snapshot', b'private')
    state, _ = probe(path)
    original, failures = getattr(os, boundary), []

    def failed(value, *args, **kwargs):
        target = (value in state['fds'] if boundary in ('fstat', 'fdopen')
                  else os.fspath(value) == str(path))
        if target:
            failures.append(number)
            raise OSError(number, 'synthetic private-read failure')
        return original(value, *args, **kwargs)

    monkeypatch.setattr(os, boundary, failed)
    with pytest.raises(API['ClientError'], match='^PRIVATE_FILE_UNAVAILABLE$') as error:
        API['read_private'](path)
    assert isinstance(error.value.__cause__, OSError)
    assert error.value.__cause__.errno == number
    assert failures == [number]
    assert state['attempts'] == (0 if boundary == 'open' else 1)


def test_replacement_after_validation_keeps_a_complete_old_snapshot(tmp_path, monkeypatch, probe):
    path = private(tmp_path / 'snapshot', b'old snapshot')
    staged = private(tmp_path / 'replacement', b'new snapshot')
    state, _ = probe(path)
    real_fdopen = os.fdopen

    def opened(fd, *args, **kwargs):
        stream = real_fdopen(fd, *args, **kwargs)
        if fd not in state['fds']:
            return stream

        class ReplacedOnRead:
            def fileno(self):
                return stream.fileno()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return stream.__exit__(*exc)

            def read(self, *args):
                os.replace(staged, path)
                return stream.read(*args)

        return ReplacedOnRead()

    monkeypatch.setattr(os, 'fdopen', opened)
    assert API['read_private'](path) == b'old snapshot'
    assert path.read_bytes() == b'new snapshot'
    assert state['attempts'] == 1


@pytest.mark.parametrize('side', ['fd', 'current'])
@pytest.mark.parametrize('kind', ['mode', 'hardlink'])
def test_unsafe_second_open_is_rejected_on_both_sides(tmp_path, probe, side, kind):
    path = private(tmp_path / 'snapshot', b'first safe snapshot')
    second = private(tmp_path / 'second', b'second safe snapshot')
    real_fstat = os.fstat
    observations = []

    def replace(fd, attempt):
        if attempt == 1:
            os.replace(second, path)
        elif attempt == 2:
            third = private(tmp_path / 'third', b'third snapshot')
            if side == 'fd':
                if kind == 'mode':
                    os.fchmod(fd, 0o640)
                else:
                    # Both aliases survive replacement of the path: the old
                    # FD still has nlink2 and must never justify another retry.
                    os.link(path, tmp_path / 'old-link-1')
                    os.link(path, tmp_path / 'old-link-2')
            elif kind == 'mode':
                third.chmod(0o640)
            else:
                os.link(third, tmp_path / 'new-link')
            os.replace(third, path)
            opened, current = real_fstat(fd), path.lstat()
            observations.append({
                'fd_inode': opened.st_ino, 'fd_mode': opened.st_mode & 0o777,
                'fd_nlink': opened.st_nlink, 'current_inode': current.st_ino,
                'current_mode': current.st_mode & 0o777, 'current_nlink': current.st_nlink,
            })

    state, _ = probe(path, replace)
    try:
        with pytest.raises(API['ClientError'], match='^PRIVATE_FILE_UNSAFE$'):
            API['read_private'](path)
        assert state['attempts'] == 2
        assert len(observations) == 1
        observed = observations[0]
        assert observed['fd_inode'] != observed['current_inode']
        unsafe = 'fd' if side == 'fd' else 'current'
        safe = 'current' if side == 'fd' else 'fd'
        assert observed[unsafe + ('_mode' if kind == 'mode' else '_nlink')] == (0o640 if kind == 'mode' else 2)
        assert observed[safe + '_mode'] == 0o600
        assert observed[safe + '_nlink'] == (1 if safe == 'current' else 0)
    finally:
        assert_closed(state['fds'], real_fstat)
        print('RETRY_SAFETY_EVIDENCE=' + json.dumps(dict(
            side=side, kind=kind, attempts=state['attempts'], observations=observations,
            all_fds_closed=True)))


@pytest.mark.parametrize('side', ['fd', 'current'])
@pytest.mark.parametrize('attempt', [1, 2])
def test_wrong_uid_is_rejected_on_each_attempt(tmp_path, monkeypatch, probe, side, attempt):
    path = private(tmp_path / 'snapshot', b'safe snapshot')
    staged = private(tmp_path / 'replacement', b'safe replacement')
    real_fstat, real_lstat = os.fstat, os.lstat
    injections = []

    def replace(fd, number):
        if attempt == 2 and number == 1:
            os.replace(staged, path)

    state, _ = probe(path, replace)

    def wrong_uid(info):
        class DifferentOwner:
            st_uid = info.st_uid + 1

            def __getattr__(self, name):
                return getattr(info, name)

        injections.append({'real_uid': info.st_uid, 'injected_uid': info.st_uid + 1,
                           'inode': info.st_ino, 'nlink': info.st_nlink})
        return DifferentOwner()

    def observed_fd(fd):
        info = real_fstat(fd)
        return wrong_uid(info) if fd in state['fds'] and state['attempts'] == attempt else info

    def observed_path(value, *args, **kwargs):
        info = real_lstat(value, *args, **kwargs)
        return wrong_uid(info) if os.fspath(value) == str(path) and state['attempts'] == attempt else info

    monkeypatch.setattr(os, 'fstat' if side == 'fd' else 'lstat',
                        observed_fd if side == 'fd' else observed_path)
    try:
        with pytest.raises(API['ClientError'], match='^PRIVATE_FILE_UNSAFE$'):
            API['read_private'](path)
        assert state['attempts'] == attempt
        assert len(injections) == 1
        assert injections[0]['real_uid'] == os.getuid()
    finally:
        assert_closed(state['fds'], real_fstat)
        print('UID_SAFETY_EVIDENCE=' + json.dumps(dict(
            side=side, expected_attempt=attempt, actual_attempts=state['attempts'],
            injections=injections, all_fds_closed=True)))
