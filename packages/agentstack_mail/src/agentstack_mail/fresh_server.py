"""Isolated fresh backend; local init/registration are deliberately absent.

This is not a model-facing bound proxy. Its explicit Auth is supplied by the
trusted caller adapter in the later wiring stage.
"""
from __future__ import annotations

import copy
import json

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import FunctionTool

from .boundary import ToolArgumentBoundary, install_tool_validation_log_sanitizer
from .fresh_runtime import FreshRuntime
from .storage_origin_contract import AUTH_SCHEMA, Auth, B1Error, integer, name_keys, string


class FreshBoundary(ToolArgumentBoundary):
    async def on_call_tool(self, context, call_next):
        try:
            schema = await self._parameter_schema(str(context.message.name))
            args = context.message.arguments
            if args is None:
                args = {}
            if not isinstance(args, dict) or schema is None:
                raise B1Error('INVALID_ARGUMENTS')
            if set(args) - set(schema['properties']):
                raise B1Error('UNKNOWN_ARGUMENTS')
            if set(schema.get('required', [])) - set(args):
                raise B1Error('AUTH_PROOF_INCOMPLETE' if 'auth' in schema.get('required', [])
                              else 'INVALID_ARGUMENTS')
            for key, value in args.items():
                if key == 'auth':
                    Auth.parse(value)
                elif key in {'target_agent_id', 'message_id', 'limit', 'ttl_seconds', 'extend_seconds'}:
                    integer(value)
                elif key in {'target_name', 'expected_target_name'}:
                    name_keys(value)
                elif key in {'ack_required', 'exclusive'}:
                    if type(value) is not bool:
                        raise B1Error('INVALID_ARGUMENTS')
                elif key == 'paths':
                    if not isinstance(value, list) or not 1 <= len(value) <= 100:
                        raise B1Error('INVALID_PATH')
                    for path in value:
                        string(path, 4096)
                elif key == 'file_reservation_ids':
                    if not isinstance(value, list) or not 1 <= len(value) <= 100:
                        raise B1Error('LEASE_SELECTOR_INVALID')
                    for lease_id in value:
                        integer(lease_id)
                elif key == 'reason':
                    if not isinstance(value, str) or len(value) > 512:
                        raise B1Error('INVALID_RESERVATION')
                elif key == 'recipients':
                    if not isinstance(value, list) or not 1 <= len(value) <= 100:
                        raise B1Error('INVALID_RECIPIENTS')
                    for recipient in value:
                        if (not isinstance(recipient, dict) or set(recipient) - {
                                'target_agent_id', 'target_name', 'expected_target_name', 'kind'}
                                or (('target_agent_id' in recipient) == ('target_name' in recipient))):
                            raise B1Error('INVALID_RECIPIENTS')
                        for field in ('target_name', 'expected_target_name'):
                            if field in recipient:
                                name_keys(recipient[field])
                        if 'target_agent_id' in recipient:
                            integer(recipient['target_agent_id'])
                        if recipient.get('kind', 'to') not in {'to', 'cc', 'bcc'}:
                            raise B1Error('INVALID_RECIPIENT_KIND')
                elif key == 'body_md':
                    if not isinstance(value, str) or len(value) > 1024 * 1024:
                        raise B1Error('INVALID_MESSAGE')
                else:
                    string(value, 4096 if key == 'cursor' else 512 if key == 'subject' else 128)
        except B1Error as error:
            raise ToolError(error.code) from None
        return await super().on_call_tool(context, call_next)


def build_fresh_server(config_path=None, *, core=None):
    if (config_path is None) == (core is None):
        raise B1Error('FRESH_BACKEND_REQUIRED')
    server = FastMCP('isolated-fresh-mail')
    install_tool_validation_log_sanitizer()
    server.add_middleware(FreshBoundary(server))

    async def call(method, **args):
        runtime = None
        try:
            target = core
            if target is None:
                runtime = await FreshRuntime.open(config_path)
                target = runtime.core
            return await getattr(target, method)(**args)
        except B1Error as error:
            raise ToolError(json.dumps({'code': error.code, **error.details})) from None
        finally:
            if runtime is not None:
                await runtime.close()

    def publish(name, function):
        tool = FunctionTool.from_function(function, name=name)
        tool.parameters['additionalProperties'] = False
        if 'auth' in tool.parameters['properties']:
            tool.parameters['properties']['auth'] = copy.deepcopy(AUTH_SCHEMA)
        server.add_tool(tool)

    async def health_check(expected_server_instance_id: str | None = None) -> dict:
        args = {} if expected_server_instance_id is None else {
            'expected_server_instance_id': expected_server_instance_id}
        return await call('health', **args)

    async def send_message(expected_server_instance_id: str, auth: dict, request_id: str,
                           recipients: list[dict], subject: str, body_md: str,
                           thread_id: str | None = None, topic: str | None = None,
                           importance: str = 'normal', ack_required: bool = False) -> dict:
        return await call('write_message', operation='send', expected_server_instance_id=expected_server_instance_id,
                          auth=auth, request_id=request_id, recipients=recipients, subject=subject,
                          body_md=body_md, thread_id=thread_id, topic=topic,
                          importance=importance, ack_required=ack_required)

    async def reply_message(expected_server_instance_id: str, auth: dict, request_id: str,
                            message_id: int, body_md: str, recipients: list[dict] | None = None,
                            subject: str | None = None, topic: str | None = None,
                            importance: str | None = None, ack_required: bool | None = None) -> dict:
        return await call('write_message', operation='reply', expected_server_instance_id=expected_server_instance_id,
                          auth=auth, request_id=request_id, message_id=message_id,
                          recipients=recipients, subject=subject, body_md=body_md,
                          topic=topic, importance=importance, ack_required=ack_required)

    async def fetch_inbox(expected_server_instance_id: str, target_agent_id: int | None = None,
                          target_name: str | None = None, auth: dict | None = None,
                          topic: str | None = None, limit: int = 50, since: str | None = None) -> list:
        return await call('peek', expected_server_instance_id=expected_server_instance_id,
                          target_agent_id=target_agent_id, target_name=target_name,
                          auth=auth, topic=topic, limit=limit, since=since)

    async def whois(expected_server_instance_id: str, target_agent_id: int | None = None,
                    target_name: str | None = None, expected_target_name: str | None = None,
                    auth: dict | None = None) -> dict:
        return await call('whois', expected_server_instance_id=expected_server_instance_id,
                          target_agent_id=target_agent_id, target_name=target_name,
                          expected_target_name=expected_target_name, auth=auth)

    async def list_candidates(expected_server_instance_id: str, target_name: str,
                              limit: int = 50, cursor: str | None = None,
                              auth: dict | None = None) -> dict:
        return await call('directory', expected_server_instance_id=expected_server_instance_id,
                          target_name=target_name, limit=limit, cursor=cursor, auth=auth)

    def receipt(operation):
        async def apply(expected_server_instance_id: str, auth: dict, request_id: str,
                        message_id: int, target_agent_id: int | None = None,
                        target_name: str | None = None, expected_target_name: str | None = None) -> dict:
            return await call('owner', operation=operation, expected_server_instance_id=expected_server_instance_id,
                              auth=auth, request_id=request_id, message_id=message_id,
                              target_agent_id=target_agent_id, target_name=target_name,
                              expected_target_name=expected_target_name)
        return apply

    async def reconnect(expected_server_instance_id: str, auth: dict) -> dict:
        return await call('owner', operation='reconnect', expected_server_instance_id=expected_server_instance_id,
                          auth=auth)

    async def notification_pending(expected_server_instance_id: str, auth: dict,
                                   message_id: int | None = None, limit: int = 50,
                                   min_importance: str = 'low') -> dict:
        return await call('notification', operation='pending', expected_server_instance_id=expected_server_instance_id,
                          auth=auth, message_id=message_id, limit=limit, min_importance=min_importance)

    async def notification_complete(expected_server_instance_id: str, auth: dict, message_id: int) -> dict:
        return await call('notification', operation='complete', expected_server_instance_id=expected_server_instance_id,
                          auth=auth, message_id=message_id)

    async def reserve_files(expected_server_instance_id: str, auth: dict, request_id: str,
                            paths: list[str], ttl_seconds: int = 3600,
                            exclusive: bool = True, reason: str = '') -> dict:
        return await call('reservation', operation='reserve', expected_server_instance_id=expected_server_instance_id,
                          auth=auth, request_id=request_id, paths_input=paths,
                          ttl_seconds=ttl_seconds, exclusive=exclusive, reason=reason)

    async def renew_reservations(expected_server_instance_id: str, auth: dict, request_id: str,
                                 paths: list[str] | None = None, file_reservation_ids: list[int] | None = None,
                                 extend_seconds: int = 1800) -> dict:
        return await call('reservation', operation='renew', expected_server_instance_id=expected_server_instance_id,
                          auth=auth, request_id=request_id, paths_input=paths,
                          file_reservation_ids=file_reservation_ids, extend_seconds=extend_seconds)

    async def release_reservations(expected_server_instance_id: str, auth: dict, request_id: str,
                                   paths: list[str] | None = None,
                                   file_reservation_ids: list[int] | None = None) -> dict:
        return await call('reservation', operation='release', expected_server_instance_id=expected_server_instance_id,
                          auth=auth, request_id=request_id, paths_input=paths,
                          file_reservation_ids=file_reservation_ids)

    async def check_reservations(expected_server_instance_id: str, auth: dict, paths: list[str],
                                 exclusive: bool = True) -> dict:
        return await call('reservation', operation='check', expected_server_instance_id=expected_server_instance_id,
                          auth=auth, paths_input=paths, exclusive=exclusive)

    async def guard_edit(expected_server_instance_id: str, auth: dict, paths: list[str],
                         extend_seconds: int = 1800) -> dict:
        return await call('reservation', operation='guard', expected_server_instance_id=expected_server_instance_id,
                          auth=auth, paths_input=paths, extend_seconds=extend_seconds)

    for name, function in (
        ('health_check', health_check), ('send_message', send_message), ('reply_message', reply_message),
        ('fetch_inbox', fetch_inbox), ('whois', whois), ('list_candidates', list_candidates),
        ('mark_message_read', receipt('read')), ('acknowledge_message', receipt('ack')),
        ('reconnect', reconnect),
        ('notification_pending', notification_pending), ('notification_complete', notification_complete),
        ('reserve_files', reserve_files), ('renew_reservations', renew_reservations),
        ('release_reservations', release_reservations), ('check_reservations', check_reservations),
        ('guard_edit', guard_edit),
    ):
        publish(name, function)
    return server
