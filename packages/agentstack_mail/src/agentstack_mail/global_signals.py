"""The single fenced, fd-relative signal materializer (no archive/Git writer)."""

import fcntl
import os
import stat

from .global_server import GlobalError, safe_directory, now
from .global_s2a import revision, utc
from .global_s2a_contract import canonical, unique_document
from .namespace_store import changed


def directory(path):
    safe_directory(path, create=True)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    check_directory(path, fd)
    return fd


def check_directory(path, fd):
    safe_directory(path)
    opened, current = os.fstat(fd), path.lstat()
    if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
        raise GlobalError("OUTPUT_SLOT_CONFLICT")


def existing(fd, name, expected, *, partial=False):
    try:
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or info.st_nlink != 1
    ):
        raise GlobalError("OUTPUT_SLOT_CONFLICT")
    opened = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        check = os.fstat(opened)
        if (check.st_dev, check.st_ino) != (info.st_dev, info.st_ino):
            raise GlobalError("OUTPUT_SLOT_CONFLICT")
        raw = os.read(opened, 16385)
        if len(raw) > 16384:
            raise GlobalError("OUTPUT_SLOT_CONFLICT")
    finally:
        os.close(opened)
    if partial and canonical(expected).encode().startswith(raw):
        return raw
    try:
        value = unique_document(raw)
    except (GlobalError, ValueError):
        raise GlobalError("OUTPUT_SLOT_CONFLICT") from None
    if value != expected:
        raise GlobalError("OUTPUT_SLOT_CONFLICT")
    return raw


def materialize(runtime, fd, mid, aid, desired):
    value = {
        "kind": "orrery-global-message-signal-v1",
        "version": 1,
        "server_instance_id": runtime.instance,
        "candidate_generation": runtime.candidate_generation,
        "authority_epoch": runtime.epoch,
        "recipient_agent_id": aid,
        "message_id": mid,
    }
    name = str(mid) + ".signal"
    temp = "." + name + ".tmp"
    old = existing(fd, name, value)
    # A stale own temp can only have the exact envelope for this pair/binding.
    if existing(fd, temp, value, partial=True) is not None:
        os.unlink(temp, dir_fd=fd)
    runtime.fault("signal_preflight")
    if desired and old is None:
        out = os.open(
            temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd
        )
        try:
            data = canonical(value).encode()
            offset = 0
            while offset < len(data):
                offset += os.write(out, data[offset:])
            runtime.fault("signal_after_write")
            os.fsync(out)
            runtime.fault("signal_after_fsync")
        finally:
            os.close(out)
        # Recheck the destination instead of overwriting a newly foreign role.
        existing(fd, name, value)
        os.replace(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
        runtime.fault("signal_after_rename")
    elif not desired and old is not None:
        existing(fd, name, value)
        os.unlink(name, dir_fd=fd)
        runtime.fault("signal_after_unlink")
    os.fsync(fd)
    runtime.fault("signal_after_dir_fsync")


def reconcile(runtime, *, pairs=None, batch_limit=None, retry_blocked=False):
    from .global_s2b import PENDING

    for path in (
        runtime.runtime_root / "signals",
        runtime.runtime_root / "signals/agents",
    ):
        safe_directory(path, create=True)
    root = runtime.runtime_root / "signals/agents"
    root_fd = directory(root)
    items = []
    more = False
    try:
        fcntl.flock(root_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        check_directory(root, root_fd)
        with runtime.transaction(write=pairs is not None) as db:
            if pairs is not None:
                if not 1 <= len(pairs) <= 100:
                    raise GlobalError("ARGUMENTS_UNSUPPORTED")
                for pair in pairs:
                    runtime.peer(db, pair["recipient_agent_id"])
                for pair in pairs:
                    from .global_s2b import dirty

                    dirty(db, pair["message_id"], pair["recipient_agent_id"])
                changed(db)
                selected = [(p["message_id"], p["recipient_agent_id"]) for p in pairs]
            else:
                selected = [
                    tuple(r)
                    for r in db.execute(
                        "SELECT d.message_id,d.recipient_agent_id FROM global_signal_dirty d LEFT JOIN message_recipients r ON r.message_id=d.message_id AND r.agent_id=d.recipient_agent_id LEFT JOIN agents a ON a.id=d.recipient_agent_id WHERE (d.state='pending' OR ?) AND (r.notify_after_ts IS NULL OR julianday(r.notify_after_ts)<=julianday(?) OR NOT "
                        + PENDING
                        + ") ORDER BY d.message_id,d.recipient_agent_id LIMIT ?",
                        (retry_blocked, now(), (batch_limit or 100) + 1),
                    )
                ]
                more = len(selected) > (batch_limit or 100)
                selected = selected[: batch_limit or 100]
        for mid, aid in selected:
            try:
                check_directory(root, root_fd)
                path = root / str(aid)
                fd = directory(path)
                try:
                    with runtime.transaction(write=True) as db:
                        # transaction(write=True) begins IMMEDIATE before this
                        # fact read and remains held through materialization.
                        row = db.execute(
                            "SELECT "
                            + PENDING
                            + " AS desired,r.notify_after_ts FROM message_recipients r JOIN messages m ON m.id=r.message_id JOIN agents a ON a.id=r.agent_id WHERE r.message_id=? AND r.agent_id=?",
                            (mid, aid),
                        ).fetchone()
                        desired = bool(row and row["desired"])
                        if (
                            desired
                            and row["notify_after_ts"]
                            and utc(row["notify_after_ts"]) > utc(now())
                        ):
                            items.append(
                                {
                                    "message_id": mid,
                                    "recipient_agent_id": aid,
                                    "state": "pending",
                                    "reason": None,
                                }
                            )
                            continue
                        runtime.fault("signal_after_fact")
                        materialize(runtime, fd, mid, aid, desired)
                        check_directory(root, root_fd)
                        check_directory(path, fd)
                        runtime.check_fence_files()
                        db.execute(
                            "DELETE FROM global_signal_dirty WHERE message_id=? AND recipient_agent_id=?",
                            (mid, aid),
                        )
                        changed(db)
                        runtime.fault("signal_after_dirty")
                        runtime.fault("signal_before_commit")
                    runtime.fault("signal_after_commit")
                    items.append(
                        {
                            "message_id": mid,
                            "recipient_agent_id": aid,
                            "state": "complete",
                            "reason": None,
                        }
                    )
                finally:
                    os.close(fd)
            except (OSError, GlobalError) as exc:
                reason = (
                    str(exc)
                    if str(exc) in ("OUTPUT_SLOT_CONFLICT", "OUTPUT_BINDING_CHANGED")
                    else "OUTPUT_IO_FAILED"
                )
                # A fence error must stop: do not record a result under a new
                # authority, nor suppress a mismatching binding as mere IO.
                if isinstance(exc, GlobalError) and str(exc) in (
                    "WRITER_FENCED",
                    "DATABASE_REPLACED",
                    "CONFIG_CHANGED",
                    "CANDIDATE_CHANGED",
                    "STALE_RUNTIME_BINDING",
                ):
                    raise
                with runtime.transaction(write=True) as db:
                    db.execute(
                        "UPDATE global_signal_dirty SET state='blocked',last_reason=? WHERE message_id=? AND recipient_agent_id=?",
                        (reason, mid, aid),
                    )
                    changed(db)
                items.append(
                    {
                        "message_id": mid,
                        "recipient_agent_id": aid,
                        "state": "blocked",
                        "reason": reason,
                    }
                )
        with runtime.transaction() as db:
            counts = dict(
                db.execute(
                    "SELECT state,count(*) FROM global_signal_dirty GROUP BY state"
                )
            )
            rev = revision(db)
        return {
            "items": items,
            "mutation_revision": rev,
            "pending_count": counts.get("pending", 0),
            "blocked_count": counts.get("blocked", 0),
            "more": more,
        }
    except BlockingIOError:
        raise GlobalError("OUTPUT_IO_FAILED") from None
    finally:
        os.close(root_fd)
