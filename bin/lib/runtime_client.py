"""Explicit isolated S1 client; an absent context preserves the legacy path.

The server wire remains fixtures/global-server-s1.json. No DB reads, ambient
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
import secrets
import shlex
import socket
import stat
import sys
import tempfile
import urllib.request
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
    'metadata': 'runtime/registration-metadata.json', 'profile': 'profile.json'}
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


def configured(path=None):
    explicit = path or os.environ.get('AGENTSTACK_CLIENT_CONFIG')
    selected = absolute(str(explicit)) if explicit else ROOT / 'runtime-client.json'
    if not selected.exists() and not selected.is_symlink():
        if explicit:
            raise ClientError('CONTEXT_UNAVAILABLE')
        return None
    return RuntimeClient(selected)


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
            if match and match[1] in S1_ERRORS:
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
    def __init__(self, path):
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
        if 'runtime_dir' in c or 'credential_file' in c or 'client_root' not in c:
            raise ClientError('FIXED_LAYOUT_CONTEXT_REQUIRED')
        self.client_root = absolute(c['client_root'])
        self.outputs = {role: self.client_root / relative for role, relative in CLIENT_LAYOUT.items()}
        if self.path != self.outputs['context']:
            raise ClientError('FIXED_LAYOUT_CONTEXT_REQUIRED')
        for key in ('runtime_root', 'client_root', 'authority', 'authority_lock', 'management_socket'):
            value = absolute(c.get(key))
            if not value.is_relative_to(self.isolation) or not value.resolve().is_relative_to(self.isolation.resolve()):
                raise ClientError('PATH_OUTSIDE_ISOLATION')
        self.runtime = absolute(c['runtime_root'])
        self.runtime_dir = self.client_root / 'runtime'
        for path in {self.isolation, self.client_root, self.runtime_dir}:
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

        self.validate_path_roles()

    def directory_contains(self, directory, path):
        # Path.resolve does not canonicalize APFS case/Unicode aliases. Match
        # existing directory identities, without folding names on Linux.
        if path.is_relative_to(directory) or path.resolve().is_relative_to(directory.resolve()):
            return True
        if not directory.exists():
            return False
        owned = directory.stat()
        for index, parent in enumerate((path, *path.parents)):
            if index > 64:
                raise ClientError('CLIENT_ROOT_OWNERSHIP_UNKNOWN')
            if parent.exists():
                current = parent.stat()
                if (stat.S_ISDIR(current.st_mode)
                        and (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino)):
                    return True
        return False

    def check_root_ownership(self):
        # Only fixed context filenames are observed; never read another root's
        # contents or walk outside the selected isolated client subtree.
        for base in {self.client_root, self.client_root.resolve()}:
            for ancestor in base.parents:
                if not ancestor.is_relative_to(self.isolation.resolve()):
                    break
                marker = ancestor / CLIENT_LAYOUT['context']
                if marker.exists() or marker.is_symlink():
                    raise ClientError('CLIENT_ROOT_OVERLAP')
        pending = [self.client_root]
        entries = 0
        directories = 0
        while pending:
            directory = pending.pop()
            directories += 1
            # Reserve room for our own later mkdir/atomic outputs before RPC.
            if directories > 128 - len(CLIENT_DIRECTORIES):
                raise ClientError('CLIENT_ROOT_OWNERSHIP_UNKNOWN')
            marker = directory / CLIENT_LAYOUT['context']
            if directory != self.client_root and (marker.exists() or marker.is_symlink()):
                raise ClientError('CLIENT_ROOT_OVERLAP')
            with os.scandir(directory) as listing:
                for entry in listing:
                    entries += 1
                    if entries > 1024 - (len(CLIENT_LAYOUT) + len(CLIENT_DIRECTORIES) + 2):
                        raise ClientError('CLIENT_ROOT_OWNERSHIP_UNKNOWN')
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))

    def output_plan(self):
        """Preflight the complete fixed output set before any network operation."""
        external = [self.authority, self.lock, absolute(self.config['management_socket'])]
        if (self.directory_contains(self.runtime, self.client_root)
                or self.directory_contains(self.client_root, self.runtime)):
            raise ClientError('CLIENT_ROOT_OVERLAP')
        for path in external:
            if self.directory_contains(self.client_root, path):
                raise ClientError('CLIENT_ROOT_OVERLAP')
        self.check_root_ownership()
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
        directory = self.runtime_dir / 'session_index'
        if directory.exists():
            for path in directory.glob('*.json'):
                self.validate_output(path, 'session')
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
            if path.parent != expected or not re.fullmatch(r'[1-9][0-9]*\.json', path.name):
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
        kinds = {'context': KIND, 'credential': 'orrery-global-credential-v1',
                 'registration': REG_PENDING_KIND, 'enrollment': ENROLL_PENDING_KIND,
                 'metadata': METADATA_KIND, 'profile': PROFILE_KIND}
        valid = (row.get('schema_version') == 3 and row.get('binding_kind') == 'global-self') if role == 'session' else row.get('kind') == kinds[role]
        if not valid or any(row.get(k) != v for k, v in self.binding.items()):
            raise ClientError('OUTPUT_SCHEMA_INVALID')
        if role in {'credential', 'enrollment', 'metadata', 'profile', 'session'}:
            owner = getattr(self, '_activating_identity', self.identity)
            if (not integer(row.get('agent_id'), 1) or (owner and row.get('agent_id') != owner['agent_id'])
                    or not owner):
                raise ClientError('RUNTIME_DIR_BELONGS_TO_OTHER_IDENTITY' if role in {'metadata', 'session'} or not owner else 'OUTPUT_OWNER_CONFLICT')
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

    def observe(self):
        row = self.call('whois')
        return {'mode': self.mode, **self.binding, 'agent_id': row['agent_id'],
                'name': row['name'], 'credential_generation': row['credential_generation'],
                'runtime_dir': str(self.runtime_dir), 'window_verification': 'local-only-server-unverified' if 'window_row_id' in self.identity else 'not-mapped',
                **{k: self.identity[k] for k in ('window_row_id', 'window_uuid') if k in self.identity}}

    def reconnect(self, *, program=None, model=None):
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
                    if not block or len(raw) + len(block) > 16384:
                        raise ClientError('MANAGEMENT_RESPONSE_INVALID')
                    raw += block
            reply = json.loads(raw)
            if not isinstance(reply, dict) or not reply.get('ok'):
                reason = reply.get('reason', '') if isinstance(reply, dict) else ''
                raise ClientError(reason if reason in S1_ERRORS else 'MANAGEMENT_REJECTED')
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
        self._activating_identity = identity
        try:
            with self.fence():
                self.write_output(self.credential, credential, 'credential')
            with self.fence():
                self.write_output(self.path, config, 'context')
                # Our intentional local configuration update is accepted, but any
                # concurrent replacement before it was rejected by the fence.
                self.raw = read_private(self.path)
                self.config = config
                self.identity = identity
        finally:
            del self._activating_identity
        if metadata is not None:
            self.save_metadata(metadata['program'], metadata['model'])

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
        if self.identity:
            if self.observe()['agent_id'] != agent_id:
                raise ClientError('REGISTRATION_IDENTITY_MISMATCH')
            return self.observe()
        pending = self.credential.with_suffix('.registration-pending.json')
        saved = read_json(pending)
        if any(saved.get(k) != v for k, v in self.binding.items()):
            raise ClientError('REGISTRATION_BINDING_MISMATCH')
        token = saved.get('registration_token')
        if not isinstance(token, str) or not token:
            raise ClientError('REGISTRATION_PENDING_INVALID')
        current = self.management('inspect', agent_id=agent_id)
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
                self.write_output(pending, {**values, 'new_credential': token}, 'enrollment')
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
            for path in directory.glob('*.json'):
                previous = read_json(path)
                if previous.get('session_id') == session_id and any(previous.get(k) != row.get(k)
                        for k in ('agent_id', 'expected_server_instance_id')):
                    raise ClientError('SESSION_OWNER_CONFLICT')
            value = {'schema_version': 3, 'binding_kind': 'global-self', **row,
                     'agent_name': row['name'], 'session_id': session_id,
                     'transcript_path': payload.get('transcript_path', ''), 'cwd': payload.get('cwd', '')}
            self.write_output(directory / (str(row['agent_id']) + '.json'), value, 'session')
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
                                             'register', 'session', 'inspect', 'claim', 'recover', 'request-status', 'policy', 'save-profile'])
    parser.add_argument('args', nargs='*')
    a = parser.parse_args(argv)
    try:
        client = configured(a.context)
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
            for k in ('agent_id', 'limit', 'window_row_id'):
                if k in args:
                    args[k] = int(args[k])
            for k in ('include_bodies', 'urgent_only'):
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
