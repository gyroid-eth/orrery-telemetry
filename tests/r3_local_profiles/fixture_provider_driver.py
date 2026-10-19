"""Fixed external-command seam for caller tests, never native acceptance.

The approved source digest continues to bind this fixed driver. A variable
capture script is independently checked, not substituted for production pinning.
This does not prove that an actual Claude/Codex binary was started. The small
check-to-exec race is accepted only inside the owned temporary fixture root with
0500 scripts; it is not a product security boundary or permission engine.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import sys


def checked_bytes(path, uid, mode, *, label='resource'):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if info.st_uid != uid:
            raise ValueError('fixture '+label+' UID mismatch')
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != mode or info.st_size > 1048576):
            raise ValueError('unsafe fixture resource')
        data = b''
        while chunk := os.read(fd, 65536):
            data += chunk
            if len(data) > 1048576:
                raise ValueError('oversized fixture resource')
        return data
    finally:
        os.close(fd)


def run(driver, *, expected_uid=None):
    driver = Path(driver)
    uid = os.getuid() if expected_uid is None else expected_uid
    root = driver.parent.parent
    directory = root / 'captures'
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError('unsafe fixture directory')
    dispatch = json.loads(checked_bytes(driver.with_name('fixture-provider-dispatch.json'), uid, 0o600,
                                         label='dispatch'))
    if set(dispatch) != {'script', 'sha256'}:
        raise ValueError('invalid fixture dispatch')
    script = Path(dispatch['script'])
    if (not script.is_absolute() or '..' in script.parts or str(script) != dispatch['script']
            or script.parent != directory or script.suffix != '.py'):
        raise ValueError('fixture script outside captures')
    data = checked_bytes(script, uid, 0o500,label='script')
    if hashlib.sha256(data).hexdigest() != dispatch['sha256']:
        raise ValueError('fixture script changed')
    driver_digest = hashlib.sha256(checked_bytes(driver, uid, 0o500,label='driver')).hexdigest()
    witness = script.with_suffix('.driver.json')
    fd = os.open(witness, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump({'pid':os.getpid(), 'driver':str(driver), 'driver_sha256':driver_digest,
                   'script':str(script), 'script_sha256':dispatch['sha256']}, stream)
    # argv[1:], env, cwd and PID survive. No dispatch or nonce env is added.
    os.execv(sys.executable, [sys.executable, str(script), *sys.argv[1:]])


if __name__ == '__main__':
    try:
        run(Path(sys.argv[0]))
    except (OSError, ValueError, KeyError, TypeError):
        sys.exit(2)
