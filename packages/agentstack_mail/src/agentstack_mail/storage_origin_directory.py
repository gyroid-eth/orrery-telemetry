"""Private candidate metadata, deliberately separate from SQLModel.metadata.

Only explicit preparation creates it. Reads never repair/prepare anything.
"""
from __future__ import annotations

import json
from collections import defaultdict

from sqlalchemy import Column, Integer, MetaData, String, Table, Text, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Agent, MailInstance
from .storage_origin_contract import B1Error, RULESET, canonical, digest, fingerprint, integer, name_keys

metadata = MetaData()
meta = Table('b1_directory_meta', metadata, Column('id', Integer, primary_key=True),
             Column('instance_id', String, nullable=False), Column('ruleset', String, nullable=False),
             Column('revision', Integer, nullable=False), Column('default_project_id', Integer, nullable=False),
             Column('source_digest', String, nullable=False), Column('registry_digest', String, nullable=False),
             Column('intent_digest', String, nullable=False))
identities = Table('b1_identity_pins', metadata, Column('agent_id', Integer, primary_key=True),
                   Column('name', String, nullable=False), Column('project_id', Integer, nullable=False))
credentials = Table('b1_credential_keys', metadata, Column('fingerprint', String(64), primary_key=True),
                    Column('state', String, nullable=False), Column('owner_id', Integer),
                    Column('members', Text, nullable=False))
registrations = Table('b1_registration_intents', metadata, Column('request_id', String, primary_key=True),
                      Column('intent_hash', String, nullable=False), Column('agent_id', Integer, nullable=False))
intents = Table('b1_operation_intents', metadata,
                Column('instance_id', String, primary_key=True), Column('actor_id', Integer, primary_key=True),
                Column('operation', String, primary_key=True), Column('request_id', String, primary_key=True),
                Column('intent_hash', String, nullable=False), Column('outcome', Text, nullable=False))


async def rows(session: AsyncSession, table):
    return [dict(r) for r in (await session.execute(select(table))).mappings()]


async def agents(session: AsyncSession):
    return {a.id: a for a in (await session.execute(select(Agent).order_by(Agent.id))).scalars()}


def source(records):
    return [{'id': a.id, 'origin': a.project_id, 'name': a.name,
             'credential': fingerprint(a.registration_token) if a.registration_token is not None else None,
             'generation': a.credential_generation,
             'retired': a.retired_at.isoformat() if a.retired_at else None}
            for a in sorted(records.values(), key=lambda a: a.id)]


async def registry_digest(session):
    return digest({t.name: sorted(await rows(session, t), key=canonical)
                   for t in (identities, credentials, registrations)})


async def intent_digest(session):
    return digest(sorted(await rows(session, intents), key=canonical))


async def instance(session):
    value = await session.get(MailInstance, 1)
    if value is None:
        raise B1Error('NOT_PREPARED')
    return value.instance_id


async def prepared(session):
    # sqlite_master is read-only and does not register new ORM metadata.
    from sqlalchemy import text
    found = (await session.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='b1_directory_meta'"))).first()
    if not found:
        raise B1Error('NOT_PREPARED')
    result = (await session.execute(select(meta).where(meta.c.id == 1))).mappings().first()
    if result is None or result['ruleset'] != RULESET:
        raise B1Error('DIRECTORY_STALE')
    return dict(result)


async def refresh(session, *, identity_change=False):
    await session.flush()
    current = await prepared(session)
    await session.execute(update(meta).where(meta.c.id == 1).values(
        revision=current['revision'] + int(identity_change), source_digest=digest(source(await agents(session))),
        registry_digest=await registry_digest(session), intent_digest=await intent_digest(session)))


async def validate(session):
    state = await prepared(session)
    if state['instance_id'] != await instance(session):
        raise B1Error('DIRECTORY_STALE')
    records = await agents(session)
    if state['source_digest'] != digest(source(records)) or state['registry_digest'] != await registry_digest(session):
        raise B1Error('DIRECTORY_STALE')
    if state['intent_digest'] != await intent_digest(session):
        raise B1Error('INTENT_STORE_STALE')
    pins = {r['agent_id']: r for r in await rows(session, identities)}
    if set(pins) != set(records) or any((a.name, a.project_id) != (pins[i]['name'], pins[i]['project_id'])
                                      for i, a in records.items()):
        raise B1Error('DIRECTORY_STALE')
    keys = {r['fingerprint']: r for r in await rows(session, credentials)}
    current = defaultdict(set)
    for i, a in records.items():
        if a.registration_token is not None:
            current[fingerprint(a.registration_token)].add(i)
    for fp, ids in current.items():
        key = keys.get(fp)
        if key is None or not ids <= set(json.loads(key['members'])):
            raise B1Error('DIRECTORY_STALE')
        if key['state'] == 'live':
            if ids != {key['owner_id']}:
                raise B1Error('DIRECTORY_STALE')
        elif key['state'] != 'burned':
            raise B1Error('DIRECTORY_STALE')
    for fp, key in keys.items():
        if key['state'] not in {'live', 'burned', 'revoked'}:
            raise B1Error('DIRECTORY_STALE')
        if key['state'] == 'live' and current[fp] != {key['owner_id']}:
            raise B1Error('DIRECTORY_STALE')
        if key['state'] != 'burned' and len(json.loads(key['members'])) > 1:
            raise B1Error('DIRECTORY_STALE')
    return state, records, keys


async def prepare(session, default_project_id):
    """Explicit isolated migration entry; caller holds BEGIN IMMEDIATE."""
    connection = await session.connection()
    await connection.run_sync(metadata.create_all)
    prepared_meta = (await session.execute(select(meta))).mappings().first()
    # Re-preparation may reconcile source-only legacy changes. It cannot bless
    # altered private history or an erased request mapping as a new baseline.
    if prepared_meta and prepared_meta['registry_digest'] != await registry_digest(session):
        raise B1Error('DIRECTORY_STALE')
    if prepared_meta and prepared_meta['intent_digest'] != await intent_digest(session):
        raise B1Error('INTENT_STORE_STALE')
    records = await agents(session)
    for a in records.values():
        integer(a.id); integer(a.project_id); integer(a.credential_generation, minimum=0)
        name_keys(a.name)  # never silently omit an unsupported legacy name
    pins = {r['agent_id']: r for r in await rows(session, identities)}
    if any(i not in records or (p['name'], p['project_id']) != (records[i].name, records[i].project_id)
           for i, p in pins.items()):
        raise B1Error('IDENTITY_HISTORY_MISMATCH')
    for i, a in records.items():
        if i not in pins:
            await session.execute(insert(identities).values(agent_id=i, name=a.name, project_id=a.project_id))
    keys = {r['fingerprint']: r for r in await rows(session, credentials)}
    current = defaultdict(set)
    for i, a in records.items():
        if a.registration_token is not None:
            current[fingerprint(a.registration_token)].add(i)
    # Historical members survive partial recovery and preparation/restart.
    for fp in set(current) | set(keys):
        old = keys.get(fp)
        members = current[fp] | (set(json.loads(old['members'])) if old else set())
        state = ('burned' if len(members) > 1 or (old and old['state'] == 'burned') else
                 'revoked' if old and old['state'] == 'revoked' else
                 'live' if current[fp] else 'revoked')
        values = dict(state=state, owner_id=next(iter(current[fp])) if state == 'live' else None,
                      members=canonical(sorted(members)))
        if old:
            await session.execute(update(credentials).where(credentials.c.fingerprint == fp).values(**values))
        else:
            await session.execute(insert(credentials).values(fingerprint=fp, **values))
    iid = await instance(session)
    if prepared_meta and (prepared_meta['instance_id'] != iid or prepared_meta['default_project_id'] != default_project_id):
        raise B1Error('DIRECTORY_STALE')
    if not prepared_meta:
        await session.execute(insert(meta).values(id=1, instance_id=iid, ruleset=RULESET, revision=0,
            default_project_id=default_project_id, source_digest='', registry_digest='', intent_digest=''))
    await refresh(session, identity_change=True)
    await validate(session)
