"""Separate opt-in phase-two entrypoint; the original adapter stays read-only."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from . import server
from .fresh_messages import FreshMessages, NOTE, TOOL_DEFINITIONS
from .model import DashboardError, ToolError, snapshot


class FreshGateway(server.Gateway):
    read_only = False

    def __init__(self, config: server.Config, policy_path: Path, dashboard=None):
        if config.mode != 'fresh' or config.project_key is not None:
            raise server.ConfigurationError('fresh-only message authority required')
        super().__init__(config, dashboard)
        try:
            self.messages = FreshMessages(policy_path, config.state_dir)
            self._check_dashboard()
        except Exception:
            raise server.ConfigurationError('fixed fresh message policy, client or Dashboard could not be verified') from None

    def _check_dashboard(self) -> None:
        try:
            view = snapshot(self.dashboard, 'fresh')
            if view.instance != self.messages.mail.instance or view.capabilities.get('mail') is not True:
                raise ValueError('mismatched Mail authority')
        except Exception:
            raise ToolError('message_dashboard_mismatch', 'Dashboard and prepared sender must name the same fresh Mail instance') from None

    def rpc(self, request):
        code, response = super().rpc(request)
        if code != 200 or response is None or 'result' not in response:
            return code, response
        method = request['method']
        if method == 'initialize':
            response['result']['instructions'] = 'Fresh sends are explicitly enabled. ' + NOTE
        elif method == 'tools/list':
            response['result']['tools'] = [*response['result']['tools'], *TOOL_DEFINITIONS]
        elif method == 'tools/call':
            params = request['params']
            name = params['name']
            if name in {definition['name'] for definition in TOOL_DEFINITIONS}:
                try:
                    if name != 'orrery_message_status':
                        self._check_dashboard()
                    body = self.messages.invoke(name, params.get('arguments', {}))
                    is_error = False
                except ToolError as exc:
                    body, is_error = {'error': {'code': exc.code, 'message': str(exc)}}, True
                except Exception:
                    body, is_error = {'error': {'code': 'message_unavailable',
                        'message': 'message state unavailable; do not retry with a different key'}}, True
                response['result'] = {'content': [{'type': 'text', 'text': json.dumps(body, ensure_ascii=False)}],
                                      'structuredContent': body, 'isError': is_error}
            elif name == 'orrery_status' and response['result'].get('isError') is False:
                body = response['result']['structuredContent']
                if body['dashboard']['mail_instance_id'] != self.messages.mail.instance:
                    body = {'error': {'code': 'message_dashboard_mismatch', 'message': 'fresh Mail instance changed'}}
                    response['result']['isError'] = True
                else:
                    body['adapter'].update(read_only=False, message_enabled_on_server=True)
                    body['message_authority'] = self.messages.public_policy()
                response['result']['structuredContent'] = body
                response['result']['content'] = [{'type': 'text', 'text': json.dumps(body, ensure_ascii=False)}]
        return code, response


def enabled_config(env: dict[str, str]) -> tuple[server.Config, Path] | None:
    flag = env.get('ORRERY_MCP_ENABLE_MESSAGE', '0').lower()
    if flag in ('0', 'false'):
        return None
    if flag not in ('1', 'true'):
        raise server.ConfigurationError('invalid message enable flag')
    if env.get('ORRERY_MCP_MODE') != 'fresh':
        raise server.ConfigurationError('message opt-in requires fresh mode')
    if not env.get('ORRERY_MCP_STATE_DIR'):
        raise server.ConfigurationError('message opt-in requires an explicit dedicated state directory')
    policy = env.get('ORRERY_MCP_FRESH_MESSAGE_POLICY')
    if not policy or not Path(policy).is_absolute():
        raise server.ConfigurationError('an explicit absolute fresh message policy is required')
    config = server.Config.from_env({**env, 'ORRERY_MCP_ENABLE_MESSAGE': '0'})
    return config, Path(policy)


def main(argv: list[str] | None = None) -> int:
    try:
        selected = enabled_config(dict(os.environ))
        if selected is None:
            return server.main(argv)
        cli = argparse.ArgumentParser(description='Opt-in fresh Mail instruction adapter')
        cli.add_argument('cmd', choices=('check', 'serve'))
        args = cli.parse_args(argv)
        config, policy = selected
        server.read_secret(config)
        gateway = FreshGateway(config, policy)
        if args.cmd == 'check':
            _, response = gateway.rpc({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                       'params': {'name': 'orrery_status', 'arguments': {}}})
            result = response['result']
            print(json.dumps(result['structuredContent'], ensure_ascii=False))
            return 2 if result['isError'] else 0
        with server.Server(('127.0.0.1', config.port), server.handler(gateway)) as service:
            print(f'fresh message MCP listening on 127.0.0.1:{config.port}; delegated send authority', flush=True)
            try:
                service.serve_forever(poll_interval=0.3)
            except KeyboardInterrupt:
                pass
        return 0
    except (server.ConfigurationError, DashboardError, OSError, ValueError, ImportError):
        print('orrery-mcp-fresh: startup refused; check private configuration and fresh client dependency', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
