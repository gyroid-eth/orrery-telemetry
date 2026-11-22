"""Explicit in-process B1 backend test factory, never the default server.

These handlers are the product's prospective backend, not a second Mail.
Bound model-facing proxy publication is B4; there is no CLI/HTTP entry here.
"""
from __future__ import annotations

import copy
import json

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import FunctionTool

from .boundary import ToolArgumentBoundary, install_tool_validation_log_sanitizer
from .storage_origin_contract import AUTH_SCHEMA, MAX_INT, Auth, B1Error, ConsumptionRequest, integer, name_keys, string


class RawB1Boundary(ToolArgumentBoundary):
    """Validate original JSON presence/types before Pydantic conversion."""
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
                raise B1Error('AUTH_PROOF_INCOMPLETE' if 'auth' in schema.get('required', []) else 'INVALID_ARGUMENTS')
            for key, value in args.items():
                if key == 'auth':
                    Auth.parse(value)
                elif key in {'target_agent_id', 'message_id', 'limit'}:
                    integer(value)
                elif key in {'target_name', 'expected_target_name'}:
                    name_keys(value)
                elif key == 'message_ids':
                    if not isinstance(value, list) or not 1 <= len(value) <= 100:
                        raise B1Error('INVALID_MESSAGE_IDS')
                    for item in value:
                        integer(item)
                elif key == 'recipients':
                    if not isinstance(value, list):
                        raise B1Error('INVALID_RECIPIENTS')
                    for selector in value:
                        if not isinstance(selector, dict) or set(selector) - {
                            'target_agent_id', 'target_name', 'expected_target_name', 'kind'}:
                            raise B1Error('INVALID_RECIPIENTS')
                        if (('target_agent_id' in selector) == ('target_name' in selector)):
                            raise B1Error('TARGET_SELECTOR_REQUIRED')
                        if 'target_agent_id' in selector:
                            integer(selector['target_agent_id'])
                        if 'target_name' in selector:
                            name_keys(selector['target_name'])
                        if 'expected_target_name' in selector:
                            name_keys(selector['expected_target_name'])
                        if selector.get('kind', 'to') not in {'to', 'cc', 'bcc'}:
                            raise B1Error('INVALID_RECIPIENT_KIND')
                else:
                    string(value, 4096 if key == 'cursor' else 128)
        except B1Error as error:
            raise ToolError(error.code) from None
        return await super().on_call_tool(context, call_next)


def build_candidate_server(core):
    server = FastMCP('unpublished-storage-origin-b1')
    install_tool_validation_log_sanitizer()
    server.add_middleware(RawB1Boundary(server))

    def publish(name, fn, *, owner=False, required=(), fields=None):
        tool = FunctionTool.from_function(fn, name=name)
        schema = tool.parameters
        schema['additionalProperties'] = False
        props = schema['properties']
        if fields is not None:
            for key in set(props) - set(fields):
                del props[key]
        for key in props:
            if key == 'auth':
                props[key] = copy.deepcopy(AUTH_SCHEMA)
            elif key in {'target_agent_id', 'message_id', 'limit'}:
                maximum = (100 if name == 'fetch_inbox' else 50) if key == 'limit' else MAX_INT
                props[key] = {'type': 'integer', 'minimum': 1, 'maximum': maximum}
            elif key == 'recipients':
                props[key] = {'type': 'array', 'minItems': 1, 'items': {
                    'type': 'object', 'additionalProperties': False,
                    'properties': {
                        'target_agent_id': {'type': 'integer', 'minimum': 1, 'maximum': MAX_INT},
                        'target_name': {'type': 'string', 'minLength': 1, 'maxLength': 128},
                        'expected_target_name': {'type': 'string', 'minLength': 1, 'maxLength': 128},
                        'kind': {'type': 'string', 'enum': ['to', 'cc', 'bcc']}},
                    'oneOf': [{'required': ['target_agent_id'], 'not': {'required': ['target_name']}},
                              {'required': ['target_name'], 'not': {'required': ['target_agent_id']}}]}}
            elif key == 'message_ids':
                props[key] = {'type': 'array', 'minItems': 1, 'maxItems': 100, 'uniqueItems': True,
                              'items': {'type': 'integer', 'minimum': 1, 'maximum': MAX_INT}}
            else:
                props[key] = {'type': 'string', 'minLength': 1,
                              'maxLength': 4096 if key == 'cursor' else
                              64 if key == 'expected_server_instance_id' else 128}
        if owner:
            schema['required'] = sorted((set(schema.get('required', [])) | {'auth'} | set(required)) & set(props))
        if name in {'fetch_inbox', 'whois', 'mark_message_read', 'acknowledge_message', 'consume_notification'}:
            schema['oneOf'] = [{'required': ['target_agent_id'], 'not': {'required': ['target_name']}},
                               {'required': ['target_name'], 'not': {'required': ['target_agent_id']}}]
        server.add_tool(tool)

    async def health_check(expected_server_instance_id: str | None = None) -> dict:
        try:
            return (await core.health() if expected_server_instance_id is None else
                    await core.health(expected_server_instance_id))
        except B1Error as e:
            raise ToolError(e.code) from None

    async def fetch_inbox(expected_server_instance_id: str, target_agent_id: int | None = None,
                          target_name: str | None = None, auth: dict | None = None,
                          topic: str | None = None, limit: int = 50, since: str | None = None) -> list:
        try:
            return await core.peek(expected_server_instance_id=expected_server_instance_id,
                                   target_agent_id=target_agent_id, target_name=target_name,
                                   auth=auth, topic=topic, limit=limit, since=since)
        except B1Error as e:
            raise ToolError(json.dumps({'code': e.code, **e.details})) from None

    async def whois(expected_server_instance_id: str, target_agent_id: int | None = None,
                    target_name: str | None = None, auth: dict | None = None,
                    expected_target_name: str | None = None) -> dict:
        try:
            return await core.whois(expected_server_instance_id=expected_server_instance_id,
                                   target_agent_id=target_agent_id, target_name=target_name, auth=auth,
                                   expected_target_name=expected_target_name)
        except B1Error as e:
            raise ToolError(json.dumps({'code': e.code, **e.details})) from None

    async def list_candidates(expected_server_instance_id: str, target_name: str,
                              auth: dict | None = None, limit: int = 50,
                              cursor: str | None = None) -> dict:
        try:
            return await core.directory(expected_server_instance_id=expected_server_instance_id,
                                        target_name=target_name, auth=auth, limit=limit, cursor=cursor)
        except B1Error as e:
            raise ToolError(json.dumps({'code': e.code, **e.details})) from None

    publish('health_check', health_check)
    publish('fetch_inbox', fetch_inbox)
    publish('whois', whois)
    publish('list_candidates', list_candidates)

    async def consume_notification(expected_server_instance_id: str, auth: dict,
                                   target_agent_id: int, message_ids: list[int], request_id: str) -> dict:
        try:
            await core.consume(ConsumptionRequest(expected_server_instance_id, Auth.parse(auth),
                target_agent_id, tuple(message_ids), request_id))
        except B1Error as e:
            raise ToolError(json.dumps({'code': e.code, **e.details})) from None
        raise ToolError('NOTIFICATION_CONSUMPTION_NOT_READY')

    publish('consume_notification', consume_notification, owner=True,
            required=('target_agent_id', 'message_ids', 'request_id'))

    def owner_handler(operation):
        async def execute(expected_server_instance_id: str, auth: dict | None = None,
                          target_agent_id: int | None = None, target_name: str | None = None,
                          expected_target_name: str | None = None,
                          message_id: int | None = None, request_id: str | None = None,
                          recipients: list[dict] | None = None) -> dict:
            try:
                return await core.owner(operation, expected_server_instance_id=expected_server_instance_id,
                    auth=auth, target_agent_id=target_agent_id, target_name=target_name,
                    expected_target_name=expected_target_name,
                    message_id=message_id, request_id=request_id, recipients=recipients)
            except B1Error as e:
                raise ToolError(json.dumps({'code': e.code, **e.details})) from None
        return execute

    for name, operation in {'send_message': 'send', 'reply_message': 'reply',
                            'mark_message_read': 'read', 'acknowledge_message': 'ack',
                            'reconnect': 'reconnect'}.items():
        fields = {'expected_server_instance_id', 'auth'}
        required = set()
        if operation != 'reconnect':
            fields.add('request_id'); required.add('request_id')
        if operation in {'reply', 'read', 'ack', 'consume'}:
            fields.add('message_id'); required.add('message_id')
        if operation in {'send', 'reply'}:
            fields.add('recipients')
        else:
            if operation != 'reconnect':
                fields.update(('target_agent_id', 'target_name', 'expected_target_name'))
        if operation == 'send':
            required.add('recipients')
        publish(name, owner_handler(operation), owner=True, required=required, fields=fields)
    return server
