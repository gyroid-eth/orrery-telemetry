"""Private LaunchIntent owner storage, shared by runtime and retained children.

No registration, model selection, credential lookup or additional ledger. The
caller chooses the already admitted fixed layout; this module cannot select an
identity from a model/profile or a name.
"""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import secrets
import stat

from local_model_auth import LocalModelError, intent_digest, private_directory, secure_read


def owner_layout(owner, mode):
    owner = Path(owner)
    if not owner.is_absolute() or '..' in owner.parts:
        raise LocalModelError('LOCAL_MODEL_INTENT_OWNER_REQUIRED')
    if mode == 'global' and owner.name == 'profile.json':
        base = owner.parent
        return base, base/'runtime/child.lock', base/'runtime/launch.json', (
            base/'credential.registration-pending.json', base/'credential.enrollment-pending.json',
            base/'runtime/mutation-pending.json')
    if mode == 'legacy' and owner.parent.name == 'child-agents' and owner.suffix == '.json':
        if not owner.stem or any(c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-' for c in owner.stem):
            raise LocalModelError('LOCAL_MODEL_INTENT_OWNER_REQUIRED')
        return owner.parent, owner.parent/('.'+owner.stem+'.resume.lock'), None, (
            owner.with_name('.'+owner.stem+'.legacy-migration.json'),
            owner.with_name('.'+owner.stem+'.registration-pending.json'))
    raise LocalModelError('LOCAL_MODEL_INTENT_OWNER_REQUIRED')


def ensure_directory(path):
    """Create missing private components only beneath a verified ancestor."""
    path = Path(path)
    missing = []
    parent = path
    while not parent.exists() and not parent.is_symlink():
        missing.append(parent.name)
        parent = parent.parent
    fd = private_directory(parent, os.getuid())
    try:
        for component in reversed(missing):
            try:
                os.mkdir(component, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(component, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError('private directory required')
    finally:
        os.close(fd)


@contextmanager
def owner_lock(owner, mode):
    _, lock, _, _ = owner_layout(owner, mode)
    ensure_directory(lock.parent)
    if mode == 'global':
        # Reuse both the inode AND its publication/content protocol. Creating
        # an empty flock here would poison later RuntimeClient constructors.
        from runtime_client import ClientError, operation_inode
        try:
            with operation_inode(lock):
                yield
        except ClientError:
            raise LocalModelError('LOCAL_MODEL_INTENT_OWNER_REQUIRED') from None
        return
    directory = private_directory(lock.parent, os.getuid(), exact_mode=0o700)
    fd = None
    try:
        flags=os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK
        try:fd=os.open(lock.name,flags,dir_fd=directory)
        except FileNotFoundError:
            fd=os.open(lock.name,flags|os.O_CREAT|os.O_EXCL,0o600,dir_fd=directory)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1):
            raise ValueError('unsafe operation lock')
        fcntl.flock(fd, fcntl.LOCK_EX)
        check = os.stat(lock.name, dir_fd=directory, follow_symlinks=False)
        if (check.st_dev, check.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError('operation lock changed')
        yield
    finally:
        if fd is not None:
            os.close(fd)
        os.close(directory)


def read_owner(owner):
    value = json.loads(secure_read(owner, os.getuid(), mode=0o600, parent_mode=0o700))
    if not isinstance(value, dict):
        raise LocalModelError('LOCAL_MODEL_INTENT_CHANGED')
    return value


def checked_intent(value):
    if (not isinstance(value, dict) or value.get('schema_version') != 1
            or not isinstance(value.get('revision'), int) or isinstance(value['revision'], bool)
            or value['revision'] < 1 or value.get('intent_digest') != intent_digest(value)):
        raise LocalModelError('LOCAL_MODEL_INTENT_CHANGED')
    return deepcopy(value)


def check_idle(owner, mode):
    _, _, receipt, pending = owner_layout(owner, mode)
    if any(path.exists() or path.is_symlink() for path in pending):
        raise LocalModelError('LOCAL_MODEL_PENDING_CONFLICT')
    if mode == 'legacy' and (Path(owner).exists() or Path(owner).is_symlink()):
        state = read_owner(owner)
        if 'local_resolution' in state:
            raise LocalModelError('LOCAL_MODEL_LAUNCH_PENDING_CONFLICT')
        if 'local_execution' in state:
            from runtime_legacy_execution import execution_is_idle
            if not execution_is_idle(state['local_execution'],state.get('launch_intent')):
                raise LocalModelError('LOCAL_MODEL_LAUNCH_PENDING_CONFLICT')
        if (state.get('resume_in_progress_at') or
                ('retired_at' in state and state['retired_at'] is None)):
            raise LocalModelError('LOCAL_MODEL_LAUNCH_PENDING_CONFLICT')
    if receipt is not None and (receipt.exists() or receipt.is_symlink()):
        row = read_owner(receipt)
        if (row.get('phase') not in ('ended', 'cancelled')
                or row.get('execution', {}).get('outcome', {}).get('state') in ('pending', 'unknown')
                or ((row.get('execution') or row.get('termination'))
                    and row.get('termination', {}).get('phase') != 'ended')):
            raise LocalModelError('LOCAL_MODEL_LAUNCH_PENDING_CONFLICT')


def write_private_bytes(owner, raw):
    """CAS bytes in an admitted private slot; never expose them in errors."""
    if type(raw) is not bytes:raise ValueError('private bytes required')
    owner = Path(owner)
    ensure_directory(owner.parent)
    directory = private_directory(owner.parent, os.getuid(), exact_mode=0o700)
    temp = '.'+owner.name+'.'+secrets.token_hex(12)+'.tmp'
    try:
        before = secure_read(owner, os.getuid(), mode=0o600, parent_mode=0o700) if owner.exists() or owner.is_symlink() else None
        fd = os.open(temp, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        current = secure_read(owner, os.getuid(), mode=0o600, parent_mode=0o700) if owner.exists() or owner.is_symlink() else None
        if current != before:
            raise LocalModelError('LOCAL_MODEL_INTENT_CHANGED')
        expected = os.fstat(directory)
        actual = owner.parent.lstat()
        if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
            raise LocalModelError('LOCAL_MODEL_INTENT_CHANGED')
        os.replace(temp, owner.name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temp, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def write_owner(owner, value):
    """CAS a private JSON owner; do not follow substituted slots or parents."""
    write_private_bytes(owner,json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False).encode())


def save_launch_intent(owner, intent, *, mode, locked=False, writer=None):
    """Runtime passes its existing C/fence and validated write_output callback.

    The standalone private seam uses the same layout lock. It is not a CLI or
    an arbitrary output-path API exposed to callers.
    """
    owner = Path(owner)
    owner_layout(owner, mode)
    intent = checked_intent(intent)
    if mode == 'legacy' and owner.stem != intent.get('name'):
        raise LocalModelError('LOCAL_MODEL_IDENTITY_MISMATCH')
    def save():
        row = read_owner(owner) if owner.exists() or owner.is_symlink() else {}
        previous = row.get('launch_intent')
        if previous is not None:
            checked_intent(previous)
            if previous == intent:
                return previous
            if intent['revision'] <= previous['revision']:
                raise LocalModelError('LOCAL_MODEL_INTENT_CHANGED')
        check_idle(owner, mode)
        (writer or write_owner)(owner, {**row, 'launch_intent':intent})
        return deepcopy(intent)
    try:
        if locked:
            return save()
        with owner_lock(owner, mode):
            return save()
    except (OSError, ValueError, TypeError, KeyError):
        raise LocalModelError('LOCAL_MODEL_INTENT_OWNER_REQUIRED') from None


def load_launch_intent(owner, *, mode):
    owner_layout(owner, mode)
    try:
        return checked_intent(read_owner(owner)['launch_intent'])
    except (OSError, ValueError, TypeError, KeyError):
        raise LocalModelError('LOCAL_MODEL_INTENT_OWNER_REQUIRED') from None


def revise_launch_intent(old, caller, context, *, confirm=False):
    from runtime_launch_intent import resolve_launch_intent
    checked_intent(old)
    if confirm is not True:
        raise LocalModelError('LOCAL_MODEL_RESOLUTION_REQUIRED')
    mode = context.get('mode', 'global')
    base = context.get('client_root')
    owner = (Path(base)/'profile.json' if mode == 'global' else
             Path(base)/'child-agents'/(old['name']+'.json')) if base else None
    def revised():
        if owner is not None:
            if load_launch_intent(owner, mode=mode) != old:
                raise LocalModelError('LOCAL_MODEL_INTENT_CHANGED')
            check_idle(owner, mode)
        new = resolve_launch_intent(caller, context)
        new['revision'] = old['revision']+1
        new['intent_digest'] = intent_digest(new)
        if owner is not None:
            save_launch_intent(owner, new, mode=mode, locked=True)
        return new
    if owner is None:
        return revised()
    with owner_lock(owner, mode):
        return revised()
