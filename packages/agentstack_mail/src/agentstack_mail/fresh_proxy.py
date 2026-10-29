"""Bound fresh MCP stdio through the existing runtime-client command.

Uses the existing runtime_delivery.proxy initialize/list/call pattern. Owner
binding is supplied by trusted preparation, never by model bootstrap inputs.
Published business schemas are derived from the actual backend and used for
the original JSON boundary, before the protected caller injects its proof.
"""
from __future__ import annotations

import copy
import json
import sys

from .fresh_client import Client, FIELDS, MAX_REQUEST_BYTES, WRITES
from .schema_contract import validate_schema
from .storage_origin_contract import B1Error, canonical

PUBLISHED = frozenset(FIELDS) - {'health_check', 'reconnect', 'guard_edit',
                                'notification_pending', 'notification_complete'}
STATUS_SCHEMA = {'type': 'object', 'properties': {}, 'additionalProperties': False}


async def verified_owner(client):
    owner = await client.call('reconnect', {})
    if (not isinstance(owner, dict) or any(owner.get(key) != client.context[key]
            for key in ('mail_instance_id', 'mail_agent_id', 'credential_generation'))):
        raise B1Error('FRESH_PROXY_BINDING_MISMATCH')
    return {key: owner[key] for key in
            ('mail_instance_id', 'mail_agent_id', 'credential_generation', 'agent_name')}


def business_schema(name, schema):
    properties = schema.get('properties', {})
    proof_fields = {'expected_server_instance_id', 'auth'} | ({'request_id'} if name in WRITES else set())
    if set(properties) != FIELDS[name] | proof_fields:
        raise B1Error('FRESH_PROXY_SCHEMA_MISMATCH')
    public = {}
    for key in sorted(FIELDS[name]):
        value = copy.deepcopy(properties[key])
        # FreshBoundary treats omission separately from explicit JSON null.
        if 'anyOf' in value:
            options = [option for option in value['anyOf'] if option.get('type') != 'null']
            if len(options) == 1:
                value = {k: v for k, v in value.items() if k != 'anyOf'} | options[0]
            else:
                value['anyOf'] = options
        if value.get('default', False) is None:
            value.pop('default')
        public[key] = value
    return dict(type='object', properties=public, additionalProperties=False,
                required=[key for key in schema.get('required', []) if key in public])


async def catalogue(client):
    await verified_owner(client)
    context, _ = client._load()
    try:
        async with Client(context['endpoint'], timeout=20) as backend:
            tools = await backend.list_tools()
    except Exception:
        raise B1Error('FRESH_TRANSPORT_UNAVAILABLE') from None
    result = {}
    for tool in tools:
        if tool.name not in PUBLISHED:
            continue
        if tool.name in result:
            raise B1Error('FRESH_PROXY_SCHEMA_MISMATCH')
        row = dict(name=tool.name, description=tool.name,
                   inputSchema=business_schema(tool.name, tool.inputSchema))
        if tool.outputSchema is not None:
            row['outputSchema'] = copy.deepcopy(tool.outputSchema)
        result[tool.name] = row
    if set(result) != PUBLISHED:
        raise B1Error('FRESH_PROXY_SCHEMA_MISMATCH')
    result['runtime_status'] = dict(name='runtime_status', description='Verify this bound Mail identity.',
                                   inputSchema=STATUS_SCHEMA,
                                   outputSchema={'type': 'object', 'additionalProperties': True})
    return result


def tool_result(value, *, error=False):
    return dict(content=[{'type': 'text', 'text': canonical(value)}],
                structuredContent=value if isinstance(value, dict) else {'result': value}, isError=error)


async def serve(client, source=None, output=None, *, resolve_external=None):
    source = sys.stdin if source is None else source
    output = sys.stdout if output is None else output
    tools = await catalogue(client)  # No protocol success before owner verification.
    external_reference = None
    if resolve_external is not None:
        tools['bootstrap'] = dict(name='bootstrap',description='Fix this process to a bridge-observed Desktop session.',
            inputSchema=dict(type='object',properties={'session_id':{'type':'string','minLength':1,'maxLength':1024},
                'agent_id':{'anyOf':[{'type':'string','minLength':1,'maxLength':1024},{'type':'null'}]}},required=['session_id'],additionalProperties=False))
    initialized = False
    while True:
        line = source.readline(MAX_REQUEST_BYTES + 1)
        if not line:
            return
        if len(line) > MAX_REQUEST_BYTES:
            raise B1Error('FRESH_PROXY_REQUEST_TOO_LARGE')
        request_id = None
        try:
            request = json.loads(line)
            if (not isinstance(request, dict) or request.get('jsonrpc') != '2.0'
                    or not isinstance(request.get('method'), str)
                    or set(request) - {'jsonrpc', 'method', 'id', 'params'}):
                raise B1Error('REQUEST_INVALID')
            if 'id' not in request:
                continue  # Notifications never execute a business operation.
            if request['id'] is not None and type(request['id']) not in {int, str}:
                raise B1Error('REQUEST_INVALID')
            request_id = request['id']
            method, params = request['method'], request.get('params', {})
            if not isinstance(params, dict):
                raise B1Error('REQUEST_INVALID')
            client._load()
            if method == 'initialize':
                initialized = True
                result = dict(protocolVersion='2025-06-18', capabilities={'tools': {'listChanged': False}},
                              serverInfo={'name': 'orrery-mail', 'version': 'fresh-isolated-v1'})
            elif not initialized:
                raise B1Error('INITIALIZE_REQUIRED')
            elif method == 'ping':
                result = {}
            elif method == 'tools/list':
                await verified_owner(client)
                result = {'tools': [tools[name] for name in sorted(tools)]}
            elif method == 'tools/call':
                try:
                    if set(params) - {'name', 'arguments', '_meta'}:
                        raise B1Error('INVALID_ARGUMENTS')
                    name, args = params.get('name'), params.get('arguments', {})
                    if not isinstance(name, str) or name not in tools:
                        raise B1Error('TOOL_UNPUBLISHED')
                    validate_schema(args, tools[name]['inputSchema'], 'INVALID_ARGUMENTS', error_type=B1Error)
                    # Public proxy has no project_key compatibility input. The
                    # local caller's one-field legacy compatibility stays local.
                    if resolve_external is not None and name == 'bootstrap':
                        reference=(args['session_id'],args.get('agent_id'))
                        if external_reference is not None and reference != external_reference:
                            raise B1Error('FRESH_APP_PROCESS_BINDING_MISMATCH')
                        selected=resolve_external(*reference)
                        if external_reference is not None and selected.path != client.path:
                            raise B1Error('FRESH_APP_PROCESS_BINDING_MISMATCH')
                        value=await verified_owner(selected)
                        client=selected;external_reference=reference
                    else:
                        if resolve_external is not None and external_reference is None:raise B1Error('BOOTSTRAP_REQUIRED')
                        value = (await verified_owner(client) if name == 'runtime_status'
                                 else await client.call(name, args))
                    result = tool_result(value)
                except B1Error as error:
                    result = tool_result({'code': error.code, **error.details}, error=True)
            else:
                raise B1Error('METHOD_UNSUPPORTED')
            reply = {'jsonrpc': '2.0', 'id': request_id, 'result': result}
        except B1Error as error:
            reply = {'jsonrpc': '2.0', 'id': request_id,
                     'error': {'code': -32602, 'message': error.code}}
        except (ValueError, TypeError, KeyError, AttributeError):
            reply = {'jsonrpc': '2.0', 'id': request_id,
                     'error': {'code': -32600, 'message': 'REQUEST_INVALID'}}
        output.write(canonical(reply) + '\n')
        output.flush()
