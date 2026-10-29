#!/usr/bin/env python3
"""Non-secret config helpers for the standard native harness launcher."""
from __future__ import annotations

import argparse
import ast
import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
import runpy
import subprocess
import sys
import time
import types

if __package__:
    from . import child_resume, child_tools, native_connections, native_server_models
else:
    # Isolated Python excludes the script directory from sys.path. Load only
    # product siblings through a private package, independent of cwd/PYTHONPATH.
    package = types.ModuleType('_orrery_thin_cli_hooks')
    package.__path__ = [str(Path(__file__).resolve().parent)]
    sys.modules[package.__name__] = package
    child_resume = importlib.import_module(package.__name__ + '.child_resume')
    child_tools = importlib.import_module(package.__name__ + '.child_tools')
    native_connections = importlib.import_module(package.__name__ + '.native_connections')
    native_server_models = importlib.import_module(package.__name__ + '.native_server_models')

ROOT = Path(__file__).resolve().parents[1]
HARNESSES = {'dsh', 'hermes'}
_VALIDATION_CACHE = {}
VALIDATION_CACHE_SECONDS = 15
VALIDATION_CACHE_ENTRIES = 128


def read(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    child_resume._atomic_json(Path(path), value)


def config_generator(root, harness):
    file = Path(root) / 'integrations' / harness / 'thin_config.py'
    try:
        tree = ast.parse(file.read_text())
        declared = any(isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == 'THIN_CONFIG_CONTRACT'
            for target in node.targets) and isinstance(node.value, ast.Constant)
            and type(node.value.value) is int and node.value.value == 1 for node in tree.body)
    except (OSError, SyntaxError):
        declared = False
    if not declared:
        raise ValueError('thin_config_unavailable')
    return file


def static_check(receipt, config, *, harness):
    """Shared catalog/launcher check; no provider imports, key reads or RPC."""
    try:
        if (receipt.get('variant') not in (('plain', 'plain-datadir') if harness == 'dsh' else ('plain',)) or type(receipt.get('thin_config_contract')) is not int or receipt['thin_config_contract'] != 1
                or receipt.get('harness') != harness or receipt.get('complete') is not True):
            return dict(available=False, reason='plain_generation_required')
        config_generator(ROOT, harness)
        details = receipt['details']
        required = ('node','runtime') if harness == 'dsh' else ('hermes_executable','hermes_source','managed_root')
        for key in required:
            value = config[key]
            if value != details[key] or not Path(value).is_absolute() or not Path(value).exists():
                return dict(available=False, reason='runtime_not_prepared')
        executable = config['node' if harness == 'dsh' else 'hermes_executable']
        if not os.access(executable, os.X_OK):
            return dict(available=False, reason='runtime_not_prepared')
        if harness == 'dsh':
            if config.get('patch_receipt'):
                return dict(available=False, reason='plain_generation_required')
            pins = read(ROOT / 'integrations/dsh/runtime-pins.json')
            for pin in pins:
                metadata = Path(config['runtime']) / 'node_modules' / pin['name'] / 'package.json'
                if read(metadata).get('version') != pin['version']:
                    return dict(available=False, reason='runtime_version_mismatch')
        else:
            helper = ROOT / 'integrations/hermes/ca_recipe.py'
            spec = importlib.util.spec_from_file_location('_static_hermes_ca', helper)
            ca = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(ca)
            ca.validate_static(ROOT, details)
        return dict(available=True, reason=None)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return dict(available=False, reason='runtime_not_prepared')


def selection(manifest, harness, backend, model, effort=None, url_index=None, server_url=None, url_key=None):
    if harness not in HARNESSES or not model:
        raise ValueError('invalid_harness_selection')
    config_generator(ROOT, harness)
    if not manifest:
        raise ValueError('harness_not_enabled')
    data = native_connections.read_manifest(manifest)
    if data.get('enabled') is not True:
        raise ValueError('harness_not_enabled')
    kind, backend_spec = native_connections.resolve_connection(data, backend)
    route = {}
    if native_server_models.dynamic(backend_spec):
        route = native_server_models.selection(backend_spec, dict(model=model, url_index=url_index,
            **({'server_url': server_url} if server_url is not None else {}),
            **({'url_key': url_key} if url_key is not None else {})))
    else:
        if url_index is not None or server_url is not None or url_key is not None:
            raise ValueError('unexpected_url_selection')
        models = backend_spec.get('models', [backend_spec.get('model')])
        if not isinstance(models,list) or not models or not all(isinstance(v,str) and v for v in models):
            raise ValueError('invalid_model_catalog')
        if model not in models:
            raise ValueError('model_mismatch')
    runner = data['harnesses'][harness]
    if not Path(runner['python']).is_absolute() or not Path(runner['config_file']).is_absolute():
        raise ValueError('runtime_not_prepared')
    template = read(runner['config_file'])
    check = static_check(read(template['generation_receipt']), template, harness=harness)
    if not check['available']:
        raise ValueError(check['reason'])
    document = dict(manifest_file=str(Path(manifest).resolve()), product_root=str(ROOT),
                    backend_id=backend, model=model, **route)
    if effort is not None:
        document['effort'] = effort
    code = ('import importlib.util,json,sys,re; '
            's=importlib.util.spec_from_file_location("orrery_thin_validation",sys.argv[1]); '
            'm=importlib.util.module_from_spec(s); s.loader.exec_module(m); '
            '\ntry:m.validate_input(json.loads(sys.argv[2]))'
            '\nexcept ValueError as e: print(str(e) if re.fullmatch(r"[a-z_]+",str(e)) else "invalid_runtime");sys.exit(2)')
    generator = config_generator(ROOT,harness)
    files = [manifest,runner['config_file'],template['generation_receipt'],generator,
             template.get('backend_file'),template.get('profiles',{}).get(backend,{}).get('template'),
             ROOT / 'integrations' / harness / ('orrery_tools.mjs' if harness=='dsh' else 'thin_guard.py')]
    fingerprints = []
    for file in files:
        if not file:
            continue
        try:
            info=Path(file).stat()
            fingerprints.append((str(file),info.st_mtime_ns,info.st_size))
        except OSError:
            fingerprints.append((str(file),None,None))
    signature = tuple(fingerprints)
    key = (str(manifest),harness,backend,model,effort,url_index,server_url)
    cached = _VALIDATION_CACHE.get(key)
    if cached and cached[0] == signature and time.monotonic()-cached[1] < VALIDATION_CACHE_SECONDS:
        reason = cached[2]
    else:
        try:
            result = subprocess.run([runner['python'], '-I', '-B', '-c', code,
                str(generator), json.dumps(document)], capture_output=True, text=True, timeout=5)
            reason = None
            if result.returncode:
                text=result.stdout.strip()
                reason=text if re.fullmatch(r'[a-z_]+',text) else 'runtime_not_prepared'
        except subprocess.TimeoutExpired:
            reason = 'validate_timeout'
        _VALIDATION_CACHE[key] = (signature,time.monotonic(),reason)
        while len(_VALIDATION_CACHE) > VALIDATION_CACHE_ENTRIES:
            _VALIDATION_CACHE.pop(next(iter(_VALIDATION_CACHE)))
    if reason:
        raise ValueError(reason)
    return {**runner, 'connection_kind': kind, **route}


def write_instructions(state_dir, project_key):
    spec = importlib.util.spec_from_file_location('harness_instruction_blocks', ROOT / 'bin/lib/instruction_blocks.py')
    renderer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(renderer)
    instructions = state_dir / 'instructions.md'
    child_resume._atomic_bytes(instructions, renderer.render(
        'harness', (ROOT / 'codex/AGENTS.md').read_bytes(), str(ROOT), project_key))
    return instructions


def fresh_instructions(state_dir, root):
    """Copy bounded common fresh sections into this generated, owned file.

    The legacy neutral renderer requires legacy include headings, so it cannot
    compose this different contract. No user instruction block is replaced.
    """
    shared = (Path(root) / 'templates/fresh/codex/AGENTS.md').read_bytes()
    boundaries = (b'For an automatic Claude/Codex child,', b'Never read `storage.sqlite3`', b'Codex hooks require prior human review;')
    if any(shared.count(value) != 1 for value in boundaries):raise ValueError('fresh_instructions_changed')
    body = shared[:shared.index(boundaries[0])] + shared[shared.index(boundaries[1]):shared.index(boundaries[2])]
    body += b'Native managed launch uses this prepared owner and private HOME. Registration and file guards remain mandatory.\n'
    body += b'Completion of a native process is not completion of its task; report the result to the specified parent through Mail.\n'
    target = Path(state_dir) / 'instructions.md'
    child_resume._atomic_bytes(target, body.replace(b'__AGENTSTACK_HOME__', str(root).encode()))
    return target


def generate_plan(document, runner):
    """One generator path for initial launch and exact-ID resume."""
    state_dir = Path(document['state_dir'])
    document['mcp_selection'] = child_tools.native_plan(document['tools'], cwd=document['cwd'],
        claude_json=os.environ.get('AGENTSTACK_CLAUDE_JSON',str(Path.home()/'.claude.json')))
    merged_mcp = read(document['mcp_config'])
    merged_mcp['mcpServers'].update(document['mcp_selection']['servers'])
    selected_mcp = state_dir / 'selected-mcp.json'
    save(selected_mcp, merged_mcp)
    document['mcp_config'] = str(selected_mcp)
    source, output = state_dir / 'input.json', state_dir / 'launch.json'
    save(source, document)
    result = subprocess.run([runner['python'], '-B', str(config_generator(ROOT,document['program'])),
        '--input', str(source), '--out', str(output)], capture_output=True, text=True)
    if result.returncode:
        reason = re.search(r'^(?:DSH|HERMES)_THIN_CONFIG_ERROR=([a-z_]+)$', result.stderr.strip())
        raise ValueError(reason[1] if reason else 'thin_config_generation_failed')
    plan = read(output)
    validate_plan(plan, document['cwd'])
    profile = {key: str(Path(plan['env'][key]).resolve()) for key in ('DSH_HOME', 'HERMES_HOME')
               if key in plan.get('env', {})}
    expected_profile = {key: str(Path(value).resolve())
                        for key, value in document.get('native_profile_env', profile).items()}
    if expected_profile != profile:
        raise ValueError('native_resume_profile_changed')
    document['native_profile_env'] = profile
    save(source, document)
    return state_dir


def prepare_resume(runtime_dir, name, project_key=None):
    if not isinstance(name, str) or not child_resume.SAFE_NAME.fullmatch(name):
        raise ValueError('invalid_agent_name')
    runtime = Path(runtime_dir).resolve()
    state = child_resume._load_state(runtime / 'child-agents' / (name + '.json'))
    child_resume._validate_identity(state, agent_name=name, project_key=project_key)
    if not state.get('resume_in_progress_at'):
        raise ValueError('native_resume_not_prepared')
    document = child_resume.native_resume_input(runtime, state)
    runner = selection(document['manifest_file'], document['program'], document['backend_id'],
                       document['model'], document.get('effort'),
                       document.get('url_index'), document.get('server_url'), document.get('url_key'))
    if runner.get('connection_kind', document['backend_kind']) != document['backend_kind']:
        raise ValueError('connection_kind_changed')
    state_dir = Path(document['state_dir'])
    previous = read(state_dir / 'launch.json')
    document.setdefault('native_profile_env', {key: previous.get('env', {}).get(key)
        for key in ('DSH_HOME', 'HERMES_HOME') if key in previous.get('env', {})})
    if document['program'] == 'hermes':
        # Use the generator's HOME/pin policy before rewriting the saved prompt,
        # Mail configuration, instructions or input for this attempt.
        generator = runpy.run_path(str(config_generator(ROOT, 'hermes')))
        try:
            generator['child_home'](document)
        except ValueError as error:
            reason = str(error)
            if reason in {'native_resume_profile_changed', 'native_resume_profile_missing',
                          'native_resume_legacy_profile', 'native_resume_home_missing',
                          'native_resume_history_missing'}:
                raise child_resume.ResumeStateError(reason, 'Native resume HOME prerequisites are unavailable') from None
            raise
    prompt = state_dir / 'resume-prompt.txt'
    child_resume._atomic_bytes(prompt, b'')
    parent = state.get('parent_agent')
    if parent and not child_resume.PROXY_AGENT_NAME.fullmatch(parent):
        raise ValueError('invalid_parent_identity')
    proxy_env = dict(AGENTSTACK_PROXY_AGENT_NAME=name,
        AGENTSTACK_PROXY_TOKEN_FILE=str(runtime / ('agent_token_' + name)),
        AGENTSTACK_PROXY_PROGRAM=state['program'], AGENTSTACK_PROJECT_KEY=state['project_key'],
        AGENTSTACK_RUNTIME_DIR=str(runtime),
        AGENTSTACK_MCP_URL=os.environ.get('AGENTSTACK_MCP_URL', 'http://127.0.0.1:18765/mcp'),
        AGENTSTACK_MAIL_ENV=os.environ.get('AGENTSTACK_MAIL_ENV', str(Path.home()/'.agentstack/mail/.env')),
        AGENTSTACK_MAIL_HTTP_BEARER_MODE=os.environ.get('AGENTSTACK_MAIL_HTTP_BEARER_MODE','auto'),
        AGENTSTACK_PYTHON=os.environ.get('AGENTSTACK_PYTHON') or sys.executable)
    if parent:
        proxy_env['AGENTSTACK_PROXY_PARENT_AGENT'] = parent
    proxy = dict(command=os.environ.get('AGENTSTACK_MCP_PROXY') or str(
        ROOT / 'integrations/codex_app/plugin/scripts/run-mcp.sh'), args=[], env=proxy_env)
    mcp = state_dir / 'resume-mail.json'
    save(mcp, child_tools.claude_config(proxy, claude_json=os.environ.get(
        'AGENTSTACK_CLAUDE_JSON', str(Path.home()/'.claude.json'))))
    document['env'].update(AGENT_NAME=name, AGENTSTACK_HOME=str(ROOT),
        AGENTSTACK_RUNTIME_DIR=str(runtime), AGENTSTACK_PROJECT_KEY=state['project_key'])
    document.update(product_root=str(ROOT), parent_agent=parent, mcp_config=str(mcp),
                    prompt_file=str(prompt), instructions_file=str(write_instructions(state_dir,state['project_key'])),
                    resume_attempt_at=state['resume_in_progress_at'])
    generate_plan(document, runner)
    (state_dir / 'native-exit.status').unlink(missing_ok=True)
    return state_dir


def resume_command(runtime_dir, name, project_key=None):
    if not isinstance(name, str) or not child_resume.SAFE_NAME.fullmatch(name):
        raise ValueError('invalid_agent_name')
    runtime = Path(runtime_dir).resolve()
    state = child_resume._load_state(runtime / 'child-agents' / (name + '.json'))
    child_resume._validate_identity(state, agent_name=name, project_key=project_key)
    document = child_resume.native_resume_input(runtime, state)
    source = read(Path(document['state_dir']) / 'input.json')
    if (not state.get('resume_in_progress_at')
            or source.get('resume_attempt_at') != state['resume_in_progress_at']
            or source.get('resume_session_id') != document['resume_session_id']):
        raise ValueError('native_resume_not_prepared')
    invocation = command(document['state_dir'], name)
    child_resume.prepare_active_state(runtime, name, project_key=state['project_key'],
                                     program=state['program'])
    return invocation


def prepare(args):
    if not re.fullmatch(r'[0-9a-f]{32}', args.generation):
        raise ValueError('invalid_registration_generation')
    runtime = Path(args.runtime_dir).resolve()
    child_file = runtime / 'child-agents' / (args.agent_name + '.json')
    state = child_resume._load_state(child_file)
    child_resume._validate_identity(state, agent_name=args.agent_name,
                                   project_key=args.project_key, program=args.harness)
    if not child_resume._registration_matches(
            child_file.with_name('.' + args.agent_name + '.registration-pending.json'), args.generation):
        raise ValueError('registration_attempt_changed')
    selection_args = (args.manifest, args.harness, args.backend, args.model, args.effort)
    if getattr(args, 'url_index', None) is not None:
        if not getattr(args, 'url_key', None):
            raise ValueError('url_selection_required')
        selection_args += (args.url_index, None, args.url_key)
    runner = selection(*selection_args)
    state_dir = child_file.parent / (args.agent_name + '.harness') / args.generation
    state_dir.mkdir(parents=True, mode=0o700)
    os.chmod(state_dir, 0o700)
    prompt = state_dir / 'initial-prompt.txt'
    child_resume._atomic_bytes(prompt, Path(args.prompt_file).read_bytes())
    instructions = write_instructions(state_dir, args.project_key)
    kind = runner.get('connection_kind') or native_connections.connection_kind(args.backend)
    document = dict(agent_name=args.agent_name, program=args.harness, backend_id=args.backend,
        backend_kind=kind,
        model=args.model, project_key=args.project_key, cwd=str(Path(args.cwd).resolve()),
        parent_agent=args.parent_agent or None, product_root=str(ROOT), runtime_dir=str(runtime),
        state_dir=str(state_dir), mcp_config=str(Path(args.mcp_config).resolve()),
        prompt_file=str(prompt), instructions_file=str(instructions),
        manifest_file=str(Path(args.manifest).resolve()), tools=json.loads(args.tools),
        env=dict(AGENT_NAME=args.agent_name, AGENTSTACK_HOME=str(ROOT),
                 AGENTSTACK_RUNTIME_DIR=str(runtime), AGENTSTACK_PROJECT_KEY=args.project_key))
    document.update({key: runner[key] for key in ('url_index', 'server_url', 'url_key') if key in runner})
    if getattr(args,'helper_model',None) not in (None,'','same'):
        document['helper_model'] = args.helper_model
    if args.effort is not None:
        document['effort'] = args.effort
    generate_plan(document, runner)
    state.update(harness=args.harness, harness_backend=args.backend,
                 harness_backend_kind=kind, harness_state_dir=str(state_dir))
    save(child_file, state)
    return state_dir


def validate_plan(plan, cwd):
    argv, env = plan.get('argv'), plan.get('env', {})
    if (not isinstance(argv, list) or not argv or
            not all(isinstance(arg, str) and '\0' not in arg for arg in argv) or
            not Path(argv[0]).is_absolute() or plan.get('cwd') != cwd):
        raise ValueError('invalid_native_launch_plan')
    if not isinstance(env, dict) or not all(isinstance(k,str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',k)
            and isinstance(v,str) and '\0' not in v for k,v in env.items()):
        raise ValueError('invalid_native_environment')
    # Config generators return references only. Credentials are acquired by the CLI.
    if any(k.endswith(('_API_KEY','_REGISTRATION_TOKEN')) or k in ('CHILD_REGISTRATION_TOKEN','MCP_AGENT_MAIL_TOKEN') for k in env):
        raise ValueError('credential_value_in_launch_plan')


def dynamic_prelaunch(source):
    """One selected-URL lookup, shared by legacy and protected fresh launch."""
    if 'url_index' in source:
        manifest = native_connections.read_manifest(source['manifest_file'])
        _, spec = native_connections.resolve_connection(manifest, source['backend_id'])
        if not native_server_models.dynamic(spec):
            raise ValueError('server_url_changed')
        route = native_server_models.selection(spec, source)
        runner = selection(source['manifest_file'], source['program'], source['backend_id'],
            source['model'], source.get('effort'), route['url_index'], route['server_url'], route['url_key'])
        generator = runpy.run_path(str(config_generator(ROOT, source['program'])))
        facts = generator['validate_input'](source)
        key = native_server_models.read_key(facts, source['program'])
        row = native_server_models.require_model(route['server_url'], source['model'], key)
        source['server_context'] = row.get('meta', {})
        return runner
    return None


def command(state_dir, name):
    import shlex
    source = read(Path(state_dir) / 'input.json')
    if source['agent_name'] != name:
        raise ValueError('native_identity_mismatch')
    runner = dynamic_prelaunch(source)
    if runner is not None:generate_plan(source, runner)
    plan = read(Path(state_dir) / 'launch.json')
    validate_plan(plan, source['cwd'])
    invocation = shlex.join(['env', *[k+'='+v for k,v in plan.get('env', {}).items()], *plan['argv']])
    cleanup = shlex.join(['/bin/bash', str(ROOT / 'hooks/cleanup-child-agent.sh'), name])
    # Same lifetime and exit cleanup as native Codex; no ORRERY runtime wrapper.
    # Record native termination before slow/unreachable Mail cleanup can keep
    # its pane shell alive. This is not a task or turn completion receipt.
    marker = shlex.quote(str(Path(state_dir) / 'native-exit.status'))
    return (invocation + '; native_rc=$?; marker_rc=0; printf "%s\\n" "$native_rc" > ' + marker +
            ' || marker_rc=1; ' + cleanup +
            ' || exit $?; test "$marker_rc" = 0 || exit "$marker_rc"; exit "$native_rc"')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('validate', 'prepare', 'command', 'resume-prepare', 'resume'))
    for key in ('harness', 'backend', 'model', 'effort', 'manifest', 'runtime-dir', 'agent-name',
                'project-key', 'generation', 'cwd', 'parent-agent', 'mcp-config', 'prompt-file', 'state', 'helper-model', 'url-key'):
        parser.add_argument('--' + key)
    parser.add_argument('--tools', default='{"base":"default","tools":{}}')
    parser.add_argument('--url-index', type=int)
    args = parser.parse_args()
    try:
        if args.action == 'validate':
            selection(args.manifest, args.harness, args.backend, args.model, args.effort, args.url_index, None, args.url_key)
        elif args.action == 'prepare':
            print(prepare(args))
        elif args.action == 'resume-prepare':
            print(prepare_resume(args.runtime_dir, args.agent_name, args.project_key))
        elif args.action == 'resume':
            print(resume_command(args.runtime_dir, args.agent_name, args.project_key))
        else:
            print(command(args.state, args.agent_name))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        reason = str(error) if re.fullmatch(r'[a-z_]+', str(error)) else type(error).__name__
        if isinstance(error, child_tools.ToolsError):
            reason = 'unsupported_mcp_selection'
        print('Harness ' + args.action + ' failed: ' + reason, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
