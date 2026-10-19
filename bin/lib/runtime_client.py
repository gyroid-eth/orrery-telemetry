"""Explicit isolated global client; an absent context preserves the legacy path.

The advertised server wire is fixed by the S1/S2a fixtures. No DB reads, ambient
project routing, automatic enrollment, installed cutover or proxy are added.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
try:
    import fcntl
except ImportError:  # legacy dashboard startup is portable; global remains Unix-only
    fcntl = None
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
from datetime import datetime, timezone, timedelta
import subprocess
import time
import threading
from functools import wraps
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
# This client is also loaded with runpy by the installed operator/dashboard.
# runpy does not add the script's directory to sys.path as direct execution
# does. Resolve sibling private helpers from this same fixed payload, not cwd.
_LIBRARY = str(Path(__file__).resolve().parent)
if _LIBRARY not in sys.path:sys.path.insert(0,_LIBRARY)
_PROCESS_API = runpy.run_path(str(Path(__file__).with_name('process_observation.py')))
ClientError = _PROCESS_API['ClientError']
integer = _PROCESS_API['integer']
remaining = _PROCESS_API['remaining']
process_start = _PROCESS_API['process_start']
request_provider_exit = _PROCESS_API['request_provider_exit']
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
    'mutation': 'runtime/mutation-pending.json', 'mutex': 'runtime/mutation.lock', 'child': 'runtime/child-state.json',
    'operation_lock': 'runtime/child.lock', 'task': 'runtime/task.md', 'launch': 'runtime/launch.json'}
CLIENT_DIRECTORIES = ('runtime', 'runtime/session_index', 'runtime/live-sessions', 'runtime/provider', 'runtime/release-debounce', 'runtime/provider-home')


def validate_child_tools(options):
    """Validate an explicit API selection BEFORE admission/locks/effects.

    Keep literal values untouched; normalization is the existing tools grammar,
    not permission approval or a preview nonempty-tools exception. Old minimal
    inputs without a parsed selection retain their existing preparation path.
    """
    if 'parsed_tools' not in options:return
    policy=runpy.run_path(str(ROOT/'hooks/child_tools.py'))
    try:policy['normalize'](options.get('base','default'),options['parsed_tools'])
    except policy['ToolsError']:raise ClientError('CHILD_TOOLS_INVALID') from None

S1_ERRORS = frozenset({'NAME_CONFLICT', 'NAME_INVALID', 'OWNER_REQUIRED', 'WRITER_FENCED',
    'STALE_RUNTIME_BINDING', 'WINDOW_INPUT_REQUIRED', 'WINDOW_OWNER_MISMATCH',
    'EXISTING_WINDOW_REQUIRED', 'CREDENTIAL_INVALID', 'CREDENTIAL_GENERATION_CONFLICT',
    'CREDENTIAL_STATE_CONFLICT', 'AGENT_ID_REQUIRED', 'AGENT_NOT_FOUND', 'AGENT_UNAVAILABLE',
    'REQUEST_CONFLICT', 'REQUEST_ID_INVALID', 'REQUEST_NOT_FOUND', 'ENROLLMENT_INPUT_INVALID',
    'STATE_UNAVAILABLE', 'AUTHORITY_LOCK_REPLACED', 'DATABASE_UNSAFE', 'SQLITE_SIDECAR_UNSAFE',
    'LIMIT_INVALID', 'TIMESTAMP_INVALID'})






_CONTRACT = None


def wire_contract():
    global _CONTRACT
    if _CONTRACT is None:
        installed = Path(__file__).with_name('schema_contract.py')
        source = ROOT / 'packages/agentstack_mail/src/agentstack_mail/schema_contract.py'
        try:
            module = runpy.run_path(str(installed if installed.is_file() else source))
            module['fixture_loader'] = module['fixture']
            module['fixture'] = module['fixture_loader']('global-server-s2c.json')
            contracts = {}
            reasons = set()
            for family in ('s2a', 's2b', 's2c'):
                loaded = module['fixture_loader']('global-server-' + family + '.json')
                declared = loaded.get('fixed_reasons',{})
                reasons.update(declared if isinstance(declared,list) else [r for rows in declared.values() for r in rows])
                delta = loaded.get('rejection_delta',{}).get('append',{})
                reasons.update(r for rows in delta.values() for r in rows)
                contracts.update(loaded['tool_contracts'])
                for name, changes in loaded.get('existing_tool_changes',{}).items():
                    if isinstance(changes,dict) and 'input_schema' in changes:
                        contracts[name] = {**contracts.get(name,{}),**changes}
            module['fixture']['tool_contracts'] = contracts
            module['fixture']['fixed_reasons'] = sorted(reasons)
        except (OSError, KeyError, ValueError) as exc:
            raise ClientError('SCHEMA_VALIDATOR_UNAVAILABLE') from exc
        _CONTRACT = module
    return _CONTRACT


_MESSAGE_CONTRACT = None


def message_contract():
    global _MESSAGE_CONTRACT
    if _MESSAGE_CONTRACT is not None:
        return _MESSAGE_CONTRACT
    import types
    shared = wire_contract()
    module = types.ModuleType('schema_contract')
    module.__dict__.update(shared)
    module.fixture = shared['fixture_loader']
    previous = {key:sys.modules.get(key) for key in ('schema_contract','global_s2b_contract')}
    sys.modules['schema_contract'] = module
    try:
        def load(name):
            installed = Path(__file__).with_name(name + '.py')
            source = ROOT / ('packages/agentstack_mail/src/agentstack_mail/' + name + '.py')
            return runpy.run_path(str(installed if installed.is_file() else source))
        messages = load('global_s2b_contract')
        m = types.ModuleType('global_s2b_contract');m.__dict__.update(messages)
        sys.modules['global_s2b_contract'] = m
        leases = load('global_s2c_contract')
        # Use the SAME intent functions with family-specific normative
        # normalization/preimage, never a handwritten client approximation.
        original_args, original_hash = messages['arguments'], messages['preimage']
        message_names = set(messages['TOOLS'])
        lease_names = set(leases['TOOLS'])
        def arguments(tool, supplied):
            if tool in lease_names:
                return leases['arguments'](tool,supplied)
            return original_args(tool,supplied)
        def preimage(tool, supplied):
            if tool in lease_names:
                return leases['preimage'](tool,supplied)
            return original_hash(tool,supplied)
        namespace = messages['make_intent'].__globals__
        namespace['TOOLS'] = messages['TOOLS'] | leases['TOOLS']
        namespace['arguments'] = arguments
        namespace['preimage'] = preimage
        _MESSAGE_CONTRACT = {**messages, 'TOOLS':namespace['TOOLS'], 'message_names':message_names}
        return _MESSAGE_CONTRACT
    finally:
        for key,value in previous.items():
            if value is None:sys.modules.pop(key,None)
            else:sys.modules[key]=value


_UI_CONTRACT = None


def operator_schemas():
    global _UI_CONTRACT
    if _UI_CONTRACT is None:
        import types
        modules = {'schema_contract':wire_contract(), 'global_s2b_contract':message_contract()}
        previous = {name:sys.modules.get(name) for name in modules}
        try:
            for name, values in modules.items():
                module = types.ModuleType(name)
                module.__dict__.update(values)
                if name == 'schema_contract':
                    module.fixture = values['fixture_loader']
                sys.modules[name] = module
            installed = Path(__file__).with_name('global_ui_contract.py')
            source = ROOT / 'packages/agentstack_mail/src/agentstack_mail/global_ui_contract.py'
            _UI_CONTRACT = runpy.run_path(str(installed if installed.is_file() else source))
        finally:
            for name,value in previous.items():
                if value is None:sys.modules.pop(name,None)
                else:sys.modules[name]=value
    return _UI_CONTRACT


def management_exchange(config, request, *, deadline=None):
    """One private socket transport; callers validate their response contract."""
    path = absolute(config['management_socket'])
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ClientError('MANAGEMENT_SOCKET_UNSAFE')
    with socket.socket(socket.AF_UNIX) as stream:
        stream.settimeout(remaining(deadline, 5))
        stream.connect(str(path))
        stream.sendall(json.dumps(request).encode() + b'\n')
        raw = b''
        while not raw.endswith(b'\n'):
            stream.settimeout(remaining(deadline, 5))
            block = stream.recv(16384)
            if not block or len(raw) + len(block) > 1048576:
                raise ClientError('MANAGEMENT_RESPONSE_INVALID')
            raw += block
    reply = json.loads(raw)
    return reply


def operator_exchange(config, binding, query, *, deadline=None):
    """One schema and binding check for client and operator UI reads."""
    schemas = operator_schemas()
    validate = wire_contract()['validate_schema']
    request = {'version': 1, 'action': 'ui_read', **binding, 'query': query}
    try:
        validate(request, schemas['REQUEST'])
    except ValueError as exc:
        raise ClientError('QUERY_INVALID') from exc
    reply = (management_exchange(config, request) if deadline is None
             else management_exchange(config, request, deadline=deadline))
    if not isinstance(reply, dict) or reply.get('ok') is not True:
        raise ClientError(reply.get('reason', 'MANAGEMENT_REJECTED') if isinstance(reply, dict) else 'MANAGEMENT_REJECTED')
    try:
        validate(reply, schemas['RESPONSE'])
    except ValueError as exc:
        raise ClientError('RESPONSE_INVALID') from exc
    if reply['binding'] != binding:
        raise ClientError('MANAGEMENT_BINDING_MISMATCH')
    if reply['query_kind'] != query['kind'] or (query['kind'] == 'messages' and reply.get('tool') != query['tool']):
        raise ClientError('RESPONSE_INVALID')
    return reply


def s2a_tools(*, receipt=False):
    return {name for name, value in wire_contract()['fixture']['tool_contracts'].items()
            if not receipt or value.get('operation') == 'write'}


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


def _read_intent_task_source(path):
    # Caller-owned input follows the configure/materialization contract, not
    # private runtime-state permissions. The published copy stays private.
    from local_model_auth import secure_read
    try:
        return secure_read(path, os.getuid())
    except (OSError, TypeError, ValueError):
        raise ClientError('LOCAL_MODEL_TASK_CHANGED') from None


def read_json(path):
    try:
        value = json.loads(read_private(path))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError) as exc:
        raise ClientError('PRIVATE_JSON_INVALID') from exc


def atomic_bytes(path, raw, before_replace=None):
    path = Path(path)
    if path.exists() or path.is_symlink():
        read_private(path)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if before_replace is not None:
            before_replace()
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_json(path, value, before_replace=None):
    atomic_bytes(path, json.dumps(value).encode(), before_replace)


def absolute(value):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ClientError('CONTEXT_PATH_INVALID')
    return Path(value)




def _legacy_session_proof():
    """Read the existing non-secret self binding, never a token or project hint."""
    name = os.environ.get('AGENT_NAME')
    session = os.environ.get('AGENTSTACK_SESSION_ID')
    project = os.environ.get('AGENTSTACK_PROJECT_KEY')
    if not name and not session:
        return False
    directory = Path(os.environ.get('AGENTSTACK_RUNTIME_DIR', str(ROOT / 'runtime'))) / 'session_index'
    matches = []
    try:
        if directory.is_symlink():
            raise ClientError('LEGACY_SESSION_MODE_UNRESOLVED')
        for path in directory.glob('*.json'):
            if path.is_symlink():
                continue
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or info.st_mode & 0o022):
                raise ClientError('LEGACY_SESSION_MODE_UNRESOLVED')
            row = json.loads(path.read_text())
            if not isinstance(row, dict):
                raise ClientError('LEGACY_SESSION_MODE_UNRESOLVED')
            if (row.get('schema_version') == 2 and row.get('binding_kind') == 'self'
                    and integer(row.get('agent_id'), 1)
                    and row.get('agent_name') and row.get('session_id')
                    and row.get('registered_by') in ('', row['agent_name'])
                    and (not name or row['agent_name'] == name)
                    and (not session or row['session_id'] == session)
                    and (not project or row.get('project_key') == project)):
                matches.append(row)
    except (OSError, ValueError, TypeError):
        raise ClientError('LEGACY_SESSION_MODE_UNRESOLVED')
    if len(matches) != 1:
        raise ClientError('LEGACY_SESSION_MODE_UNRESOLVED')
    return True


def selected(argv=None, *, path=None, bootstrap=False):
    """The sole mode selector. Selection is ephemeral and does no owner/network IO.

    Only a new top-level process requests bootstrap; hooks inherit its mode.
    Unknown options belong to the caller and are deliberately left untouched.
    """
    argv = list(argv or [])
    explicit = path
    mode = os.environ.get('AGENTSTACK_RUNTIME_MODE')
    config = os.environ.get('AGENTSTACK_GLOBAL_SERVER_CONFIG')
    source = ('installed-default' if '--installed-default' in argv else 'inherited-mode') if mode is not None else 'default'
    for index, argument in enumerate(argv):
        for flag in ('--context', '--runtime-mode', '--server-config'):
            if argument != flag and not argument.startswith(flag + '='):
                continue
            if argument == flag:
                if index + 1 == len(argv) or argv[index + 1].startswith('--'):
                    raise ClientError('CONTEXT_UNAVAILABLE' if flag == '--context' else 'RUNTIME_SELECTION_INVALID')
                value = argv[index + 1]
            else:
                value = argument.split('=', 1)[1]
            if not value:
                raise ClientError('CONTEXT_UNAVAILABLE' if flag == '--context' else 'RUNTIME_SELECTION_INVALID')
            if flag == '--context': explicit = value
            elif flag == '--runtime-mode': mode, source = value, 'explicit-mode'
            else: config = value
    if bootstrap and mode is None:
        env = Path(os.environ.get('AGENTSTACK_HOME', str(ROOT))) / 'env.sh'
        if env.exists() or env.is_symlink():
            if not env.is_file() or not os.access(env, os.R_OK):
                raise ClientError('RUNTIME_DEFAULT_UNAVAILABLE')
            # Reuse the existing shell configuration syntax, once at bootstrap.
            reply = subprocess.run(['/bin/bash', '-c',
                'set -e; . "$1" >&2; printf "%s\\0%s" "${AGENTSTACK_RUNTIME_MODE-legacy}" "${AGENTSTACK_GLOBAL_SERVER_CONFIG-}"',
                'runtime-default', str(env)], capture_output=True)
            if reply.returncode:
                raise ClientError('RUNTIME_DEFAULT_UNAVAILABLE')
            try: mode, default_config = reply.stdout.decode().split('\0')
            except (UnicodeError, ValueError): raise ClientError('RUNTIME_DEFAULT_UNAVAILABLE')
            config = config or default_config or None
            source = 'installed-default'
    if mode is not None and mode not in ('legacy', 'global'):
        raise ClientError('RUNTIME_MODE_INVALID')
    explicit = explicit or os.environ.get('AGENTSTACK_CLIENT_CONFIG')
    context = absolute(str(explicit)) if explicit else ROOT / 'runtime-client.json'
    # Disabled global hooks do no owner IO. Legacy hooks retain their old path.
    if '--disabled-hook' in argv and os.environ.get('AGENTSTACK_MAIL_DISABLED') == '1':
        if mode == 'global' or (mode is None and (explicit or config or context.exists() or context.is_symlink())):
            return {'mode': 'global', 'context': None, 'server_config': None,
                    'source': 'disabled-hook', 'reason': None}
    if context.exists() or context.is_symlink():
        document = read_json(context)
        if document.get('kind') != KIND or document.get('mode') != 'global':
            raise ClientError('LEGACY_CONTEXT_REQUIRES_PR7')
        context_mode = document['mode']
        if mode is not None and mode != context_mode and source != 'installed-default':
            raise ClientError('RUNTIME_MODE_CONTEXT_CONFLICT')
        mode = context_mode
        source = 'context'
    else:
        if explicit:
            raise ClientError('CONTEXT_UNAVAILABLE')
        context = None
    config_path = absolute(config) if config else None
    if config_path is not None:
        server = read_json(config_path)
        if server.get('kind') != 'orrery-global-server-s2c-v1':
            raise ClientError('ISOLATED_CONFIG_REQUIRED')
        if mode == 'legacy':
            raise ClientError('RUNTIME_MODE_CONTEXT_CONFLICT')
        mode = 'global'
        if context is not None:
            pairs = [('expected_server_instance_id','mail_instance_id'),
                     ('candidate_generation','candidate_generation'),('authority_epoch','authority_epoch'),
                     ('runtime_root','runtime_root'),('authority','authority'),('authority_lock','authority_lock')]
            if any(document.get(local) != server.get(remote) for local, remote in pairs):
                raise ClientError('SERVER_BINDING_MISMATCH')
    if context is None and mode == 'global' and source == 'installed-default' and _legacy_session_proof():
        mode, source = 'legacy', 'legacy-session-proof'
    return {'mode': mode or 'legacy', 'context': context,
            'server_config': config_path,
            'source': source, 'reason': None}


def configured(path=None, *, recovery=None, selection=None, read_only=False):
    choice = selection or selected(sys.argv[1:] if path is None else [], path=path)
    if choice['mode'] == 'legacy':
        return None
    if choice['context'] is None:
        raise ClientError('CONTEXT_UNAVAILABLE')
    return RuntimeClient(choice['context'], recovery=recovery,read_only=read_only)


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


def validate_client_name(value):
    if not isinstance(value,str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}',value):
        raise ClientError('CLIENT_NAME_INVALID')
    return value


def local_endpoint(value):
    url = urlsplit(value)
    if (url.scheme != 'http' or url.hostname not in {'127.0.0.1','::1'}
            or not url.port or url.username or url.password or url.query or url.fragment):
        raise ClientError('ENDPOINT_NOT_LOCAL')
    return value




def validate_owner_receipt(inspected, identity, token, binding):
    if not isinstance(inspected, dict) or inspected.get('ok') is not True:
        raise ClientError('MANAGEMENT_REJECTED')
    if any(inspected.get(k) != binding[v] for k,v in (
            ('server_instance_id','expected_server_instance_id'),
            ('candidate_generation','candidate_generation'),('authority_epoch','authority_epoch'))):
        raise ClientError('MANAGEMENT_BINDING_MISMATCH')
    if (inspected.get('agent_id') != identity['agent_id']
            or inspected.get('credential_generation') != identity['credential_generation']
            or not isinstance(token,str)
            or inspected.get('credential_fingerprint') != hashlib.sha256(token.encode()).hexdigest()[:16]):
        raise ClientError('CREDENTIAL_BINDING_MISMATCH')
    return inspected


# Bounded wait for a mutex whose creator has made the file but not yet written
# its fixed content. Creation is a few syscalls; this only has to cover load.
MUTEX_CREATION_WAIT = 10.0
MUTEX_KINDS = {'operation_lock': 'orrery-client-operation-mutex-v1',
               'mutex': 'orrery-client-local-mutex-v1'}


def mutex_bytes(kind):
    return json.dumps({'kind': kind}).encode()


def create_mutex_inode(path, kind):
    """Create the mutex the way every client version does: O_EXCL on the path.

    Older clients create the same fixed path with O_EXCL and then write the
    content. Doing the same keeps one inode for old and new callers alike: the
    loser of O_EXCL uses the winner's inode, and a published inode is never
    replaced. The content lands after the create, so readers wait for it with
    wait_for_mutex_content instead of reading an empty file as invalid.
    """
    if path.exists() or path.is_symlink():
        return
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        return
    created = None
    data = mutex_bytes(kind)
    try:
        created = os.fstat(fd)
        remaining = data
        while remaining:
            remaining = remaining[os.write(fd, remaining):]
        os.fsync(fd)
    except BaseException:
        # Do not leave our own partial file for every later caller to time out
        # on. The file's bytes, not how far this call got, decide: nobody
        # locks the inode before its content is whole, and once it is whole
        # another caller may already hold it, so a whole file is kept. The
        # non-blocking lock also keeps it if anyone holds it. Remove only the
        # inode this call created, never another one.
        try:
            current = os.lstat(path)
            if (created and os.fstat(fd).st_size < len(data)
                    and (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino)):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.unlink(path)
        except OSError:
            pass
        raise
    finally:
        os.close(fd)


def mutex_content_pending(path, kind):
    """True while the file holds a strict prefix, empty included, of its content.

    Any other state, including an unreadable or unsafe file, is left to the
    caller's own checks so that their reasons stay unchanged.
    """
    try:
        raw = read_private(path)
    except ClientError:
        return False
    expected = mutex_bytes(kind)
    return len(raw) < len(expected) and expected.startswith(raw)


def wait_for_mutex_content(path, kind):
    """Wait for a creator to finish writing; a remnant that stays short fails.

    MUTEX_INCOMPLETE is left by a creator that stopped between the create and
    the write. It is never removed or rewritten here, because a slow creator
    still writing looks the same; see the operator recovery in the docs.
    """
    deadline = time.monotonic() + MUTEX_CREATION_WAIT
    while mutex_content_pending(path, kind):
        if time.monotonic() >= deadline:
            raise ClientError('MUTEX_INCOMPLETE')
        time.sleep(0.02)


def check_operation_inode(path):
    if read_json(path) != {'kind': 'orrery-client-operation-mutex-v1'}:
        raise ClientError('OUTPUT_SCHEMA_INVALID')


@contextmanager
def observation_inode(path, *, exclusive=False):
    """Share the existing writer inode; never create it or wait for a writer."""
    if mutex_content_pending(path, 'orrery-client-operation-mutex-v1'):
        raise ClientError('OBSERVATION_BUSY')
    check_operation_inode(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise ClientError('OBSERVATION_MUTEX_UNAVAILABLE') from exc
    try:
        info = os.fstat(fd)
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ClientError('OBSERVATION_BUSY') from exc
        current = path.lstat()
        if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
            raise ClientError('PRIVATE_FILE_UNSAFE')
        check_operation_inode(path)
        yield
        current = path.lstat()
        if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
            raise ClientError('PRIVATE_FILE_UNSAFE')
        check_operation_inode(path)
    finally:
        os.close(fd)


@contextmanager
def operation_inode(path):
    """The fixed operation mutex, also used before a root has a context."""
    create_mutex_inode(path,'orrery-client-operation-mutex-v1')
    wait_for_mutex_content(path,'orrery-client-operation-mutex-v1')
    check_operation_inode(path)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(fd)
        fcntl.flock(fd,fcntl.LOCK_EX)
        current=path.lstat()
        if (info.st_dev,info.st_ino)!=(current.st_dev,current.st_ino):
            raise ClientError('PRIVATE_FILE_UNSAFE')
        check_operation_inode(path)
        yield
    finally:os.close(fd)


def prepare_client_root(config, name):
    """One fixed-layout root creation for operator and child entrypoints.

    The template comes from an admitted client or server binding. A second tab
    shares the same existing operation mutex; it cannot overwrite activation.
    """
    validate_client_name(name)
    local_endpoint(config['mcp_url'])
    isolation=absolute(config['isolation_root'])
    home=absolute(config['agentstack_home'])
    if home!=isolation/'agentstack':raise ClientError('CLIENT_HOME_MISMATCH')
    safe_private_directory(isolation)
    parent=home/'clients'
    root=parent/name
    for directory in (home,parent,root,root/'runtime'):
        directory.mkdir(mode=0o700,exist_ok=True)
        safe_private_directory(directory)
    path=root/CLIENT_LAYOUT['context']
    with operation_inode(root/CLIENT_LAYOUT['operation_lock']):
        if not path.exists() and not path.is_symlink():
            atomic_json(path,{**config,'client_name':name,'identity':None})
        saved=read_json(path)
        if (saved.get('wrapper_root')!=config['wrapper_root'] or any(saved.get(k)!=config[k]
                for k in ('expected_server_instance_id','candidate_generation','authority_epoch'))):
            raise ClientError('CHILD_BINDING_MISMATCH')
    return path


class RuntimeClient:
    def __init__(self, path, *, recovery=None, read_only=False):
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
        if read_only:
            # tempfile.gettempdir() tests candidates by creating/removing a
            # file. Read-only admission must not mutate even TMPDIR's mtime.
            candidates = ([tempfile.tempdir] if tempfile.tempdir is not None else
                          [os.environ.get(key) for key in ('TMPDIR','TEMP','TMP')]+['/tmp','/var/tmp','/usr/tmp'])
            temporary = next((Path(p).resolve() for p in candidates if isinstance(p,str)
                              and Path(p).is_absolute() and Path(p).is_dir()
                              and os.access(p,os.R_OK|os.W_OK|os.X_OK)),None)
            roots = [Path('/private/tmp').resolve()]+([temporary] if temporary is not None else [])
        else:
            roots = [Path(tempfile.gettempdir()).resolve(), Path('/private/tmp').resolve()]
        if not any(self.isolation.resolve().is_relative_to(p) for p in roots):
            raise ClientError('ISOLATION_REQUIRED')
        if any(k in c for k in ('runtime_dir', 'credential_file', 'client_root')) or not all(k in c for k in ('agentstack_home', 'client_name')):
            raise ClientError('FIXED_LAYOUT_CONTEXT_REQUIRED')
        name = c['client_name']
        validate_client_name(name)
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
        self.endpoint = local_endpoint(c.get('mcp_url', ''))
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
        # Recovery preparation only validates the existing pending proof and
        # sets in-memory ownership. Read-only admission must keep those checks,
        # including interrupted credential/context activation, without a mutex
        # or output write. The normal path still acquires C below.
        self.prepare_recovery_owner()
        self.validate_path_roles()
        if not read_only:
            with self.operation_mutex():
                pass

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
        elif role == 'release':
            if path.parent != self.runtime_dir / 'release-debounce' or not re.fullmatch(r'[1-9][0-9]*\.json', path.name):
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
        if role == 'task':
            read_private(path)
            return
        if role in MUTEX_KINDS:
            # Preflight can meet another caller's mutex between its create and
            # its write; wait for it exactly like the acquisition does.
            wait_for_mutex_content(path, MUTEX_KINDS[role])
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
        if role == 'release':
            if (row.get('kind') != 'orrery-global-release-debounce-v2'
                    or row.get('agent_id') != (self.identity or {}).get('agent_id')
                    or not integer(row.get('lease_id'), 1) or path.stem != str(row['lease_id'])
                    or not integer(row.get('lease_revision'), 0)
                    or not isinstance(row.get('expires_ts'), str)
                    or not isinstance(row.get('generation'), str)
                    or not re.fullmatch(r'[0-9a-f-]{36}', row['generation'])
                    or any(row.get(k) != v for k,v in self.binding.items())):
                raise ClientError('OUTPUT_SCHEMA_INVALID')
            return
        if role == 'launch':
            delivery_contract()['validate'](row, 'launch_intent', ClientError)
            return
        if role == 'child':
            wire_contract()['validate_schema'](row, child_schema(), 'OUTPUT_SCHEMA_INVALID', error_type=ClientError)
            return
        if role == 'operation_lock':
            if row != {'kind': 'orrery-client-operation-mutex-v1'}:
                raise ClientError('OUTPUT_SCHEMA_INVALID')
            return
        if role == 'mutex':
            if row != {'kind': 'orrery-client-local-mutex-v1'}:
                raise ClientError('OUTPUT_SCHEMA_INVALID')
            return
        if role == 'mutation':
            if row.get('kind') == 'orrery-global-message-intent-v1':
                resolving = self.recovery == ('resolve-mutation',None)
                owner_binding = {'expected_server_instance_id':row.get('server_instance_id'),
                    'candidate_generation':row.get('candidate_generation'), 'authority_epoch':row.get('authority_epoch')} if resolving else self.binding
                try:
                    message_contract()['resume_intent'](row, {**owner_binding,
                        'agent_id': row.get('agent_id') if resolving else (self.identity or {}).get('agent_id'),
                        'expected_credential_generation': row.get('credential_generation') if resolving else (self.identity or {}).get('credential_generation'),
                        'registration_token': 'validation-placeholder'})
                except ValueError as exc:
                    raise ClientError(str(exc)) from exc
                return
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
        if role == 'profile' and row.get('configuration_stage') == 'prepared':
            from runtime_intent_store import checked_intent
            from runtime_caller_schema import CALLER_FIELDS, OPTIONAL_FIELDS, canonical_caller
            from runtime_launch_intent import model_reference
            from local_model_auth import LocalModelError
            try:
                intent = checked_intent(row['launch_intent'])
                caller = {key:value for key,value in intent.items() if key in CALLER_FIELDS|OPTIONAL_FIELDS}
                caller['model'] = model_reference(intent)
                canonical_caller(caller)
                if (set(row) != {'kind','client_config','configuration_stage','launch_intent',*self.binding}
                        or row['kind'] != PROFILE_KIND or row['client_config'] != str(self.path)
                        or any(row.get(key) != value for key,value in self.binding.items())
                        or intent.get('operator_uid') != os.getuid()
                        or (self.identity and self.identity.get('name') != intent.get('name'))):
                    raise ValueError()
                if intent['model']['kind']=='local-profile':
                    if (not re.fullmatch(r'[0-9a-f]{64}',intent.get('task_sha256',''))
                            or not isinstance(intent.get('configuration_evidence'),dict)
                            or intent['configuration_evidence'].get('requires_native_validation') is not True):
                        raise ValueError()
                elif intent['model']['kind']=='catalog':
                    from runtime_launch_intent import materialize_launch_intent
                    if 'task_sha256' not in intent or (intent['task_sha256'] is not None and
                            not re.fullmatch(r'[0-9a-f]{64}',intent['task_sha256'])):raise ValueError()
                    materialize_launch_intent(intent,{'mode':'global','client_root':str(self.client_root),
                        'operator_uid':os.getuid()},operation='spawn')
                else:raise ValueError()
            except (KeyError,TypeError,ValueError,LocalModelError):
                raise ClientError('OUTPUT_SCHEMA_INVALID') from None
            return
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
        if role == 'child':
            value = {k:v for k,v in value.items() if k != 'launch_id'}
        # mkstemp uses O_EXCL for the private temporary file; rename only after
        # the destination's own format/owner and Mail-root boundary are checked.
        # Context activation updates self.raw before its caller releases F.
        # Other writers can fence independently at their commit point.
        def commit():
            self.output_plan()
            self.validate_output(path, role)
            atomic_json(path, value, before_replace=lambda: self.validate_output(path, role))
        if role == 'context':
            commit()
        else:
            with self.fence():
                commit()

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
        except urllib.error.HTTPError as exc:
            raise ClientError('HTTP_REJECTED') from exc
        except (ConnectionError, TimeoutError, urllib.error.URLError) as exc:
            raise ClientError('TRANSPORT_FAILED') from exc
        except (OSError, ValueError) as exc:
            raise ClientError('RESPONSE_INVALID') from exc

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
        # Admit the remote schema once at catalog load, including branches
        # that the current response does not select. Value validation stays
        # independent of schema-definition validation.
        for tool in listing:
            if isinstance(tool, dict):
                for field in ('inputSchema', 'outputSchema'):
                    if isinstance(tool.get(field), dict):
                        wire_contract()['validate_schema_definition'](tool[field], error_type=ClientError)
        self.input_schemas = {t['name']:t.get('inputSchema') for t in listing if isinstance(t,dict) and isinstance(t.get('name'),str)}
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
        with self.operation_mutex():
            with self.fence():
                _, schemas = self.capabilities()
            if tool not in schemas:
                raise ClientError('CAPABILITY_UNAVAILABLE')
            args = dict(arguments or {})
            for key in ('project_key', 'human_key', 'agent_name', 'sender_name', 'name'):
                if key != 'name' or tool != 'resolve_agent_identity':
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
                self.inspect_owner()
            if tool == 'register_agent' and self.identity and 'window_row_id' in self.identity:
                args.update(window_row_id=self.identity['window_row_id'], window_uuid=self.identity['window_uuid'])
            if set(args) - set(schemas[tool].get('properties', {})):
                raise ClientError('ARGUMENTS_UNSUPPORTED')
            if 'request_id' in schemas[tool].get('required', []) and tool in s2a_tools(receipt=True):
                if tool in message_contract()['TOOLS'] and (tool != 'macro_contact_handshake' or 'welcome' in self.output_schemas[tool].get('properties',{})):
                    return self.message_mutation(tool, args)
                return self.s2a_mutation(tool, args, schemas[tool])
            with self.fence():
                result = result_value(self.rpc('tools/call', {'name': tool, 'arguments': args}))
            if tool in {'whois', 'register_agent'}:
                self.validate_identity(result)
                if tool == 'register_agent' and any(result.get(k) != self.identity.get(k) for k in ('window_row_id', 'window_uuid')):
                    raise ClientError('WINDOW_BINDING_MISMATCH')
            if tool in s2a_tools():
                self.validate_mutation_output(tool, result)
                for key,expected in (('server_instance_id',self.binding['expected_server_instance_id']),
                        ('candidate_generation',self.binding['candidate_generation']),
                        ('authority_epoch',self.binding['authority_epoch']),
                        ('agent_id',self.identity['agent_id']),('credential_generation',self.identity['credential_generation'])):
                    if key in result and result[key] != expected:
                        raise ClientError('RESPONSE_INVALID')
            if tool == 'fetch_inbox' and isinstance(result, dict):
                result = result.get('result')
            if tool == 'fetch_inbox' and not isinstance(result, (list,dict)):
                raise ClientError('RESPONSE_INVALID')
            return result

    @contextmanager
    def mutation_mutex(self):
        path = self.outputs['mutex']
        # One immutable inode; create_mutex_inode never replaces a held lock.
        create_mutex_inode(path,'orrery-client-local-mutex-v1')
        wait_for_mutex_content(path,'orrery-client-local-mutex-v1')
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
        with self.mutation_mutex(), self.fence():
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
            self.validate_mutation_output(tool, result, values)
            if result['request_id'] != pending['request_id']:
                raise ClientError('RESPONSE_INVALID')
            if tool in {'refresh_registration', 'set_contact_policy'}:
                self.validate_identity(result)
            pending.update(phase='committed',receipt=result)
            self.save_mutation(pending)
            if tool == 'bind_window_identity':
                config = {**self.config, 'identity': {**self.identity, 'window_row_id':result['window_row_id'], 'window_uuid':result['window_uuid']}}
                self.write_output(self.path, config, 'context')
                self.config, self.identity, self.raw = config, config['identity'], read_private(self.path)
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

    def validate_mutation_output(self, tool, result, values=None):
        schema = self.output_schemas.get(tool)
        if not isinstance(schema, dict) or schema.get('type') != 'object':
            raise ClientError('CAPABILITY_INVALID')
        try:
            wire_contract()['validate_schema'](result, schema, 'RESPONSE_INVALID', error_type=ClientError)
            canonical_schema = wire_contract()['fixture']['tool_contracts'].get(tool,{}).get('output_schema')
            if tool == 'macro_contact_handshake' and 'welcome' not in self.output_schemas[tool].get('properties',{}):
                canonical_schema = wire_contract()['fixture_loader']('global-server-s2a.json')['tool_contracts'][tool]['output_schema']
            if canonical_schema is not None:
                wire_contract()['validate_schema'](result,canonical_schema,'RESPONSE_INVALID',error_type=ClientError)
            if values is not None and tool in {'request_contact', 'respond_contact', 'close_contact', 'macro_contact_handshake'}:
                contact = result.get('contact') if tool == 'macro_contact_handshake' else result
                source = values['from_agent_id'] if tool == 'respond_contact' else self.identity['agent_id']
                target = self.identity['agent_id'] if tool == 'respond_contact' else values['to_agent_id']
                if (contact.get('from_agent_id') != source or contact.get('to_agent_id') != target
                        or contact.get('from_identity', {}).get('agent_id') != source
                        or contact.get('to_identity', {}).get('agent_id') != target):
                    raise ClientError('RESPONSE_INVALID')
        except (TypeError, ValueError, KeyError) as exc:
            raise ClientError('RESPONSE_INVALID') from exc

    def resolve_mutation(self,current,expected_digest,**proof):
        with self.operation_mutex():return self._resolve_mutation(current,expected_digest,**proof)

    def _resolve_mutation(self, current, expected_digest, *, expected_old, expected_new, old_agent_id, new_agent_id, confirm=False, lifecycle_parent=None, operator=False):
        if not confirm:raise ClientError('MUTATION_PENDING_REQUIRES_RESOLUTION')
        path=self.outputs['mutation']
        with self.mutation_mutex(), self.fence():
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
            inspected=current.inspect_owner() if lifecycle_parent is not None or operator else {}
            if inspected.get('retired_at') is not None:
                # Retired cleanup is local and only for the same pinned identity.
                # Ordinary resolve/rebind and retired sends keep their old fence.
                if (self.binding!=current.binding or self.identity['agent_id']!=current.identity['agent_id']
                        or old!=new or self.identity['credential_generation']!=pending['credential_generation']):
                    raise ClientError('MUTATION_PENDING_BINDING_CHANGED')
                child=read_json(self.outputs['child'])
                if not operator:
                    if lifecycle_parent is None:raise ClientError('OWNER_REQUIRED')
                    parent=lifecycle_parent
                    if (parent.binding!=self.binding or child['parent']!={'client_name':parent.config['client_name'],
                        'server_instance_id':self.binding['expected_server_instance_id'],'agent_id':parent.identity['agent_id']}):raise ClientError('OWNER_REQUIRED')
                    parent.call('whois')
                delivery=runtime_delivery(self);launch=delivery.launch();term=launch.get('termination') or {}
                if (pending['tool']!='send_message' or term.get('phase') not in ('report-pending','report-required')
                        or pending['request_id']!=delivery.terminal_uuid(launch,term.get('report_kind'))):raise ClientError('MUTATION_PENDING_CONFLICT')
                delivery.save(self.outputs['launch'],{**launch,'termination':{**term,'phase':'report-not-required'},
                    'operator_resolution':{'action':'mutation-abandoned','operator_evidence':'Explicit local acknowledgement of unconfirmed terminal report'}},'launch_intent')
            else:
                if lifecycle_parent is not None or operator:raise ClientError('ARGUMENTS_UNSUPPORTED')
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

    def check_explicit_target(self):
        if not hasattr(self, '_explicit_target_name'):
            return
        name = self._explicit_target_name
        identity = self.identity
        if not identity or not isinstance(identity.get('name'),str) or not identity['name']:
            raise ClientError('GLOBAL_TARGET_OWNERSHIP_UNKNOWN')
        if identity['name'] != name:
            raise ClientError('REGISTRATION_IDENTITY_MISMATCH')
        credential = read_json(self.credential)
        if (any(credential.get(key) != identity.get(key) for key in ('agent_id','credential_generation'))
                or credential.get('name',name) != name):
            raise ClientError('REGISTRATION_IDENTITY_MISMATCH')

    def reconnect(self, *, program=None, model=None):
        # operation_mutex may have refreshed context since offline admission.
        # Detect a now-known mismatch before even the fresh owner request.
        self.check_explicit_target()
        metadata = self.registration_metadata()
        local = self.check_local_registration((self.identity or {}).get('name'),
            program if program is not None else metadata.get('program'),
            model if model is not None else metadata.get('model'),metadata=model is None)
        if local is not None and model is not None:model = local['projection']['model']
        if hasattr(self, '_explicit_target_name'):
            current = self.inspect_owner()
            if current.get('name') != self._explicit_target_name:
                raise ClientError('REGISTRATION_IDENTITY_MISMATCH')
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
        with self.operation_mutex():
            _, schemas = self.capabilities()
            if 'set_contact_policy' in schemas:
                # S1 lacks this capability. No speculative S2 request is sent.
                self.call('set_contact_policy', {'policy': policy})

    def operator_read(self, query):
        with self.operation_mutex(), self.fence():
            return operator_exchange(self.config, self.binding, query)

    def management(self, action, **values):
        with self.fence():
            self.capabilities()
            reply = management_exchange(self.config, {'version': 1, **self.binding, 'action': action, **values})
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

    def inspect_owner(self):
        # whois refuses retired identities; this existing private projection
        # proves the same local credential without changing retirement.
        token = self.local_owner()
        inspected = self.management('inspect', agent_id=self.identity['agent_id'])
        return validate_owner_receipt(inspected,self.identity,token,self.binding)

    def activate_credential(self, token, row, metadata=None):
        identity = {k: row[k] for k in ('agent_id', 'credential_generation', 'name')}
        if self.identity:
            identity.update({k: self.identity[k] for k in ('window_row_id', 'window_uuid') if k in self.identity})
        config = {**self.config, 'identity': identity}
        credential = {'kind': 'orrery-global-credential-v1', **self.binding,
                      **{k:v for k,v in identity.items() if k not in ('window_row_id','window_uuid')}, 'registration_token': token}
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
        if self.identity is not None and not self.outputs['registration'].exists():
            raise ClientError('EXISTING_IDENTITY_REQUIRED')
        if (self.credential.exists() or self.credential.is_symlink()) and not self.outputs['registration'].exists():
            raise ClientError('CREDENTIAL_ALREADY_PRESENT')
        return self.register_intent(name, program, model)

    @contextmanager
    def operation_mutex(self):
        key = str(self.outputs['operation_lock'])
        held = getattr(_OPERATION_HELD, 'paths', None)
        if held is None:
            held = _OPERATION_HELD.paths = set()
        if key in held:
            yield
            return
        with self._operation_mutex_inode():
            held.add(key)
            try:
                try:
                    current = read_json(self.path)
                except ClientError as exc:
                    raise ClientError('CONTEXT_CHANGED') from exc
                if {k:v for k,v in current.items() if k != 'identity'} != {k:v for k,v in self.config.items() if k != 'identity'}:
                    raise ClientError('CONTEXT_CHANGED')
                self.config = current
                self.raw = read_private(self.path)
                self.identity = current.get('identity')
                self.output_plan()
                yield
            finally:
                held.remove(key)

    @contextmanager
    def _operation_mutex_inode(self):
        path=self.outputs['operation_lock']
        with operation_inode(path):
            self.validate_output(path,'operation_lock')
            yield

    def message_mutation(self, tool, args):
        contract = message_contract()
        explicit_uuid = args.get('request_id')
        args = {**args, 'request_id': args.get('request_id', str(uuid.uuid4()))}
        try:
            candidate = contract['make_intent'](tool, args)
            path = self.outputs['mutation']
            with self.mutation_mutex(), self.fence():
                existing = path.exists() or path.is_symlink()
                if existing:
                    self.validate_output(path, 'mutation')
                    pending = read_json(path)
                    if (pending.get('kind') != candidate['kind']
                            or pending.get('request_hash') != candidate['request_hash']
                            or (explicit_uuid is not None and pending['request_id'] != explicit_uuid)):
                        raise ClientError('MUTATION_PENDING_CONFLICT')
                    values = contract['resume_intent'](pending, args)
                else:
                    pending = candidate
                    values = args
                    self.save_mutation(pending)
                planned = read_private(path)
                reply = None
                try:
                    reply = self.rpc('tools/call', {'name': tool, 'arguments': values})
                    result = result_value(reply)
                except ClientError as exc:
                    if (isinstance(reply, dict) and reply.get('result', {}).get('isError') is True
                            and wire_contract()['definitive_rejection'](str(exc), existing_pending=existing,
                                contract=wire_contract()['fixture'])):
                        if self.outputs['launch'].exists():runtime_delivery(self).terminal_rejection(tool,pending['request_id'],str(exc))
                        self.clear_mutation(planned)
                    raise
                self.validate_mutation_output(tool, result, values)
                self.save_mutation(contract['complete_intent'](pending, result, values))
                if self.outputs['launch'].exists():runtime_delivery(self).terminal_receipt(tool,result)
                self.clear_mutation(read_private(path))
                return result
        except ValueError as exc:
            if isinstance(exc, ClientError):
                raise
            raise ClientError(str(exc)) from exc

    def replay_pending(self):
        path = self.outputs['mutation']
        if not path.exists() and not path.is_symlink():
            return None
        pending = read_json(path)
        if pending.get('kind') == 'orrery-global-message-intent-v1':
            args = dict(pending['canonical_intent'])
        else:
            args = dict(pending['canonical_payload']['arguments'])
        args['request_id'] = pending['request_id']
        return self.call(pending['tool'], args)

    def all_leases(self):
        cursor = None
        while True:
            page = self.call('list_file_reservations', {
                'owner_agent_id': self.identity['agent_id'], 'active_only': True,
                'cursor': cursor, 'limit': 100})
            yield page['items']
            if not page['more']:
                break
            cursor = page['next_cursor']

    def guard_edit(self, payload):
        # Only the very first transport failure may use warn-open. Any pending
        # renewal outcome or a failure after a definite check remains blocked.
        if self.outputs['mutation'].exists() or self.outputs['mutation'].is_symlink():
            self.replay_pending()
        path = hook_path(payload)
        if payload.get('tool_name') in ('Delete', 'MoveSource') and Path(path).is_symlink():
            # The server covers a physical target, not the link entry removed
            # by these operations. Target coverage cannot authorize deletion.
            raise ClientError('RESERVATION_ENTRY_UNSUPPORTED: symlink entry deletion')
        try:
            checked = self.call('check_file_reservations', {'paths': [path]})
        except ClientError as exc:
            if str(exc) == 'TRANSPORT_FAILED':
                raise ClientError('INITIAL_TRANSPORT_UNREACHABLE') from exc
            raise
        if not checked['covered']:
            raise ClientError('RESERVATION_REQUIRED')
        owned = {key for item in checked['items'] for key in item['reservation_ids']}
        leases = {row['id']: row for page in self.all_leases() for row in page if row['id'] in owned}
        if set(leases) != owned or not owned:
            raise ClientError('RESERVATION_REQUIRED')
        now = datetime.now(timezone.utc)
        due = [key for key, row in leases.items()
               if (datetime.fromisoformat(row['expires_ts'].replace('Z', '+00:00')) - now).total_seconds() <= RENEW_THRESHOLD_SECONDS]
        for start in range(0, len(due), 100):
            self.call('renew_file_reservations', {'file_reservation_ids': due[start:start+100],
                'extend_seconds': RENEW_TTL_SECONDS})
        if due and not self.call('check_file_reservations', {'paths': [path]})['covered']:
            raise ClientError('RESERVATION_REQUIRED')
        return {'ok': True, 'renewed': len(due)}

    def post_edit(self, payload):
        if hook_failed(payload):
            return {'ok': True, 'released': 0}
        try:
            checked = self.call('check_file_reservations', {'paths': [hook_path(payload)]})
        except ClientError as exc:
            if str(exc) == 'TRANSPORT_FAILED':
                raise ClientError('INITIAL_TRANSPORT_UNREACHABLE') from exc
            raise
        ids = {key for item in checked['items'] for key in item['reservation_ids']}
        leases = {row['id']: row for page in self.all_leases() for row in page if row['id'] in ids}
        generation = str(uuid.uuid4())
        directory = self.runtime_dir / 'release-debounce'
        with self.operation_mutex(), self.fence():
            directory.mkdir(mode=0o700, exist_ok=True)
            safe_private_directory(directory)
            for key, row in leases.items():
                value = {'kind': 'orrery-global-release-debounce-v2', **self.binding,
                    'agent_id': self.identity['agent_id'], 'generation': generation,
                    'lease_id': key, 'lease_revision': row['revision'], 'expires_ts': row['expires_ts']}
                self.write_output(self.release_slot(key), value, 'release')
        if leases and self.release_grace_seconds() == 0:
            return self.release_worker(generation, sorted(leases))
        if leases:
            subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--context', str(self.path),
                'release-worker', generation, *map(str, sorted(leases))], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        return {'ok': True, 'scheduled': len(leases)}

    def release_slot(self, key):
        if not integer(key, 1):
            raise ClientError('LEASE_ID_INVALID')
        directory = self.runtime_dir / 'release-debounce'
        safe_private_directory(directory)
        return directory / (str(key) + '.json')

    @staticmethod
    def release_grace_seconds():
        value = (os.environ.get('AGENTSTACK_RELEASE_GRACE_SECONDS')
            or os.environ.get('FILE_RESERVATION_RELEASE_GRACE_SECONDS') or '90')
        return int(value) if re.fullmatch(r'[0-9]+', value) else 90

    def release_worker(self, generation, ids):
        time.sleep(self.release_grace_seconds())
        with self.operation_mutex():
            current = {row['id']: row for page in self.all_leases() for row in page if row['id'] in ids}
            eligible = []
            slots = []
            for key in ids:
                slot = self.release_slot(key)
                if not slot.exists() and not slot.is_symlink():
                    continue
                self.validate_output(slot, 'release')
                saved = read_json(slot)
                if saved['generation'] != generation:
                    continue
                slots.append(slot)
                lease = current.get(key)
                if (lease and lease['revision'] == saved['lease_revision']
                        and lease['expires_ts'] == saved['expires_ts']):
                    eligible.append(key)
            for start in range(0, len(eligible), 100):
                self.call('release_file_reservations', {'file_reservation_ids': eligible[start:start+100]})
            for slot in slots:
                # Holding the operation mutex keeps newer post-edit plans intact.
                self.validate_output(slot, 'release')
                slot.unlink()
            return {'ok': True, 'released': len(eligible), 'stale': not bool(slots)}

    def record_live(self, payload, *, refresh=False):
        session = payload.get('session_id')
        if not isinstance(session, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', session):
            raise ClientError('SESSION_ID_INVALID')
        directory = self.runtime_dir / 'live-sessions'
        with self.operation_mutex():
            if refresh:
                self.reconnect()
            directory.mkdir(mode=0o700, exist_ok=True)
            safe_private_directory(directory)
            pid = payload.get('provider_pid') or os.environ.get('AGENTSTACK_PROVIDER_PID') or provider_ancestor()
            pid = int(pid) if str(pid).isdigit() else None
            evidence = {'kind': 'orrery-global-live-session-v1', **self.binding,
                'agent_id': self.identity['agent_id'], 'session_id': session,
                'pid': pid, 'process_start': process_start(pid), 'pane_id': os.environ.get('TMUX_PANE'),'ended':False}
            slot = directory / (session + '.json')
            if slot.exists() or slot.is_symlink():
                previous = read_json(slot)
                if previous.get('kind') != evidence['kind'] or previous.get('agent_id') != evidence['agent_id']:
                    raise ClientError('SESSION_OWNER_CONFLICT')
                # App lifecycle belongs to this same canonical live slot;
                # refreshing process evidence must not erase its route state.
                same = all(previous.get(key) == evidence.get(key) for key in
                           (*self.binding, 'session_id', 'pid', 'process_start'))
                for key in ('app_state', 'app_subagent'):
                    if key in previous:
                        evidence[key] = previous[key]
                if same:
                    launch_path = self.outputs['launch']
                    if launch_path.exists() or launch_path.is_symlink():
                        launch = read_json(launch_path)
                        if all(previous.get(key) == launch.get(key) for key in ('launch_id','receipt_nonce')):
                            for key in ('launch_id','receipt_nonce'):
                                evidence[key] = previous[key]
            with self.fence():
                atomic_json(slot, evidence)
        return evidence

    def session_liveness(self, exclude=None):
        directory = self.runtime_dir / 'live-sessions'
        if not directory.exists() and not directory.is_symlink():
            return 'unknown'
        safe_private_directory(directory)
        for slot in directory.iterdir():
            if not slot.name.endswith('.json'):
                raise ClientError('SESSION_LIVENESS_UNKNOWN')
            evidence = read_json(slot)
            if (evidence.get('kind') != 'orrery-global-live-session-v1'
                    or evidence.get('agent_id') != self.identity['agent_id']
                    or slot.name != str(evidence.get('session_id')) + '.json'
                    or any(evidence.get(k) != v for k, v in self.binding.items())):
                raise ClientError('SESSION_LIVENESS_UNKNOWN')
            if evidence.get('session_id') == exclude or evidence.get('ended') is True:
                continue
            pane = evidence.get('pane_id')
            if pane:
                launch=self.outputs['launch']
                snapshot={'context':self.config,'launch':read_json(launch) if launch.exists() else None}
                state,_,_=pane_evidence(evidence,snapshot=snapshot)
                if state=='live':return 'other-session-live'
                if state=='unknown':return 'unknown'
            status = process_start(evidence.get('pid'))
            if status is None or evidence.get('process_start') is None:
                return 'unknown'
            if status != 'absent':
                return 'other-session-live' if status == evidence['process_start'] else 'unknown'
        return 'absent'

    def end_session(self, payload):
        with self.operation_mutex():
            if self.outputs['launch'].exists():
                delivery=runtime_delivery(self)
                session=payload.get('session_id')
                slot=self.runtime_dir/'live-sessions'/(session+'.json') if isinstance(session,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}',session) else None
                standalone=slot is not None and slot.exists() and read_json(slot).get('launch_id') is None
                if not standalone:return delivery.end_launch(payload)
                # Preserve an old standalone hook, without closing an unrelated
                # unfinished launch which has not published a live slot yet.
                if delivery.launch()['phase'] not in ('ended','cancelled'):
                    return {'ok':True,'end':'unknown','changed':False}
            directory = self.runtime_dir / 'live-sessions'
            current = payload.get('session_id')
            if not isinstance(current,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',current):
                return {'ok':True,'end':'unknown','changed':False}
            status = self.session_liveness(exclude=current)
            if status != 'absent':
                return {'ok': True, 'end': status, 'changed': False}
            current_slot = directory / (current + '.json')
            if not current_slot.exists():
                return {'ok': True, 'end': 'unknown', 'changed': False}
            if self.outputs['child'].exists():
                self.retain_child(read_json(self.outputs['child']))
            else:
                pending=self.outputs['mutation']
                if pending.exists():
                    if read_json(pending).get('tool')!='release_file_reservations':raise ClientError('MUTATION_PENDING_CONFLICT')
                    self.replay_pending()
                for page in self.all_leases():
                    if page:
                        self.call('release_file_reservations', {'file_reservation_ids': [row['id'] for row in page]})
            with self.fence():
                atomic_json(current_slot, {**read_json(current_slot), 'ended': True})
                if self.outputs['launch'].exists():
                    delivery = runtime_delivery(self)
                    launch = delivery.launch()
                    if launch['phase'] == 'running':
                        delivery.save(self.outputs['launch'], {**launch, 'phase': 'ended'}, 'launch_intent')
            return {'ok': True, 'end': 'last-session', 'changed': True}

    def retain_child(self, state):
        # Caller holds the common operation mutex and fence. Retire alone
        # delegates all ACTIVE release to the server's canonical operation.
        inspected = self.management('inspect', agent_id=self.identity['agent_id'])
        if inspected.get('retired_at') is None:
            self.call('retire_agent')
        self.write_output(self.outputs['child'], {**state, 'phase': 'retained',
            'resume_expires_at': (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()}, 'child')

    def _prepare_child_launch(self, task_file=None, *, embed_task=False):
        """Existing task publication, shared by explicit entry and operator.

        Caller has already selected/admitted the fixed child and parent.
        This is preparation only: no registration, instruction configure or exec.
        """
        with self.operation_mutex(), self.fence():
            state = read_json(self.outputs['child'])
            if task_file:
                approved = None
                if self.profile_path.exists() or self.profile_path.is_symlink():
                    self.validate_output(self.profile_path,'profile')
                    saved = read_json(self.profile_path).get('launch_intent')
                    if saved is not None:
                        from runtime_intent_store import checked_intent
                        from local_model_auth import LocalModelError
                        try:approved = checked_intent(saved)
                        except LocalModelError as exc:raise ClientError(str(exc)) from None
                        if str(task_file) != approved['task']:
                            raise ClientError('LOCAL_MODEL_INTENT_CHANGED')
                content = _read_intent_task_source(task_file) if approved is not None else Path(task_file).read_bytes()
                if len(content) > 65536:
                    raise ClientError('PAYLOAD_TOO_LARGE')
                if approved is not None and hashlib.sha256(content).hexdigest() != approved.get('task_sha256'):
                    raise ClientError('LOCAL_MODEL_TASK_CHANGED')
                task = self.outputs['task']
                if task.exists() or task.is_symlink():
                    if read_private(task) != content:
                        raise ClientError('TASK_PENDING_CONFLICT')
                else:
                    atomic_bytes(task,content,lambda:self.validate_output(task,'task'))
            if embed_task:
                self.write_output(self.outputs['child'],{**state,'task_delivery':'embedded'},'child')
            return ({**self.observe(),'prepared':True,'runtime_ready':False,
                     'execution_reason':'GLOBAL_CHILD_EXPLICIT_LAUNCH_REQUIRED'}, state)

    def prepare_child(self, options, *, _caller=None, _intent_digest=None,
                      _registration_sha256=None):
        validate_child_tools(options)
        name = options.get('child_client_name')
        validate_client_name(name)
        if name == self.config['client_name']:
            raise ClientError('CLIENT_ROOT_OVERLAP')
        # Local preparation reads an explicitly configured fixed child root;
        # it never manufactures a root/configuration before admission.
        requested = options.get('model','')
        if isinstance(requested,str) and requested.startswith('local:'):
            fixed = self.clients_parent/name/CLIENT_LAYOUT['context']
            if not fixed.exists() and not fixed.is_symlink():
                raise ClientError('LOCAL_MODEL_INTENT_OWNER_REQUIRED')
            local_child = RuntimeClient(fixed,read_only=True)
            local_child.check_local_registration(options.get('name',name),options.get('program','claude-code'),requested)
        path = prepare_client_root(self.config,name)
        root = path.parent
        saved = read_json(path)
        if saved.get('identity') and not (root / CLIENT_LAYOUT['child']).exists():
            raise ClientError('CLIENT_ROOT_NOT_CHILD')
        pending_path = root / CLIENT_LAYOUT['registration']
        pending = read_json(pending_path) if pending_path.exists() else None
        recovery = None
        if pending and pending.get('row'):
            recovery = ('finalize-create', pending['row']['agent_id'])
        child = RuntimeClient(path, recovery=recovery)
        child.check_local_registration(options.get('name',name),options.get('program','claude-code'),
                                       options.get('model',options.get('program','claude-code')))
        parent = self.observe()
        with child.operation_mutex():
            if _caller is not None:
                from runtime_local_launch import admit_caller_request
                admit_caller_request(child,_caller,expected_digest=_intent_digest)
                if _caller['parent']!=parent['name']:
                    raise ClientError('CHILD_PARENT_MISMATCH')
            if _registration_sha256 is not None:
                with child.fence():
                    pending=child.outputs['registration']
                    if not pending.exists() and not pending.is_symlink():
                        raise ClientError('TARGET_UNAVAILABLE')
                    if hashlib.sha256(read_private(pending)).hexdigest()!=_registration_sha256:
                        raise ClientError('REGISTRATION_PENDING_CONFLICT')
            program = options.get('program', 'claude-code')
            model = options.get('model', program)
            task = options.get('task_description', 'Delegated child agent')
            intent_digest = hashlib.sha256(json.dumps({'name': options.get('name', name),
                'program': program, 'model': model, 'task_description': task}, sort_keys=True).encode()).hexdigest()
            if child.outputs['child'].exists():
                state = read_json(child.outputs['child'])
                if state.get('registration_intent_digest') != intent_digest:
                    raise ClientError('CHILD_REGISTRATION_INTENT_CONFLICT')
                if state['tools_selection'] != {'base': options.get('base', 'default'), 'tools': options.get('parsed_tools', {})}:
                    raise ClientError('CHILD_TOOLS_PENDING_CONFLICT')
                if state['parent'] != {'client_name': self.config['client_name'],
                        'server_instance_id': self.binding['expected_server_instance_id'], 'agent_id': parent['agent_id']}:
                    raise ClientError('CHILD_PARENT_MISMATCH')
            else:
                state = {'kind': 'orrery-global-child-state-v1', 'registration_intent_digest': intent_digest,
                    'parent': {'client_name': self.config['client_name'],
                        'server_instance_id': self.binding['expected_server_instance_id'], 'agent_id': parent['agent_id']},
                    'phase': 'prepared', 'working_directory': options.get('workdir', str(self.isolation)),
                    'tools_selection': {'base': options.get('base', 'default'), 'tools': options.get('parsed_tools',{})},
                    'task_delivery': 'embedded' if options.get('embed_task') else 'mail',
                    'resume_expires_at': None, 'active_request_id': None}
                with child.fence():
                    child.write_output(child.outputs['child'], state, 'child')
            if not child.identity or pending:
                child.register_child(self, options.get('name', name), program, model, task)
            row = child.observe()
            return {'mode': 'global', 'prepared': True, 'runtime_ready': False,
                'execution_reason': 'GLOBAL_CHILD_EXPLICIT_LAUNCH_REQUIRED',
                'client_name': name, 'agent_id': row['agent_id'], 'name': row['name'], 'client_config': str(path)}

    def resolve_registration_name(self, name):
        if self.outputs['child'].exists() or self.outputs['child'].is_symlink():
            state=read_json(self.outputs['child'])
            parent_path=self.clients_parent/state['parent']['client_name']/CLIENT_LAYOUT['context']
            parent=RuntimeClient(parent_path)
            if parent.identity['agent_id']!=state['parent']['agent_id']:
                raise ClientError('CHILD_PARENT_MISMATCH')
            return parent.call('resolve_agent_identity',{'target_name':name})['target']['agent_id']
        try:
            return self.operator_read({'kind':'resolve_name','name':name})['data']['agent_id']
        except ClientError as exc:
            if str(exc) in {'MANAGEMENT_REJECTED','MANAGEMENT_ACTION_INVALID','MANAGEMENT_ACTION_UNSUPPORTED','ACTION_INVALID'}:
                raise ClientError('NAME_CONFLICT') from exc
            raise

    def register_child(self, parent, name, program, model, task):
        return self.register_intent(name, program, model, task)

    def register_intent(self, name, program, model, task=None):
        """One same-token registration journal for standalone and child roots."""
        local = self.check_local_registration(name,program,model)
        if local is not None:model = local['projection']['model']
        path = self.outputs['registration']
        with self.fence():
            if path.exists():
                saved = read_json(path)
                if saved.get('terminal'):
                    raise ClientError('REGISTRATION_OWNER_CONFLICT')
                if any(saved.get(k) != v for k,v in {'name': name,'program':program,'model':model,'task_description':task}.items()):
                    raise ClientError('REGISTRATION_PENDING_CONFLICT')
            else:
                saved = {'kind': REG_PENDING_KIND, **self.binding, 'name': name, 'program': program,
                    'model': model, 'task_description': task, 'registration_token': secrets.token_urlsafe(32)}
                self.write_output(path, saved, 'registration')
            token = saved['registration_token']
            if saved.get('row'):
                return self.finalize_registration(saved['row']['agent_id'])
            self.capabilities()
            args = {**self.binding, **{k:saved[k] for k in ('name','program','model','registration_token')}}
            if task is not None:
                args['task_description'] = task
            try:
                row = result_value(self.rpc('tools/call', {'name': 'register_agent', 'arguments': args}))
            except ClientError as exc:
                if str(exc) != 'NAME_CONFLICT':
                    raise
                aid = self.resolve_registration_name(name)
                if aid is None:
                    raise ClientError('TARGET_UNAVAILABLE')
                row = result_value(self.rpc('tools/call', {'name':'whois','arguments':{
                    **self.binding, 'agent_id': aid, 'registration_token':token}}))
            if (not integer(row.get('agent_id'),1) or row.get('name') != name
                    or row.get('credential_generation') != 1
                    or any(row.get(k) != self.binding[v] for k,v in (
                        ('server_instance_id','expected_server_instance_id'),
                        ('candidate_generation','candidate_generation'),('authority_epoch','authority_epoch')))):
                raise ClientError('REGISTRATION_RESPONSE_MISMATCH')
            saved = {**saved, 'row': {k:v for k,v in row.items() if k != 'registration_token'}}
            self.write_output(path, saved, 'registration')
        self.activate_credential(token, row, {'program':program,'model':model})
        path.unlink()
        return self.observe()

    def abandon_registration(self, expected_digest):
        path = self.outputs['registration']
        with self.operation_mutex(),self.fence():
            try:path.lstat()
            except FileNotFoundError:raise ClientError('TARGET_UNAVAILABLE')
            raw=read_private(path)
            if hashlib.sha256(raw).hexdigest()!=expected_digest:
                raise ClientError('REGISTRATION_PENDING_CONFLICT')
            pending=json.loads(raw)
            target=self.resolve_registration_name(pending['name'])
            if target is None:raise ClientError('TARGET_UNAVAILABLE')
            try:
                result_value(self.rpc('tools/call',{'name':'whois','arguments':{
                    **self.binding,'agent_id':target,'registration_token':pending['registration_token']}}))
            except ClientError as exc:
                if str(exc)!='OWNER_REQUIRED':
                    raise
            else:
                raise ClientError('REGISTRATION_OWNERSHIP_VERIFIED_RETRY_REQUIRED')
            self.write_output(path,{**pending,'terminal':True},'registration')
            return {'ok':True,'terminal':True,'new_identity_requires_new_root':True}

    def purge_child(self):
        with self.operation_mutex(),self.fence():
            self.check_explicit_target()
            state=read_json(self.outputs['child'])
            if state['phase'] not in ('retained','purging','purged'):
                raise ClientError('CHILD_NOT_RETAINED')
            for role in ('registration','enrollment','mutation'):
                if self.outputs[role].exists() or self.outputs[role].is_symlink():
                    raise ClientError('CHILD_PENDING_OPERATION')
            if state['phase']=='purged':
                return {'ok':True,'purged':True}
            expires=state['resume_expires_at']
            if expires is None or datetime.fromisoformat(expires.replace('Z','+00:00'))>datetime.now(timezone.utc):
                raise ClientError('CHILD_RETENTION_NOT_EXPIRED')
            current=self.management('inspect',agent_id=self.identity['agent_id'])
            if (hasattr(self, '_explicit_target_name') and
                    current.get('name') != self._explicit_target_name):
                raise ClientError('REGISTRATION_IDENTITY_MISMATCH')
            if current.get('retired_at') is None:
                raise ClientError('CHILD_NOT_RETIRED')
            if self.session_liveness() != 'absent':
                raise ClientError('SESSION_LIVENESS_UNKNOWN')
            # Only the fixed generated task slot is currently owned by4b.
            # Provider transcripts/configs are4c; never recursively remove them.
            self.write_output(self.outputs['child'],{**state,'phase':'purging'},'child')
            task=self.outputs['task']
            self.validate_output(task,'task')
            if task.exists():task.unlink()
            self.write_output(self.outputs['child'],{**state,'phase':'purged'},'child')
            return {'ok':True,'purged':True,'credential_retained':True,'history_retained':True}


    def inspect_child(self):
        row = self.management('inspect', agent_id=self.identity['agent_id']) if self.identity else None
        state = read_json(self.outputs['child']) if self.outputs['child'].exists() else None
        return {'ok': True, 'identity': row, 'state': state, 'runtime_ready': False}

    def resume_child(self, options):
        validate_child_tools(options)
        with self.operation_mutex():
            state = read_json(self.outputs['child'])
            metadata=self.registration_metadata()
            self.check_local_registration(self.identity['name'],metadata.get('program'),metadata.get('model'),metadata=True)
            if state['phase'] in ('purging', 'purged'):
                raise ClientError('CHILD_PURGED')
            expires = state['resume_expires_at']
            if expires is not None and datetime.fromisoformat(expires.replace('Z', '+00:00')) <= datetime.now(timezone.utc):
                raise ClientError('CHILD_RETENTION_EXPIRED')
            status = self.session_liveness()
            never_executed = not (self.runtime_dir / 'live-sessions').exists() and state['phase'] in ('prepared', 'resume-planned')
            if status == 'other-session-live' or (status == 'unknown' and not never_executed):
                raise ClientError('SESSION_LIVENESS_UNKNOWN')
            inspected = self.management('inspect', agent_id=self.identity['agent_id'])
            self.write_output(self.outputs['child'], {**state, 'phase': 'resume-planned'}, 'child')
            if inspected.get('retired_at') is not None:
                self.call('unretire_agent')
            row = self.reconnect()
            return {'ok':True, **row, 'prepared':True, 'runtime_ready':False,
                    'execution_reason':'GLOBAL_CHILD_EXPLICIT_LAUNCH_REQUIRED'}

    def check_profile_destination(self, path=None):
        if path is not None:
            raise ClientError('FIXED_LAYOUT_PATH_NOT_SUPPORTED')
        return self.output_plan()['profile']

    def save_profile(self, path=None):
        path = self.check_profile_destination(path)
        row = self.observe()
        previous = read_json(path) if path.exists() else {}
        value = {'kind': PROFILE_KIND, 'client_config': str(self.path), **{k:v for k,v in row.items() if k not in ('window_row_id','window_uuid')}}
        for key in ('execution', 'launch_intent', 'local_model_configuration'):
            if key in previous:
                value[key] = previous[key]
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
        self.check_local_registration(saved.get('name'),saved.get('program'),saved.get('model'),metadata=True)
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
            previous = None
            if previous_path.exists():
                previous = read_json(previous_path)
                if any(previous.get(k) != row.get(k) for k in ('agent_id', 'expected_server_instance_id')):
                    raise ClientError('SESSION_OWNER_CONFLICT')
            value = {'schema_version': 3, 'binding_kind': 'global-self', **{k:v for k,v in row.items() if k not in ('window_row_id','window_uuid')},
                     'agent_name': row['name'], 'session_id': session_id,
                     'transcript_path': payload.get('transcript_path', ''), 'cwd': payload.get('cwd', '')}
            if previous_path.exists() and previous.get('session_id')==session_id:
                if 'instruction_observation' in previous:value['instruction_observation']=previous['instruction_observation']
            else:
                # Record candidates once for this session; subsequent cwd/resume
                # observations are not evidence that instructions were reloaded.
                try:
                    launch=read_json(self.outputs['launch']) if self.outputs['launch'].exists() else {}
                    provider=launch.get('provider')
                    if (provider in ('claude','codex') and isinstance(value['cwd'],str)
                            and os.path.isabs(value['cwd']) and Path(value['cwd']).is_dir()):
                        helper=runpy.run_path(str(ROOT/'bin/lib/instruction_blocks.py'))
                        home=self.runtime_dir/'provider-home'/provider
                        try:value['instruction_observation']=helper['instruction_observation'](provider,home,self.config['wrapper_root'],value['cwd'])
                        except helper['Refused']:pass
                except (OSError,ValueError,ClientError):
                    pass
            self.write_output(directory / 'self.json', value, 'session')
        return value

    def exports(self, provider):
        programs = {'claude': 'claude-code', 'codex': 'codex', 'gemini': 'antigravity'}
        defaults = {'claude': 'claude-code', 'codex': 'codex', 'gemini': 'gemini-3.8-flash-high'}
        model = os.environ.get('AGENTSTACK_' + provider.upper() + '_MODEL') or defaults[provider]
        if self.profile_path.exists() or self.profile_path.is_symlink():
            profile=read_json(self.profile_path)
            if 'launch_intent' in profile:
                from local_model_auth import LocalModelError
                from runtime_launch_intent import model_reference
                try:model=model_reference(profile['launch_intent'])
                except LocalModelError as exc:raise ClientError(str(exc)) from None
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
        return runtime_delivery(self).start(provider, directory)

    def check_local_registration(self, name, program, model, *, metadata=False):
        from local_model_auth import LocalModelError
        from runtime_local_launch import admit_local_registration
        try:return admit_local_registration(self,name,program,model,metadata=metadata)
        except LocalModelError as exc:raise ClientError(str(exc)) from None


def safe_private_directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ClientError('PRIVATE_DIRECTORY_REQUIRED')


def provider_ancestor(*, runner_pid=None):
    pid=os.getppid()
    for _ in range(8):
        if runner_pid is not None and pid==runner_pid:return None
        try:
            result=subprocess.run(['ps','-p',str(pid),'-o','ppid=','-o','comm='],capture_output=True,text=True,timeout=3)
            if result.returncode or not result.stdout.strip():return None
            parent,command=result.stdout.strip().split(None,1)
            name=Path(command).name.lstrip('-')
            if runner_pid is not None:
                # A bound SDK callback identifies its caller, not the
                # configured launcher shim farther up the same parent chain.
                if name not in {'bash','sh','zsh','dash','fish','ksh'}:return pid
                pid=int(parent)
                if pid<=1:return None
                continue
            if name in {'claude','codex','node','agy','antigravity'}:
                return pid
            if name not in {'bash','sh','zsh','dash','fish','ksh','python','python3'} and not name.startswith('python3.'):
                return None
            pid=int(parent)
            if pid<=1:return None
        except (OSError,ValueError,subprocess.TimeoutExpired):
            return None
    return None




def unproven_client_placement(home):
    # Negative admission for absent transport code, never a mode decision.
    roots=Path(home)/'clients'
    return roots.exists() or roots.is_symlink()


_TMUX_API = None


def runtime_tmux_api():
    global _TMUX_API
    if _TMUX_API is None:
        _TMUX_API = runpy.run_path(str(Path(__file__).with_name('runtime_tmux.py')))
    return _TMUX_API


def pane_evidence(live, *, deadline=None, lines=20, snapshot=None):
    """Read the same PID/start/pane proof for notification and observation."""
    if live.get('ended'):
        return 'absent', None, None
    start = (process_start(live.get('pid')) if deadline is None
             else process_start(live.get('pid'), deadline=deadline))
    if start == 'absent':
        return 'absent', None, None
    if not start or not live.get('process_start') or start != live['process_start']:
        return 'unknown', None, None
    pane = live.get('pane_id')
    if not isinstance(pane, str) or re.fullmatch(r'%[0-9]+', pane) is None:
        return 'unknown', None, None
    if snapshot is None:return 'unknown',None,None
    try:
        api=runtime_tmux_api()
        found=api['live_target'](live,snapshot['context'],snapshot.get('launch'),api=globals(),deadline=deadline)
        if found is None:return 'unknown',None,None
        transport,actual=found
        command=transport.command('display-message','-p','-t',pane,'#{pane_current_command}',deadline=deadline)
        if command.returncode or command.stdout.strip() in ('bash','zsh','sh','fish','dash','ksh',''):
            return 'unknown',None,None
        capture=transport.command('capture-pane','-p','-t',pane,'-S','-'+str(lines),deadline=deadline)
        if capture.returncode:return 'unknown',None,None
        return 'live',pane,capture.stdout
    except (OSError,RuntimeError,ValueError,subprocess.TimeoutExpired):
        return 'unknown',None,None


def hook_path(payload):
    value = payload.get('tool_input', {}).get('file_path')
    if not isinstance(value,str) or not value:
        raise ClientError('HOOK_PATH_REQUIRED')
    cwd = payload.get('cwd')
    if not Path(value).is_absolute():
        if not isinstance(cwd,str) or not Path(cwd).is_absolute():
            raise ClientError('HOOK_CWD_REQUIRED')
        value = str(Path(cwd) / value)
    # Server owns the common filesystem normalizer and coverage predicates.
    return value


def hook_failed(value):
    if isinstance(value,dict):
        if value.get('error') not in (None,'',False) or value.get('success') is False or str(value.get('status','')).lower() in ('error','failed','blocked'):
            return True
        return any(hook_failed(v) for v in value.values())
    if isinstance(value,list):
        return any(hook_failed(v) for v in value)
    if isinstance(value,str):
        return bool(re.match(r'^(error:|pretooluse:|posttooluse:|blocked\b|permission denied\b)',value.strip(),re.I))
    return False


def child_schema():
    installed = Path(__file__).with_name('global-client-4b.json')
    source = ROOT / 'packages/agentstack_mail/fixtures/global-client-4b.json'
    return json.loads((installed if installed.is_file() else source).read_text())['local_schemas']['child_state']


RENEW_THRESHOLD_SECONDS = 600
RENEW_TTL_SECONDS = 1800

_OPERATION_HELD = threading.local()
_DELIVERY_RUNTIME = None


def delivery_contract():
    global _DELIVERY_RUNTIME
    if _DELIVERY_RUNTIME is None:
        _DELIVERY_RUNTIME = runpy.run_path(str(Path(__file__).with_name('runtime_delivery.py')))
    return _DELIVERY_RUNTIME


def runtime_delivery(client):
    return delivery_contract()['RuntimeDelivery'](client, globals())


def _serialized(method):
    @wraps(method)
    def operation(self, *args, **kwargs):
        with self.operation_mutex():
            return method(self, *args, **kwargs)
    return operation


# One operation mutex for current-state writers and their readers. External
# provider execution is deliberately outside these methods/scopes.
for _method in ('observe', 'reconnect', 'activate_credential', 'create', 'enroll',
                'save_metadata', 'save_profile', 'record_session', 'finalize_registration',
                'finalize_enrollment', 'replay_pending', 'register_intent', 'inspect_owner'):
    if hasattr(RuntimeClient, _method):
        setattr(RuntimeClient, _method, _serialized(getattr(RuntimeClient, _method)))


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
            'expected_server_instance_id', 'candidate_generation', 'authority_epoch')):
        raise ClientError('PROFILE_BINDING_MISMATCH')
    if operation == 'run':
        delivery=runtime_delivery(client)
        execution=delivery.execution()
        return delivery.start('daemon',execution['cwd'])
    if operation == 'reconnect':
        row = client.reconnect()
    return {'ok': True, 'kind': PROFILE_KIND, **row, 'ready': False,
            'runtime_execution': 'explicit-launch-required'}


_CHILD_PROVIDERS = {'codex':'codex', 'codex-cli':'codex',
                    'claude-code':'claude', 'antigravity':'gemini'}
_SHELL_CALLER_FIELDS = ('provider', 'parent', 'resources', 'worktree', 'chrome',
                        'clear_tools', 'effort', 'strict_output_cap', 'task_delivery')


def _entry_child(parent, options, *, read_only=False):
    name = options.child_client_name
    validate_client_name(name)
    path = parent.clients_parent / name / CLIENT_LAYOUT['context']
    if not path.exists() and not path.is_symlink():
        raise ClientError('CHILD_PREPARATION_REQUIRED')
    target = RuntimeClient(path, read_only=read_only)
    state = read_json(target.outputs['child'])
    if not parent.identity:
        raise ClientError('CHILD_PARENT_MISMATCH')
    expected = {'agent_id':parent.identity['agent_id'],
                'client_name':parent.config['client_name'],
                'server_instance_id':parent.binding['expected_server_instance_id']}
    if state['parent'] != expected:
        raise ClientError('CHILD_PARENT_MISMATCH')
    return target, state


def _shell_caller_input(options, parent):
    from runtime_local_launch import validate_caller_request
    from runtime_caller_schema import PROVIDERS
    from local_model_auth import LocalModelError
    if (not options.name or not options.model or not options.task_file
            or len(options.positionals) != 1):
        raise ClientError('LOCAL_MODEL_CALLER_INVALID')
    provider = PROVIDERS.get(options.provider) if options.provider else PROVIDERS.get(
        options.program or ('codex' if options.codex else 'claude-code'))
    if (provider is None or (options.codex and provider != 'codex')
            or (options.program is not None and PROVIDERS.get(options.program) != provider)):
        raise ClientError('LOCAL_MODEL_CALLER_INVALID')
    if not parent.identity or not parent.identity.get('name'):
        raise ClientError('CHILD_PARENT_MISMATCH')
    if options.parent is not None and options.parent != parent.identity['name']:
        raise ClientError('CHILD_PARENT_MISMATCH')
    policy = runpy.run_path(str(ROOT/'hooks/child_tools.py'))
    try:
        tools = policy['parse_cli'](options.tools or [])
        caller = {'provider':provider, 'model':options.model,
            'cwd':options.positionals[0], 'task':options.task_file, 'name':options.name,
            'parent':parent.identity['name'], 'base':options.base or 'default',
            'tools':tools, 'clear_tools':bool(options.clear_tools),
            'detached':True if options.detached is None else options.detached,
            'resources':[item for value in (options.resources or [])
                         for item in (value.split(',') if value else [])],
            'worktree':bool(options.worktree), 'chrome':bool(options.chrome)}
        for key in ('effort', 'strict_output_cap', 'task_delivery'):
            value = getattr(options,key)
            if value is not None:caller[key] = value
        if options.embed_task:
            if caller.get('task_delivery','embedded') != 'embedded':
                raise ClientError('LOCAL_MODEL_CALLER_INVALID')
            caller['task_delivery'] = 'embedded'
        return validate_caller_request(caller)
    except policy['ToolsError']:
        raise ClientError('CHILD_TOOLS_INVALID') from None
    except LocalModelError as exc:
        raise ClientError(str(exc)) from None


def _check_shell_spawn(parent, target, state, options, *, expected_digest=None):
    from local_model_auth import LocalModelError
    from runtime_local_launch import admit_caller_request, admit_local_launch
    frame = read_json(target.profile_path) if target.profile_path.exists() or target.profile_path.is_symlink() else {}
    present = frame.get('launch_intent') is not None
    new_fields = any(getattr(options,key) is not None for key in _SHELL_CALLER_FIELDS) or options.detached is False
    explicit = options.codex or any(getattr(options,key) is not None
        for key in ('name','program','model','base','tools','detached'))
    full = new_fields or (options.model or '').startswith('local:') or (present and explicit)
    metadata = target.registration_metadata()
    provider = _CHILD_PROVIDERS.get(metadata.get('program'))
    caller = _shell_caller_input(options,parent) if full else None
    try:
        if caller is not None:
            if provider != caller['provider']:
                raise LocalModelError('LOCAL_MODEL_IDENTITY_MISMATCH')
            checked = admit_caller_request(target,caller,expected_digest=expected_digest)
        else:
            checked = admit_local_launch(target,provider,state['working_directory'],
                expected_digest=expected_digest)
        digest = checked['intent']['intent_digest'] if checked is not None else None
        if checked is not None:
            intent = checked['intent']
            if state['working_directory'] != intent['cwd']:
                raise LocalModelError('LOCAL_MODEL_IDENTITY_MISMATCH')
            if (state['tools_selection'] != {'base':intent['base'], 'tools':intent['tools']}
                    or state['task_delivery'] != intent.get('task_delivery','mail')):
                raise LocalModelError('LOCAL_MODEL_INTENT_CHANGED')
            if options.task_file is not None:
                if options.task_file != intent['task']:
                    raise LocalModelError('LOCAL_MODEL_INTENT_CHANGED')
                content = _read_intent_task_source(options.task_file)
            else:
                # A minimal reference reuses its approved private copy; the
                # original may have been removed after task publication.
                content = read_private(target.outputs['task'])
            if len(content) > 65536:raise ClientError('PAYLOAD_TOO_LARGE')
            if hashlib.sha256(content).hexdigest() != intent.get('task_sha256'):
                raise LocalModelError('LOCAL_MODEL_TASK_CHANGED')
            target.check_local_registration(target.identity['name'],metadata.get('program'),
                                            metadata.get('model'),metadata=True)
            if caller is not None or options.embed_task or options.task_delivery is not None:
                delivery = 'embedded' if options.embed_task else (options.task_delivery or 'mail')
                if delivery != intent.get('task_delivery','mail'):
                    raise LocalModelError('LOCAL_MODEL_INTENT_CHANGED')
        return {'caller':caller, 'intent_digest':digest}
    except LocalModelError as exc:
        raise ClientError(str(exc)) from None


def entry_operation(entry, argv):
    if os.environ.get('AGENTSTACK_MAIL_DISABLED') == '1' and entry in ('pre-edit', 'post-edit', 'end-session', 'session-start', 'notifications', 'registered', 'record-self'):
        return 0
    parser = argparse.ArgumentParser(prog=entry)
    parser.add_argument('--context')
    parser.add_argument('--child-client-name', '--client-name', dest='child_client_name')
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--name')
    parser.add_argument('--program')
    parser.add_argument('--model')
    parser.add_argument('--task-description')
    parser.add_argument('--task-file')
    parser.add_argument('--embed-task', action='store_true')
    parser.add_argument('--codex', action='store_true')
    parser.add_argument('--base', choices=['default','mail-only'])
    parser.add_argument('--tools', action='append')
    parser.add_argument('--detached', action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument('--provider', choices=['codex','codex-cli','claude','claude-code'])
    parser.add_argument('--parent')
    parser.add_argument('--resources', action='append')
    parser.add_argument('--worktree', action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument('--chrome', '--claude-chrome', action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument('--clear-tools', action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument('--effort')
    parser.add_argument('--strict-output-cap', action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument('--task-delivery', choices=['mail','embedded'])
    parser.add_argument('--operator', action='store_true')
    parser.add_argument('--agent-id', type=int)
    parser.add_argument('--expected-digest')
    parser.add_argument('positionals', nargs='*')
    choice = selected(argv)
    if choice['mode'] == 'legacy':
        return 125
    options = parser.parse_args(argv)
    spawn_fields = any(getattr(options,key) is not None for key in _SHELL_CALLER_FIELDS) or options.detached is False
    if entry != 'spawn-child' and spawn_fields:
        raise ClientError('ARGUMENTS_UNSUPPORTED')
    admitted = None
    if entry == 'spawn-child':
        parent = configured(options.context, selection=choice, read_only=True)
        if parent is None:return 125
        target, state = _entry_child(parent,options,read_only=True)
        admitted = _check_shell_spawn(parent,target,state,options)
    recovery = ('finalize-create', options.agent_id) if entry == 'finalize-child' and options.operator else None
    if entry in ('purge-child', 'reregister'):
        admission = runpy.run_path(str(Path(__file__).with_name('runtime_named_target.py')))
        requested = options.positionals[0] if len(options.positionals) == 1 else None
        client = admission['require_named_target'](choice, requested, globals())
    else:
        client = configured(options.context, recovery=recovery)
    if client is None:
        return 125
    if client.mode != 'global':
        raise ClientError('LEGACY_CONTEXT_REQUIRES_PR7')
    values = {k:v for k,v in vars(options).items() if v is not None}
    # Keep old downstream option values while the spawn adapter can distinguish
    # omitted CLI fields in its Namespace from explicit defaults.
    values.setdefault('base','default')
    values.setdefault('detached',False)
    if entry == 'preregister-child':
        if options.tools:
            tools=runpy.run_path(str(ROOT/'hooks/child_tools.py'))
            values['parsed_tools']=tools['parse_cli'](options.tools)
        values['program'] = options.program or ('codex' if options.codex else 'claude-code')
        values['model'] = options.model or values['program']
        result = client.prepare_child(values)
    elif entry in ('spawn-child','resume-child'):
        target, state = _entry_child(client,options)
        if entry == 'resume-child':
            result = target.resume_child(values)
        else:
            with target.operation_mutex():
                _, state = _entry_child(client,options,read_only=True)
                checked = _check_shell_spawn(client,target,state,options,
                    expected_digest=admitted['intent_digest'])
                if checked['intent_digest'] != admitted['intent_digest']:
                    raise ClientError('LOCAL_MODEL_INTENT_CHANGED')
                caller = checked['caller']
                embedded = (caller.get('task_delivery','mail') == 'embedded') if caller is not None else options.embed_task
                result, state = target._prepare_child_launch(options.task_file, embed_task=embedded)
        if not options.prepare_only:
            program = target.registration_metadata().get('program')
            provider = _CHILD_PROVIDERS.get(program)
            if provider is None:
                raise ClientError('PROVIDER_UNAVAILABLE')
            launch_options = {}
            if admitted is not None and admitted['intent_digest'] is not None:
                launch_options['expected_intent_digest'] = admitted['intent_digest']
            detached = admitted['caller']['detached'] if admitted is not None and admitted['caller'] is not None else True
            result = runtime_delivery(target).start(provider, state['working_directory'], detached=detached,
                launch_kind='resume' if entry == 'resume-child' else 'startup',
                provider_session_id=options.positionals[0] if entry == 'resume-child' and options.positionals else None,
                **launch_options)
    elif entry in ('pre-edit','post-edit','end-session','session-start','notifications','registered','record-self'):
        raw = sys.stdin.read(1048577)
        if len(raw) > 1048576:
            raise ClientError('HOOK_INPUT_TOO_LARGE')
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload,dict):
            raise ClientError('HOOK_INPUT_INVALID')
        payload.setdefault('session_id',os.environ.get('AGENTSTACK_SESSION_ID'))
        if entry == 'pre-edit':
            result = client.guard_edit(payload)
        elif entry == 'post-edit':
            result = client.post_edit(payload)
        elif entry == 'end-session':
            result = client.end_session(payload)
        elif entry == 'notifications':
            result = {'ok':True}
        elif entry == 'registered':
            result = client.observe()
        elif entry == 'record-self':
            response = payload.get('tool_response',payload.get('tool_result'))
            if isinstance(response,str):response=json.loads(response)
            if isinstance(response,dict) and 'content' in response:response=result_value({'result':response})
            if not isinstance(response,dict) or response.get('agent_id') != client.identity['agent_id']:
                result = {'ok':True,'recorded':False}
            else:
                result = client.record_session(payload)
        else:
            result = runtime_delivery(client).session_start(payload) if client.outputs['launch'].exists() else client.record_live(payload, refresh=True)
    elif entry == 'abandon-child-registration':
        if not options.operator or not options.expected_digest:
            raise ClientError('OPERATOR_CONFIRMATION_REQUIRED')
        result = client.abandon_registration(options.expected_digest)
    elif entry == 'purge-child':
        result = client.purge_child()
    elif entry == 'reregister':
        result = client.reconnect(program=options.program, model=options.model)
    elif entry == 'inspect-child':
        result = client.inspect_child()
    elif entry == 'cancel-child-resume':
        if not options.operator:
            raise ClientError('OPERATOR_CONFIRMATION_REQUIRED')
        with client.operation_mutex():
            state=read_json(client.outputs['child'])
            if state['phase'] != 'resume-planned':
                raise ClientError('LAUNCH_OUTCOME_UNKNOWN')
            status = client.session_liveness()
            if status != 'absent' and (status != 'unknown' or (client.runtime_dir / 'live-sessions').exists()):
                raise ClientError('SESSION_LIVENESS_UNKNOWN')
            client.retain_child(state)
            result={'ok':True,'runtime_ready':False}
    elif entry == 'finalize-child':
        if not options.operator or not options.agent_id:
            raise ClientError('OPERATOR_CONFIRMATION_REQUIRED')
        target = RuntimeClient(client.path, recovery=('finalize-create',options.agent_id))
        result = target.finalize_registration(options.agent_id)
    else:
        raise ClientError('CHILD_OPERATION_UNSUPPORTED')
    notifications = []
    if entry in ('session-start','notifications') and client.outputs['launch'].exists():
        notifications = runtime_delivery(client).hook_notifications('claude-hook')
        if notifications:
            result['hookSpecificOutput'] = {'hookEventName':'SessionStart' if entry == 'session-start' else 'PostToolUse', 'additionalContext':'\n'.join(n['notification'] for n in notifications)}
    print(json.dumps(result), flush=True)
    for notice in notifications:
        delivery = runtime_delivery(client)
        try:
            delivery.callback(notice['message_id'], notice['attempt_id'], confirmed=True)
        except (ClientError, OSError, ValueError) as exc:
            delivery.report_error(notice['message_id'], exc)
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    tmux_commands = {'tmux-capabilities': 'capabilities',
                     'tmux-legacy-transport': 'legacy-transport',
                     'tmux-diagnostics': 'diagnostics',
                     'tmux-legacy-target': 'legacy-target'}
    if argv and argv[0] in ('target','capture','attach','send-compact','end','replay-end'):
        return runpy.run_path(str(Path(__file__).with_name('runtime_target.py')))['main'](argv,api=globals())
    if argv and argv[0] in tmux_commands:
        # A legacy consumer's safety admission must not construct an owner or
        # turn its inherited mode into a second selector.
        return runpy.run_path(str(Path(__file__).with_name('runtime_tmux.py')))['main'](
            [tmux_commands[argv[0]], *argv[1:]])
    if argv and argv[0] == 'select':
        try:
            choice = selected(argv[1:], bootstrap='--bootstrap' in argv)
            if '--json' in argv:
                print(json.dumps(choice, default=str))
            elif '--shell' in argv:
                print('export AGENTSTACK_RUNTIME_MODE=' + shlex.quote(choice['mode']))
                if choice['mode'] == 'global' and choice['server_config'] is not None:
                    print('export AGENTSTACK_GLOBAL_SERVER_CONFIG=' + shlex.quote(str(choice['server_config'])))
                elif choice['mode'] == 'legacy':
                    print('unset AGENTSTACK_GLOBAL_SERVER_CONFIG')
            else:
                print('orrery-client-global-v1\n' + (str(choice['context']) if choice['context'] else '') if choice['mode'] == 'global' else 'orrery-client-legacy-v1')
            return 0
        except ClientError as exc:
            print('runtime-client: ' + str(exc), file=sys.stderr)
            return 64  # supported selector refused; partial helpers use argparse exit 2
    if argv and argv[0] in ('service-exec', 'service-health', 'operator-enroll'):
        try:
            choice = selected(argv[1:])
            if choice['mode'] != 'global':
                if argv[0] == 'service-exec':
                    os.execv(argv[1], argv[1:])
                return 125
            if choice['server_config'] is None:
                raise ClientError('SERVER_CONFIG_REQUIRED')
            operator = runpy.run_path(str(Path(__file__).with_name('runtime_operator.py')))['RuntimeOperator'](choice['server_config'], api=globals())
            if argv[0] == 'service-health':
                operator.read({'kind':'health'}); return 0
            if argv[0] == 'service-exec':
                # The existing immutable runner still owns PID/signal/restart.
                # Global never enters legacy service.foreground/runtime_config.
                index = argv.index('--server-executable') + 1
                executable = absolute(argv[index])
                endpoint = urlsplit(local_endpoint(os.environ.get('AGENTSTACK_MCP_URL','')))
                env = {**os.environ, 'AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE': 'passthrough'}
                os.execve(str(executable), [str(executable), '--global-config', str(operator.path),
                    '--host', endpoint.hostname, '--port', str(endpoint.port), '--path', endpoint.path], env)
            parser = argparse.ArgumentParser(prog='operator-enroll')
            parser.add_argument('action', choices=['inspect','claim','recover'])
            parser.add_argument('--agent-id', type=int, required=True)
            parser.add_argument('--request-id')
            parser.add_argument('--runtime-mode')
            parser.add_argument('--server-config')
            parser.add_argument('--expected-generation', type=int)
            args = parser.parse_args(argv[1:])
            request = {'version':1, **operator.binding, 'action':args.action, 'agent_id':args.agent_id}
            if args.action != 'inspect':
                # Existing management protocol; credential supplied on stdin,
                # never an argv value or echoed in diagnostics.
                request.update(request_id=args.request_id, expected_generation=args.expected_generation,
                               new_credential=sys.stdin.read().strip())
            reply = management_exchange(operator.config, request)
            if reply.get('ok') is not True: raise ClientError(reply.get('reason','MANAGEMENT_REJECTED'))
            print(json.dumps(reply)); return 0
        except (ClientError, OSError, ValueError, KeyError, IndexError) as exc:
            print('runtime-client: ' + (str(exc) if isinstance(exc,ClientError) else 'SERVICE_INPUT_INVALID'), file=sys.stderr)
            return 2
    if argv and argv[0] == 'entry':
        try:
            return entry_operation(argv[1], argv[2:])
        except (ClientError, OSError, ValueError, KeyError, TypeError) as exc:
            print('runtime-client: ' + (str(exc) if isinstance(exc,ClientError) else 'CLIENT_INPUT_INVALID'),file=sys.stderr)
            return 2
    parser = argparse.ArgumentParser()
    parser.add_argument('--context')
    parser.add_argument('--runtime-mode', choices=['legacy','global'])
    parser.add_argument('--server-config')
    parser.add_argument('--parent-context')
    parser.add_argument('--operator',action='store_true')
    parser.add_argument('--assume-yes',action='store_true')
    parser.add_argument('operation', choices=['mode', 'observe', 'reconnect', 'bootstrap', 'launch', 'call',
                                             'register', 'session', 'inspect', 'claim', 'recover', 'request-status', 'policy', 'save-profile', 'resolve-mutation', 'call-json', 'release-worker', 'replay-pending', 'proxy', 'operator-json', 'execute-launch', 'provider-exec', 'watch', 'doctor-local', 'daemon-serve', 'codex-plan', 'codex-apply', 'instruction-configure', 'watch-service', 'watch-auto'])
    parser.add_argument('args', nargs='*')
    a = parser.parse_args(argv)
    try:
        choice = selected(argv)
        if (a.parent_context or a.operator) and a.operation!='resolve-mutation':raise ClientError('ARGUMENTS_UNSUPPORTED')
        local_action = None
        if a.operation == 'instruction-configure':
            if len(a.args) >= 3:
                if (a.args[:1] != ['operator'] or a.args[1] not in ('codex','claude')
                        or a.args[2] not in ('local-plan','local-apply')
                        or len(a.args) != (3 if a.args[2]=='local-plan' else 4)):
                    raise ClientError('ARGUMENTS_UNSUPPORTED')
                local_action = a.args[2]
                if local_action == 'local-apply':
                    if not a.assume_yes:raise ClientError('OPERATOR_CONFIRMATION_REQUIRED')
                    if not re.fullmatch(r'[0-9a-f]{64}',a.args[3]):raise ClientError('ARGUMENTS_UNSUPPORTED')
        if a.assume_yes and local_action != 'local-apply':raise ClientError('ARGUMENTS_UNSUPPORTED')
        if local_action:
            # A read-only plan must not populate installed helper __pycache__
            # directories before the operator has approved any outputs.
            sys.dont_write_bytecode = True
        if a.operation == 'mode':
            print(choice['mode']); return 0
        if a.operation == 'watch-auto':
            a.operation = 'watch-service' if choice['server_config'] is not None else 'watch'
        if a.operation == 'watch-service':
            if choice['mode'] != 'global' or choice['server_config'] is None:
                raise ClientError('SERVER_CONFIG_REQUIRED')
            delivery_contract()['watch_service'](choice['server_config'], globals())
            return 0
        recovery = None
        if a.operation == 'resolve-mutation':
            recovery = ('resolve-mutation', None)
        if a.operation in {'claim', 'recover'}:
            context_path = selected(path=a.context)['context']
            if context_path is None:
                raise ClientError('CONTEXT_REQUIRED')
            config = read_json(context_path)
            recovery = (a.operation, (config.get('identity') or {}).get('agent_id'))
        if local_action and choice['mode']=='legacy':
            from local_model_auth import LocalModelError
            try:
                from runtime_legacy_configuration import instruction_configure
                raw=sys.stdin.read(1048577)
                if len(raw.encode('utf-8'))>1048576:raise ClientError('CLIENT_INPUT_INVALID')
                value=instruction_configure(a.args[1],local_action,json.loads(raw),
                    approved_id=a.args[3] if local_action=='local-apply' else None)
            except LocalModelError as exc:raise ClientError(str(exc)) from None
            print(json.dumps(value));return 0
        client = (configured(a.context,recovery=recovery,selection=choice,read_only=True) if local_action
                  else configured(a.context,recovery=recovery,selection=choice))
        if client is None:
            raise ClientError('CONTEXT_REQUIRED')
        if a.operation == 'instruction-configure':
            if local_action:
                raw = sys.stdin.read(1048577)
                if len(raw.encode('utf-8')) > 1048576:raise ClientError('CLIENT_INPUT_INVALID')
                payload = json.loads(raw)
                value = runtime_delivery(client).instruction_configure(a.args[1],local_action=local_action,
                    payload=payload,approved_id=a.args[3] if local_action=='local-apply' else None)
                print(json.dumps(value));return 0
            if len(a.args)!=2 or a.args[0]!='operator':raise ClientError('OPERATOR_CONFIRMATION_REQUIRED')
            print(json.dumps(runtime_delivery(client).instruction_configure(a.args[1])));return 0
        if a.operation in ('codex-plan','codex-apply'):
            if a.args != ['operator']:
                raise ClientError('OPERATOR_CONFIRMATION_REQUIRED')
            value=runtime_delivery(client).configure_codex(a.operation[6:],json.load(sys.stdin) if a.operation=='codex-apply' else None)
            print(json.dumps(value));return 0
        if a.operation == 'daemon-serve':
            delivery = runtime_delivery(client)
            delivery.session_start({'session_id':os.environ.get('AGENTSTACK_LAUNCH_ID'),
                'provider_pid':os.getpid(), 'cwd':os.getcwd()})
            try:
                delivery.proxy()
            finally:
                client.end_session({'session_id':os.environ.get('AGENTSTACK_LAUNCH_ID')})
            return 0
        if a.operation == 'watch':
            delivery = runtime_delivery(client)
            delivery.watch()
        if a.operation == 'doctor-local':
            print(json.dumps(runtime_delivery(client).diagnostics()))
            return 0
        if a.operation == 'proxy':
            runtime_delivery(client).proxy()
            return 0
        if a.operation == 'provider-exec':
            runtime_delivery(client).exec_provider(a.args[0],a.args[1],a.args[2] if len(a.args)>2 else None)
            return 0
        if a.operation == 'execute-launch':
            return runtime_delivery(client).execute(*a.args)
        if a.operation == 'operator-json':
            value = runtime_delivery(client).operator(a.args[0], json.load(sys.stdin))
        elif a.operation == 'observe':
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
        elif a.operation == 'call-json':
            value = client.call(a.args[0], json.load(sys.stdin))
        elif a.operation == 'release-worker':
            value = client.release_worker(a.args[0], [int(key) for key in a.args[1:]])
        elif a.operation == 'replay-pending':
            value = client.replay_pending()
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
            parent=configured(a.parent_context) if a.parent_context else None
            if a.operator:
                if (choice['mode']!='global' or choice['server_config'] is None
                        or Path(choice['server_config'])!=client.runtime/'server-config.json'
                        or not sys.stdin.isatty() or input('Acknowledge unknown terminal report locally? Type yes: ').strip()!='yes'):
                    raise ClientError('OPERATOR_CONFIRMATION_REQUIRED')
            value=client.resolve_mutation(current,a.args[0],**proof,lifecycle_parent=parent,operator=a.operator)
        elif a.operation == 'request-status':
            value = client.management('request_status', request_id=a.args[0])
        else:
            value = client.save_profile(a.args[0] if a.args else None)
        print(json.dumps(value))
        return 0
    except (ClientError, OSError, ValueError, TypeError, KeyError, IndexError) as exc:
        reason = str(exc) if isinstance(exc, ClientError) else 'CLIENT_INPUT_INVALID'
        if a.operation == 'release-worker' and 'client' in locals() and client is not None:
            try:
                with client.operation_mutex(), client.fence():
                    for key in a.args[1:]:
                        slot = client.release_slot(int(key))
                        client.validate_output(slot, 'release')
                        saved = read_json(slot)
                        if saved['generation'] == a.args[0]:
                            client.write_output(slot, {**saved, 'last_error': reason}, 'release')
            except (ClientError, OSError, ValueError, KeyError):
                pass  # A changed fence forbids even a local audit write.
        print('runtime-client: ' + reason, file=sys.stderr)
        if reason == 'CODEX_HOOK_CONFIGURE_REQUIRED':
            print('Review the fixed provider HOME hook plan with codex-plan operator, then codex-apply operator. See docs/global-delivery-client.md#codex-hook-trust.',file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
