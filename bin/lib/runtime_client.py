"""Explicit isolated global client; an absent context preserves the legacy path.

The advertised server wire is fixed by the S1/S2a fixtures. No DB reads, ambient
project routing, automatic enrollment, installed cutover or proxy are added.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import secrets
import shlex
import socket
import stat
import sys
import tempfile
import urllib.request
import uuid
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
KIND = 'orrery-runtime-client-s1'
PROFILE_KIND = 'orrery-global-client-profile-v1'
METADATA_KIND = 'orrery-global-registration-metadata-v1'
REG_PENDING_KIND = 'orrery-global-registration-pending-v1'
ENROLL_PENDING_KIND = 'orrery-global-enrollment-pending-v1'
# The sole client output layout. No caller-selected role paths are accepted.
CLIENT_LAYOUT = {'context': 'runtime-client.json', 'credential': 'credential.json',
    'registration': 'credential.registration-pending.json',
    'enrollment': 'credential.enrollment-pending.json',
    'metadata': 'runtime/registration-metadata.json', 'profile': 'profile.json',
    'mutation': 'runtime/mutation-pending.json', 'mutex': 'runtime/mutation.lock'}
CLIENT_DIRECTORIES = ('runtime', 'runtime/session_index')
S1_ERRORS = frozenset({'NAME_CONFLICT', 'NAME_INVALID', 'OWNER_REQUIRED', 'WRITER_FENCED',
    'STALE_RUNTIME_BINDING', 'WINDOW_INPUT_REQUIRED', 'WINDOW_OWNER_MISMATCH',
    'EXISTING_WINDOW_REQUIRED', 'CREDENTIAL_INVALID', 'CREDENTIAL_GENERATION_CONFLICT',
    'CREDENTIAL_STATE_CONFLICT', 'AGENT_ID_REQUIRED', 'AGENT_NOT_FOUND', 'AGENT_UNAVAILABLE',
    'REQUEST_CONFLICT', 'REQUEST_ID_INVALID', 'REQUEST_NOT_FOUND', 'ENROLLMENT_INPUT_INVALID',
    'STATE_UNAVAILABLE', 'AUTHORITY_LOCK_REPLACED', 'DATABASE_UNSAFE', 'SQLITE_SIDECAR_UNSAFE',
    'LIMIT_INVALID', 'TIMESTAMP_INVALID'})




class ClientError(RuntimeError):
    pass


_CONTRACT = None


def wire_contract():
    global _CONTRACT
    if _CONTRACT is None:
        installed = Path(__file__).with_name('schema_contract.py')
        source = ROOT / 'packages/agentstack_mail/src/agentstack_mail/schema_contract.py'
        try:
            module = runpy.run_path(str(installed if installed.is_file() else source))
            module['fixture'] = module['s2a_fixture']()
        except (OSError, KeyError, ValueError) as exc:
            raise ClientError('SCHEMA_VALIDATOR_UNAVAILABLE') from exc
        _CONTRACT = module
    return _CONTRACT


def s2a_tools(*, receipt=False):
    return {name for name, value in wire_contract()['fixture']['tool_contracts'].items()
            if not receipt or value['operation'] == 'write'}


def known_error(reason):
    if reason in S1_ERRORS:
        return True
    try:
        return reason in wire_contract()['fixture']['fixed_reasons']
    except ClientError:
        return False


def read_private(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            current = os.lstat(path)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1
                    or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)):
                raise ClientError('PRIVATE_FILE_UNSAFE')
            raw = stream.read(1048577)
            if len(raw) > 1048576:
                raise ClientError('PRIVATE_FILE_TOO_LARGE')
            return raw
    except OSError as exc:
        raise ClientError('PRIVATE_FILE_UNAVAILABLE') from exc


def read_json(path):
    try:
        value = json.loads(read_private(path))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError) as exc:
        raise ClientError('PRIVATE_JSON_INVALID') from exc


def atomic_json(path, value, before_replace=None):
    path = Path(path)
    if path.exists() or path.is_symlink():
        read_private(path)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        if before_replace is not None:
            before_replace()
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def absolute(value):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ClientError('CONTEXT_PATH_INVALID')
    return Path(value)


def integer(value, minimum=0):
    return type(value) is int and value >= minimum


def configured(path=None, *, recovery=None):
    explicit = path or os.environ.get('AGENTSTACK_CLIENT_CONFIG')
    selected = absolute(str(explicit)) if explicit else ROOT / 'runtime-client.json'
    if not selected.exists() and not selected.is_symlink():
        if explicit:
            raise ClientError('CONTEXT_UNAVAILABLE')
        return None
    return RuntimeClient(selected, recovery=recovery)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ClientError('TRANSPORT_REDIRECT_REJECTED')


def result_value(reply):
    if not isinstance(reply, dict) or reply.get('error'):
        raise ClientError('RPC_REJECTED')
    result = reply.get('result')
    if not isinstance(result, dict):
        raise ClientError('RESPONSE_INVALID')
    if result.get('isError'):
        # Never echo arbitrary tool text, which can contain secrets. Expose only
        # the S1 fixed uppercase error at the end of FastMCP's error envelope.
        for part in result.get('content', []):
            match = re.search(r'(?:^|: )([A-Z][A-Z_]{2,63})$', part.get('text', ''))
            if match and known_error(match[1]):
                raise ClientError(match[1])
        raise ClientError('TOOL_REJECTED')
    structured = result.get('structuredContent')
    if isinstance(structured, (dict, list)):
        return structured
    for part in result.get('content', []):
        try:
            value = json.loads(part.get('text', ''))
        except (ValueError, AttributeError):
            continue
        if isinstance(value, (dict, list)):
            return value
    raise ClientError('RESPONSE_INVALID')


class RuntimeClient:
    def __init__(self, path, *, recovery=None):
        self.path = absolute(str(path))
        self.raw = read_private(self.path)
        self.config = read_json(self.path)
        c = self.config
        if c.get('kind') != KIND or c.get('mode') not in {'global', 'legacy'}:
            raise ClientError('CONTEXT_INVALID')
        self.mode = c['mode']
        if c.get('activation_enabled') is not False:
            raise ClientError('ACTIVATION_NOT_SUPPORTED')
        if absolute(c.get('wrapper_root')).resolve() != ROOT.resolve():
            raise ClientError('WRAPPER_ROOT_MISMATCH')
        self.isolation = absolute(c.get('isolation_root'))
        if not self.path.is_relative_to(self.isolation):
            raise ClientError('CONTEXT_OUTSIDE_ISOLATION')
        roots = [Path(tempfile.gettempdir()).resolve(), Path('/private/tmp').resolve()]
        if not any(self.isolation.resolve().is_relative_to(p) for p in roots):
            raise ClientError('ISOLATION_REQUIRED')
        if any(k in c for k in ('runtime_dir', 'credential_file', 'client_root')) or not all(k in c for k in ('agentstack_home', 'client_name')):
            raise ClientError('FIXED_LAYOUT_CONTEXT_REQUIRED')
        name = c['client_name']
        if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', name):
            raise ClientError('CLIENT_NAME_INVALID')
        self.client_home = absolute(c['agentstack_home'])
        if self.client_home != self.isolation / 'agentstack':
            raise ClientError('CLIENT_HOME_MISMATCH')
        self.clients_parent = self.client_home / 'clients'
        self.client_root = self.clients_parent / name
        self.outputs = {role: self.client_root / relative for role, relative in CLIENT_LAYOUT.items()}
        if self.path != self.outputs['context']:
            raise ClientError('FIXED_LAYOUT_CONTEXT_REQUIRED')
        for key in ('runtime_root', 'agentstack_home', 'authority', 'authority_lock', 'management_socket'):
            value = absolute(c.get(key))
            if not value.is_relative_to(self.isolation) or not value.resolve().is_relative_to(self.isolation.resolve()):
                raise ClientError('PATH_OUTSIDE_ISOLATION')
        self.runtime = absolute(c['runtime_root'])
        self.runtime_dir = self.client_root / 'runtime'
        for path in {self.isolation, self.client_home, self.clients_parent, self.client_root, self.runtime_dir}:
            info = path.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077):
                raise ClientError('PRIVATE_DIRECTORY_REQUIRED')
        self.lock = absolute(c['authority_lock'])
        self.authority = absolute(c['authority'])
        self.binding = {key: c.get(key) for key in ('expected_server_instance_id',
                         'candidate_generation', 'authority_epoch')}
        if any(not isinstance(v, str) or not v for v in self.binding.values()):
            raise ClientError('BINDING_INVALID')
        if (not isinstance(c.get('lock_identity'), list) or len(c['lock_identity']) != 2
                or any(not integer(v) for v in c['lock_identity'])):
            raise ClientError('LOCK_IDENTITY_INVALID')
        url = urlsplit(c.get('mcp_url', ''))
        if (url.scheme != 'http' or url.hostname not in {'127.0.0.1', '::1'}
                or not url.port or url.username or url.password or url.query or url.fragment):
            raise ClientError('ENDPOINT_NOT_LOCAL')
        self.endpoint = c['mcp_url']
        self.credential = self.outputs['credential']
        self.metadata_path = self.outputs['metadata']
        self.profile_path = self.outputs['profile']
        self.identity = c.get('identity')
        if self.identity is not None and (not isinstance(self.identity, dict)
                or not integer(self.identity.get('agent_id'), 1)
                or not integer(self.identity.get('credential_generation'))):
            raise ClientError('IDENTITY_INVALID')
        if self.identity and (('window_row_id' in self.identity) != ('window_uuid' in self.identity)
                or ('window_row_id' in self.identity and (not integer(self.identity['window_row_id'], 1)
                    or not isinstance(self.identity['window_uuid'], str) or not self.identity['window_uuid']))):
            raise ClientError('WINDOW_BINDING_INVALID')

        self.recovery = recovery
        self.prepare_recovery_owner()
        self.validate_path_roles()

    def directory_contains(self, directory, path):
        # Path.resolve does not canonicalize APFS case/Unicode aliases. Match
        # existing directory identities, without folding names on Linux.
        if path.is_relative_to(directory) or path.resolve().is_relative_to(directory.resolve()):
            return True
        if not directory.exists():
            return False
        owned = directory.stat()
        for parent in (path, *path.parents):
            if parent.exists():
                current = parent.stat()
                if (stat.S_ISDIR(current.st_mode)
                        and (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino)):
                    return True
        return False

    def prepare_recovery_owner(self):
        """Only explicit operator recovery may inspect an interrupted activation."""
        if self.recovery is None or self.recovery == ('resolve-mutation', None):
            return
        action, agent_id = self.recovery
        role = 'registration' if action == 'finalize-create' else 'enrollment'
        pending = self.outputs[role]
        if not pending.exists():
            return
        saved = read_json(pending)
        expected = REG_PENDING_KIND if role == 'registration' else ENROLL_PENDING_KIND
        if saved.get('kind') != expected or any(saved.get(k) != v for k, v in self.binding.items()):
            raise ClientError('RECOVERY_PENDING_MISMATCH')
        row = saved.get('row', {}) if role == 'registration' else saved
        if not isinstance(row, dict):
            raise ClientError('RECOVERY_PENDING_MISMATCH')
        if role == 'enrollment' and (not integer(saved.get('expected_generation'))
                or saved.get('action') != action or not isinstance(saved.get('new_credential'), str)
                or not saved['new_credential']):
            raise ClientError('RECOVERY_PENDING_MISMATCH')
        if row.get('agent_id', agent_id) != agent_id or (self.identity and self.identity['agent_id'] != agent_id):
            raise ClientError('RECOVERY_PENDING_MISMATCH')
        if not integer(agent_id, 1):
            raise ClientError('RECOVERY_PENDING_MISMATCH')
        self._recovery_identity = {'agent_id': agent_id}
        self._recovery_pending = saved

    def clear_recovery_owner(self):
        for field in ('_recovery_identity', '_recovery_pending'):
            if hasattr(self, field):
                delattr(self, field)

    def output_plan(self):
        """Preflight the complete fixed output set before any network operation."""
        external = [self.authority, self.lock, absolute(self.config['management_socket'])]
        if (self.directory_contains(self.runtime, self.clients_parent)
                or self.directory_contains(self.clients_parent, self.runtime)):
            raise ClientError('CLIENT_ROOT_OVERLAP')
        for path in external:
            if self.directory_contains(self.clients_parent, path):
                raise ClientError('CLIENT_ROOT_OVERLAP')
        # Immediate children cannot nest; inspect only the fixed parent chain.
        for directory in (self.client_home, self.clients_parent, self.client_root):
            info = directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ClientError('PRIVATE_DIRECTORY_REQUIRED')
        for relative in CLIENT_DIRECTORIES:
            directory = self.client_root / relative
            if directory.exists() or directory.is_symlink():
                info = directory.lstat()
                if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_mode & 0o077):
                    raise ClientError('PRIVATE_DIRECTORY_REQUIRED')
        paths = list(self.outputs.values()) + external
        identities = [(p.stat().st_dev, p.stat().st_ino) for p in paths if p.exists()]
        if len(set(identities)) != len(identities):
            raise ClientError('CONTEXT_PATH_ROLE_CONFLICT')
        for role, path in self.outputs.items():
            self.validate_output(path, role)
        self.validate_output(self.runtime_dir / 'session_index/self.json', 'session')
        return dict(self.outputs)

    def validate_path_roles(self):
        return self.output_plan()

    def reject_mail_output(self, path):
        path = Path(path)
        if (path.is_relative_to(self.runtime) or path.resolve().is_relative_to(self.runtime.resolve())):
            raise ClientError('CLIENT_OUTPUT_IN_MAIL_STATE')
        if not path.resolve().is_relative_to(self.isolation.resolve()):
            raise ClientError('PATH_OUTSIDE_ISOLATION')

    def validate_output(self, path, role):
        path = Path(path)
        if role == 'session':
            expected = self.runtime_dir / 'session_index'
            if path.parent != expected or path.name != 'self.json':
                raise ClientError('FIXED_LAYOUT_PATH_NOT_SUPPORTED')
        elif role not in self.outputs or path != self.outputs[role]:
            raise ClientError('FIXED_LAYOUT_PATH_NOT_SUPPORTED')
        self.reject_mail_output(path)
        if not path.exists() and not path.is_symlink():
            return
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise ClientError('OUTPUT_FILE_UNSAFE')
        try:
            row = read_json(path)
        except ClientError as exc:
            # Released enrollment writes a private raw token and an identity
            # sidecar. Recognize it without authenticating or converting it.
            if role == 'credential':
                raw = read_private(path).decode('utf-8', errors='replace').strip()
                sidecar = path.with_name(path.name + '.identity.json')
                if 20 <= len(raw) <= 64 and sidecar.exists():
                    identity = read_json(sidecar)
                    if identity.get('kind') == 'orrery-enrollment-active-v1':
                        raise ClientError('LEGACY_FILE_REQUIRES_IMPORT') from exc
                if 20 <= len(raw) <= 64 and re.fullmatch(r'[A-Za-z0-9_-]+', raw):
                    raise ClientError('LEGACY_FILE_REQUIRES_IMPORT') from exc
            raise ClientError('OUTPUT_SCHEMA_INVALID') from exc
        if ((role == 'profile' and row.get('kind') == 'orrery-persistent-agent-v1')
                or (role == 'session' and row.get('schema_version') == 2
                    and row.get('binding_kind') == 'self')):
            raise ClientError('LEGACY_FILE_REQUIRES_IMPORT')
        if role == 'mutex':
            if row != {'kind': 'orrery-client-local-mutex-v1'}:
                raise ClientError('OUTPUT_SCHEMA_INVALID')
            return
        if role == 'mutation':
            required = {'kind','tool','request_id','server_instance_id','candidate_generation','authority_epoch',
                        'agent_id','credential_generation','canonical_payload','request_hash','phase','receipt'}
            if (set(row) != required or row.get('kind') != 'orrery-global-operation-pending-v1'
                    or row.get('tool') not in s2a_tools(receipt=True) or row.get('phase') not in {'planned','committed'}
                    or not integer(row.get('agent_id'),1) or not integer(row.get('credential_generation'))
                    or not isinstance(row.get('canonical_payload'),dict)
                    or not isinstance(row.get('request_id'),str)
                    or re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',row['request_id']) is None):
                raise ClientError('OUTPUT_SCHEMA_INVALID')
            payload = row['canonical_payload']
            if (set(payload) != {'version','tool','server_instance_id','candidate_generation','authority_epoch','agent_id','expected_credential_generation','arguments'}
                    or not isinstance(payload.get('arguments'),dict)
                    or set(payload['arguments']) & {'registration_token','request_id','project_key','to_project','from_project'}):
                raise ClientError('OUTPUT_SCHEMA_INVALID')
            raw = wire_contract()['canonical'](payload)
            if (hashlib.sha256(raw.encode()).hexdigest() != row['request_hash']
                    or payload.get('version') != 'orrery-global-operation-request-v1'
                    or any(payload.get(k) != row[k] for k in ('tool','server_instance_id','candidate_generation','authority_epoch','agent_id'))
                    or payload.get('expected_credential_generation') != row['credential_generation']
                    or not isinstance(payload.get('arguments'),dict)):
                raise ClientError('OUTPUT_SCHEMA_INVALID')
            # Own stale binding is evidence for explicit resolution, never auth.
            # Other identity/instance is only readable through operator recovery.
            if (self.recovery != ('resolve-mutation', None) and
                    (row['server_instance_id'] != self.binding['expected_server_instance_id']
                     or not self.identity or row['agent_id'] != self.identity['agent_id'])):
                raise ClientError('OUTPUT_OWNER_CONFLICT')
            return
        kinds = {'context': KIND, 'credential': 'orrery-global-credential-v1',
                 'registration': REG_PENDING_KIND, 'enrollment': ENROLL_PENDING_KIND,
                 'metadata': METADATA_KIND, 'profile': PROFILE_KIND}
        valid = (row.get('schema_version') == 3 and row.get('binding_kind') == 'global-self') if role == 'session' else row.get('kind') == kinds[role]
        if not valid or any(row.get(k) != v for k, v in self.binding.items()):
            raise ClientError('OUTPUT_SCHEMA_INVALID')
        if role in {'credential', 'enrollment', 'metadata', 'profile', 'session'}:
            owner = getattr(self, '_activating_identity', getattr(self, '_recovery_identity', self.identity))
            if (not integer(row.get('agent_id'), 1) or (owner and row.get('agent_id') != owner['agent_id'])
                    or not owner):
                raise ClientError('RUNTIME_DIR_BELONGS_TO_OTHER_IDENTITY' if role in {'metadata', 'session'} or not owner else 'OUTPUT_OWNER_CONFLICT')
        if role == 'credential' and hasattr(self, '_recovery_pending'):
            pending = self._recovery_pending
            if pending['kind'] == REG_PENDING_KIND:
                if row.get('registration_token') != pending.get('registration_token') or row.get('credential_generation') != pending.get('row', {}).get('credential_generation', 1):
                    raise ClientError('RECOVERY_CREDENTIAL_MISMATCH')
            else:
                generation = pending.get('expected_generation')
                if row.get('credential_generation') == generation + 1 and row.get('registration_token') == pending.get('new_credential'):
                    pass
                elif row.get('credential_generation') != pending.get('previous_credential_generation') or hashlib.sha256(str(row.get('registration_token')).encode()).hexdigest() != pending.get('previous_credential_fingerprint'):
                    raise ClientError('RECOVERY_CREDENTIAL_MISMATCH')
        if role == 'credential' and (not integer(row.get('credential_generation'))
                or not (row.get('registration_token') is None or isinstance(row.get('registration_token'), str))):
            raise ClientError('OUTPUT_SCHEMA_INVALID')
        if role == 'metadata' and not all(isinstance(row.get(k), str) and row[k] for k in ('program', 'model')):
            raise ClientError('OUTPUT_SCHEMA_INVALID')
        if role == 'registration' and (not isinstance(row.get('registration_token'), str)
                or not all(isinstance(row.get(k), str) and row[k] for k in ('program', 'model'))):
            raise ClientError('OUTPUT_SCHEMA_INVALID')
        if role == 'enrollment' and (not integer(row.get('expected_generation'))
                or row.get('action') not in {'claim', 'recover'} or not isinstance(row.get('new_credential'), str)):
            raise ClientError('OUTPUT_SCHEMA_INVALID')
        if role == 'profile' and row.get('client_config') != str(self.path):
            raise ClientError('OUTPUT_OWNER_CONFLICT')

    def write_output(self, path, value, role):
        # mkstemp uses O_EXCL for the private temporary file; rename only after
        # the destination's own format/owner and Mail-root boundary are checked.
        self.output_plan()
        self.validate_output(path, role)
        atomic_json(path, value, before_replace=lambda: self.validate_output(path, role))

    def registration_metadata(self):
        if self.metadata_path.exists() or self.metadata_path.is_symlink():
            self.validate_output(self.metadata_path, 'metadata')
            return read_json(self.metadata_path)
        # Read compatibility only: old contexts stay pinned and are not edited.
        return self.config.get('registration_metadata', {})

    def save_metadata(self, program, model):
        with self.fence():
            self.write_output(self.metadata_path, {'kind': METADATA_KIND, **self.binding,
                'agent_id': self.identity['agent_id'], 'program': program, 'model': model}, 'metadata')

    def _authority(self):
        a = read_json(self.authority)
        expected = {'kind': 'orrery-global-authority-v1', 'phase': 'active',
            'root_status': 'active', 'runtime_root': str(self.runtime),
            'mail_instance_id': self.binding['expected_server_instance_id'],
            'candidate_generation': self.binding['candidate_generation'],
            'authority_epoch': self.binding['authority_epoch']}
        if any(a.get(k) != v for k, v in expected.items()):
            raise ClientError('WRITER_FENCED')
        return a

    @contextmanager
    def fence(self):
        read_private(self.lock)
        fd = os.open(self.lock, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if [info.st_dev, info.st_ino] != self.config['lock_identity']:
                raise ClientError('AUTHORITY_LOCK_REPLACED')
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1):
                raise ClientError('PRIVATE_FILE_UNSAFE')
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ClientError('WRITER_FENCED') from exc
            def check():
                current = os.lstat(self.lock)
                if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                    raise ClientError('AUTHORITY_LOCK_REPLACED')
                read_private(self.lock)
                if read_private(self.path) != self.raw:
                    raise ClientError('CONTEXT_CHANGED')
                self.validate_path_roles()
                return self._authority()
            authority = check()
            if self.mode == 'legacy':
                raise ClientError('LEGACY_CONTEXT_REQUIRES_PR7')
            yield
            if check() != authority:
                raise ClientError('WRITER_FENCED')
        finally:
            os.close(fd)

    def rpc(self, method, params):
        payload = {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}
        request = urllib.request.Request(self.endpoint, method='POST',
            data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json',
                                                        'Accept': 'application/json'})
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            with opener.open(request, timeout=15) as stream:
                raw = stream.read(1048577)
            if len(raw) > 1048576:
                raise ClientError('RESPONSE_TOO_LARGE')
            reply = json.loads(raw)
            if not isinstance(reply, dict) or reply.get('error'):
                raise ClientError('RPC_REJECTED')
            return reply
        except (OSError, ValueError) as exc:
            raise ClientError('TRANSPORT_FAILED') from exc

    def capabilities(self):
        health = result_value(self.rpc('tools/call', {'name': 'health_check', 'arguments': {}}))
        if (not isinstance(health, dict) or health.get('wire_version') != 1
                or health.get('namespace_contract_version') != 2
                or health.get('activation_enabled') is not False
                or health.get('mode') != 'global-preparation' or health.get('status') != 'ok'
                or not integer(health.get('mutation_revision'))
                or any(health.get(k) != self.binding[v] for k, v in (
                    ('server_instance_id', 'expected_server_instance_id'),
                    ('candidate_generation', 'candidate_generation'),
                    ('authority_epoch', 'authority_epoch')))):
            raise ClientError('SERVER_BINDING_MISMATCH')
        listing = self.rpc('tools/list', {}).get('result', {}).get('tools')
        if not isinstance(listing, list):
            raise ClientError('CAPABILITY_INVALID')
        self.output_schemas = {t['name']: t.get('outputSchema') for t in listing
                               if isinstance(t, dict) and isinstance(t.get('name'), str)}
        return health, {t['name']: t['inputSchema'] for t in listing
                        if isinstance(t, dict) and isinstance(t.get('inputSchema'), dict)}

    def local_owner(self):
        if self.identity is None:
            raise ClientError('IDENTITY_REQUIRED')
        value = read_json(self.credential)
        if (value.get('kind') != 'orrery-global-credential-v1'
                or value.get('agent_id') != self.identity['agent_id']
                or value.get('credential_generation') != self.identity['credential_generation']
                or any(value.get(k) != self.identity.get(k) for k in ('window_row_id', 'window_uuid'))
                or any(value.get(k) != v for k, v in self.binding.items())):
            raise ClientError('CREDENTIAL_BINDING_MISMATCH')
        token = value.get('registration_token')
        if not isinstance(token, str) or not token or any(c.isspace() for c in token):
            raise ClientError('CREDENTIAL_INVALID')
        return token

    def validate_identity(self, row):
        if (not isinstance(row, dict) or row.get('agent_id') != self.identity['agent_id']
                or row.get('credential_generation') != self.identity['credential_generation']
                or row.get('retired_at') is not None
                or not isinstance(row.get('name'), str)
                or any(row.get(k) != self.binding[v] for k, v in (
                    ('server_instance_id', 'expected_server_instance_id'),
                    ('candidate_generation', 'candidate_generation'),
                    ('authority_epoch', 'authority_epoch')))):
            raise ClientError('IDENTITY_BINDING_MISMATCH')
        return row

    def call(self, tool, arguments=None):
        with self.fence():
            _, schemas = self.capabilities()
            if tool not in schemas:
                raise ClientError('CAPABILITY_UNAVAILABLE')
            args = dict(arguments or {})
            for key in ('project_key', 'human_key', 'agent_name', 'sender_name', 'name'):
                args.pop(key, None)
            if tool not in {'health_check', 'ensure_project'}:
                args.update(self.binding, agent_id=self.identity['agent_id'] if self.identity else None,
                            registration_token=self.local_owner())
            if 'expected_credential_generation' in schemas[tool].get('properties', {}):
                args['expected_credential_generation'] = self.identity['credential_generation']
            if tool == 'register_agent':
                # A valid token alone does not prove the pinned local credential
                # generation. Check it before this operation changes the row.
                current = result_value(self.rpc('tools/call', {'name': 'whois', 'arguments': {
                    **self.binding, 'agent_id': self.identity['agent_id'],
                    'registration_token': args['registration_token']}}))
                self.validate_identity(current)
            if tool in {'retire_agent', 'unretire_agent'}:
                # whois intentionally refuses a retired row. Local operator
                # inspect supplies its generation without reviving it first.
                inspected = self.management('inspect', agent_id=self.identity['agent_id'])
                if (inspected.get('credential_generation') != self.identity['credential_generation']
                        or inspected.get('credential_fingerprint') != hashlib.sha256(args['registration_token'].encode()).hexdigest()[:16]):
                    raise ClientError('CREDENTIAL_BINDING_MISMATCH')
            if tool == 'register_agent' and self.identity and 'window_row_id' in self.identity:
                args.update(window_row_id=self.identity['window_row_id'], window_uuid=self.identity['window_uuid'])
            if set(args) - set(schemas[tool].get('properties', {})):
                raise ClientError('ARGUMENTS_UNSUPPORTED')
            if 'request_id' in schemas[tool].get('required', []) and tool in s2a_tools(receipt=True):
                return self.s2a_mutation(tool, args, schemas[tool])
            result = result_value(self.rpc('tools/call', {'name': tool, 'arguments': args}))
            if tool in {'whois', 'register_agent'}:
                self.validate_identity(result)
                if tool == 'register_agent' and any(result.get(k) != self.identity.get(k) for k in ('window_row_id', 'window_uuid')):
                    raise ClientError('WINDOW_BINDING_MISMATCH')
            if tool == 'fetch_inbox' and isinstance(result, dict):
                result = result.get('result')
            if tool == 'fetch_inbox' and not isinstance(result, list):
                raise ClientError('RESPONSE_INVALID')
            return result

    @contextmanager
    def mutation_mutex(self):
        path = self.outputs['mutex']
        if not path.exists() and not path.is_symlink():
            # O_EXCL owns this one immutable inode. Never replace a held lock.
            try:
                fd = os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                with os.fdopen(fd,'w') as stream:
                    json.dump({'kind':'orrery-client-local-mutex-v1'},stream)
                    stream.flush();os.fsync(stream.fileno())
            except FileExistsError:
                pass
        self.validate_output(path,'mutex')
        fd = os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            identity = os.fstat(fd)
            try:
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ClientError('MUTATION_PENDING_CONFLICT') from exc
            if (identity.st_dev,identity.st_ino) != (path.stat().st_dev,path.stat().st_ino):
                raise ClientError('PRIVATE_FILE_UNSAFE')
            self.validate_output(path,'mutex')
            yield
            if (identity.st_dev,identity.st_ino) != (path.stat().st_dev,path.stat().st_ino):
                raise ClientError('PRIVATE_FILE_UNSAFE')
        finally:
            os.close(fd)

    def save_mutation(self, value):
        self.write_output(self.outputs['mutation'],value,'mutation')
        fd=os.open(self.runtime_dir,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)

    def s2a_mutation(self, tool, args, schema):
        contract = wire_contract()
        tool_contract = contract['fixture']['tool_contracts'][tool]
        # UUID has a separate local lifecycle; all semantic defaults, TTL and
        # preimage bytes come from exactly the server's stdlib implementation.
        candidate = {**args, 'request_id': args.get('request_id', str(uuid.uuid4()))}
        values = contract['normalize_arguments'](candidate, schema, error_type=ClientError)
        raw, hashed = contract['request_preimage'](tool, values, tool_contract['accepted_ignored_arguments'], error_type=ClientError)
        payload = json.loads(raw)
        path=self.outputs['mutation']
        with self.mutation_mutex():
            existing_pending = path.exists() or path.is_symlink()
            if existing_pending:
                self.validate_output(path,'mutation')
                pending=read_json(path)
                if (pending['server_instance_id']!=self.binding['expected_server_instance_id']
                    or pending['candidate_generation']!=self.binding['candidate_generation']
                    or pending['authority_epoch']!=self.binding['authority_epoch']
                    or pending['agent_id']!=self.identity['agent_id']
                    or pending['credential_generation']!=self.identity['credential_generation']):
                    raise ClientError('MUTATION_PENDING_REQUIRES_RESOLUTION')
                if pending['request_hash']!=hashed or ('request_id' in args and args['request_id']!=pending['request_id']):
                    raise ClientError('MUTATION_PENDING_CONFLICT')
            else:
                request_id=args.get('request_id',str(uuid.uuid4()))
                if not isinstance(request_id,str) or re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',request_id) is None:
                    raise ClientError('REQUEST_ID_INVALID')
                pending={'kind':'orrery-global-operation-pending-v1','tool':tool,'request_id':request_id,
                    'server_instance_id':self.binding['expected_server_instance_id'],
                    **{k:values[k] for k in ('candidate_generation','authority_epoch','agent_id')},
                    'credential_generation':self.identity['credential_generation'],'canonical_payload':payload,
                    'request_hash':hashed,'phase':'planned','receipt':None}
                self.save_mutation(pending)
            values['request_id']=pending['request_id']
            planned_bytes = read_private(path)
            reply = None
            try:
                # Clear only a recognized rejection received from the tool;
                # a transport/local validation exception never proves outcome.
                reply = self.rpc('tools/call', {'name': tool, 'arguments': values})
                result = result_value(reply)
            except ClientError as exc:
                envelope = reply.get('result', {}) if isinstance(reply, dict) else {}
                if (isinstance(envelope, dict) and envelope.get('isError') is True
                        and contract['definitive_rejection'](str(exc), existing_pending=existing_pending, contract=contract['fixture'])):
                    self.clear_mutation(planned_bytes)
                raise
            self.validate_mutation_output(tool, result)
            if result['request_id'] != pending['request_id']:
                raise ClientError('RESPONSE_INVALID')
            if tool in {'refresh_registration', 'set_contact_policy'}:
                self.validate_identity(result)
            else:
                contact = result.get('contact') if tool == 'macro_contact_handshake' else result
                source = values['from_agent_id'] if tool == 'respond_contact' else self.identity['agent_id']
                target = self.identity['agent_id'] if tool == 'respond_contact' else values['to_agent_id']
                if (not isinstance(contact, dict) or contact.get('from_agent_id') != source
                        or contact.get('to_agent_id') != target):
                    raise ClientError('RESPONSE_INVALID')
            pending.update(phase='committed',receipt=result)
            self.save_mutation(pending)
            # A matching result is durable before acknowledging the local slot.
            self.clear_mutation(read_private(path))
            return result

    def clear_mutation(self, expected_bytes):
        path = self.outputs['mutation']
        self.validate_output(path, 'mutation')
        if read_private(path) != expected_bytes:
            raise ClientError('MUTATION_PENDING_CONFLICT')
        path.unlink()
        fd = os.open(self.runtime_dir, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def validate_mutation_output(self, tool, result):
        schema = self.output_schemas.get(tool)
        if not isinstance(schema, dict) or schema.get('type') != 'object':
            raise ClientError('CAPABILITY_INVALID')
        try:
            wire_contract()['validate_schema'](result, schema, 'RESPONSE_INVALID', error_type=ClientError)
        except (TypeError, ValueError, KeyError) as exc:
            raise ClientError('RESPONSE_INVALID') from exc

    def resolve_mutation(self, current, expected_digest, *, expected_old, expected_new, old_agent_id, new_agent_id, confirm=False):
        if not confirm:raise ClientError('MUTATION_PENDING_REQUIRES_RESOLUTION')
        path=self.outputs['mutation']
        with self.mutation_mutex():
            self.validate_output(path,'mutation')
            raw=read_private(path);pending=json.loads(raw)
            if hashlib.sha256(raw).hexdigest()!=expected_digest:
                raise ClientError('MUTATION_PENDING_CONFLICT')
            old={k:pending[k] for k in ('server_instance_id','candidate_generation','authority_epoch')}
            new={'server_instance_id':current.binding['expected_server_instance_id'],
                 **{k:current.binding[k] for k in ('candidate_generation','authority_epoch')}}
            if old!=expected_old or new!=expected_new or pending['agent_id']!=old_agent_id or current.identity['agent_id']!=new_agent_id:
                raise ClientError('MUTATION_PENDING_BINDING_CHANGED')
            if old['server_instance_id']==new['server_instance_id'] and old['candidate_generation']==new['candidate_generation'] and old_agent_id!=new_agent_id:
                raise ClientError('OUTPUT_OWNER_CONFLICT')
            current.call('whois')
            # Only dispose the old local evidence; never replay/repin/rotate.
            self.validate_output(path,'mutation')
            if read_private(path)!=raw:raise ClientError('MUTATION_PENDING_CONFLICT')
            path.unlink()
            fd=os.open(self.runtime_dir,os.O_RDONLY)
            try:os.fsync(fd)
            finally:os.close(fd)
            return {'resolved':True,'request_id':pending['request_id'],'old_outcome':'unknown-no-resend'}

    def observe(self):
        row = self.call('whois')
        result={'mode':self.mode,**self.binding,'agent_id':row['agent_id'],'name':row['name'],
            'credential_generation':row['credential_generation'],'runtime_dir':str(self.runtime_dir),
            'window_verification':'local-only-server-unverified' if 'window_row_id' in self.identity else 'not-mapped',
            **{k:self.identity[k] for k in ('window_row_id','window_uuid') if k in self.identity}}
        if 'window_row_id' in self.identity:
            with self.fence():
                _,schemas=self.capabilities()
            if 'verify_window_identity' in schemas:
                verified=self.call('verify_window_identity',{k:self.identity[k] for k in ('window_row_id','window_uuid')})
                self.validate_identity(verified)
                result.update(window_verification=verified['verification'],window_ready=verified['ready'])
        return result

    def reconnect(self, *, program=None, model=None):
        if program is None and model is None:
            with self.fence():
                _,schemas=self.capabilities()
            if 'refresh_registration' in schemas:
                if 'window_row_id' in self.identity and 'touch_window_identity' in schemas:
                    self.call('touch_window_identity',{k:self.identity[k] for k in ('window_row_id','window_uuid')})
                else:
                    self.call('refresh_registration')
                observation=self.observe()
                observation.update(registration_verified=True,reconnect_mode='refresh-preserving-metadata')
                # Liveness cannot depend on a receipt-bearing policy mutation.
                # Apply policy only through its explicit operator/tool entrance.
                return observation
        metadata = self.registration_metadata()
        if program is None:
            program = metadata.get('program')
        if model is None:
            model = metadata.get('model')
        registered = program is not None or model is not None
        if registered:
            if not all(isinstance(v, str) and v for v in (program, model)):
                raise ClientError('REGISTRATION_METADATA_REQUIRED')
            self.call('register_agent', {'program': program, 'model': model})
            self.save_metadata(program, model)
        # S1 whois omits program/model. Without locally known metadata, only
        # authenticate/observe; never invent labels that overwrite the server.
        observation = self.observe()
        observation['registration_verified'] = registered
        observation['reconnect_mode'] = 'register' if registered else 'observe-only'
        if registered and 'window_row_id' in self.identity:
            observation['window_verification'] = 'server-verified-by-register'
        self.apply_contact_policy()
        return observation

    def apply_contact_policy(self):
        policy = os.environ.get('AGENTSTACK_CONTACT_POLICY', 'open').lower()
        policy = {'closed': 'contacts_only'}.get(policy, policy)
        if policy in {'', 'skip', 'none', 'off', 'disabled'}:
            return
        with self.fence():
            _, schemas = self.capabilities()
            if 'set_contact_policy' in schemas:
                # S1 lacks this capability. No speculative S2 request is sent.
                self.call('set_contact_policy', {'policy': policy})

    def management(self, action, **values):
        with self.fence():
            self.capabilities()
            path = absolute(self.config['management_socket'])
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ClientError('MANAGEMENT_SOCKET_UNSAFE')
            request = {'version': 1, **self.binding, 'action': action, **values}
            with socket.socket(socket.AF_UNIX) as stream:
                stream.settimeout(5)
                stream.connect(str(path))
                stream.sendall(json.dumps(request).encode() + b'\n')
                raw = b''
                while not raw.endswith(b'\n'):
                    block = stream.recv(16384)
                    if not block or len(raw) + len(block) > 1048576:
                        raise ClientError('MANAGEMENT_RESPONSE_INVALID')
                    raw += block
            reply = json.loads(raw)
            if not isinstance(reply, dict) or not reply.get('ok'):
                reason = reply.get('reason', '') if isinstance(reply, dict) else ''
                raise ClientError(reason if known_error(reason) else 'MANAGEMENT_REJECTED')
            if any(reply.get(k) != self.binding[v] for k, v in (
                    ('server_instance_id', 'expected_server_instance_id'),
                    ('candidate_generation', 'candidate_generation'), ('authority_epoch', 'authority_epoch'))):
                raise ClientError('MANAGEMENT_BINDING_MISMATCH')
            if action != 'request_status' and reply.get('agent_id') != values.get('agent_id'):
                raise ClientError('MANAGEMENT_IDENTITY_MISMATCH')
            return reply

    def activate_credential(self, token, row, metadata=None):
        identity = {k: row[k] for k in ('agent_id', 'credential_generation', 'name')}
        if self.identity:
            identity.update({k: self.identity[k] for k in ('window_row_id', 'window_uuid') if k in self.identity})
        config = {**self.config, 'identity': identity}
        credential = {'kind': 'orrery-global-credential-v1', **self.binding,
                      **identity, 'registration_token': token}
        # One activation order for create and enrollment, including replay.
        outputs = [(self.credential, credential, 'credential'), (self.path, config, 'context')]
        if metadata is not None:
            outputs.append((self.metadata_path, {'kind': METADATA_KIND, **self.binding,
                'agent_id': identity['agent_id'], **metadata}, 'metadata'))
        self._activating_identity = identity
        try:
            for path, value, role in outputs:
                with self.fence():
                    self.write_output(path, value, role)
                    if role == 'context':
                        self.raw = read_private(self.path)
                        self.config = config
                        self.identity = identity
        finally:
            del self._activating_identity

    def create(self, name, program, model):
        if self.identity is not None:
            raise ClientError('EXISTING_IDENTITY_REQUIRED')
        if self.credential.exists() or self.credential.is_symlink():
            raise ClientError('CREDENTIAL_ALREADY_PRESENT')
        journal = self.credential.with_suffix('.registration-pending.json')
        with self.fence():
            if journal.exists():
                raise ClientError('REGISTRATION_PENDING_OPERATOR_REQUIRED')
            token = secrets.token_urlsafe(32)
            self.write_output(journal, {'kind': REG_PENDING_KIND, 'name': name, 'program': program, 'model': model, 'registration_token': token, **self.binding}, 'registration')
            _, schemas = self.capabilities()
            if 'register_agent' not in schemas:
                raise ClientError('CAPABILITY_UNAVAILABLE')
            try:
                row = result_value(self.rpc('tools/call', {'name': 'register_agent', 'arguments': {
                    **self.binding, 'name': name, 'program': program, 'model': model,
                    'registration_token': token}}))
            except ClientError as exc:
                if str(exc) in {'NAME_CONFLICT', 'NAME_INVALID'}:
                    journal.unlink()
                raise
            if (not isinstance(row, dict) or any(row.get(k) != self.binding[v] for k, v in (
                    ('server_instance_id', 'expected_server_instance_id'),
                    ('candidate_generation', 'candidate_generation'), ('authority_epoch', 'authority_epoch')))
                    or not integer(row.get('agent_id'), 1) or row.get('credential_generation') != 1
                    or row.get('name') != name or row.get('registration_token') != token):
                raise ClientError('REGISTRATION_RESPONSE_MISMATCH')
            self.write_output(journal, {'kind': REG_PENDING_KIND, 'row': {k: v for k, v in row.items() if k != 'registration_token'},
                                  'program': program, 'model': model, 'registration_token': token, **self.binding}, 'registration')
        self.activate_credential(token, row, {'program': program, 'model': model})
        journal.unlink()
        return self.observe()

    def check_profile_destination(self, path=None):
        if path is not None:
            raise ClientError('FIXED_LAYOUT_PATH_NOT_SUPPORTED')
        return self.output_plan()['profile']

    def save_profile(self, path=None):
        path = self.check_profile_destination(path)
        row = self.observe()
        value = {'kind': PROFILE_KIND, 'client_config': str(self.path), **row}
        with self.fence():
            self.write_output(path, value, 'profile')
        return {'ok': True, 'kind': PROFILE_KIND, 'agent_id': row['agent_id'],
                'profile_path': str(path), 'ready': False}

    def finalize_registration(self, agent_id):
        pending = self.outputs['registration']
        if self.identity and not pending.exists():
            if self.observe()['agent_id'] != agent_id:
                raise ClientError('REGISTRATION_IDENTITY_MISMATCH')
            return self.observe()
        saved = read_json(pending)
        if any(saved.get(k) != v for k, v in self.binding.items()):
            raise ClientError('REGISTRATION_BINDING_MISMATCH')
        token = saved.get('registration_token')
        if not isinstance(token, str) or not token:
            raise ClientError('REGISTRATION_PENDING_INVALID')
        if saved.get('row', {}).get('agent_id', agent_id) != agent_id:
            raise ClientError('REGISTRATION_IDENTITY_MISMATCH')
        current = self.management('inspect', agent_id=agent_id)
        if saved.get('row') and saved['row'].get('credential_generation') != current.get('credential_generation'):
            raise ClientError('REGISTRATION_CREDENTIAL_MISMATCH')
        if current.get('credential_fingerprint') != hashlib.sha256(token.encode()).hexdigest()[:16]:
            raise ClientError('REGISTRATION_CREDENTIAL_MISMATCH')
        with self.fence():
            row = result_value(self.rpc('tools/call', {'name': 'whois', 'arguments': {
                **self.binding, 'agent_id': agent_id, 'registration_token': token}}))
            if (row.get('agent_id') != agent_id or row.get('credential_generation') != current['credential_generation']
                    or any(row.get(k) != self.binding[v] for k, v in (
                        ('server_instance_id', 'expected_server_instance_id'),
                        ('candidate_generation', 'candidate_generation'), ('authority_epoch', 'authority_epoch')))):
                raise ClientError('REGISTRATION_IDENTITY_MISMATCH')
        metadata = {k: saved[k] for k in ('program', 'model') if k in saved}
        self.activate_credential(token, row, metadata if len(metadata) == 2 else None)
        pending.unlink()
        self.clear_recovery_owner()
        return self.observe()

    def enroll(self, action, request_id, expected_generation):
        if not self.identity:
            raise ClientError('IDENTITY_REQUIRED')
        pending = self.credential.with_suffix('.enrollment-pending.json')
        values = {'kind': ENROLL_PENDING_KIND, 'agent_id': self.identity['agent_id'], 'request_id': request_id,
                  'expected_generation': expected_generation, 'action': action, **self.binding}
        with self.fence():
            if pending.exists():
                saved = read_json(pending)
                if any(saved.get(k) != v for k, v in values.items()):
                    raise ClientError('ENROLLMENT_PENDING_CONFLICT')
                token = saved['new_credential']
            else:
                token = secrets.token_urlsafe(32)
                previous = read_json(self.credential) if self.credential.exists() or self.credential.is_symlink() else None
                self.write_output(pending, {**values, 'new_credential': token,
                    'previous_credential_generation': previous['credential_generation'] if previous is not None else None,
                    'previous_credential_fingerprint': hashlib.sha256(str(previous.get('registration_token')).encode()).hexdigest() if previous is not None else None}, 'enrollment')
        receipt = self.management(action, agent_id=self.identity['agent_id'], request_id=request_id,
                                  expected_generation=expected_generation, new_credential=token)
        current = self.management('inspect', agent_id=self.identity['agent_id'])
        fingerprint = hashlib.sha256(token.encode()).hexdigest()[:16]
        if (current.get('credential_fingerprint') != fingerprint
                or current.get('credential_generation') != receipt.get('new_generation')):
            raise ClientError('ENROLLMENT_RECEIPT_STALE')
        # Authenticate the current value before replacing the local credential.
        with self.fence():
            row = result_value(self.rpc('tools/call', {'name': 'whois', 'arguments': {
                **self.binding, 'agent_id': self.identity['agent_id'], 'registration_token': token}}))
            if (row.get('agent_id') != self.identity['agent_id']
                    or row.get('credential_generation') != current['credential_generation']
                    or any(row.get(k) != self.binding[v] for k, v in (
                        ('server_instance_id', 'expected_server_instance_id'),
                        ('candidate_generation', 'candidate_generation'), ('authority_epoch', 'authority_epoch')))):
                raise ClientError('ENROLLMENT_RECEIPT_STALE')
        self.activate_credential(token, row)
        pending.unlink()
        self.clear_recovery_owner()
        return receipt

    def record_session(self, payload):
        row = self.observe()
        response = payload.get('tool_response', payload.get('tool_result'))
        if isinstance(response, str):
            response = json.loads(response)
        if isinstance(response, dict) and 'content' in response:
            response = result_value({'result': response})
        self.validate_identity(response)
        session_id = payload.get('session_id')
        if not isinstance(session_id, str) or not re.fullmatch('[A-Za-z0-9_-]{1,128}', session_id):
            raise ClientError('SESSION_ID_INVALID')
        if 'window_row_id' in self.identity and any(response.get(k) != self.identity[k]
                for k in ('window_row_id', 'window_uuid')):
            raise ClientError('WINDOW_BINDING_MISMATCH')
        directory = self.runtime_dir / 'session_index'
        with self.fence():
            directory.mkdir(mode=0o700, exist_ok=True)
            info = directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ClientError('SESSION_DIRECTORY_UNSAFE')
            previous_path = directory / 'self.json'
            if previous_path.exists():
                previous = read_json(previous_path)
                if any(previous.get(k) != row.get(k) for k in ('agent_id', 'expected_server_instance_id')):
                    raise ClientError('SESSION_OWNER_CONFLICT')
            value = {'schema_version': 3, 'binding_kind': 'global-self', **row,
                     'agent_name': row['name'], 'session_id': session_id,
                     'transcript_path': payload.get('transcript_path', ''), 'cwd': payload.get('cwd', '')}
            self.write_output(directory / 'self.json', value, 'session')
        return value

    def exports(self, provider):
        programs = {'claude': 'claude-code', 'codex': 'codex', 'gemini': 'antigravity'}
        defaults = {'claude': 'claude-code', 'codex': 'codex', 'gemini': 'gemini-3.8-flash-high'}
        model = os.environ.get('AGENTSTACK_' + provider.upper() + '_MODEL') or defaults[provider]
        row = self.reconnect(program=programs[provider], model=model)
        values = {'AGENT_NAME': row['name'], 'AGENTSTACK_AGENT_ID': str(row['agent_id']),
                  'AGENTSTACK_CLIENT_CONFIG': str(self.path), 'AGENTSTACK_RUNTIME_DIR': str(self.runtime_dir),
                  'AGENTSTACK_MCP_URL': self.endpoint, 'AGENTSTACK_RUNTIME_MODE': self.mode}
        values.update(AGENTSTACK_MAIL_INSTANCE_ID=self.binding['expected_server_instance_id'],
                      AGENTSTACK_CANDIDATE_GENERATION=self.binding['candidate_generation'],
                      AGENTSTACK_AUTHORITY_EPOCH=self.binding['authority_epoch'])
        if 'window_uuid' in row:
            values['AGENTSTACK_WINDOW_UUID'] = row['window_uuid']
        stale = ['AGENTSTACK_PROJECT_KEY', 'PROJECT_KEY', 'PARENT_AGENT', 'CHILD_REGISTRATION_TOKEN',
                 'AGENTSTACK_RESERVED_IDENTITY', 'MCP_URL', 'MCP_AGENT_MAIL_TOKEN',
                 'AGENTSTACK_PROXY_AGENT_NAME', 'AGENTSTACK_PROXY_TOKEN_FILE', 'AGENTSTACK_PROXY_PROGRAM',
                 'AGENTSTACK_CODEX_LAUNCH_BINDING', 'AGENTSTACK_CODEX_LAUNCH_ID', 'AGENTSTACK_CODEX_LAUNCH_KIND']
        stale.extend(k for k in os.environ if k.startswith('AGENTSTACK_LOOKUP_'))
        return 'unset ' + ' '.join(shlex.quote(key) for key in stale) + ';\n' + '\n'.join(
            'export ' + key + '=' + shlex.quote(value) for key, value in values.items())

    def launch(self, provider, directory):
        directory = Path(directory).resolve()
        if not directory.is_dir():
            raise ClientError('WORKING_DIRECTORY_UNAVAILABLE')
        home = Path(os.environ.get('HOME', '')).resolve()
        if not home.is_relative_to(self.isolation.resolve()):
            raise ClientError('ISOLATED_PROVIDER_HOME_REQUIRED')
        for key in ('CODEX_HOME', 'CLAUDE_CONFIG_DIR'):
            if os.environ.get(key) and not Path(os.environ[key]).resolve().is_relative_to(self.isolation.resolve()):
                raise ClientError('ISOLATED_PROVIDER_HOME_REQUIRED')
        exports = self.exports(provider)
        binary = os.environ.get('AGENTSTACK_' + provider.upper() + '_BIN',
                                {'claude': 'claude', 'codex': 'codex', 'gemini': 'agy'}[provider])
        args = [binary]
        if provider == 'codex':
            args += ['-C', str(directory), '--sandbox', os.environ.get('AGENTSTACK_CODEX_SANDBOX', 'workspace-write'),
                     '--ask-for-approval', os.environ.get('AGENTSTACK_CODEX_APPROVAL', 'on-request'),
                     '-c', 'check_for_update_on_startup=false']
        if provider == 'gemini':
            args += ['--model', os.environ.get('AGENTSTACK_GEMINI_MODEL', 'gemini-3.8-flash-high'),
                     '--effort', os.environ.get('AGENTSTACK_GEMINI_EFFORT', 'high')]
        # The short bash step evaluates only shell-quoted values generated here,
        # not user command text. Real providers are never started by the tests.
        with self.fence():
            os.chdir(directory)
            os.execv('/bin/bash', ['/bin/bash', '-c', exports + '\nexec "$@"', 'global-launch', *args])


def profile_operation(path, operation):
    # Legacy validation remains in its existing entrypoint, with its original
    # reasons and format; only the explicit new kind selects this adapter.
    try:
        global_profile = json.loads(Path(path).read_text()).get('kind') == PROFILE_KIND
    except (OSError, ValueError, AttributeError):
        global_profile = False
    if not global_profile:
        context = configured()
        if context is not None:
            with context.fence():
                pass
            raise ClientError('LEGACY_PROFILE_CONTEXT_CONFLICT')
        return None
    p = read_json(path)
    client = configured(absolute(p.get('client_config')))
    if client is None or client.mode != 'global':
        raise ClientError('PROFILE_CONTEXT_REQUIRED')
    if absolute(str(path)) != client.profile_path:
        raise ClientError('FIXED_LAYOUT_PATH_NOT_SUPPORTED')
    row = client.observe()
    if any(p.get(k) != row.get(k) for k in ('agent_id', 'credential_generation',
            'expected_server_instance_id', 'candidate_generation', 'authority_epoch', 'window_row_id', 'window_uuid')):
        raise ClientError('PROFILE_BINDING_MISMATCH')
    if operation == 'run':
        raise ClientError('GLOBAL_PROXY_RUNTIME_REQUIRES_PR4C')
    if operation == 'reconnect':
        row = client.reconnect()
    return {'ok': True, 'kind': PROFILE_KIND, **row, 'ready': False,
            'runtime_execution': 'requires-pr4c'}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--context')
    parser.add_argument('operation', choices=['mode', 'observe', 'reconnect', 'bootstrap', 'launch', 'call',
                                             'register', 'session', 'inspect', 'claim', 'recover', 'request-status', 'policy', 'save-profile', 'resolve-mutation'])
    parser.add_argument('args', nargs='*')
    a = parser.parse_args(argv)
    try:
        recovery = None
        if a.operation == 'resolve-mutation':
            recovery = ('resolve-mutation', None)
        if a.operation in {'claim', 'recover'}:
            selected = a.context or os.environ.get('AGENTSTACK_CLIENT_CONFIG') or str(ROOT / 'runtime-client.json')
            config = read_json(Path(selected))
            recovery = (a.operation, (config.get('identity') or {}).get('agent_id'))
        client = configured(a.context, recovery=recovery)
        if a.operation == 'mode':
            if client is not None:
                with client.fence():
                    pass
            print(client.mode if client else 'legacy')
            return 0
        if client is None:
            raise ClientError('CONTEXT_REQUIRED')
        if a.operation == 'observe':
            value = client.observe()
        elif a.operation == 'reconnect':
            value = client.reconnect(program=a.args[0] if a.args else None,
                                     model=a.args[1] if len(a.args) > 1 else None)
        elif a.operation == 'bootstrap':
            print(client.exports(a.args[0]))
            return 0
        elif a.operation == 'launch':
            client.launch(*a.args)
            return 0
        elif a.operation == 'call':
            args = dict(x.split('=', 1) for x in a.args[1:])
            for k in ('agent_id', 'limit', 'window_row_id','to_agent_id','from_agent_id','after_id','ttl_seconds'):
                if k in args:
                    args[k] = int(args[k])
            for k in ('include_bodies', 'urgent_only','accept','auto_accept'):
                if k in args:
                    args[k] = args[k].lower() == 'true'
            value = client.call(a.args[0], args)
        elif a.operation == 'register':
            value = client.create(*a.args)
        elif a.operation == 'session':
            value = client.reconnect(program=a.args[0], model=a.args[1]) if client.identity else client.create(a.args[2], a.args[0], a.args[1])
        elif a.operation == 'inspect':
            value = client.management('inspect', agent_id=client.identity['agent_id'])
        elif a.operation in {'claim', 'recover'}:
            value = client.enroll(a.operation, a.args[0], int(a.args[1]))
        elif a.operation == 'policy':
            client.apply_contact_policy()
            value = {'ok': True}
        elif a.operation == 'resolve-mutation':
            proof=json.loads(a.args[2])
            current=configured(a.args[1])
            value=client.resolve_mutation(current,a.args[0],**proof)
        elif a.operation == 'request-status':
            value = client.management('request_status', request_id=a.args[0])
        else:
            value = client.save_profile(a.args[0] if a.args else None)
        print(json.dumps(value))
        return 0
    except (ClientError, OSError, ValueError, TypeError, KeyError, IndexError) as exc:
        reason = str(exc) if isinstance(exc, ClientError) else 'CLIENT_INPUT_INVALID'
        print('runtime-client: ' + reason, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
