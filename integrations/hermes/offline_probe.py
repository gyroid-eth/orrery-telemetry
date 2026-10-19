"""Opt-in installed-Hermes spike; no provider or live Mail request.

Run with the installed interpreter using -I -B, an empty parent environment,
and a persistent private HOME/HERMES_HOME. Dependency/source paths are explicit.
All provider routes below use reserved .invalid hosts and socket calls are denied.
"""
import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import shlex
import site
import subprocess
import sys
import time
import uuid
from types import SimpleNamespace
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--site', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--fixture-mcp', action='store_true')
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    denied_network = []

    def audit(event, values):
        if event in ('socket.connect', 'socket.getaddrinfo'):
            denied_network.append(event)
            raise RuntimeError('offline probe forbids network')

    sys.addaudithook(audit)
    site.addsitedir(args.site)
    sys.path.insert(0, args.source)
    if args.fixture_mcp:
        from mcp.server import MCPServer
        server = MCPServer('offline-bound-shape')

        @server.tool()
        def send_message(to: list[str], subject: str, body_md: str) -> dict:
            # Deliberately no success receipt: this is not ORRERY Mail.
            return {'fixture_only': True, 'provider_key_present': 'DEEPSEEK_API_KEY' in os.environ}

        server.run(transport='stdio')
        return

    # A failed rerun must not leave the previous run's green summary in place.
    (output / 'results.json').write_text(json.dumps({'probe_completed': False, 'real_mail_sent': False}) + '\n')
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    aux_tasks = [name for name, value in DEFAULT_CONFIG['auxiliary'].items() if isinstance(value, dict)]
    route = {'provider': 'fixture', 'model': 'fixture-model', 'key_env': 'DEEPSEEK_API_KEY',
             'base_url': 'https://provider.invalid', 'api_mode': 'anthropic_messages', 'fallback_chain': []}
    config = {'model': {'provider': 'fixture', 'default': 'fixture-model'},
              'providers': {'fixture': {'api': 'https://provider.invalid', 'key_env': 'DEEPSEEK_API_KEY',
                                       'transport': 'anthropic_messages', 'discover_models': False}},
              'auth': {'adopt_external_logins': False}, 'fallback_providers': [],
              'updates': {'check': False}, 'model_catalog': {'enabled': False},
              'telemetry': {'shared_metrics': {'enabled': False, 'send': False}},
              'auxiliary': {name: dict(route) for name in aux_tasks}}
    # Memory, skills, curator and background review remain enabled through defaults.
    (Path(os.environ['HERMES_HOME']) / 'config.yaml').write_text(json.dumps(config, indent=2))
    os.environ['DEEPSEEK_API_KEY'] = 'offline-canary-' + os.urandom(12).hex()
    import model_tools
    from hermes_cli import plugins
    from agent import shell_hooks
    from tools.mcp_tool_config import _build_safe_env
    from tools.environments.local import build_subprocess_env
    from hermes_cli import runtime_provider
    from agent import auxiliary_client as aux
    from hermes_cli.oneshot import _oneshot_exit_code

    spec = importlib.util.spec_from_file_location('spike_guard', Path(__file__).with_name('reservation_guard.py'))
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    manager = plugins.get_plugin_manager()
    plugins.discover_plugins()
    # Isolated test manager only: no hook allowlist or production plugin is approved.
    hooks_before = len(manager.iter_hook_callbacks('pre_tool_call'))
    assert not sys.stdin.isatty(), 'offline probe requires non-interactive stdin'
    skipped = shell_hooks.register_from_config({'hooks': {'pre_tool_call': [
        {'command': '/nonexistent/guard', 'fail_closed': True}]}})
    assert skipped == [] and len(manager.iter_hook_callbacks('pre_tool_call')) == hooks_before
    try:
        guard.require_guard(manager, lambda **kw: None)
    except RuntimeError:
        startup_denied = True
    else:
        raise AssertionError('missing guard did not stop startup')

    work = output / 'work'
    work.mkdir(exist_ok=True)
    (work / 'allowed').mkdir(exist_ok=True)
    outside = output / 'outside.txt'
    outside.write_text('unchanged')
    clock = [2000000000.0]
    attempt = {'attempt_id': str(uuid.uuid4()), 'phase': 'spike', 'agent_name': 'FixtureChild',
               'started_at_ns': time.time_ns()}
    task = {'attempt_id': attempt['attempt_id'], 'task_id': 'offline-task', 'worktree': str(work),
            'allowed_paths': ['allowed/**'], 'reservation_source': 'offline-fixture',
            'expected': [{'path': 'allowed/ok.txt', 'contains': 'offline guard probe'}],
            'report_to': 'FixtureParent',
            'reservation_receipts': [{'id': 1, 'path_pattern': 'allowed/**', 'owner': 'FixtureChild',
                                      'expires_ts': '2033-05-18T03:34:20Z'}]}
    (output / 'attempt.json').write_text(json.dumps(attempt, indent=2))
    (output / 'task.json').write_text(json.dumps(task, indent=2))
    events = []
    callback = guard.create_guard(attempt, task, events.append, now=lambda: clock[0])
    manager._hooks.setdefault('pre_tool_call', []).append(callback)
    guard.require_guard(manager, callback)
    # Hermes intentionally refuses overwriting a file this task has never read.
    # Repeated probes use the real read path, preserving that stale-write guard.
    if (work / 'allowed/ok.txt').exists():
        read_result = json.loads(model_tools.handle_function_call('read_file',
            {'path': str(work / 'allowed/ok.txt')}, task_id='offline-task'))
        assert 'error' not in read_result, 'fixture could not read the existing artifact'
    results = {}
    content = 'offline guard probe ' + attempt['attempt_id']
    for name, target in [('allowed', work / 'allowed/ok.txt'), ('outside', outside),
                         ('unreserved', work / 'unreserved.txt'), ('expired', work / 'allowed/expired.txt')]:
        if name == 'expired':
            clock[0] += 120
        results[name] = json.loads(model_tools.handle_function_call('write_file',
            {'path': str(target), 'content': content}, task_id='offline-task'))
    assert (work / 'allowed/ok.txt').read_text() == content, results
    assert outside.read_text() == 'unchanged'
    assert not (work / 'unreserved.txt').exists() and not (work / 'allowed/expired.txt').exists()
    assert [e.get('reason') for e in events[-4:]] == [None, 'outside-worktree', 'not-reserved', 'reservation-expired'], events
    manager._hooks['pre_tool_call'].remove(callback)
    def crash(**kw):
        raise RuntimeError('fixture guard crash')
    manager._hooks['pre_tool_call'].append(crash)
    crash_result = json.loads(model_tools.handle_function_call('write_file',
        {'path': str(work / 'allowed/crash.txt'), 'content': 'must not write'}, task_id='offline-task'))
    assert not (work / 'allowed/crash.txt').exists() and 'error' in crash_result
    manager._hooks['pre_tool_call'].remove(crash)
    (output / 'events.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in events))
    (output / 'guard-results.json').write_text(json.dumps(results, indent=2))

    child_script = output / 'env-child.py'
    child_script.write_text('import os,json; print(json.dumps({"key_present":"DEEPSEEK_API_KEY" in os.environ}))')
    safe_env = _build_safe_env({})
    mcp_env_result = subprocess.run([sys.executable, '-I', '-B', str(child_script)], env=safe_env,
                                    capture_output=True, text=True, check=True)
    tool_env_result = subprocess.run([sys.executable, '-I', '-B', str(child_script)], env=build_subprocess_env(),
                                     capture_output=True, text=True, check=True)
    shell_spec = shell_hooks.ShellHookSpec(event='pre_tool_call',
        command=shlex.join([sys.executable, '-I', '-B', str(child_script)]), fail_closed=True)
    hook_result = shell_hooks.run_once(shell_spec, {'tool_name': 'write_file', 'args': {}})
    env_results = {'mcp_child': json.loads(mcp_env_result.stdout),
                   'tool_child': json.loads(tool_env_result.stdout),
                   'shell_hook_child': json.loads(hook_result['stdout'])}
    assert env_results == {'mcp_child': {'key_present': False}, 'tool_child': {'key_present': False},
                           'shell_hook_child': {'key_present': True}}, env_results

    runtime = runtime_provider.resolve_runtime_provider(requested='fixture', target_model='fixture-model')
    assert runtime['api_mode'] == 'anthropic_messages' and runtime['base_url'].startswith('https://provider.invalid')
    route_results = {}
    for task_name in ('title_generation', 'compression', 'curator', 'background_review', 'vision'):
        resolved = aux._resolve_task_provider_model(task_name)
        assert resolved[0] == 'custom' and resolved[2] == 'https://provider.invalid' and resolved[4] == 'anthropic_messages'
        # Projection only. SDK client construction can import hermes_bootstrap and
        # prepare dependencies for a borrowing HERMES_HOME; it is outside this probe.
        route_results[task_name] = {'provider': resolved[0], 'model': resolved[1], 'api_mode': resolved[4],
                                   'base_url': resolved[2], 'client_constructed': False}
    discovery = []
    with patch.object(aux, '_get_provider_chain', side_effect=lambda: discovery.append('discovery') or []):
        assert aux._try_payment_fallback('fixture', task='compression', main_runtime=runtime) == (None, None, '')
        with patch.object(aux, '_try_main_provider_route', return_value=None):
            assert aux._resolve_auto_route(runtime, task='compression') == (None, None, '')
    assert not discovery
    (output / 'env-results.json').write_text(json.dumps(env_results, indent=2) + '\n')
    (output / 'route-results.json').write_text(json.dumps(route_results, indent=2) + '\n')

    async def mcp_probe():
        from tools.mcp_tool import MCPServerTask
        server = MCPServerTask('offline-bound-shape')
        config = {'command': sys.executable, 'args': ['-I', '-B', str(Path(__file__).resolve()),
                  '--source', args.source, '--site', args.site, '--output', str(output), '--fixture-mcp'],
                  'env': {'HERMES_HOME': os.environ['HERMES_HOME']}, 'protocol': 'legacy'}
        try:
            await asyncio.wait_for(server.start(config), timeout=15)
            listing = await server.session.list_tools()
            schema = listing.tools[0].input_schema
            assert set(schema['properties']) == {'to', 'subject', 'body_md'}
            response = await server.session.call_tool('send_message', {'to': ['FixtureParent'],
                'subject': 'offline fixture', 'body_md': 'not real Mail'})
            payload = json.loads(response.content[0].text)
            assert payload == {'fixture_only': True, 'provider_key_present': False}, payload
            return {'stdio_handshake': True, 'schema_fields': sorted(schema['properties']), **payload}
        finally:
            await server.shutdown()
    mcp_result = asyncio.run(mcp_probe())
    rc_cases = {'completed': _oneshot_exit_code('done', {'completed': True}),
                'partial': _oneshot_exit_code('partial', {'partial': True}),
                'failed': _oneshot_exit_code('failed', {'failed': True}),
                'interrupted': _oneshot_exit_code('interrupted', {'interrupted': True}),
                'empty': _oneshot_exit_code('', {'completed': True})}
    assert rc_cases == {'completed': 0, 'partial': 2, 'failed': 2, 'interrupted': 130, 'empty': 1}
    summary = {'probe_completed': True, 'attempt_id': attempt['attempt_id'],
               'real_builtin_write': True, 'three_denials': True, 'missing_guard_startup_denied': startup_denied,
               'crashing_guard_denied': True, 'unapproved_hook_skipped': True,
               'environment': env_results, 'mcp': mcp_result, 'auxiliary_routes': route_results,
               'cross_provider_discovery_calls': discovery, 'oneshot_exit_codes': rc_cases,
               'network_calls_denied': denied_network, 'real_mail_sent': False}
    (output / 'results.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
