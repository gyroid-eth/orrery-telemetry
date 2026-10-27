"""Explicit old-copy queries over the real Mail ORM, with no live resources."""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from agentstack_mail.archive_cli import ArchiveError, MAX_BODY, MAX_OUTPUT, archive_connection, main, query_archive
from agentstack_mail.models import Agent, Message, MessageRecipient, Project

CANARY = 'synthetic-archive-owner-secret-never-output'


@pytest.fixture
def archive(tmp_path):
    path = tmp_path / 'old-copy.sqlite'
    async def build():
        engine = create_async_engine(f'sqlite+aiosqlite:///{path}')
        try:
            async with engine.begin() as connection:
                await connection.run_sync(SQLModel.metadata.create_all)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions() as session:
                session.add_all([Project(id=n, slug=f'old-{n}', human_key=f'/old/{n}') for n in (1, 2)])
                await session.flush()
                session.add_all([Agent(id=n, project_id=origin, name=name, program='fixture',
                    model='fixture', registration_token=CANARY) for n, origin, name in
                    [(11, 1, 'Twin'), (22, 2, 'Twin'), (33, 1, 'Recipient')]])
                await session.flush()
                for n, origin, sender in [(1, 1, 11), (2, 1, 11), (3, 2, 22)]:
                    session.add(Message(id=n, project_id=origin, sender_id=sender, thread_id='reused-thread',
                        subject=f'archive needle {n}', body_md=f'old body {n}', importance='high', ack_required=True,
                        created_ts=datetime(2026, 1, n, tzinfo=timezone.utc), attachments=[{
                            'type': 'file', 'path': 'projects/old/attachments/kept.webp',
                            'data_base64': CANARY, 'extra_private_field': CANARY}]))
                session.add(Message(id=4, project_id=1, sender_id=11, subject='large body',
                    body_md='日' * (MAX_BODY + 100), thread_id='large'))
                await session.flush()
                session.add(MessageRecipient(message_id=1, agent_id=33,
                    read_ts=datetime(2026, 2, 1, tzinfo=timezone.utc), ack_ts=None))
                await session.commit()
        finally:
            await engine.dispose()
    asyncio.run(build())
    return path


def test_search_spans_old_origins_without_credentials_or_side_effects(archive):
    before = hashlib.sha256(archive.read_bytes()).hexdigest()
    siblings = set(archive.parent.iterdir())
    result = query_archive(archive, 'search', query='archive needle')
    assert [item['id'] for item in result['items']] == [1, 2, 3]
    assert {item['storage_origin_id'] for item in result['items']} == {1, 2}
    assert result['items'][0]['recipients'][0]['ack_ts'] is None
    assert result['items'][0]['recipients'][0]['read_ts'] is not None
    assert result['items'][0]['importance'] == 'high' and result['items'][0]['ack_required'] is True
    assert CANARY not in json.dumps(result)
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == before
    assert set(archive.parent.iterdir()) == siblings


def test_thread_uses_message_origin_when_old_thread_ids_collide(archive):
    assert [m['id'] for m in query_archive(archive, 'thread', message_id=1)['items']] == [1, 2]
    assert [m['id'] for m in query_archive(archive, 'thread', message_id=3)['items']] == [3]


def test_attachment_paths_are_projected_without_opening_attachment_contents(archive, monkeypatch):
    attachment = archive.parent / 'projects/old/attachments/kept.webp'
    attachment.parent.mkdir(parents=True)
    attachment.write_bytes(b'not-image-data-that-must-not-be-opened')
    real_read = type(attachment).read_bytes
    def guarded_read(path):
        assert path != attachment, 'attachment content was read'
        return real_read(path)
    monkeypatch.setattr(type(attachment), 'read_bytes', guarded_read)
    result = query_archive(archive, 'attachments', message_id=1)
    assert result['items'] == [{'id': 1, 'attachments': [
        {'type': 'file', 'path': 'projects/old/attachments/kept.webp'}], 'attachments_truncated': False}]
    assert CANARY not in json.dumps(result)


def test_connection_rejects_writes_and_unfinalized_wal(archive):
    before = archive.read_bytes()
    with archive_connection(archive) as database:
        assert database.execute('PRAGMA query_only').fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            database.execute('DELETE FROM messages')
    assert archive.read_bytes() == before
    archive.with_name(archive.name + '-wal').write_bytes(b'unfinalized-copy')
    with pytest.raises(ArchiveError, match='ARCHIVE_COPY_NOT_FINALIZED'):
        query_archive(archive, 'search', query='needle')


def test_bounded_pages_body_and_literal_search(archive):
    first = query_archive(archive, 'search', query='needle', limit=1)
    assert first['returned'] == 1 and first['has_more'] and first['next_offset'] == 1
    assert query_archive(archive, 'search', query='needle', limit=1, offset=1)['items'][0]['id'] == 2
    large = query_archive(archive, 'search', query='large body')
    assert len(large['items'][0]['body_md']) == MAX_BODY and large['items'][0]['body_truncated']
    assert len(json.dumps(large, ensure_ascii=False).encode()) < MAX_OUTPUT
    assert query_archive(archive, 'search', query="' OR 1=1 --")['items'] == []
    with pytest.raises(ArchiveError, match='ARCHIVE_LIMIT_INVALID'):
        query_archive(archive, 'search', query='needle', limit=101)
    with pytest.raises(ArchiveError, match='ARCHIVE_MESSAGE_NOT_FOUND'):
        query_archive(archive, 'thread', message_id=999)


def test_cli_requires_explicit_copy_and_emits_only_query_output(archive, capsys):
    main(['--archive-copy', str(archive), 'thread', '--message-id', '1'])
    result = json.loads(capsys.readouterr().out)
    assert result['archive_read_only'] and result['returned'] == 2
    assert CANARY not in json.dumps(result)
    with pytest.raises(SystemExit):
        main(['search', 'needle'])
