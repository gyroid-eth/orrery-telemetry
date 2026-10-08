#!/usr/bin/env python3
"""Review plans and scoped hook trust through Codex's public app-server API.

No model/thread/login requests are made. The caller marks a reviewed plan
approved only after its existing human approval; plugin enablement is not consent.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import selectors
import re
import signal
import subprocess
import time


class TrustUnavailable(RuntimeError):
    pass


def payload_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob('*')):
        if any(part in {'__pycache__', '.pytest_cache', '.DS_Store'} for part in path.parts):
            continue
        if path.is_symlink():
            raise TrustUnavailable('plugin payload contains a symlink')
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode() + b'\0')
            digest.update(str(path.stat().st_mode & 0o111).encode() + b'\0')
            digest.update(path.read_bytes())
    return digest.hexdigest()


def atomic_json(path: Path, value: dict) -> None:
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class AppServer:
    def __init__(self, binary: str, home: Path, cwd: Path, timeout: float = 30):
        self.deadline = time.monotonic() + timeout
        environment = os.environ.copy()
        environment['CODEX_HOME'] = str(home)
        self.process = subprocess.Popen(
            [binary, 'app-server'], cwd=cwd, env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.buffer = b''
        self.request_id = 0

    def __enter__(self):
        try:
            self.request('initialize', {'clientInfo': {'name': 'orrery_installer', 'version': '1'},
                                        'capabilities': {'experimentalApi': True}})
            self.send({'method': 'initialized'})
            return self
        except BaseException:
            self.close()
            raise

    def send(self, value):
        try:
            self.process.stdin.write((json.dumps(value) + '\n').encode())
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise TrustUnavailable('app-server transport closed') from exc

    def request(self, method, params):
        self.request_id += 1
        expected = self.request_id
        self.send({'id': expected, 'method': method, 'params': params})
        while time.monotonic() < self.deadline:
            while b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                try:
                    response = json.loads(line)
                except ValueError as exc:
                    raise TrustUnavailable('invalid app-server JSON') from exc
                if response.get('id') != expected:
                    continue
                if 'error' in response:
                    raise TrustUnavailable(f'{method} rejected')
                if not isinstance(response.get('result'), dict):
                    raise TrustUnavailable(f'{method} returned invalid result')
                return response['result']
            if self.selector.select(min(.2, max(0, self.deadline-time.monotonic()))):
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise TrustUnavailable('app-server exited before response')
                self.buffer += chunk
                if len(self.buffer) > 4 * 1024 * 1024:
                    raise TrustUnavailable('app-server response too large')
            elif self.process.poll() is not None:
                raise TrustUnavailable('app-server exited before response')
        raise TrustUnavailable('app-server timeout')

    def close(self):
        self.selector.close()
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=2)
        for stream in (self.process.stdin, self.process.stdout):
            stream.close()

    def __exit__(self, *args):
        self.close()


def scoped_hooks(server, cwd: Path, plugin_id: str, root: Path):
    if not re.fullmatch(r'agentstack-codex-app@[a-z0-9-]+', plugin_id):
        raise TrustUnavailable('plugin is not owned by ORRERY')
    manifest = json.loads((root/'.codex-plugin/plugin.json').read_text())
    if manifest.get('name') != 'agentstack-codex-app':
        raise TrustUnavailable('plugin manifest ownership mismatch')
    entries = server.request('hooks/list', {'cwds': [str(cwd)]}).get('data', [])
    matches = [entry for entry in entries if entry.get('cwd') == str(cwd)]
    if len(matches) != 1 or matches[0].get('errors'):
        raise TrustUnavailable('hook discovery is incomplete')
    hooks = [h for h in matches[0].get('hooks', []) if h.get('pluginId') == plugin_id]
    definition = json.loads((root/'hooks/hooks.json').read_text())['hooks']
    expected = {''.join(c for c in event.lower() if c.isalnum()): groups
                for event, groups in definition.items()}
    if len(hooks) != sum(len(g['hooks']) for groups in expected.values() for g in groups):
        raise TrustUnavailable('expected plugin hooks are missing or duplicated')
    seen = set()
    keys = set()
    for hook in hooks:
        event = ''.join(c for c in hook.get('eventName', '').lower() if c.isalnum())
        if event not in expected or event in seen:
            raise TrustUnavailable('unexpected plugin hook event')
        seen.add(event)
        key = hook.get('key')
        if key in keys or not isinstance(key, str):
            raise TrustUnavailable('hook key is duplicated or unavailable')
        keys.add(key)
        source = Path(hook.get('sourcePath', '')).resolve()
        if source != (root/'hooks/hooks.json').resolve() or hook.get('isManaged'):
            raise TrustUnavailable('hook ownership mismatch')
        if hook.get('source') != 'plugin' or not hook.get('key') or not hook.get('currentHash'):
            raise TrustUnavailable('hook identity unavailable')
        handler = hook.get('handler') or {'type': hook.get('handlerType'),
                                          'command': hook.get('command'),
                                          'async': hook.get('async', False)}
        configured = expected[event][0]['hooks'][0]
        allowed_commands = {configured['command'], configured['command'].replace('$PLUGIN_ROOT', str(root))}
        if (handler.get('command') not in allowed_commands or handler.get('type') != 'command'
                or handler.get('async', False) != configured.get('async', False)
                or not isinstance(hook.get('timeoutSec'), int) or hook['timeoutSec'] <= 0):
            raise TrustUnavailable('hook definition mismatch')
    return sorted(hooks, key=lambda h: h['key'])


def identity(hook):
    return {k: v for k, v in hook.items() if k not in {'enabled', 'trustStatus', 'displayOrder'}}


def capture_plan(binary: str, home: Path, cwd: Path, root: Path, plugin_id: str):
    with AppServer(binary, home, cwd) as server:
        hooks = scoped_hooks(server, cwd, plugin_id, root)
    return {'schema_version': 1, 'approved': False, 'enable_hooks': True,
            'codex_binary': binary, 'shared_codex_home': str(home), 'cwd': str(cwd),
            'plugin_id': plugin_id, 'plugin_root': str(root),
            'payload_digest': payload_digest(root),
            'declared_hooks': json.loads((root/'hooks/hooks.json').read_text()),
            'hooks': [identity(h) for h in hooks]}


def apply_trust(plan: dict):
    if plan.get('schema_version') != 1 or plan.get('approved') is not True or plan.get('enable_hooks') is not True:
        raise TrustUnavailable('reviewed hook approval is required')
    home, cwd, root = (Path(plan[k]).resolve() for k in ('shared_codex_home', 'cwd', 'plugin_root'))
    if (not home.is_absolute() or payload_digest(root) != plan['payload_digest']
            or json.loads((root/'hooks/hooks.json').read_text()) != plan.get('declared_hooks')):
        raise TrustUnavailable('approved plugin payload changed')
    with AppServer(plan['codex_binary'], home, cwd) as server:
        hooks = scoped_hooks(server, cwd, plan['plugin_id'], root)
        if [identity(h) for h in hooks] != plan.get('hooks'):
            raise TrustUnavailable('reviewed hook definitions changed')
        config = server.request('config/read', {'includeLayers': True, 'cwd': str(cwd)})
        users = [layer for layer in config.get('layers', []) if layer.get('name', {}).get('type') == 'user']
        if len(users) != 1 or not users[0].get('version'):
            raise TrustUnavailable('user config version unavailable')
        edits = {h['key']: {'trusted_hash': h['currentHash'], 'enabled': True} for h in hooks}
        if payload_digest(root) != plan['payload_digest']:
            raise TrustUnavailable('approved plugin payload changed')
        server.request('config/batchWrite', {'edits': [{'keyPath': 'hooks.state', 'value': edits, 'mergeStrategy': 'upsert'}],
                       'expectedVersion': users[0]['version'], 'reloadUserConfig': True})
        if payload_digest(root) != plan['payload_digest']:
            raise TrustUnavailable('approved plugin payload changed during trust write')
        verified = scoped_hooks(server, cwd, plan['plugin_id'], root)
        if [identity(h) for h in verified] != plan['hooks'] or any(
                h.get('trustStatus') != 'trusted' or h.get('enabled') is not True for h in verified):
            raise TrustUnavailable('hook trust was not observed after write')
    return {'schema_version': 1, 'hook_trust': 'observed_current', 'history': 'unobserved',
            'plugin_id': plan['plugin_id'], 'shared_codex_home': str(home), 'hook_count': len(hooks)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    plan = sub.add_parser('plan')
    for name in ('codex-binary', 'shared-codex-home', 'cwd', 'plugin-root', 'plugin-id', 'output'):
        plan.add_argument('--'+name, required=True)
    apply = sub.add_parser('apply')
    apply.add_argument('--approved-plan', required=True)
    apply.add_argument('--receipt', required=True)
    for name in ('shared-codex-home', 'codex-binary', 'plugin-id'):
        apply.add_argument('--'+name, required=True)
    args = parser.parse_args()
    try:
        if args.operation == 'plan':
            atomic_json(Path(args.output), capture_plan(args.codex_binary, Path(args.shared_codex_home).resolve(),
                        Path(args.cwd).resolve(), Path(args.plugin_root).resolve(), args.plugin_id))
            return 0
        reviewed = json.loads(Path(args.approved_plan).read_text())
        if (reviewed.get('shared_codex_home') != str(Path(args.shared_codex_home).resolve())
                or reviewed.get('codex_binary') != args.codex_binary
                or reviewed.get('plugin_id') != args.plugin_id):
            raise TrustUnavailable('approved plan target differs from the installer')
        receipt = apply_trust(reviewed)
    except (TrustUnavailable, OSError, ValueError, KeyError, TypeError) as exc:
        if args.operation == 'plan':
            print('Hook discovery unavailable; use /hooks to review ORRERY hooks.')
            return 1
        receipt = {'schema_version': 1, 'hook_trust': 'needs_review', 'history': 'unobserved',
                   'reason': str(exc) if isinstance(exc, TrustUnavailable) else 'invalid plan or unavailable payload'}
    atomic_json(Path(args.receipt), receipt)
    print(json.dumps(receipt, sort_keys=True))
    if receipt['hook_trust'] != 'observed_current':
        print('Open /hooks and review ORRERY hooks, then start a new Codex process.')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
