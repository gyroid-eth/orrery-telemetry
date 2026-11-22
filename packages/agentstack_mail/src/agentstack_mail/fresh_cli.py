"""Explicit fresh Mail preparation/serve; init defaults to isolated mode."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .fresh_runtime import FreshRuntime, mail_port_allowed, read_config
from .storage_origin_contract import B1Error


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init')
    init.add_argument('--root', type=Path, required=True)
    init.add_argument('--mode', choices=('isolated','fresh'), default='isolated')
    for name in ('health', 'register', 'serve'):
        command = commands.add_parser(name)
        command.add_argument('--config', type=Path, required=True)
        if name == 'register':
            for field in ('request-id', 'name', 'program', 'model'):
                command.add_argument('--' + field, required=True)
        if name == 'serve':
            command.add_argument('--port', type=int, required=True)
    return result


async def execute(args):
    runtime = (await FreshRuntime.initialize(args.root, mode=args.mode) if args.command == 'init'
               else await FreshRuntime.open(args.config))
    try:
        if args.command == 'init':
            return {'status': 'initialized', 'config_path': str(runtime.root / 'config.json'),
                    'mail_instance_id': runtime.config['instance_id'], 'mode': runtime.config['mode']}
        if args.command == 'register':
            return await runtime.register_local(request_id=args.request_id, name=args.name,
                                                program=args.program, model=args.model)
        return await runtime.core.health()
    finally:
        await runtime.close()


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == 'serve':
            # Validation is read-only; the factory opens the same explicit config.
            asyncio.run(execute(args))
            if not mail_port_allowed(args.port, read_config(args.config)[1]['mode']):
                raise B1Error('FRESH_PORT_FORBIDDEN')
            from .fresh_server import build_fresh_server
            server = build_fresh_server(args.config)
            server.run(transport='streamable-http', host='127.0.0.1', port=args.port,
                       path='/mcp', stateless_http=True, json_response=True)
        else:
            print(json.dumps(asyncio.run(execute(args)), ensure_ascii=False))
    except B1Error as error:
        raise SystemExit(error.code) from None


if __name__ == '__main__':
    main()
