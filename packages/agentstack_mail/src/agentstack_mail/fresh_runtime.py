"""Explicit fresh Mail runtime over the real ORM and B1 contracts.

No environment lookup, default database, service activation, or legacy import.
Local registration is a trusted same-user operation, never a model tool.
"""
from __future__ import annotations

import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import event, insert, select
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from . import storage_origin_directory as directory
from .models import MailInstance, Message, MessageRecipient, Project
from .storage_origin_contract import B1Error, canonical, digest, string
from .storage_origin_core import StorageOriginCore
from . import fresh_notifications as notifications

KIND = 'orrery-fresh-mail-isolated-v1'
FRESH_KIND = 'orrery-fresh-mail-v1'


def _directory(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise B1Error('FRESH_DIRECTORY_UNSAFE')


def _read_private(path: Path, *, max_bytes: int = 65536) -> dict:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1):
                raise B1Error('FRESH_PRIVATE_FILE_UNSAFE')
            raw = stream.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise B1Error('FRESH_PRIVATE_FILE_INVALID')
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, ValueError, UnicodeError):
        raise B1Error('FRESH_PRIVATE_FILE_UNAVAILABLE') from None


def _write_private(path: Path, value: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write((canonical(value) + '\n').encode())
            stream.flush()
            os.fsync(stream.fileno())
        # Same-directory link publishes complete bytes atomically, never clobbers.
        os.link(temporary, path)
    finally:
        os.unlink(temporary)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _isolated_root(path: Path) -> Path:
    if not path.is_absolute() or path.is_symlink():
        raise B1Error('FRESH_ISOLATION_REQUIRED')
    resolved = path.resolve()
    bases = (Path('/private/tmp').resolve(), Path(tempfile.gettempdir()).resolve())
    if not any(base != resolved and base in resolved.parents for base in bases):
        raise B1Error('FRESH_ISOLATION_REQUIRED')
    return resolved


def _fresh_root(path: Path) -> Path:
    # A trusted, explicit init chooses an empty path. Never infer a HOME,
    # project, existing Mail directory or fallback deployment from environment.
    if (not path.is_absolute() or path.is_symlink() or path.resolve() != path
            or path.parent == path):raise B1Error('FRESH_ROOT_UNSAFE')
    return path


def read_config(config_path):
    """One protected config reader for backend, client and watcher; no DB IO."""
    path = Path(config_path)
    if not path.is_absolute() or path.name != 'config.json' or path.parent.is_symlink():
        raise B1Error('FRESH_CONFIG_INVALID')
    root = path.parent.resolve();_directory(root)
    value = _read_private(path)
    if set(value) != {'kind','mode','root','database','instance_id'}:
        raise B1Error('FRESH_CONFIG_INVALID')
    if (value['mode'], value['kind']) not in [('isolated',KIND), ('fresh',FRESH_KIND)]:
        raise B1Error('FRESH_CONFIG_INVALID')
    root = _isolated_root(path.parent) if value['mode'] == 'isolated' else _fresh_root(path.parent)
    if value['root'] != str(root) or value['database'] != str(root/'storage.sqlite3'):
        raise B1Error('FRESH_CONFIG_INVALID')
    string(value['instance_id'],64)
    return root, value


def mail_port_allowed(port, mode='isolated'):
    return (mode in ('isolated','fresh') and type(port) is int and 1024 <= port <= 65535
            and port not in {8770,8781,8791} and (port != 8765 or mode == 'fresh'))


class FreshMailCore(StorageOriginCore):
    """Pin every transaction, including first registration, to configured I."""

    def __init__(self, sessions, instance_id):
        super().__init__(sessions)
        self.instance_id = string(instance_id, 64)

    @asynccontextmanager
    async def _transaction(self, *, write=False):
        async with super()._transaction(write=write) as session:
            actual = await directory.instance(session)
            if not hmac.compare_digest(actual.encode(), self.instance_id.encode()):
                raise B1Error('INSTANCE_MISMATCH')
            yield session

    async def reservation(self, operation, **arguments):
        from .fresh_reservations import apply
        return await apply(self, operation, **arguments)

    async def notification(self, operation, **arguments):
        return await notifications.apply(self, operation, **arguments)

    async def owner(self, operation, **arguments):
        result = await super().owner(operation, **arguments)
        if operation in {'read', 'ack'}:
            await notifications.reconcile(self, [(result['message_id'], result['agent_id'])])
        return result

    async def write_message(self, operation, *, expected_server_instance_id, auth,
                            request_id, body_md, recipients=None, subject=None,
                            thread_id=None, topic=None, importance=None,
                            ack_required=None, message_id=None):
        result = await self._store_message(operation, expected_server_instance_id=expected_server_instance_id,
            auth=auth, request_id=request_id, body_md=body_md, recipients=recipients, subject=subject,
            thread_id=thread_id, topic=topic, importance=importance, ack_required=ack_required, message_id=message_id)
        await notifications.reconcile(self, [(result['message_id'], r['agent_id']) for r in result['recipients']])
        return result

    async def _store_message(self, operation, *, expected_server_instance_id, auth,
                             request_id, body_md, recipients=None, subject=None,
                             thread_id=None, topic=None, importance=None,
                             ack_required=None, message_id=None):
        if operation not in {'send', 'reply'}:
            raise B1Error('OPERATION_UNPUBLISHED')
        string(request_id)
        if (not isinstance(body_md, str) or len(body_md) > 1024 * 1024
                or (importance is not None and importance not in {'low', 'normal', 'high', 'urgent'})
                or (ack_required is not None and type(ack_required) is not bool)):
            raise B1Error('INVALID_MESSAGE')
        try:
            body_md.encode('utf-8')
        except UnicodeError:
            raise B1Error('INVALID_MESSAGE') from None
        if subject is not None:
            string(subject, 512)
        if operation == 'send' and subject is None:
            raise B1Error('SUBJECT_REQUIRED')
        if thread_id is not None:
            string(thread_id)
        if topic is not None:
            string(topic, 64)
        intent = dict(body_md=body_md, recipients=recipients, subject=subject,
                      thread_id=thread_id, topic=topic, importance=importance,
                      ack_required=ack_required, message_id=message_id)
        try:
            intent_hash = digest(intent)
        except (TypeError, ValueError):
            raise B1Error('INVALID_MESSAGE') from None
        failure = None
        async with self.owner_transaction(expected_server_instance_id, auth) as ctx:
            s, state, records, actor = ctx.session, ctx.state, ctx.records, ctx.actor
            ctx.assert_live(s)
            selector = dict(instance_id=state['instance_id'], actor_id=actor.id,
                            operation='fresh-' + operation, request_id=request_id)
            previous = (await s.execute(select(directory.intents).where(
                *[directory.intents.c[key] == value for key, value in selector.items()]
            ))).mappings().first()
            if previous:
                if previous['intent_hash'] != intent_hash:
                    raise B1Error('REQUEST_INTENT_CONFLICT')
                outcome = json.loads(previous['outcome'])
                if outcome['status'] == 'refused':
                    raise B1Error(outcome['code'], **outcome['details'])
                result = outcome['result']
                saved = await s.get(Message, result['message_id'])
                pairs = (await s.execute(select(MessageRecipient).where(
                    MessageRecipient.message_id == result['message_id']))).scalars().all()
                expected = {(r['agent_id'], r['kind']) for r in result['recipients']}
                if (saved is None or saved.sender_id != actor.id
                        or {(r.agent_id, r.kind) for r in pairs} != expected):
                    raise B1Error('RESULT_REFERENCE_MISSING')
                return result
            try:
                decision = await self._decision(s, state, records, actor, operation,
                                                message_id, recipients)
                parent = await s.get(Message, message_id) if operation == 'reply' else None
                message = Message(
                    project_id=decision['storage_project_id'], sender_id=actor.id,
                    subject=subject if subject is not None else ('Re: ' + parent.subject)[:512],
                    body_md=body_md,
                    importance=importance if importance is not None else (parent.importance if parent else 'normal'),
                    ack_required=ack_required if ack_required is not None else (parent.ack_required if parent else False),
                    thread_id=(parent.thread_id or str(parent.id)) if parent else thread_id,
                    topic=topic if topic is not None else (parent.topic if parent else None),
                    created_ts=datetime.now(timezone.utc), attachments=[])
                s.add(message)
                await s.flush()
                if message.thread_id is None:
                    message.thread_id = str(message.id)
                for recipient in decision['recipients']:
                    s.add(MessageRecipient(message_id=message.id,
                                           agent_id=recipient['agent_id'], kind=recipient['kind']))
                await s.flush()
                result = dict(mail_instance_id=state['instance_id'], message_id=message.id,
                              sender_id=actor.id, thread_id=message.thread_id,
                              created_ts=message.created_ts.isoformat(),
                              recipients=decision['recipients'], decision_only=False,
                              delivery_status='notification_pending')
                outcome = {'status': 'accepted', 'result': result}
            except B1Error as error:
                if error.code.endswith('NOT_READY'):
                    raise
                failure = error
                outcome = {'status': 'refused', 'code': error.code, 'details': error.details}
            # Storage publication failure must roll back the whole Mail write,
            # rather than commit it as a business refusal without a receipt.
            if failure is None:
                await notifications.create(s, message.id, decision['recipients'])
            await s.execute(insert(directory.intents).values(
                **selector, intent_hash=intent_hash, outcome=canonical(outcome)))
            await directory.refresh(s)
        if failure:
            raise failure
        return result


class FreshRuntime:
    def __init__(self, root, config, engine):
        self.root, self.config, self.engine = root, config, engine
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)
        self.core = FreshMailCore(self.sessions, config['instance_id'])
        self.core.output_root = root

    @staticmethod
    def _engine(database):
        engine = create_async_engine(URL.create('sqlite+aiosqlite', database=str(database)),
                                     connect_args={'timeout': 5})

        @event.listens_for(engine.sync_engine, 'connect')
        def pragmas(connection, _):
            cursor = connection.cursor()
            cursor.execute('PRAGMA foreign_keys=ON')
            cursor.close()

        return engine

    @classmethod
    async def initialize(cls, root, *, mode='isolated'):
        if mode not in ('isolated','fresh'):raise B1Error('FRESH_MODE_INVALID')
        root = _isolated_root(Path(root)) if mode == 'isolated' else _fresh_root(Path(root))
        try:
            root.mkdir(mode=0o700)
        except FileExistsError:
            raise B1Error('FRESH_DESTINATION_EXISTS') from None
        _directory(root)
        for name in ('registration-intents', 'bindings'):
            (root / name).mkdir(mode=0o700)
        database = root / 'storage.sqlite3'
        fd = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        config = dict(kind=KIND if mode == 'isolated' else FRESH_KIND, mode=mode, root=str(root),
                      database=str(database), instance_id=uuid4().hex)
        runtime = cls(root, config, cls._engine(database))
        try:
            async with runtime.engine.begin() as connection:
                await connection.run_sync(SQLModel.metadata.create_all)
            async with runtime.sessions() as session:
                session.add(MailInstance(instance_id=config['instance_id']))
                session.add(Project(id=1, slug='local', human_key='orrery://local'))
                await session.commit()
            await runtime.core.prepare(default_project_id=1)
            _write_private(root / 'config.json', config)
            return runtime
        except BaseException:
            await runtime.close()
            raise

    @classmethod
    async def open(cls, config_path):
        root, config = read_config(config_path)
        for name in ('registration-intents', 'bindings'):
            _directory(root / name)
        database = root / 'storage.sqlite3'
        info = database.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise B1Error('FRESH_DATABASE_UNSAFE')
        runtime = cls(root, config, cls._engine(database))
        try:
            await runtime.core.health(config['instance_id'])
            return runtime
        except BaseException:
            await runtime.close()
            raise

    async def register_local(self, *, request_id, name, program, model):
        try:
            if str(UUID(request_id)) != request_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise B1Error('INVALID_REGISTRATION_REQUEST') from None
        values = dict(request_id=request_id, name=string(name),
                      program=string(program), model=string(model))
        intent_path = self.root / 'registration-intents' / (request_id + '.json')
        try:
            _write_private(intent_path, dict(values, mail_instance_id=self.config['instance_id'],
                                             token=secrets.token_urlsafe(32)))
        except FileExistsError:
            pass
        intent = _read_private(intent_path)
        if (set(intent) != set(values) | {'token', 'mail_instance_id'}
                or intent['mail_instance_id'] != self.config['instance_id']
                or any(intent[key] != value for key, value in values.items())):
            raise B1Error('REQUEST_INTENT_CONFLICT')
        result = await self.core.register_new(**values, token=intent['token'])
        binding = dict(kind='orrery-fresh-owner-v1', mail_instance_id=result['mail_instance_id'],
                       mail_agent_id=result['agent_id'], credential_generation=result['credential_generation'],
                       registration_token=intent['token'])
        binding_path = self.root / 'bindings' / (str(result['agent_id']) + '.json')
        try:
            _write_private(binding_path, binding)
        except FileExistsError:
            if _read_private(binding_path) != binding:
                raise B1Error('FRESH_BINDING_CONFLICT') from None
        return dict(result, binding_path=str(binding_path))

    async def close(self):
        await self.engine.dispose()
