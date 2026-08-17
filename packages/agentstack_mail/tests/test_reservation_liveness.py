"""The staleness sweep must not take reservations away from a working agent.

Two independent defects let it do exactly that, both observed in production on
2026-08-16 (5,058 of 12,699 reservations in the live database were released
within 120s of being granted, across five agents and several days):

1. ``last_active_ts`` was refreshed only by ``register_agent`` and by sending a
   message. An agent that reserved, renewed, and released files for an hour
   without mailing anyone looked idle after 30 minutes.
2. A pattern that matched no file on disk was treated as evidence of an idle
   holder -- indistinguishable from a file that exists and is old. The single
   most common reservation ("I am about to create this note") has nothing to
   stat until the write lands, so it was swept ~1s after grant and the write
   was then blocked for holding no reservation.

The fixes for both are load-bearing in ways a naive test misses, so the cases
below also pin the *order* of the liveness bump against the sweep, and the
point at which an unmatched pattern stops being given the benefit of the doubt.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from agentstack_mail import app
from agentstack_mail.config import clear_settings_cache
from agentstack_mail.db import ensure_schema, get_session, reset_database_state
from agentstack_mail.models import Agent, FileReservation, Project
from fastmcp import Client

PROJECT_SLUG = "reservation-liveness"
AGENT_NAME = "BlueLake"


@pytest.fixture()
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    database = tmp_path / "mail.sqlite3"
    monkeypatch.setenv("AGENTSTACK_MAIL_DATABASE_URL", f"sqlite+aiosqlite:///{database}")
    monkeypatch.setenv("AGENTSTACK_MAIL_STORAGE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv(
        "AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR", str(tmp_path / "signals")
    )
    monkeypatch.setenv("AGENTSTACK_MAIL_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setenv("AGENTSTACK_MAIL_FILE_RESERVATION_INACTIVITY_SECONDS", "600")
    monkeypatch.setenv("AGENTSTACK_MAIL_FILE_RESERVATION_ACTIVITY_GRACE_SECONDS", "300")
    monkeypatch.setenv("AGENTSTACK_MAIL_GIT_AUTHOR_NAME", "Reservation Test")
    monkeypatch.setenv("AGENTSTACK_MAIL_GIT_AUTHOR_EMAIL", "test@example.com")
    clear_settings_cache()
    reset_database_state()
    yield
    reset_database_state()
    clear_settings_cache()


async def _seed(
    workspace: Path,
    path_pattern: str,
    *,
    idle_for: timedelta,
) -> tuple[int, int]:
    """One project, one long-idle agent, one live reservation."""
    await ensure_schema()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with get_session() as session:
        project = Project(slug=PROJECT_SLUG, human_key=str(workspace))
        session.add(project)
        await session.flush()
        assert project.id is not None
        agent = Agent(
            project_id=project.id,
            name=AGENT_NAME,
            program="test",
            model="test",
            last_active_ts=now - idle_for,
        )
        session.add(agent)
        await session.flush()
        assert agent.id is not None
        reservation = FileReservation(
            project_id=project.id,
            agent_id=agent.id,
            path_pattern=path_pattern,
            expires_ts=now + timedelta(hours=1),
        )
        session.add(reservation)
        await session.commit()
        await session.refresh(reservation)
        assert reservation.id is not None
        return project.id, reservation.id


def _as_utc(naive: datetime) -> datetime:
    return naive.replace(tzinfo=timezone.utc)


def _no_archive(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_archive_write(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(app, "_write_file_reservation_records", no_archive_write)


def test_a_reservation_for_a_file_not_created_yet_survives_the_sweep(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: None,
) -> None:
    """The canonical write-a-new-note flow. No monkeypatched probe: the real one."""
    workspace = tmp_path / "workspace"
    (workspace / "05_Agents").mkdir(parents=True)
    _no_archive(monkeypatch)

    async def exercise() -> FileReservation | None:
        project_id, reservation_id = await _seed(
            workspace,
            "05_Agents/LOG_a note that does not exist yet.md",
            idle_for=timedelta(hours=2),
        )
        await app._expire_stale_file_reservations(project_id)
        async with get_session() as session:
            return await session.get(FileReservation, reservation_id)

    reservation = asyncio.run(exercise())
    assert reservation is not None
    assert reservation.released_ts is None, (
        "a reservation taken to create a file was swept before the file existed"
    )


def test_an_existing_file_gone_cold_is_still_swept(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: None,
) -> None:
    """The null case: the sweep must keep working where it has real evidence.

    Without this, 'never sweep anything' would pass the test above.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cold = workspace / "abandoned.md"
    cold.write_text("old\n", encoding="utf-8")
    stale_mtime = (datetime.now(timezone.utc) - timedelta(hours=3)).timestamp()
    os.utime(cold, (stale_mtime, stale_mtime))
    _no_archive(monkeypatch)

    async def exercise() -> FileReservation | None:
        project_id, reservation_id = await _seed(
            workspace,
            "abandoned.md",
            idle_for=timedelta(hours=2),
        )
        await app._expire_stale_file_reservations(project_id)
        async with get_session() as session:
            return await session.get(FileReservation, reservation_id)

    reservation = asyncio.run(exercise())
    assert reservation is not None
    assert reservation.released_ts is not None, (
        "an idle agent holding an untouched file should still be swept"
    )


def _last_active(project_id: int) -> datetime:
    async def read() -> datetime:
        async with get_session() as session:
            agent = await session.get(Agent, 1)
            assert agent is not None and agent.last_active_ts is not None
            return agent.last_active_ts

    return asyncio.run(read())


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        (
            "file_reservation_paths",
            {"paths": ["docs/thing.md"], "ttl_seconds": 600},
        ),
        (
            "renew_file_reservations",
            {"paths": ["docs/thing.md"], "extend_seconds": 600},
        ),
        (
            "release_file_reservations",
            {"paths": ["docs/thing.md"]},
        ),
    ],
)
def test_reservation_traffic_counts_as_being_alive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: None,
    tool_name: str,
    arguments: dict[str, Any],
) -> None:
    """Holding files is proof of life, even from an agent that mails no one."""
    workspace = tmp_path / "workspace"
    (workspace / "docs").mkdir(parents=True)
    (workspace / "docs" / "thing.md").write_text("x\n", encoding="utf-8")
    _no_archive(monkeypatch)

    async def exercise() -> tuple[datetime, datetime]:
        project_id, _ = await _seed(
            workspace, "docs/thing.md", idle_for=timedelta(hours=2)
        )
        before = _stamp_of(await _read_agent())
        async with Client(app.build_mcp_server()) as client:
            result = await client.call_tool(
                tool_name,
                {
                    "project_key": str(workspace),
                    "agent_name": AGENT_NAME,
                    **arguments,
                },
                raise_on_error=False,
            )
            assert result.is_error is False, result.content
        del project_id
        return before, _stamp_of(await _read_agent())

    async def _read_agent() -> Agent:
        async with get_session() as session:
            agent = await session.get(Agent, 1)
            assert agent is not None
            return agent

    def _stamp_of(agent: Agent) -> datetime:
        assert agent.last_active_ts is not None
        return agent.last_active_ts

    before, after = asyncio.run(exercise())
    assert after > before, (
        f"{tool_name} left last_active_ts untouched, so the sweep will keep "
        "treating a working agent as idle"
    )
    assert (datetime.now(timezone.utc).replace(tzinfo=None) - after) < timedelta(minutes=5)


def test_granting_a_reservation_does_not_sweep_the_agents_older_ones(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: None,
) -> None:
    """Pins the *order* of the liveness bump against the sweep.

    ``file_reservation_paths`` sweeps before it grants. If the bump happens
    after that sweep instead of before it, the caller's own older reservations
    are collected on the way in -- and every assertion about last_active_ts
    still passes, because the bump did happen. Only the casualty shows it.
    """
    workspace = tmp_path / "workspace"
    (workspace / "docs").mkdir(parents=True)
    cold = workspace / "docs" / "held.md"
    cold.write_text("old\n", encoding="utf-8")
    stale_mtime = (datetime.now(timezone.utc) - timedelta(hours=3)).timestamp()
    os.utime(cold, (stale_mtime, stale_mtime))
    _no_archive(monkeypatch)

    async def exercise() -> FileReservation | None:
        _project_id, held_id = await _seed(
            workspace, "docs/held.md", idle_for=timedelta(hours=2)
        )
        async with Client(app.build_mcp_server()) as client:
            result = await client.call_tool(
                "file_reservation_paths",
                {
                    "project_key": str(workspace),
                    "agent_name": AGENT_NAME,
                    "paths": ["docs/another.md"],
                    "ttl_seconds": 600,
                },
                raise_on_error=False,
            )
            assert result.is_error is False, result.content
        async with get_session() as session:
            return await session.get(FileReservation, held_id)

    held = asyncio.run(exercise())
    assert held is not None
    assert held.released_ts is None, (
        "reserving a new path swept the caller's own existing reservation: "
        "the liveness bump ran after the sweep instead of before it"
    )


def test_a_concurrent_sweep_cannot_release_an_agent_that_just_proved_itself(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: None,
) -> None:
    """The decision is made from a snapshot; the write must re-check it.

    A second caller's sweep reads the holder as idle, then spends time in the
    filesystem and git probes. If the holder becomes active in that window, the
    release must not land -- the snapshot is stale.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cold = workspace / "abandoned.md"
    cold.write_text("old\n", encoding="utf-8")
    stale_mtime = (datetime.now(timezone.utc) - timedelta(hours=3)).timestamp()
    os.utime(cold, (stale_mtime, stale_mtime))
    _no_archive(monkeypatch)

    real_probe = app._probe_reservation_activities

    async def exercise() -> FileReservation | None:
        project_id, reservation_id = await _seed(
            workspace, "abandoned.md", idle_for=timedelta(hours=2)
        )

        async def probe_then_wake_the_holder(*args: Any, **kwargs: Any) -> Any:
            results = await real_probe(*args, **kwargs)
            # The holder comes back to life mid-probe, exactly as it would by
            # reserving or renewing anything from its own session.
            async with get_session() as session:
                agent = await session.get(Agent, 1)
                assert agent is not None
                await app._touch_agent_activity(agent)
            return results

        monkeypatch.setattr(
            app, "_probe_reservation_activities", probe_then_wake_the_holder
        )
        await app._expire_stale_file_reservations(project_id)
        async with get_session() as session:
            return await session.get(FileReservation, reservation_id)

    reservation = asyncio.run(exercise())
    assert reservation is not None
    assert reservation.released_ts is None, (
        "a sweep released a reservation whose holder became active while the "
        "sweep was still probing"
    )


def test_an_unmatched_pattern_stops_being_excused_after_the_grace_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: None,
) -> None:
    """Otherwise a typo or a broad glob squats on an exclusive lease until TTL.

    ``**/*`` over an empty workspace matches nothing, so under a blanket
    'unmatched means unknown' rule it would block the creation of every file in
    the project for the whole TTL (an hour by default, renewable forever).
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _no_archive(monkeypatch)

    async def exercise() -> FileReservation | None:
        project_id, reservation_id = await _seed(
            workspace, "**/*", idle_for=timedelta(hours=2)
        )
        # Older than the 300s activity grace this fixture configures.
        async with get_session() as session:
            reservation = await session.get(FileReservation, reservation_id)
            assert reservation is not None
            reservation.created_ts = datetime.now(timezone.utc).replace(
                tzinfo=None
            ) - timedelta(hours=1)
            await session.commit()
        await app._expire_stale_file_reservations(project_id)
        async with get_session() as session:
            return await session.get(FileReservation, reservation_id)

    reservation = asyncio.run(exercise())
    assert reservation is not None
    assert reservation.released_ts is not None, (
        "an unmatched pattern held by an idle agent squatted past the grace "
        "window instead of being swept"
    )


def test_a_late_bump_cannot_move_last_active_backwards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: None,
) -> None:
    """``now`` is sampled before the writer lock, so ordering is not guaranteed.

    A bump that waited behind a newer write must not commit its older stamp
    last: every consumer of last_active_ts (the sweep, broadcast recipient
    selection, the dashboard) reads it as "how recently was this agent alive".
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    async def exercise() -> tuple[datetime, datetime]:
        await _seed(workspace, "docs/thing.md", idle_for=timedelta(hours=2))
        newer = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=5)
        async with get_session() as session:
            agent = await session.get(Agent, 1)
            assert agent is not None
            agent.last_active_ts = newer
            await session.commit()
        async with get_session() as session:
            agent = await session.get(Agent, 1)
            assert agent is not None
            await app._touch_agent_activity(agent)
        async with get_session() as session:
            agent = await session.get(Agent, 1)
            assert agent is not None and agent.last_active_ts is not None
            return newer, agent.last_active_ts

    newer, final = asyncio.run(exercise())
    assert final == newer, (
        "a bump sampled before the writer lock overwrote a newer stamp, making "
        "a live agent look less recently active than it is"
    )


def test_the_macro_cycle_counts_as_liveness_too(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: None,
) -> None:
    """The macro is what the documented hooks actually call."""
    workspace = tmp_path / "workspace"
    (workspace / "docs").mkdir(parents=True)
    (workspace / "docs" / "thing.md").write_text("x\n", encoding="utf-8")
    _no_archive(monkeypatch)

    async def exercise() -> tuple[datetime, datetime]:
        await _seed(workspace, "docs/other.md", idle_for=timedelta(hours=2))
        async with get_session() as session:
            agent = await session.get(Agent, 1)
            assert agent is not None and agent.last_active_ts is not None
            before = agent.last_active_ts
        async with Client(app.build_mcp_server()) as client:
            result = await client.call_tool(
                "macro_file_reservation_cycle",
                {
                    "project_key": str(workspace),
                    "agent_name": AGENT_NAME,
                    "paths": ["docs/thing.md"],
                    "ttl_seconds": 600,
                },
                raise_on_error=False,
            )
            assert result.is_error is False, result.content
        async with get_session() as session:
            agent = await session.get(Agent, 1)
            assert agent is not None and agent.last_active_ts is not None
            return before, agent.last_active_ts

    before, after = asyncio.run(exercise())
    assert after > before


def test_a_no_op_release_does_not_resurrect_a_dormant_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: None,
) -> None:
    """Cleanup traffic from a finished agent is not evidence it is working.

    last_active_ts also decides who is still a broadcast recipient, so a
    leftover release that frees nothing must not put a dormant identity back
    into that set.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _no_archive(monkeypatch)

    async def exercise() -> tuple[datetime, datetime]:
        await _seed(workspace, "docs/held.md", idle_for=timedelta(hours=2))
        async with get_session() as session:
            agent = await session.get(Agent, 1)
            assert agent is not None and agent.last_active_ts is not None
            before = agent.last_active_ts
        async with Client(app.build_mcp_server()) as client:
            result = await client.call_tool(
                "release_file_reservations",
                {
                    "project_key": str(workspace),
                    "agent_name": AGENT_NAME,
                    "paths": ["docs/nothing-here.md"],
                },
                raise_on_error=False,
            )
            assert result.is_error is False, result.content
            assert (result.structured_content or {}).get("released") == 0
        async with get_session() as session:
            agent = await session.get(Agent, 1)
            assert agent is not None and agent.last_active_ts is not None
            return before, agent.last_active_ts

    before, after = asyncio.run(exercise())
    assert after == before, "a release that freed nothing still marked the agent alive"


def test_a_no_op_renew_does_not_resurrect_a_dormant_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: None,
) -> None:
    """The renew half of the rule the no-op release test pins."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _no_archive(monkeypatch)

    async def exercise() -> tuple[datetime, datetime]:
        await _seed(workspace, "docs/held.md", idle_for=timedelta(hours=2))
        async with get_session() as session:
            agent = await session.get(Agent, 1)
            assert agent is not None and agent.last_active_ts is not None
            before = agent.last_active_ts
        async with Client(app.build_mcp_server()) as client:
            result = await client.call_tool(
                "renew_file_reservations",
                {
                    "project_key": str(workspace),
                    "agent_name": AGENT_NAME,
                    "paths": ["docs/nothing-here.md"],
                    "extend_seconds": 600,
                },
                raise_on_error=False,
            )
            assert result.is_error is False, result.content
            assert (result.structured_content or {}).get("renewed") == 0
        async with get_session() as session:
            agent = await session.get(Agent, 1)
            assert agent is not None and agent.last_active_ts is not None
            return before, agent.last_active_ts

    before, after = asyncio.run(exercise())
    assert after == before, "a renew that extended nothing still marked the agent alive"


def test_renewing_does_not_restart_the_awaiting_first_write_grace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: None,
) -> None:
    """``created_ts`` is what decides the grace, so renew must not touch it.

    If renew reset it, an unmatched pattern could be kept 'young' forever and
    squat on an exclusive lease -- the squatting this grace exists to end.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _no_archive(monkeypatch)

    async def exercise() -> tuple[datetime, datetime, FileReservation | None]:
        project_id, reservation_id = await _seed(
            workspace, "**/*", idle_for=timedelta(hours=2)
        )
        old_created = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
        async with get_session() as session:
            reservation = await session.get(FileReservation, reservation_id)
            assert reservation is not None
            reservation.created_ts = old_created
            await session.commit()
        async with Client(app.build_mcp_server()) as client:
            result = await client.call_tool(
                "renew_file_reservations",
                {
                    "project_key": str(workspace),
                    "agent_name": AGENT_NAME,
                    "extend_seconds": 3600,
                },
                raise_on_error=False,
            )
            assert result.is_error is False, result.content
            assert (result.structured_content or {}).get("renewed") == 1
        async with get_session() as session:
            reservation = await session.get(FileReservation, reservation_id)
            assert reservation is not None
            created_after_renew = reservation.created_ts
        # Renewing counts as liveness, so put the agent back to sleep before
        # asking whether the *pattern* still earns the grace.
        async with get_session() as session:
            agent = await session.get(Agent, 1)
            assert agent is not None
            agent.last_active_ts = datetime.now(timezone.utc).replace(
                tzinfo=None
            ) - timedelta(hours=2)
            await session.commit()
        await app._expire_stale_file_reservations(project_id)
        async with get_session() as session:
            return old_created, created_after_renew, await session.get(
                FileReservation, reservation_id
            )

    old_created, created_after_renew, reservation = asyncio.run(exercise())
    assert created_after_renew == old_created, "renew moved created_ts forward"
    assert reservation is not None
    assert reservation.released_ts is not None, (
        "a renewed but never-written pattern kept its grace and squatted"
    )


def test_a_spared_reservation_is_not_reported_as_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: None,
) -> None:
    """The sweep's own return value must agree with what it left in the DB."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cold = workspace / "abandoned.md"
    cold.write_text("old\n", encoding="utf-8")
    stale_mtime = (datetime.now(timezone.utc) - timedelta(hours=3)).timestamp()
    os.utime(cold, (stale_mtime, stale_mtime))
    _no_archive(monkeypatch)

    real_probe = app._probe_reservation_activities
    idle_stamp = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)

    archived: list[Any] = []

    async def record_archive(_project: Any, pairs: Any, *_args: Any, **_kwargs: Any) -> None:
        archived.append(pairs)

    monkeypatch.setattr(app, "_write_file_reservation_records", record_archive)

    async def exercise() -> tuple[Any, FileReservation | None, list[Any]]:
        project_id, reservation_id = await _seed(
            workspace, "abandoned.md", idle_for=timedelta(hours=2)
        )

        async def probe_then_wake_the_holder(*args: Any, **kwargs: Any) -> Any:
            results = await real_probe(*args, **kwargs)
            async with get_session() as session:
                agent = await session.get(Agent, 1)
                assert agent is not None
                await app._touch_agent_activity(agent)
            return results

        monkeypatch.setattr(
            app, "_probe_reservation_activities", probe_then_wake_the_holder
        )
        sweep = await app._expire_stale_file_reservations(project_id)
        async with get_session() as session:
            return sweep, await session.get(FileReservation, reservation_id), archived

    sweep, reservation, archived = asyncio.run(exercise())
    assert reservation is not None and reservation.released_ts is None
    statuses = [s for s in sweep.statuses if s.reservation.id == reservation.id]
    assert statuses, "the spared reservation vanished from the reported statuses"
    assert statuses[0].stale is False, (
        "a reservation the sweep decided to spare is still reported as stale"
    )
    assert "agent_became_active_during_sweep" in statuses[0].stale_reasons
    # Everything downstream must agree that nothing was collected: the caller
    # reads auto_released, the in-memory row is what archive records are built
    # from, and the archive writer must not have run at all.
    assert sweep.auto_released == [], "the sweep reported releasing a row it spared"
    assert statuses[0].reservation.released_ts is None, (
        "the in-memory row was stamped released even though the database row lives"
    )
    assert archived == [], "archive records were written for a reservation that was spared"
    assert statuses[0].last_agent_activity is not None
    age = datetime.now(timezone.utc) - statuses[0].last_agent_activity
    assert age < timedelta(minutes=1), (
        "the spared status reports the hours-old snapshot the verdict was made "
        f"from, not the activity that spared it (age={age})"
    )


def test_releasing_one_path_does_not_expose_the_agents_other_reservations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: None,
) -> None:
    """Pins the liveness bump ahead of the archive write inside release.

    Release commits to the database, then writes git artifacts, and only then
    was the agent marked alive. A sweeper running in that window still sees the
    old timestamp and collects this agent's *other* reservations -- so a call
    that proves the agent is working becomes the moment its holdings are taken.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name in ("one.md", "two.md"):
        target = workspace / name
        target.write_text("old\n", encoding="utf-8")
        stale_mtime = (datetime.now(timezone.utc) - timedelta(hours=3)).timestamp()
        os.utime(target, (stale_mtime, stale_mtime))

    async def exercise() -> FileReservation | None:
        project_id, kept_id = await _seed(
            workspace, "two.md", idle_for=timedelta(hours=2)
        )
        async with get_session() as session:
            agent = await session.get(Agent, 1)
            assert agent is not None
            released_target = FileReservation(
                project_id=project_id,
                agent_id=agent.id,
                path_pattern="one.md",
                expires_ts=datetime.now(timezone.utc).replace(tzinfo=None)
                + timedelta(hours=1),
            )
            session.add(released_target)
            await session.commit()

        async def sweep_during_the_archive_write(*_args: Any, **_kwargs: Any) -> None:
            # A second caller sweeps while this release is still writing git
            # artifacts. Any reservation tool call from another agent does this.
            await app._expire_stale_file_reservations(project_id)

        monkeypatch.setattr(
            app, "_write_file_reservation_records", sweep_during_the_archive_write
        )
        async with Client(app.build_mcp_server()) as client:
            result = await client.call_tool(
                "release_file_reservations",
                {
                    "project_key": str(workspace),
                    "agent_name": AGENT_NAME,
                    "paths": ["one.md"],
                },
                raise_on_error=False,
            )
            assert result.is_error is False, result.content
            assert (result.structured_content or {}).get("released") == 1
        async with get_session() as session:
            return await session.get(FileReservation, kept_id)

    kept = asyncio.run(exercise())
    assert kept is not None
    assert kept.released_ts is None, (
        "a sweep running during release's archive write collected the "
        "releasing agent's other reservation"
    )


def test_no_sweepable_gap_between_the_release_write_and_the_liveness_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: None,
) -> None:
    """Both writes must land in *one* commit, not two.

    Moving the liveness bump earlier is not enough, and neither is inlining it:
    if the release commits before the bump, a sweep in that window still reads
    the old timestamp and collects the agent's other reservations. So drive a
    sweep after every commit the release makes. With a single transaction there
    is exactly one commit and the agent is already fresh by then; with two, the
    first one exposes the gap.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name in ("one.md", "two.md"):
        target = workspace / name
        target.write_text("old\n", encoding="utf-8")
        stale_mtime = (datetime.now(timezone.utc) - timedelta(hours=3)).timestamp()
        os.utime(target, (stale_mtime, stale_mtime))
    _no_archive(monkeypatch)

    commits: list[int] = []

    async def exercise() -> FileReservation | None:
        project_id, kept_id = await _seed(
            workspace, "two.md", idle_for=timedelta(hours=2)
        )
        async with get_session() as session:
            agent = await session.get(Agent, 1)
            assert agent is not None
            session.add(
                FileReservation(
                    project_id=project_id,
                    agent_id=agent.id,
                    path_pattern="one.md",
                    expires_ts=datetime.now(timezone.utc).replace(tzinfo=None)
                    + timedelta(hours=1),
                )
            )
            await session.commit()

        real_get_session = app.get_session
        sweeping = {"busy": False}

        class SweepAfterEveryCommit:
            """Wraps the release's session so each commit is followed by a sweep."""

            def __init__(self, inner: Any) -> None:
                self._inner = inner
                self._session: Any = None

            async def __aenter__(self) -> Any:
                self._session = await self._inner.__aenter__()
                real_commit = self._session.commit

                async def commit_then_sweep(*args: Any, **kwargs: Any) -> Any:
                    result = await real_commit(*args, **kwargs)
                    commits.append(1)
                    if not sweeping["busy"]:
                        # Anything another agent does with reservations sweeps
                        # here. The guard keeps the sweep's own commits from
                        # recursing back into this wrapper.
                        sweeping["busy"] = True
                        try:
                            await app._expire_stale_file_reservations(project_id)
                        finally:
                            sweeping["busy"] = False
                    return result

                self._session.commit = commit_then_sweep
                return self._session

            async def __aexit__(self, *args: Any) -> Any:
                return await self._inner.__aexit__(*args)

        # Every session, not just the first: the release tool opens several
        # (project lookup, agent lookup) before the one that matters.
        def sweeping_session(*args: Any, **kwargs: Any) -> Any:
            return SweepAfterEveryCommit(real_get_session(*args, **kwargs))

        async with Client(app.build_mcp_server()) as client:
            monkeypatch.setattr(app, "get_session", sweeping_session)
            try:
                result = await client.call_tool(
                    "release_file_reservations",
                    {
                        "project_key": str(workspace),
                        "agent_name": AGENT_NAME,
                        "paths": ["one.md"],
                    },
                    raise_on_error=False,
                )
                assert result.is_error is False, result.content
                assert (result.structured_content or {}).get("released") == 1
            finally:
                monkeypatch.setattr(app, "get_session", real_get_session)
        async with get_session() as session:
            return await session.get(FileReservation, kept_id)

    kept = asyncio.run(exercise())
    assert commits, "the release never committed; the wrapper did not take effect"
    assert kept is not None
    assert kept.released_ts is None, (
        "a sweep running between the release's commits collected the agent's "
        "other reservation: the release and the liveness write are not atomic"
    )


def test_release_reports_only_the_rows_it_actually_released(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: None,
) -> None:
    """A row someone else released first is not this call's work.

    Counting the selection instead of the update claims work that did not
    happen and writes archive records stamped with a time that is not the one
    in the database. The competing release is done on a second connection, so
    it really does land between this call's SELECT and its UPDATE.
    """
    import sqlite3

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "one.md").write_text("x\n", encoding="utf-8")
    archived: list[Any] = []

    async def record_archive(_project: Any, pairs: Any, *_a: Any, **_kw: Any) -> None:
        archived.append(pairs)

    monkeypatch.setattr(app, "_write_file_reservation_records", record_archive)

    real_overlap = app._patterns_overlap

    async def exercise() -> tuple[Any, list[Any]]:
        _project_id, reservation_id = await _seed(
            workspace, "one.md", idle_for=timedelta(hours=2)
        )
        stolen = {"done": False}

        def steal_then_match(*args: Any, **kwargs: Any) -> Any:
            if not stolen["done"]:
                stolen["done"] = True
                connection = sqlite3.connect(str(tmp_path / "mail.sqlite3"))
                try:
                    connection.execute(
                        "UPDATE file_reservations SET released_ts = ? WHERE id = ?",
                        (
                            datetime.now(timezone.utc)
                            .replace(tzinfo=None)
                            .isoformat(sep=" "),
                            reservation_id,
                        ),
                    )
                    connection.commit()
                finally:
                    connection.close()
            return real_overlap(*args, **kwargs)

        monkeypatch.setattr(app, "_patterns_overlap", steal_then_match)
        async with Client(app.build_mcp_server()) as client:
            result = await client.call_tool(
                "release_file_reservations",
                {
                    "project_key": str(workspace),
                    "agent_name": AGENT_NAME,
                    "paths": ["one.md"],
                },
                raise_on_error=False,
            )
            assert result.is_error is False, result.content
        assert stolen["done"], "the competing release never ran"
        return result.structured_content or {}, archived

    payload, records = asyncio.run(exercise())
    assert payload.get("released") == 0, (
        "reported releasing a row that another caller had already released"
    )
    assert records == [], "wrote archive records for rows it did not release"


def test_a_competitor_writing_the_same_microsecond_is_not_claimed_as_ours(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: None,
) -> None:
    """Ownership of a released row cannot be inferred from its timestamp.

    Identifying "rows this call released" by the stamp it wrote also matches
    rows a *different* caller stamped in the same microsecond. Measured on this
    machine: 673 duplicate values per 500,000 samples of the clock in use, so
    this is a real collision, not a theoretical one. The competitor here is
    forced onto the exact stamp to make it deterministic.
    """
    import sqlite3

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "one.md").write_text("x\n", encoding="utf-8")
    archived: list[Any] = []

    async def record_archive(_project: Any, pairs: Any, *_a: Any, **_kw: Any) -> None:
        archived.append(pairs)

    monkeypatch.setattr(app, "_write_file_reservation_records", record_archive)

    real_naive_utc = app._naive_utc
    real_overlap = app._patterns_overlap
    stamps: list[datetime] = []

    def recording_naive_utc(*args: Any, **kwargs: Any) -> Any:
        value = real_naive_utc(*args, **kwargs)
        stamps.append(value)
        return value

    async def exercise() -> tuple[Any, list[Any], FileReservation | None]:
        _project_id, reservation_id = await _seed(
            workspace, "one.md", idle_for=timedelta(hours=2)
        )
        stolen = {"done": False}

        def steal_with_the_same_stamp(*args: Any, **kwargs: Any) -> Any:
            # Runs after the tool has computed naive_now and selected the row,
            # and before its UPDATE.
            if not stolen["done"] and stamps:
                stolen["done"] = True
                connection = sqlite3.connect(str(tmp_path / "mail.sqlite3"))
                try:
                    connection.execute(
                        "UPDATE file_reservations SET released_ts = ? WHERE id = ?",
                        (stamps[-1].isoformat(sep=" "), reservation_id),
                    )
                    connection.commit()
                finally:
                    connection.close()
            return real_overlap(*args, **kwargs)

        monkeypatch.setattr(app, "_naive_utc", recording_naive_utc)
        monkeypatch.setattr(app, "_patterns_overlap", steal_with_the_same_stamp)
        async with Client(app.build_mcp_server()) as client:
            result = await client.call_tool(
                "release_file_reservations",
                {
                    "project_key": str(workspace),
                    "agent_name": AGENT_NAME,
                    "paths": ["one.md"],
                },
                raise_on_error=False,
            )
            assert result.is_error is False, result.content
        assert stolen["done"], "the competing release never ran"
        monkeypatch.setattr(app, "_naive_utc", real_naive_utc)
        async with get_session() as session:
            return (
                result.structured_content or {},
                archived,
                await session.get(FileReservation, reservation_id),
            )

    payload, records, reservation = asyncio.run(exercise())
    assert reservation is not None and reservation.released_ts is not None
    assert payload.get("released") == 0, (
        "claimed a row another caller released with the same microsecond stamp"
    )
    assert records == [], "wrote archive records for a row it did not release"
