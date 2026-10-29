"""Explicit isolated caller: protected owner proof, business JSON, exact replay.

prepare is a trusted local operation, not a model tool. Explicit fresh runtime
and guard entries use this client; normal launchers retain their default.
No legacy identity lookup exists.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from .fresh_runtime import FreshRuntime, _directory, _read_private, _write_private, mail_port_allowed, read_config
from .reservation_paths import ReservationError, normalize_paths
from .storage_origin_contract import Auth, B1Error, canonical, digest

KIND = 'orrery-fresh-client-v1'
MAX_REQUEST_BYTES = 16 * 1024 * 1024  # Only the explicit saved-business-request reader.
FIELDS = {
    'health_check': set(), 'reconnect': set(),
    'send_message': {'recipients', 'subject', 'body_md', 'thread_id', 'topic', 'importance', 'ack_required'},
    'reply_message': {'message_id', 'body_md', 'recipients', 'subject', 'topic', 'importance', 'ack_required'},
    'fetch_inbox': {'target_agent_id', 'target_name', 'topic', 'limit', 'since'},
    'whois': {'target_agent_id', 'target_name', 'expected_target_name'},
    'list_candidates': {'target_name', 'limit', 'cursor'},
    'mark_message_read': {'message_id', 'target_agent_id', 'target_name', 'expected_target_name'},
    'acknowledge_message': {'message_id', 'target_agent_id', 'target_name', 'expected_target_name'},
    'reserve_files': {'paths', 'ttl_seconds', 'exclusive', 'reason'},
    'renew_reservations': {'paths', 'file_reservation_ids', 'extend_seconds'},
    'release_reservations': {'paths', 'file_reservation_ids'},
    'check_reservations': {'paths', 'exclusive'},
    'guard_edit': {'paths', 'extend_seconds'},
    'notification_pending': {'message_id', 'limit', 'min_importance'},
    'notification_complete': {'message_id'},
}
WRITES = {'send_message', 'reply_message', 'mark_message_read', 'acknowledge_message',
          'reserve_files', 'renew_reservations', 'release_reservations'}


def startup_projection(context_path):
    """Trusted host startup data only; never publish a binding or owner token."""
    client = FreshBoundClient(context_path)
    context, _ = client._load()
    if context['server_mode'] != 'fresh':
        raise B1Error('FRESH_MODE_REQUIRED')
    return dict(runtime_mode='fresh', mail_instance_id=context['mail_instance_id'],
                operator_id=context['mail_agent_id'], context_digest=client.context_digest,
                context_path=str(client.path), server_config=str(Path(context['root'])/'config.json'),
                endpoint=context['endpoint'])


async def startup_health(context_path):
    projected = startup_projection(context_path)
    try:
        result = await asyncio.wait_for(_http(projected['endpoint'], 'health_check',
            {'expected_server_instance_id': projected['mail_instance_id']}), timeout=2)
    except (asyncio.TimeoutError, B1Error):
        raise B1Error('FRESH_SERVICE_UNAVAILABLE') from None
    if not isinstance(result, dict) or result.get('mail_instance_id') != projected['mail_instance_id']:
        raise B1Error('FRESH_SERVICE_MISMATCH')
    return {'ok': True, 'mail_instance_id': projected['mail_instance_id']}


def Client(*args, **kwargs):
    """Load the SDK for actual transport use; host-only targets need no MCP."""
    from fastmcp import Client as SDKClient
    return SDKClient(*args, **kwargs)


def _uuid(value):
    try:
        if str(UUID(value)) != value:
            raise ValueError
        return value
    except (ValueError, TypeError, AttributeError):
        raise B1Error('FRESH_REQUEST_ID_INVALID') from None


def _endpoint(value, *, server_mode='isolated'):
    try:
        url = urlsplit(value)
        if (url.scheme != 'http' or url.hostname not in {'127.0.0.1', 'localhost', '::1'}
                or url.username is not None or url.password is not None
                or not mail_port_allowed(url.port, server_mode)
                or url.path != '/mcp' or url.query or url.fragment):
            raise ValueError
        return value
    except (ValueError, TypeError, AttributeError):
        raise B1Error('FRESH_ENDPOINT_INVALID') from None


def _owner(path, root):
    _directory(root / 'bindings')
    value = _read_private(path)
    if (set(value) != {'kind', 'mail_instance_id', 'mail_agent_id',
                       'credential_generation', 'registration_token'}
            or value['kind'] != 'orrery-fresh-owner-v1'
            or path != root / 'bindings' / (str(value['mail_agent_id']) + '.json')):
        raise B1Error('FRESH_BINDING_INVALID')
    proof = dict(actor_agent_id=value['mail_agent_id'], registration_token=value['registration_token'],
                 expected_credential_generation=value['credential_generation'])
    Auth.parse(proof)
    return value, proof


async def _http(endpoint, tool, arguments):
    try:
        async with Client(endpoint, timeout=20) as client:
            result = await client.call_tool(tool, arguments, raise_on_error=False)
    except Exception:
        # Never echo transport exception text: it may include request credentials.
        raise B1Error('FRESH_TRANSPORT_UNAVAILABLE') from None
    if result.is_error:
        for content in result.content:
            text = getattr(content, 'text', '')
            try:
                error = json.loads(text)
            except (ValueError, TypeError):
                continue
            if isinstance(error, dict) and isinstance(error.get('code'), str):
                raise B1Error(error['code'], **{k: v for k, v in error.items() if k != 'code'})
        # Boundary codes are fixed uppercase strings, never the submitted JSON.
        for content in result.content:
            text = getattr(content, 'text', '')
            if text and all(c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789' for c in text):
                raise B1Error(text)
        raise B1Error('FRESH_BACKEND_REFUSED')
    return result.data


class FreshBoundClient:
    def __init__(self, context_path, *, dispatch=None):
        self.path = Path(context_path)
        self.dispatch = dispatch or _http  # Trusted host injection, never business JSON.
        self.context = _read_private(self.path)
        self.context_digest = digest(self.context)
        self._load()

    def _load(self):
        context = _read_private(self.path)
        if digest(context) != self.context_digest:
            raise B1Error('FRESH_CLIENT_CONTEXT_CHANGED')
        if (set(context) != {'kind', 'root', 'client_id', 'mail_instance_id', 'mail_agent_id',
                             'credential_generation', 'binding_path', 'binding_digest', 'endpoint', 'server_mode'}
                or context['kind'] != KIND):
            raise B1Error('FRESH_CLIENT_CONTEXT_INVALID')
        root, server = read_config(Path(context['root'])/'config.json')
        if (server['mode'] != context['server_mode'] or server['instance_id'] != context['mail_instance_id']
                or str(root) != context['root']):raise B1Error('FRESH_CLIENT_CONFIG_MISMATCH')
        _directory(root / 'clients')
        directory = root / 'clients' / _uuid(context['client_id'])
        _directory(directory)
        _directory(directory / 'requests')
        if self.path != directory / 'context.json':
            raise B1Error('FRESH_CLIENT_CONTEXT_INVALID')
        _endpoint(context['endpoint'], server_mode=server['mode'])
        binding, proof = _owner(Path(context['binding_path']), root)
        if (digest(binding) != context['binding_digest'] or any(binding[key] != context[key]
                for key in ('mail_instance_id', 'mail_agent_id', 'credential_generation'))):
            raise B1Error('FRESH_BINDING_CHANGED')
        return context, proof

    @classmethod
    async def prepare(cls, config_path, binding_path, endpoint, *, client_id, dispatch=None):
        """Trusted local config + registered owner; idempotent private publication."""
        _uuid(client_id)
        runtime = await FreshRuntime.open(config_path)
        try:
            _endpoint(endpoint, server_mode=runtime.config['mode'])
            binding, proof = _owner(Path(binding_path), runtime.root)
            if binding['mail_instance_id'] != runtime.config['instance_id']:
                raise B1Error('INSTANCE_MISMATCH')
            expected = dict(expected_server_instance_id=binding['mail_instance_id'], auth=proof)
            owner = await (dispatch or _http)(endpoint, 'reconnect', expected)
            if any(owner.get(key) != binding[key] for key in
                   ('mail_instance_id', 'mail_agent_id', 'credential_generation')):
                raise B1Error('FRESH_BINDING_MISMATCH')
            context = dict(kind=KIND, root=str(runtime.root), client_id=client_id,
                           server_mode=runtime.config['mode'],
                           binding_path=str(binding_path), binding_digest=digest(binding), endpoint=endpoint,
                           **{key: binding[key] for key in
                              ('mail_instance_id', 'mail_agent_id', 'credential_generation')})
            directory = runtime.root / 'clients' / client_id
            for folder in (directory.parent, directory, directory / 'requests'):
                folder.mkdir(mode=0o700, exist_ok=True)
                _directory(folder)
            path = directory / 'context.json'
            try:
                _write_private(path, context)
            except FileExistsError:
                if _read_private(path) != context:
                    raise B1Error('FRESH_CLIENT_CONTEXT_CONFLICT') from None
            return cls(path, dispatch=dispatch)
        finally:
            await runtime.close()

    def _business(self, tool, arguments):
        if tool not in FIELDS or not isinstance(arguments, dict):
            raise B1Error('INVALID_ARGUMENTS')
        # This one legacy compatibility field is deliberately ignored, regardless
        # of its value. It is neither identity nor a path anchor.
        if set(arguments) - FIELDS[tool] - {'project_key'}:
            raise B1Error('UNKNOWN_ARGUMENTS')
        try:
            args = json.loads(canonical({k: v for k, v in arguments.items() if k != 'project_key'}))
        except (ValueError, TypeError, UnicodeError):
            raise B1Error('INVALID_ARGUMENTS') from None
        if 'paths' in args:
            paths = args['paths']
            if (not isinstance(paths, list) or not 1 <= len(paths) <= 100
                    or any(not isinstance(p, str) or not p for p in paths)):
                raise B1Error('INVALID_PATH')
            try:
                args['paths'] = [p.value for p in normalize_paths(paths, anchor=Path.cwd())]
            except ReservationError as error:
                raise B1Error(str(error)) from None
        if tool in {'fetch_inbox', 'mark_message_read', 'acknowledge_message'}:
            if 'target_agent_id' not in args and 'target_name' not in args:
                args['target_agent_id'] = self.context['mail_agent_id']
        return args

    async def _invoke(self, tool, business, request_id=None):
        context, proof = self._load()
        args = dict(business, expected_server_instance_id=context['mail_instance_id'])
        if tool != 'health_check':
            args['auth'] = proof
        if request_id is not None:
            args['request_id'] = request_id
        try:
            return await self.dispatch(context['endpoint'], tool, args)
        except B1Error as error:
            if request_id is not None:
                error.details['request_id'] = request_id
            raise

    async def call(self, tool, arguments, *, request_id=None):
        self._load()
        args = self._business(tool, arguments)
        if tool not in WRITES:
            if request_id is not None:
                raise B1Error('FRESH_REQUEST_ID_UNEXPECTED')
            return await self._invoke(tool, args)
        request_id = _uuid(request_id or str(uuid4()))
        saved = dict(kind='orrery-fresh-request-v1', context_digest=self.context_digest,
                     tool=tool, arguments=args, request_id=request_id)
        if len(canonical(saved).encode()) + 1 > MAX_REQUEST_BYTES:
            raise B1Error('FRESH_REQUEST_TOO_LARGE')
        path = self.path.parent / 'requests' / (request_id + '.json')
        try:
            _write_private(path, saved)
        except FileExistsError:
            if _read_private(path, max_bytes=MAX_REQUEST_BYTES) != saved:
                raise B1Error('REQUEST_INTENT_CONFLICT') from None
        return await self._invoke(tool, args, request_id)

    async def replay(self, request_id):
        self._load()
        request_id = _uuid(request_id)
        saved = _read_private(self.path.parent / 'requests' / (request_id + '.json'),
                              max_bytes=MAX_REQUEST_BYTES)
        if (set(saved) != {'kind', 'context_digest', 'tool', 'arguments', 'request_id'}
                or saved['kind'] != 'orrery-fresh-request-v1'
                or saved['context_digest'] != self.context_digest or saved['request_id'] != request_id
                or saved['tool'] not in WRITES or not isinstance(saved['arguments'], dict)
                or set(saved['arguments']) - FIELDS[saved['tool']]):
            raise B1Error('FRESH_REQUEST_INVALID')
        # No current cwd, name lookup, new request ID, or locally cached success.
        return await self._invoke(saved['tool'], saved['arguments'], request_id)


def main(argv=None):
    import sys
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] in {'child-preregister', 'child-spawn', 'child-run'}:
        from .fresh_child import main as child_main
        return child_main(arguments[0], arguments[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prepare = commands.add_parser('prepare')
    for flag in ('config', 'binding', 'endpoint', 'client-id'):
        prepare.add_argument('--' + flag, required=True)
    call = commands.add_parser('call-json')
    call.add_argument('--context', required=True)
    call.add_argument('--tool', required=True)
    call.add_argument('--request-id')
    replay = commands.add_parser('replay')
    replay.add_argument('--context', required=True)
    replay.add_argument('--request-id', required=True)
    proxy = commands.add_parser('proxy')
    proxy.add_argument('--context', required=True)
    for action in ('startup-plan', 'startup-health'):
        startup = commands.add_parser(action)
        startup.add_argument('--context', required=True)
    session = commands.add_parser('session-hook')
    session.add_argument('--context', required=True)
    managed_hook = commands.add_parser('harness-hook')
    managed_hook.add_argument('--context', required=True)
    managed_hook.add_argument('--action', choices=('registered','post-edit'), required=True)
    notify = commands.add_parser('notifications')
    notify.add_argument('--context', required=True)
    notify.add_argument('--wrapper-root', required=True)
    watch = commands.add_parser('watch-fresh')
    watch.add_argument('--config', required=True)
    watch.add_argument('--wrapper-root', required=True)
    watch.add_argument('--once', action='store_true')
    for operation in ('launch', 'resume'):
        launch = commands.add_parser(operation)
        launch.add_argument('--context', required=True)
        launch.add_argument('--wrapper-root', required=True)
        launch.add_argument('provider', choices=('claude', 'codex', 'dsh', 'hermes'))
        launch.add_argument('cwd')
        if operation == 'resume':launch.add_argument('--session-id', required=True)
    options = parser.parse_args(argv)

    async def run():
        if options.command == 'prepare':
            client = await FreshBoundClient.prepare(options.config, options.binding, options.endpoint,
                                                    client_id=options.client_id)
            return {'context_path': str(client.path), 'mail_instance_id': client.context['mail_instance_id'],
                    'mail_agent_id': client.context['mail_agent_id']}
        client = FreshBoundClient(options.context)
        if options.command == 'replay':
            return await client.replay(options.request_id)
        import sys
        return await client.call(options.tool, json.loads(sys.stdin.read(MAX_REQUEST_BYTES + 1)),
                                 request_id=options.request_id)
    try:
        if options.command in {'startup-plan', 'startup-health'}:
            result = (startup_projection(options.context) if options.command == 'startup-plan'
                      else asyncio.run(startup_health(options.context)))
            print(canonical(result))
            return 0
        if options.command == 'harness-hook':
            from .fresh_harness import hook
            raw = sys.stdin.read(262145)
            if len(raw) > 262144:raise B1Error('FRESH_HOOK_EVENT_INVALID')
            asyncio.run(hook(FreshBoundClient(options.context), options.action, json.loads(raw)))
            return 0
        if options.command == 'watch-fresh':
            from .fresh_delivery import watch_service
            result = watch_service(options.config, options.wrapper_root, once=options.once)
            if result is not None:print(canonical(result))
            return 0 if result is None or result['ok'] else 1
        if options.command in {'launch', 'resume', 'session-hook', 'notifications'}:
            from .fresh_launch import launch, record_session
            client = FreshBoundClient(options.context)
            if options.command in {'session-hook', 'notifications'}:
                import sys
                raw = sys.stdin.read(262145)
                if len(raw) > 262144:raise B1Error('FRESH_SESSION_EVENT_INVALID')
                if options.command == 'notifications':
                    from .fresh_notifications import hook
                    asyncio.run(hook(client, json.loads(raw), options.wrapper_root))
                else:asyncio.run(record_session(client, json.loads(raw)))
                return 0
            return launch(client, options.wrapper_root, options.provider, options.cwd,
                          session_id=getattr(options, 'session_id', None))
        if options.command == 'proxy':
            from .fresh_proxy import serve
            asyncio.run(serve(FreshBoundClient(options.context)))
            return 0
        print(canonical(asyncio.run(run())))
        return 0
    except B1Error as error:
        if options.command in {'proxy', 'launch', 'resume', 'session-hook', 'notifications', 'watch-fresh', 'harness-hook'}:
            import sys
            print('FRESH PROXY: ' + error.code, file=sys.stderr)
            return 2
        print(canonical({'code': error.code} if options.command.startswith('startup-')
                        else {'code': error.code, **error.details}))
        return 1
    except (OSError, ValueError, TypeError):
        if options.command in {'proxy', 'launch', 'resume', 'session-hook', 'notifications', 'watch-fresh', 'harness-hook'}:
            import sys
            print('FRESH PROXY: FRESH_CLIENT_UNAVAILABLE', file=sys.stderr)
            return 2
        print(canonical({'code': 'FRESH_CLIENT_UNAVAILABLE'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
