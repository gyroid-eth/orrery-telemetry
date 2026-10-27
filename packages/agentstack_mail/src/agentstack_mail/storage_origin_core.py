"""Explicit, unpublished storage-origin kernel over the existing Mail ORM.

A caller supplies a session factory. This module never resolves a database,
reads a token file, starts a server, changes settings, or enables global mode.
"""
from __future__ import annotations

import base64
import asyncio
import hmac
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import IntegrityError

from . import storage_origin_directory as directory
from .models import Agent, EnrollmentAudit, Message, MessageRecipient, Project
from .storage_origin_contract import (
    ABSENT, RULESET, Auth, B1Error, ConsumptionRequest, DeliveryTarget, OwnerBinding,
    canonical, digest, fingerprint, integer,
    name_keys, string, token,
)


class OwnerContext:
    """A same-task/session transaction capability, never a wire credential."""
    def __init__(self, session, state, records, keys, actor):
        self.session, self.state, self.records, self.keys, self.actor = session, state, records, keys, actor
        self._task = asyncio.current_task()
        self._transaction = session.sync_session.get_transaction()
        self._active = True

    def __repr__(self):
        return 'OwnerContext(active=%s)' % self._active

    def assert_live(self, session):
        if (not self._active or session is not self.session or asyncio.current_task() is not self._task
                or session.sync_session.get_transaction() is not self._transaction
                or not self._transaction.is_active):
            raise B1Error('OWNER_CONTEXT_EXPIRED')


class StorageOriginCore:
    def __init__(self, sessions):
        self.sessions = sessions

    @asynccontextmanager
    async def _transaction(self, *, write=False):
        async with self.sessions() as session:
            await session.execute(text('BEGIN IMMEDIATE' if write else 'BEGIN'))
            try:
                yield session
                if write:
                    await session.commit()
            finally:
                await session.rollback()

    async def prepare(self, *, default_project_id):
        integer(default_project_id)
        async with self._transaction(write=True) as s:
            if await s.get(Project, default_project_id) is None:
                raise B1Error('NOT_PREPARED')
            await directory.prepare(s, default_project_id)

    async def _context(self, session, expected_instance):
        expected_instance = string(expected_instance, 64)
        state, records, keys = await directory.validate(session)
        if not hmac.compare_digest(state['instance_id'].encode(), expected_instance.encode()):
            raise B1Error('INSTANCE_MISMATCH')
        return state, records, keys

    @asynccontextmanager
    async def owner_transaction(self, expected_instance, proof):
        """B4 writer interface: verify and apply effects inside this scope only."""
        async with self._transaction(write=True) as s:
            state, records, keys = await self._context(s, expected_instance)
            actor = self._verify(proof, records, keys)
            ctx = OwnerContext(s, state, records, keys, actor)
            try:
                yield ctx
            finally:
                ctx._active = False

    def _verify(self, proof, records, keys):
        agent = self._verify_identity(proof, records, keys)
        if agent.retired_at is not None:
            raise B1Error('ACTOR_RETIRED')
        return agent

    def _verify_identity(self, proof, records, keys):
        """Retained credential check; private host management is its only bypass."""
        proof = Auth.parse(proof)
        agent = records.get(proof.actor_agent_id)
        if agent is None or agent.registration_token is None:
            raise B1Error('OWNER_CREDENTIAL_MISMATCH')
        fp = fingerprint(agent.registration_token)
        if keys[fp]['state'] == 'burned':
            raise B1Error('CREDENTIAL_BURNED')
        if keys[fp]['state'] != 'live' or keys[fp]['owner_id'] != agent.id:
            raise B1Error('OWNER_CREDENTIAL_MISMATCH')
        if not hmac.compare_digest(agent.registration_token.encode('utf-8'), proof.registration_token.encode('utf-8')):
            raise B1Error('OWNER_CREDENTIAL_MISMATCH')
        if agent.credential_generation != proof.expected_credential_generation:
            raise B1Error('CREDENTIAL_GENERATION_MISMATCH')
        return agent

    def host_target(self, owner, target_proof, target_agent_id):
        """Same-transaction target capability. Never accepts a wire admin flag."""
        owner.assert_live(owner.session)
        target = self._verify_identity(target_proof, owner.records, owner.keys)
        if target.id != integer(target_agent_id):
            raise B1Error('OWNER_CREDENTIAL_MISMATCH')
        return target

    async def host_retire(self, owner, target_proof, target_agent_id):
        target = self.host_target(owner, target_proof, target_agent_id)
        if target.id == owner.actor.id:
            raise B1Error('FRESH_OPERATOR_RETIRE_FORBIDDEN')
        changed = target.retired_at is None
        if changed:
            target.retired_at = datetime.now(timezone.utc)
            await directory.refresh(owner.session, identity_change=True)
        return {'changed': changed, 'retired_at': target.retired_at.isoformat()}

    async def health(self, expected_server_instance_id=ABSENT):
        async with self._transaction() as s:
            state, _, _ = await directory.validate(s)
            if expected_server_instance_id is not ABSENT:
                if string(expected_server_instance_id, 64) != state['instance_id']:
                    raise B1Error('INSTANCE_MISMATCH')
            return {'mail_instance_id': state['instance_id'], 'ruleset': RULESET,
                    'activation_enabled': False, 'directory_revision': state['revision']}

    async def _candidates(self, session, records, name):
        keys = set(name_keys(name))
        return sorted((a for a in records.values() if keys.intersection(name_keys(a.name))), key=lambda a: a.id)

    async def _candidate_view(self, s, a):
        origin = await s.get(Project, a.project_id)
        return {'target_agent_id': a.id, 'name': a.name,
                'state': 'retired' if a.retired_at is not None else 'active',
                'source_label': origin.slug if origin else 'unresolved-origin'}

    async def _candidate_page(self, s, state, records, name, limit=50, cursor=None):
        integer(limit)
        if limit > 50:
            raise B1Error('INVALID_LIMIT')
        query = {'instance': state['instance_id'], 'ruleset': RULESET,
                 'keys': list(name_keys(name)), 'revision': state['revision']}
        position = 0
        if cursor is not None:
            try:
                raw = string(cursor, 4096)
                decoded = json.loads(base64.urlsafe_b64decode(raw.encode()).decode())
                if not isinstance(decoded, dict) or set(decoded) != set(query) | {'position'}:
                    raise ValueError
                position = integer(decoded['position'], minimum=0)
                if {k: decoded[k] for k in query} != query:
                    raise ValueError
            except (B1Error, ValueError, TypeError, UnicodeError):
                raise B1Error('CURSOR_MISMATCH') from None
        candidates = await self._candidates(s, records, name)
        if position > len(candidates):
            raise B1Error('CURSOR_MISMATCH')
        next_position = position + limit
        next_cursor = (base64.urlsafe_b64encode(canonical(dict(query, position=next_position)).encode()).decode()
                       if next_position < len(candidates) else None)
        return {'mail_instance_id': state['instance_id'], 'directory_revision': state['revision'],
                'candidate_count': len(candidates),
                'candidates': [await self._candidate_view(s, a) for a in candidates[position:next_position]],
                'next_cursor': next_cursor}

    async def directory(self, *, expected_server_instance_id, target_name, limit=50, cursor=None, auth=None):
        async with self._transaction() as s:
            state, records, keys = await self._context(s, expected_server_instance_id)
            if auth is not None:
                self._verify(auth, records, keys)
            return await self._candidate_page(s, state, records, target_name, limit, cursor)

    async def _resolve(self, s, state, records, *, target_agent_id=None, target_name=None,
                       expected_target_name=None, active=True):
        if (target_agent_id is None) == (target_name is None):
            raise B1Error('TARGET_SELECTOR_REQUIRED')
        if target_agent_id is not None:
            target = records.get(integer(target_agent_id))
            if target is None:
                raise B1Error('TARGET_NOT_FOUND')
        else:
            page = await self._candidate_page(s, state, records, target_name)
            code = ('TARGET_NAME_NOT_FOUND' if page['candidate_count'] == 0 else
                    'TARGET_NAME_AMBIGUOUS' if page['candidate_count'] > 1 else None)
            if code:
                raise B1Error(code, **page)
            target = records[page['candidates'][0]['target_agent_id']]
        if expected_target_name is not None and string(expected_target_name) != target.name:
            raise B1Error('TARGET_NAME_STALE')
        if active and target.retired_at is not None:
            raise B1Error('TARGET_RETIRED', mail_instance_id=state['instance_id'],
                          directory_revision=state['revision'], candidate_count=1,
                          candidates=[await self._candidate_view(s, target)], next_cursor=None)
        return target

    async def whois(self, *, expected_server_instance_id, auth=None, **selector):
        async with self._transaction() as s:
            state, records, keys = await self._context(s, expected_server_instance_id)
            if auth is not None:
                self._verify(auth, records, keys)
            target = await self._resolve(s, state, records, active=False, **selector)
            return dict(await self._candidate_view(s, target), mail_instance_id=state['instance_id'])

    async def peek(self, *, expected_server_instance_id, auth=None, target_agent_id=None,
                   target_name=None, topic=None, limit=50, since=None):
        integer(limit)
        if limit > 100:
            raise B1Error('INVALID_LIMIT')
        async with self._transaction() as s:
            state, records, keys = await self._context(s, expected_server_instance_id)
            if auth is not None:
                self._verify(auth, records, keys)
            target = await self._resolve(s, state, records, target_agent_id=target_agent_id,
                                         target_name=target_name, active=False)
            q = select(Message, MessageRecipient).join(MessageRecipient, Message.id == MessageRecipient.message_id).where(
                MessageRecipient.agent_id == target.id)
            if topic is not None:
                q = q.where(Message.topic == string(topic, 64))
            if since is not None:
                try:
                    timestamp = datetime.fromisoformat(string(since))
                    if timestamp.utcoffset() is None:
                        raise ValueError
                    timestamp = timestamp.astimezone(timezone.utc)
                except ValueError:
                    raise B1Error('INVALID_SINCE') from None
                q = q.where(Message.created_ts > timestamp)
            result = await s.execute(q.order_by(Message.created_ts.desc(), Message.id.desc()).limit(limit))
            return [{'id': m.id, 'storage_project_id': m.project_id, 'sender_id': m.sender_id,
                     'from': records[m.sender_id].name, 'subject': m.subject, 'body_md': m.body_md,
                     'thread_id': m.thread_id, 'topic': m.topic, 'kind': r.kind,
                     'attachments': m.attachments, 'read_ts': r.read_ts.isoformat() if r.read_ts else None,
                     'ack_ts': r.ack_ts.isoformat() if r.ack_ts else None} for m, r in result]

    async def _decision(self, s, state, records, actor, operation, message_id, recipients):
        parent = await s.get(Message, integer(message_id)) if operation == 'reply' else None
        if operation == 'reply' and parent is None:
            raise B1Error('MESSAGE_NOT_FOUND')
        if recipients is None and parent is not None:
            recipients = [{'target_agent_id': parent.sender_id, 'kind': 'to'}]
        if not isinstance(recipients, list) or not recipients:
            raise B1Error('RECIPIENTS_REQUIRED')
        resolved = []
        for selector in recipients:
            if not isinstance(selector, dict) or set(selector) - {'target_agent_id', 'target_name', 'expected_target_name', 'kind'}:
                raise B1Error('INVALID_RECIPIENT')
            kind = selector.get('kind', 'to')
            if kind not in {'to', 'cc', 'bcc'}:
                raise B1Error('INVALID_RECIPIENT_KIND')
            target = await self._resolve(s, state, records, **{k: v for k, v in selector.items() if k != 'kind'})
            if target.contact_policy == 'block_all':
                raise B1Error('CONTACT_DENIED')
            if target.contact_policy != 'open':
                # Full legacy auto/contact authorization is connected in B4.
                raise B1Error('CONTACT_POLICY_NOT_READY')
            resolved.append({'agent_id': target.id, 'storage_project_id': target.project_id, 'kind': kind})
        if len({x['agent_id'] for x in resolved}) != len(resolved):
            raise B1Error('DUPLICATE_RECIPIENT')
        return {'mail_instance_id': state['instance_id'], 'directory_revision': state['revision'],
                'actor_agent_id': actor.id, 'actor_storage_project_id': actor.project_id,
                'storage_project_id': parent.project_id if parent else actor.project_id,
                'parent_message_id': parent.id if parent else None,
                'thread_id': parent.thread_id if parent else None,
                'recipients': resolved, 'decision_only': True}

    async def owner(self, operation, *, expected_server_instance_id, auth, target_agent_id=None,
                    target_name=None, expected_target_name=None, message_id=None,
                    request_id=None, recipients=None):
        if operation not in {'send', 'reply', 'read', 'ack', 'reconnect', 'consume'}:
            raise B1Error('OPERATION_UNPUBLISHED')
        failure = None
        async with self.owner_transaction(expected_server_instance_id, auth) as ctx:
            s, state, records, actor = ctx.session, ctx.state, ctx.records, ctx.actor
            ctx.assert_live(s)
            if operation == 'reconnect':
                return {'mail_instance_id': state['instance_id'], 'mail_agent_id': actor.id,
                        'credential_generation': actor.credential_generation,
                        'owner_storage_project_id': actor.project_id, 'agent_name': actor.name}
            if operation in {'send', 'reply'}:
                # No message/receipt is accepted while its final writer is absent.
                return await self._decision(s, state, records, actor, operation, message_id, recipients)
            integer(message_id)
            string(request_id)
            intent = {'target_agent_id': target_agent_id, 'target_name': target_name,
                      'expected_target_name': expected_target_name, 'message_id': message_id}
            selector = dict(instance_id=state['instance_id'], actor_id=actor.id,
                            operation=operation, request_id=request_id)
            where = [directory.intents.c[k] == v for k, v in selector.items()]
            previous = (await s.execute(select(directory.intents).where(*where))).mappings().first()
            if previous:
                if previous['intent_hash'] != digest(intent):
                    raise B1Error('REQUEST_INTENT_CONFLICT')
                outcome = json.loads(previous['outcome'])
                if outcome['status'] == 'refused':
                    raise B1Error(outcome['code'], **outcome['details'])
                reference = outcome['result']
                if (await s.get(MessageRecipient, (reference['message_id'], reference['agent_id'])) is None
                        or await s.get(Message, reference['message_id']) is None):
                    raise B1Error('RESULT_REFERENCE_MISSING')
                return reference
            try:
                target = await self._resolve(s, state, records, target_agent_id=target_agent_id,
                    target_name=target_name, expected_target_name=expected_target_name)
                if target.id != actor.id:
                    raise B1Error('TARGET_OWNER_MISMATCH')
                pair = await s.get(MessageRecipient, (message_id, actor.id))
                if pair is None or await s.get(Message, message_id) is None:
                    raise B1Error('NOT_A_RECIPIENT')
                if operation == 'consume':
                    raise B1Error('NOTIFICATION_CONSUMPTION_NOT_READY')
                stamp = datetime.now(timezone.utc)
                pair.read_ts = pair.read_ts or stamp
                if operation == 'ack':
                    pair.ack_ts = pair.ack_ts or stamp
                await s.flush()
                result = {'message_id': message_id, 'agent_id': actor.id,
                          'read_ts': pair.read_ts.isoformat(),
                          'ack_ts': pair.ack_ts.isoformat() if pair.ack_ts else None}
                outcome = {'status': 'accepted', 'result': result}
            except B1Error as error:
                if error.code.endswith('NOT_READY'):
                    raise
                failure = error
                outcome = {'status': 'refused', 'code': error.code, 'details': error.details}
            await s.execute(insert(directory.intents).values(**selector, intent_hash=digest(intent), outcome=canonical(outcome)))
            await directory.refresh(s)
        if failure:
            raise failure
        return result

    async def register_new(self, *, request_id, name, token: str, program, model):
        name_keys(name); string(request_id); string(program); string(model)
        # The supplied token is already held by the trusted registration helper.
        from .storage_origin_contract import token as validate_token
        fp = fingerprint(validate_token(token, fresh=True))
        async with self._transaction(write=True) as s:
            state, records, keys = await directory.validate(s)
            ih = digest({'name': name, 'fingerprint': fp, 'program': program, 'model': model,
                         'origin': state['default_project_id']})
            previous = (await s.execute(select(directory.registrations).where(
                directory.registrations.c.request_id == request_id))).mappings().first()
            if previous:
                if previous['intent_hash'] != ih:
                    raise B1Error('REQUEST_INTENT_CONFLICT')
                a = records[previous['agent_id']]
            else:
                if fp in keys:
                    raise B1Error('CREDENTIAL_UNAVAILABLE')
                if any(a.project_id == state['default_project_id'] and a.name == name for a in records.values()):
                    raise B1Error('NAME_UNAVAILABLE')
                a = Agent(project_id=state['default_project_id'], name=name, program=program, model=model,
                          registration_token=token, contact_policy='open')
                s.add(a)
                try:
                    await s.flush()
                except IntegrityError:
                    raise B1Error('NAME_UNAVAILABLE') from None
                await s.execute(insert(directory.identities).values(agent_id=a.id, name=a.name, project_id=a.project_id))
                await s.execute(insert(directory.credentials).values(fingerprint=fp, state='live', owner_id=a.id,
                                                                     members=canonical([a.id])))
                await s.execute(insert(directory.registrations).values(request_id=request_id, intent_hash=ih, agent_id=a.id))
                await directory.refresh(s, identity_change=True)
            return {'agent_id': a.id, 'name': a.name, 'mail_instance_id': state['instance_id'],
                    'credential_generation': a.credential_generation}

    async def retire(self, agent_id):
        """Private management primitive; never exposed as a model/caller boolean."""
        integer(agent_id)
        async with self._transaction(write=True) as s:
            _, records, _ = await directory.validate(s)
            if agent_id not in records:
                raise B1Error('TARGET_NOT_FOUND')
            records[agent_id].retired_at = records[agent_id].retired_at or datetime.now(timezone.utc)
            await directory.refresh(s, identity_change=True)

    async def recover(self, agent_id, *, expected_generation, new_token):
        """Private same-UID management CAS primitive. B4 connects the socket."""
        integer(agent_id); integer(expected_generation, minimum=0)
        token(new_token, fresh=True)
        async with self._transaction(write=True) as s:
            state, records, keys = await directory.validate(s)
            a = records.get(agent_id)
            if a is None:
                raise B1Error('TARGET_NOT_FOUND')
            if a.credential_generation != expected_generation:
                raise B1Error('CREDENTIAL_GENERATION_MISMATCH')
            integer(expected_generation + 1, minimum=0)
            new_fp = fingerprint(new_token)
            if new_fp in keys:
                raise B1Error('CREDENTIAL_UNAVAILABLE')
            old_fp = fingerprint(a.registration_token) if a.registration_token else None
            if old_fp and keys[old_fp]['state'] != 'burned':
                await s.execute(update(directory.credentials).where(directory.credentials.c.fingerprint == old_fp).values(
                    state='revoked', owner_id=None))
            a.registration_token, a.credential_generation = new_token, expected_generation + 1
            await s.execute(insert(directory.credentials).values(fingerprint=new_fp, state='live',
                owner_id=agent_id, members=canonical([agent_id])))
            origin = await s.get(Project, a.project_id)
            s.add(EnrollmentAudit(request_id=uuid4().hex, peer_uid=os.getuid(), server_instance_id=state['instance_id'],
                project_key=origin.human_key, agent_id=agent_id, operation='recover',
                old_generation=expected_generation, new_generation=a.credential_generation,
                old_fingerprint=old_fp[:16] if old_fp else None, new_fingerprint=new_fp[:16],
                reason='explicit_recovery', result='applied'))
            await directory.refresh(s, identity_change=True)

    async def resolve_v1(self, binding, owner_token):
        """Updater-only lookup after private index preparation/validation.

        Preparation/integrity errors stop the migration phase; they do not
        classify otherwise valid owners as needing credential recovery.
        This is never a raw MCP reconnect fallback.
        """
        try:
            async with self._transaction() as s:
                state, records, keys = await directory.validate(s)
                project_rows = (await s.execute(select(Project).where(Project.human_key == binding['project_key']))).scalars().all()
                if len(project_rows) != 1:
                    raise B1Error('NEEDS_RESOLUTION')
                found = [a for a in records.values() if a.project_id == project_rows[0].id and
                         a.name == binding['agent_name'] and a.program == binding['program']]
                if len(found) != 1:
                    raise B1Error('NEEDS_RESOLUTION')
                a = found[0]
                self._verify({'actor_agent_id': a.id, 'registration_token': owner_token,
                              'expected_credential_generation': a.credential_generation}, records, keys)
                return {'external_id': string(binding['external_id'], 256), 'mail_instance_id': state['instance_id'],
                        'mail_agent_id': a.id, 'credential_generation': a.credential_generation,
                        'owner_storage_project_id': a.project_id, 'agent_name': a.name}
        except B1Error as error:
            if error.code in {'NOT_PREPARED', 'DIRECTORY_STALE', 'INTENT_STORE_STALE'}:
                raise
            raise B1Error('NEEDS_RESOLUTION') from None
        except (KeyError, TypeError):
            raise B1Error('NEEDS_RESOLUTION') from None

    async def check_binding(self, binding, proof):
        if not isinstance(binding, OwnerBinding):
            raise B1Error('BINDING_INVALID')
        async with self._transaction() as s:
            state, records, keys = await self._context(s, binding.mail_instance_id)
            actor = self._verify(proof, records, keys)
            if (actor.id, actor.credential_generation, actor.project_id) != (
                binding.mail_agent_id, binding.credential_generation, binding.owner_storage_project_id):
                raise B1Error('BINDING_OWNER_MISMATCH')
            return {'mail_instance_id': state['instance_id'], 'mail_agent_id': actor.id}

    async def consume(self, request):
        if not isinstance(request, ConsumptionRequest):
            raise B1Error('INVALID_CONSUMPTION_REQUEST')
        proof = {'actor_agent_id': request.auth.actor_agent_id,
                 'registration_token': request.auth.registration_token,
                 'expected_credential_generation': request.auth.expected_credential_generation}
        async with self.owner_transaction(request.mail_instance_id, proof) as ctx:
            if request.target_agent_id != ctx.actor.id:
                raise B1Error('TARGET_OWNER_MISMATCH')
            for message_id in request.message_ids:
                if (await ctx.session.get(MessageRecipient, (message_id, ctx.actor.id)) is None
                        or await ctx.session.get(Message, message_id) is None):
                    raise B1Error('NOT_A_RECIPIENT')
            raise B1Error('NOTIFICATION_CONSUMPTION_NOT_READY')

    async def confirm_delivery(self, target):
        if not isinstance(target, DeliveryTarget):
            raise B1Error('INVALID_DELIVERY_TARGET')
        raise B1Error('DELIVERY_CONFIRMATION_NOT_READY')
