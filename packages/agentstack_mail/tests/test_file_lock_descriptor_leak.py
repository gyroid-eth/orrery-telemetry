"""The lock must give its descriptor back, whichever thread releases it.

`AsyncFileLock` acquires and releases through `asyncio.to_thread`, which draws
from a pool, so the two halves routinely run on different workers. filelock's
default counter is thread-local: a release on a foreign worker sees a count of
zero and returns without closing the file. The unlink that follows then removes
the name while the descriptor stays open, which is why the leak is invisible on
disk and only shows up under `lsof +L1` — a live server had accumulated 140
unlinked lock descriptors after two days of ordinary traffic (2026-08-24).

The check is descriptor count, not lock semantics: the old code passed every
mutual-exclusion test it had while leaking on every release.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from agentstack_mail.storage import AsyncFileLock


def _open_descriptors() -> int:
    """Descriptors this process holds. /dev/fd is the portable POSIX view."""
    return len(os.listdir("/dev/fd"))


@pytest.mark.skipif(not Path("/dev/fd").is_dir(), reason="needs /dev/fd")
def test_locks_do_not_leak_descriptors_when_release_lands_on_another_thread(
    tmp_path: Path,
) -> None:
    async def churn(count: int) -> None:
        # Concurrency is what puts acquire and release on different pool
        # workers. A sequential loop often reuses one worker and hides the bug,
        # which is exactly how it survived until a server ran out of files.
        async def once(index: int) -> None:
            async with AsyncFileLock(tmp_path / f"lock-{index}", timeout_seconds=30.0):
                await asyncio.sleep(0)

        await asyncio.gather(*(once(index) for index in range(count)))

    asyncio.run(churn(4))  # warm the pool so its own descriptors are not counted
    before = _open_descriptors()
    asyncio.run(churn(60))
    after = _open_descriptors()

    assert after - before <= 4, (
        f"leaked descriptors: {before} -> {after}. "
        "A release that runs on a different thread than the acquire must still "
        "close the file (SoftFileLock(..., thread_local=False))."
    )
    assert not list(tmp_path.glob("lock-*")), "lock files must be unlinked too"
