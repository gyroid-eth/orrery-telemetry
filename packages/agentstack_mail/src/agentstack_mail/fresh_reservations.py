"""Fresh absolute leases on the existing ORM, under B1's owner transaction."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import insert, select

from . import storage_origin_directory as directory
from .global_reservations import expiry
from .models import FileReservation
from .reservation_paths import ReservationError, normalize_paths, paths_overlap
from .schema_contract import ContractError
from .storage_origin_contract import B1Error, canonical, digest, integer, string


def paths(values):
    try:
        result = list(dict.fromkeys(normalize_paths(values)))
        if any(path.kind != 'filesystem' for path in result):
            raise B1Error('ABSOLUTE_PATH_REQUIRED')
        return result
    except ReservationError as error:
        raise B1Error(str(error)) from None


def overlaps(left, right):
    try:
        return paths_overlap(left, right)
    except ReservationError as error:
        raise B1Error(str(error)) from None


def validate_ttl(seconds):
    # Keep the existing >=60-second lease policy; do not add a one-day cap.
    if type(seconds) is not int or seconds < 60:
        raise B1Error('TTL_OUT_OF_RANGE')


def until(stamp, seconds):
    try:
        return datetime.fromisoformat(expiry(stamp, seconds))
    except ContractError as error:
        raise B1Error(str(error)) from None


def view(row):
    return dict(id=row.id, owner_agent_id=row.agent_id, path_pattern=row.path_pattern,
                exclusive=row.exclusive, reason=row.reason, created_ts=row.created_ts.isoformat(),
                expires_ts=row.expires_ts.isoformat(),
                released_ts=row.released_ts.isoformat() if row.released_ts else None)


async def active(session, stamp):
    return (await session.execute(select(FileReservation).where(
        FileReservation.released_ts.is_(None), FileReservation.expires_ts > stamp
    ).order_by(FileReservation.id))).scalars().all()


async def selected(session, actor_id, stamp, *, values=None, ids=None):
    if values is not None and ids is not None:
        raise B1Error('LEASE_SELECTOR_CONFLICT')
    if ids is not None:
        if not isinstance(ids, list) or not 1 <= len(ids) <= 100:
            raise B1Error('LEASE_SELECTOR_INVALID')
        rows = []
        for lease_id in dict.fromkeys(integer(value) for value in ids):
            row = await session.get(FileReservation, lease_id)
            if row is None:
                raise B1Error('LEASE_NOT_FOUND')
            rows.append(row)
    else:
        rows = [row for row in await active(session, stamp) if row.agent_id == actor_id]
        if values is not None:
            wanted = {path.value for path in paths(values)}
            rows = [row for row in rows if row.path_pattern in wanted]
            if wanted - {row.path_pattern for row in rows}:
                raise B1Error('LEASE_NOT_FOUND')
    if any(row.agent_id != actor_id for row in rows):
        raise B1Error('LEASE_OWNER_REQUIRED')
    if len(rows) > 100:
        raise B1Error('LEASE_SELECTION_TOO_LARGE')
    return rows


async def apply(core, operation, *, expected_server_instance_id, auth, request_id=None,
                paths_input=None, file_reservation_ids=None, ttl_seconds=3600,
                extend_seconds=1800, exclusive=True, reason=''):
    if operation == 'guard':
        return await guard(core, expected_server_instance_id, auth, paths_input, extend_seconds)
    if operation not in {'reserve', 'renew', 'release', 'check'}:
        raise B1Error('OPERATION_UNPUBLISHED')
    if type(exclusive) is not bool or not isinstance(reason, str) or len(reason) > 512:
        raise B1Error('INVALID_RESERVATION')
    if operation in {'reserve', 'renew'}:
        validate_ttl(ttl_seconds if operation == 'reserve' else extend_seconds)
    if operation != 'check':
        string(request_id)
    intent = dict(paths=paths_input, file_reservation_ids=file_reservation_ids,
                  ttl_seconds=ttl_seconds, extend_seconds=extend_seconds, exclusive=exclusive, reason=reason)
    try:
        intent_hash = digest(intent)
    except (ValueError, TypeError):
        raise B1Error('INVALID_RESERVATION') from None
    failure = None
    async with core.owner_transaction(expected_server_instance_id, auth) as ctx:
        session, state, actor = ctx.session, ctx.state, ctx.actor
        ctx.assert_live(session)
        selector = dict(instance_id=state['instance_id'], actor_id=actor.id,
                        operation='fresh-lease-' + operation, request_id=request_id)
        if operation != 'check':
            previous = (await session.execute(select(directory.intents).where(
                *[directory.intents.c[key] == value for key, value in selector.items()]
            ))).mappings().first()
            if previous:
                if previous['intent_hash'] != intent_hash:
                    raise B1Error('REQUEST_INTENT_CONFLICT')
                outcome = json.loads(previous['outcome'])
                if outcome['status'] == 'refused':
                    raise B1Error(outcome['code'], **outcome['details'])
                result = outcome['result']
                for saved in result['reservations']:
                    row = await session.get(FileReservation, saved['id'])
                    if row is None or row.agent_id != actor.id or row.path_pattern != saved['path_pattern']:
                        raise B1Error('RESULT_REFERENCE_MISSING')
                return result
        try:
            stamp = datetime.now(timezone.utc)
            if operation in {'reserve', 'check'}:
                requested = paths(paths_input)
                current = await active(session, stamp)
                scopes = [(row, paths([row.path_pattern])[0]) for row in current]
                conflicts, own_matches = [], []
                for path in requested:
                    for row, scope in scopes:
                        if not overlaps(path, scope):
                            continue
                        if row.agent_id == actor.id:
                            own_matches.append(row.id)
                        elif exclusive or row.exclusive:
                            conflicts.append(row)
                if operation == 'check':
                    unique = {row.id: row for row in conflicts}
                    return dict(mail_instance_id=state['instance_id'], actor_agent_id=actor.id,
                                paths=[path.value for path in requested], conflict_free=not unique,
                                conflicts=[view(row) for row in list(unique.values())[:100]],
                                conflicts_truncated=len(unique) > 100,
                                own_matching_ids=list(dict.fromkeys(own_matches))[:100])
                expires = until(stamp, ttl_seconds)
                if conflicts:
                    raise B1Error('RESERVATION_CONFLICT')
                rows = []
                for path in requested:
                    own = next((row for row in current if row.agent_id == actor.id
                                and row.path_pattern == path.value and row.exclusive == exclusive), None)
                    if own is None:
                        own = FileReservation(project_id=actor.project_id, agent_id=actor.id,
                            path_pattern=path.value, exclusive=exclusive, reason=reason,
                            created_ts=stamp, expires_ts=expires)
                        session.add(own)
                    else:
                        own.expires_ts = max(own.expires_ts, expires)
                    rows.append(own)
            else:
                rows = await selected(session, actor.id, stamp, values=paths_input,
                                      ids=file_reservation_ids)
                if operation == 'renew':
                    # S2c renew never resurrects a released/expired lease.
                    if any(row.released_ts is not None or row.expires_ts <= stamp for row in rows):
                        raise B1Error('LEASE_EXPIRED')
                    plans = [(row, until(row.expires_ts, extend_seconds)) for row in rows]
                    for row, expires in plans:
                        row.expires_ts = expires
                else:
                    for row in rows:
                        row.released_ts = row.released_ts or stamp
            await session.flush()
            result = dict(mail_instance_id=state['instance_id'], actor_agent_id=actor.id,
                          reservations=[view(row) for row in rows])
            outcome = {'status': 'accepted', 'result': result}
        except B1Error as error:
            if operation == 'check':
                raise
            failure = error
            outcome = {'status': 'refused', 'code': error.code, 'details': error.details}
        await session.execute(insert(directory.intents).values(
            **selector, intent_hash=intent_hash, outcome=canonical(outcome)))
        await directory.refresh(session)
    if failure:
        raise failure
    return result


async def guard(core, expected_instance, proof, values, extend_seconds):
    """Current concrete-path coverage, never a replayable grant receipt.

    All paths and all foreign conflicts are checked before any lease changes.
    The actual edit follows the hook: this does not lock the filesystem itself.
    """
    validate_ttl(extend_seconds)
    async with core.owner_transaction(expected_instance, proof) as ctx:
        session, actor = ctx.session, ctx.actor
        ctx.assert_live(session)
        requested = paths(values)
        if any(path.is_glob for path in requested):
            raise B1Error('CHECK_REQUIRES_CONCRETE_PATH')
        if any(Path(path.value).is_dir() for path in requested):
            raise B1Error('CHECK_REQUIRES_FILE_PATH')
        stamp = datetime.now(timezone.utc)
        rows = await active(session, stamp)
        scopes = [(row, paths([row.path_pattern])[0]) for row in rows]
        witnesses, items = {}, []
        for path in requested:
            own = None
            for row, scope in scopes:
                if not overlaps(path, scope):
                    continue
                if row.agent_id != actor.id:
                    # Editing conflicts with a foreign reservation, including a
                    # shared one: the edit is a write regardless of lease mode.
                    raise B1Error('RESERVATION_CONFLICT')
                if own is None:
                    own = row
            if own is None:
                raise B1Error('RESERVATION_REQUIRED')
            witnesses[own.id] = own
            items.append(dict(path=path.value, reservation_id=own.id))
        # Only near-expiry witnesses need renewal; do not add extend_seconds on
        # every edit. Expired/released rows never entered the witness set.
        threshold = until(stamp, 600)
        planned = [(row, until(row.expires_ts, extend_seconds))
                   for row in witnesses.values() if row.expires_ts <= threshold]
        for row, expires in planned:
            row.expires_ts = expires
        await session.flush()
        return dict(mail_instance_id=ctx.state['instance_id'], actor_agent_id=actor.id,
                    covered=True, items=items, renewed=len(planned))
