"""Persistent, one-task legacy managed-spawn orchestration.

HTTP retries and dashboard restarts occur normally: keep one request ID and a
separate worker, and never derive readiness from registration or wrapper exit.
The worker calls existing product CLIs, not a second registration/proxy stack.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
LOCK = threading.Lock()
PROVIDERS = ('deepseek', 'strata')
HARNESSES = ('dsh', 'hermes')
LOG = logging.getLogger(__name__)


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError, TypeError):
        return default


def save(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.new')
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as out:
        json.dump(value, out, ensure_ascii=False)
        out.write('\n')
    temporary.replace(path)


def error(code):
    return {'ok': False, 'error': code, 'error_code': code, 'managed': True}


def cleanup_success(status):
    if status.get('phase') in ('cleaned', 'cleaned_after_failure'):
        return True
    if status.get('error_phase') == 'cleaning':
        return False
    return None


def mode(core):
    return 'global' if core.GLOBAL_MODE else 'legacy'


def _catalog_module():
    try:
        return importlib.import_module('dashboard.managed_catalog')
    except ModuleNotFoundError as exc:
        # Direct launchd execution puts dashboard/, not its parent, on sys.path.
        # A missing dependency inside the module must remain a real failure.
        if exc.name not in ('dashboard', 'dashboard.managed_catalog'):
            raise
        return importlib.import_module('managed_catalog')


def _product_module(name):
    # The installed integrations/ and dashboard/ are siblings under this root.
    # Resolve that product's code independently of launchd's cwd or PYTHONPATH.
    root = str(ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    return importlib.import_module(name)


def catalog(config_file, *, selected_mode):
    # Catalog implementation is owned separately; a missing module/config is off.
    if not config_file:
        return []
    try:
        module = _catalog_module()
        backends, availability, enabled = module.load_catalog_inputs(config_file, mode=selected_mode)
        for harness in HARNESSES:
            selection = read(ROOT / 'dashboard' / (harness + '-provider.json'))
            if isinstance(selection, dict) and selection.get('enabled') is not True:
                for status in availability.values():
                    status[harness] = {'available': False, 'reason': 'disabled_by_opt_in'}
        return module.catalog_providers(backends, availability, mode=selected_mode, enabled=enabled)
    except (ImportError, OSError, ValueError, KeyError, TypeError) as exc:
        LOG.warning("managed catalog unavailable (%s)", type(exc).__name__)
        return []


def managed(payload):
    return isinstance(payload, dict) and (payload.get('provider') in PROVIDERS or payload.get('harness') in HARNESSES)


def normalize(payload):
    fields = {'attempt_id', 'provider', 'model', 'harness', 'helper_model', 'parent', 'standalone',
              'task', 'dir', 'worktree', 'allowed_paths', 'expected', 'async', 'effort', 'role', 'group', 'emoji'}
    if not isinstance(payload, dict) or set(payload) - fields:
        raise ValueError('unsupported_managed_fields')
    result = dict(payload)
    aid = result.get('attempt_id')
    if not isinstance(aid, str) or str(uuid.UUID(aid)) != aid:
        raise ValueError('invalid_attempt_id')
    for name in ('model', 'parent', 'task', 'dir'):
        if not isinstance(result.get(name), str) or not result[name].strip():
            raise ValueError('missing_' + name)
    if result.get('provider') not in PROVIDERS or result.get('harness') not in HARNESSES:
        raise ValueError('unsupported_managed_pair')
    if result.get('helper_model', 'same') != 'same' or result.get('standalone', False) is not False:
        raise ValueError('unsupported_managed_controls')
    if result.get('worktree', True) is not True or result.get('async', True) is not True:
        raise ValueError('private_async_work_required')
    source = Path(result['dir']).expanduser().resolve(strict=True)
    if not source.is_dir():
        raise ValueError('source_directory_required')
    result['dir'] = str(source)
    paths = result.get('allowed_paths')
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
        raise ValueError('literal_scope_required')
    for name in paths:
        path = PurePosixPath(name)
        if (not name or path.is_absolute() or str(path) != name or path == PurePosixPath('.')
                or any(part in ('..', '.git') for part in path.parts) or any(c in name for c in '*?[]\\')):
            raise ValueError('literal_scope_required')
        target = source / name
        if not target.resolve().is_relative_to(source) or target.is_symlink() or (target.exists() and not target.is_file()):
            raise ValueError('scope_must_be_source_files')
    expected = result.get('expected')
    if not isinstance(expected, list) or not expected:
        raise ValueError('expected_artifacts_required')
    for spec in expected:
        if not isinstance(spec, dict) or spec.get('path') not in paths:
            raise ValueError('expected_outside_scope')
        keys = set(spec) - {'path'}
        if keys == {'contains'} and isinstance(spec['contains'], str):
            continue
        if keys == {'sha256'} and isinstance(spec['sha256'], str) and len(spec['sha256']) == 64:
            try:
                int(spec['sha256'], 16)
                continue
            except ValueError:
                pass
        raise ValueError('invalid_expected_check')
    for key, default in (('helper_model', 'same'), ('standalone', False), ('worktree', True), ('async', True)):
        result[key] = default
    return result


def trusted_selection(request, config, providers):
    provider = next((p for p in providers if p['id'] == request['provider']), None)
    model = next((m for m in provider.get('models', []) if isinstance(m, dict) and m.get('id') == request['model']), None) if provider else None
    harness = next((h for h in model.get('harnesses', []) if h.get('id') == request['harness']), None) if model else None
    if not harness or harness.get('available') is not True:
        raise ValueError('managed_pair_unavailable')
    if request.get('effort') is not None and request['effort'] not in (provider.get('efforts') or []):
        raise ValueError('unsupported_effort')
    template = config['harnesses'][request['harness']]
    source = read(template['config_file'])
    if not source:
        raise ValueError('run_template_unavailable')
    if request['harness'] == 'dsh':
        selected = read(source['backend_file'])['backends'][request['provider']]
    else:
        selected = source['profiles'][request['provider']]
        if not selected.get('model') and selected.get('template'):
            # Use the same pure projector as catalog admission; never evaluate
            # a key command or load a provider to resolve inherited metadata.
            projection = ROOT / 'integrations/hermes/profile_projection.py'
            code = ('import json,runpy,sys;from pathlib import Path;'
                    'sys.path.insert(0,sys.argv[2]);m=runpy.run_path(sys.argv[1]);'
                    'p=m["project"](json.loads(sys.argv[3]),Path(sys.argv[4]),["title_generation","background_review"]);'
                    'print(json.dumps({"model":p["model"]}))')
            source_path = Path(source['hermes_source']).resolve()
            install = Path(source['managed_root']).resolve() / 'installs' / hashlib.sha256(str(source_path).encode()).hexdigest()[:16]
            environment = Path(read(install / 'facts.json')['packages']['venv']['environment']).resolve()
            if not environment.is_relative_to((install / 'environments').resolve()):
                raise ValueError('committed_interpreter_unavailable')
            inherited = subprocess.run([str(environment / 'bin/python'), '-I', '-B', '-c', code,
                str(projection), source['hermes_source'], json.dumps(selected),
                str(Path(source['managed_root']) / 'catalog-preview')],
                env={'PATH': '/usr/bin:/bin'}, capture_output=True, text=True, check=True, timeout=5)
            selected = json.loads(inherited.stdout)
    if selected['model'] != request['model']:
        raise ValueError('template_model_mismatch')
    return template, source


class Manager:
    def __init__(self, config_file, *, selected_mode='legacy', provider_loader=catalog, spawn=None):
        self.config_file, self.selected_mode = config_file, selected_mode
        self.provider_loader, self.spawn = provider_loader, spawn or subprocess.Popen

    def root(self, *, require_enabled=True):
        config = read(self.config_file, {})
        if ((require_enabled and config.get('enabled') is not True)
                or not Path(config.get('state_root', '')).is_absolute()):
            raise ValueError('managed_spawn_disabled')
        return Path(config['state_root']), config

    def lookup(self, attempt_id):
        try:
            if str(uuid.UUID(attempt_id)) != attempt_id:
                return error('invalid_attempt_id')
            root, _ = self.root(require_enabled=False)
            status = read(root / attempt_id / 'status.json')
            return {**status, 'cleanup_success': cleanup_success(status)} if status else error('attempt_not_found')
        except (ValueError, TypeError, AttributeError):
            return error('attempt_not_found')

    def submit(self, payload):
        try:
            request = normalize(payload)
            if self.selected_mode != 'legacy':
                raise ValueError('unsupported_mode')
            root, config = self.root(require_enabled=False)
            aid = request['attempt_id']
            digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
            with LOCK:
                job = root / aid
                if job.exists():
                    old = read(job / 'request.json', {})
                    return self.lookup(aid) if old.get('digest') == digest else error('attempt_conflict')
                if config.get('enabled') is not True:
                    raise ValueError('managed_spawn_disabled')
                providers = self.provider_loader(self.config_file, selected_mode=self.selected_mode)
                template, source = trusted_selection(request, config, providers)
                if root.resolve().is_relative_to(Path(source['project_key']).resolve()):
                    raise ValueError('private_state_outside_live_project_required')
                if request['harness'] == 'hermes':
                    for previous in root.iterdir() if root.exists() else []:
                        old = read(previous / 'request.json', {}).get('request', {})
                        status = read(previous / 'status.json', {})
                        if old.get('harness') == 'hermes' and old.get('provider') == request['provider']:
                            if (status.get('finished') is not True
                                    or ((previous / 'state/registration-intent.json').exists()
                                        and status.get('runtime_success') is not True)):
                                return error('profile_busy')
                job.mkdir(mode=0o700, parents=True)
                save(job / 'request.json', {'request': request, 'digest': digest,
                     'config_file': str(self.config_file), 'selected_mode': self.selected_mode})
                state = {'ok': True, 'managed': True, 'attempt_id': aid, 'name': None,
                         'state': 'pending', 'phase': 'queued', 'pending': True, 'running': False,
                         'finished': False, 'task_success': None, 'runtime_success': None, 'cleanup_success': None,
                         'state_path': str(job / 'state'), 'runtime_attempt_id': None,
                         'error': None, 'error_code': None}
                save(job / 'status.json', state)
                # A worker survives a dashboard restart. Its process gets no model
                # credentials; the existing pane supervisor performs the lookup.
                env = worker_environment(source)
                with (job / 'worker.log').open('ab', buffering=0) as log:
                    os.chmod(job / 'worker.log', 0o600)
                    child = self.spawn([template['python'], '-B', str(Path(__file__).resolve()),
                                        '--job', str(job)], env=env, cwd=ROOT,
                                       stdout=log, stderr=log, start_new_session=True)
                save(job / 'worker.json', {'pid': child.pid})
                return state
        except (ValueError, TypeError, KeyError, OSError) as exc:
            # Never return CLI stdout, exception text or credential diagnostics.
            code = str(exc) if type(exc) is ValueError and str(exc).replace('_', '').isalnum() else 'managed_request_failed'
            if 'job' in locals() and (job / 'status.json').exists() and not (job / 'worker.json').exists():
                state.update(state='failed', pending=False, finished=True, phase='worker_start_failed', error_code=code, error=code)
                save(job / 'status.json', state)
            return error(code)

    def stop(self, name):
        try:
            root, _ = self.root(require_enabled=False)
        except ValueError:
            return None
        for job in root.iterdir() if root.exists() else []:
            value = read(job / 'status.json', {})
            identity = read(job / 'state/mail-identity.json', {})
            if name and name in (value.get('name'), identity.get('agent_name')):
                if value.get('finished') is True:
                    if value.get('runtime_success') is True and value.get('phase') in ('cleaned', 'cleaned_after_failure'):
                        return {**value, 'ok': True, 'managed': True}
                    return {**error('managed_cleanup_unconfirmed'), 'attempt_id': job.name,
                            'name': name, 'state_path': value.get('state_path')}
                save(job / 'stop-request.json', {'requested_at': time.time()})
                return {'ok': True, 'managed': True, 'pending': True, 'attempt_id': job.name, 'name': name}
        return None


def worker_environment(config):
    # Explicit executable paths plus ordinary system binaries. No inherited API,
    # owner token, Node loader or shell startup configuration.
    paths = [str(Path(config[k]).parent) for k in ('node', 'proxy_python', 'hermes_python', 'tmux') if config.get(k)]
    return {'HOME': config['home'], 'PATH': ':'.join(dict.fromkeys(paths + ['/usr/local/bin', '/usr/bin', '/bin'])),
            'LANG': 'en_US.UTF-8', 'LC_ALL': 'en_US.UTF-8', 'PYTHONPATH': str(ROOT),
            **{key: os.environ[key] for key in ('AGENTSTACK_HOME', 'AGENTSTACK_PROJECT_KEY',
                  'AGENTSTACK_MCP_URL', 'AGENTSTACK_RUNTIME_DIR', 'AGENTSTACK_MAIL_ENV',
                  'AGENTSTACK_MAIL_HTTP_BEARER_MODE') if key in os.environ}}


def command_runner(config, log_path):
    env = worker_environment(config)
    def run(argv, *, timeout=60, check=True, background=False):
        checkpoint = Path(log_path).with_name('cli-last.json')
        save(checkpoint, {'script': Path(argv[2]).name if len(argv) > 2 else None,
                          'action': argv[3] if len(argv) > 3 and not argv[3].startswith('-') else None,
                          'phase': 'started'})
        if background:
            with Path(log_path).open('ab') as log:
                return subprocess.Popen(argv, env=env, stdout=log, stderr=log, start_new_session=True)
        result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=timeout)
        save(checkpoint, {**read(checkpoint), 'phase': 'exited', 'exit_code': result.returncode})
        if check and result.returncode:
            raise RuntimeError('managed_cli_failed')
        return result
    return run


def parent_connection(core, parent):
    # Whois is read-only in the existing dashboard Mail context. It does not
    # require possession of the selected parent's owner token. Never acquire
    # that token to read its inbox, or introduce another transport/identity.
    def call(tool, args):
        if tool != 'whois':
            raise ValueError('unsupported_parent_call')
        result = core._mcp_call('whois', {'project_key': core._project_key(),
                                 'agent_name': args['name'],
                                 'include_recent_commits': False})
        if result.get('ok') is not True:
            raise RuntimeError('parent_whois_failed')
        return result['data']
    return call


def run_job(job, *, adapter=None, parent_call=None, run=None, sleep=time.sleep, clock=time.monotonic,
            provider_loader=catalog):
    job = Path(job)
    spec = read(job / 'request.json')
    request, config = spec['request'], read(spec['config_file'])
    current = read(job / 'status.json')
    state = job / 'state'
    def update(**values):
        current.update(values)
        current['cleanup_success'] = cleanup_success(current)
        save(job / 'status.json', current)
    finalize_started = False
    try:
        providers = provider_loader(spec['config_file'], selected_mode=spec['selected_mode'])
        template, source = trusted_selection(request, config, providers)
        if adapter is None:
            adapter = _product_module('integrations.' + request['harness'] + '.managed_lifecycle')
        if spec['selected_mode'] != 'legacy':
            raise ValueError('unsupported_mode')
        if source['project_key'] != str(Path(source['project_key']).resolve()):
            raise ValueError('absolute_project_key_required')
        if parent_call is None:
            core = _product_module('dashboard.server')
            if core.GLOBAL_MODE or core._project_key() != source['project_key']:
                raise ValueError('parent_project_mismatch')
            if (core.MCP_HTTP_URL != source['connection']['mcp_url']
                    or Path(core.RUNTIME_DIR).resolve() != Path(source['connection']['runtime_dir']).resolve()
                    or Path(core.MAIL_ENV_PATH).resolve() != Path(source['connection']['mail_env']).resolve()):
                raise ValueError('mail_connection_mismatch')
            parent_call = parent_connection(core, request['parent'])
        # Ordinary parent-name/context mistakes must fail before registering.
        who = parent_call('whois', {'name': request['parent']})
        if who.get('name') != request['parent'] or who.get('retired_at'):
            raise ValueError('parent_context_mismatch')
        work = job / 'work'
        work.mkdir(mode=0o700)
        source_root = Path(request['dir'])
        for relative in request['allowed_paths']:
            src, dest = source_root / relative, work / relative
            if src.exists():
                if not src.resolve().is_relative_to(source_root) or not src.is_file() or src.is_symlink():
                    raise ValueError('source_scope_changed')
                dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copyfile(src, dest)
        projected = dict(source)
        if request.get('effort') is not None:
            if request['harness'] == 'dsh':
                backend = read(projected['backend_file'])
                backend['backends'][request['provider']]['effort'] = request['effort']
                save(job / 'backend.json', backend)
                projected['backend_file'] = str(job / 'backend.json')
            else:
                profiles = json.loads(json.dumps(projected['profiles']))
                profiles[request['provider']]['reasoning_effort'] = ('none' if request['effort'] == 'off' else request['effort'])
                projected['profiles'] = profiles
        save(job / 'run-template.json', projected)
        run = run or command_runner(projected, job / 'runner.log')
        update(phase='preparing')
        prompt = (request['task'] + '\n\nManaged task: edit only the scoped files in ' + str(work)
                  + '. Allowed files: ' + json.dumps(request['allowed_paths'])
                  + '. Required result checks: ' + json.dumps(request['expected'])
                  + '. When the task is complete, send one completion report to '
                  + request['parent'] + ' with mcp__orrery_mail__send_message. '
                  + 'Include the artifact paths in your report. Do not wait for the TUI to exit.')
        adapter.prepare(state=state, config_file=job / 'run-template.json', backend_id=request['provider'],
                        task={'prompt': prompt, 'allowed_paths': request['allowed_paths'], 'expected': request['expected']},
                        worktree=work, parent=request['parent'], run=run)
        update(phase='starting')
        handle = adapter.start(state, run=run)
        deadline = clock() + source.get('task_timeout_seconds', 660)
        while True:
            evidence = adapter.status(state)
            update(name=evidence.get('agent_name'), runtime_attempt_id=evidence.get('runtime_attempt_id'),
                   task_success=evidence.get('task_success'), runtime_success=evidence.get('runtime_success'))
            if evidence.get('ready') is True:
                update(state='ready', phase='running', pending=False, running=True)
            if evidence.get('terminal') or (job / 'stop-request.json').exists():
                if current['state'] != 'ready':
                    raise RuntimeError('managed_runtime_not_ready')
                break
            if clock() >= deadline:
                raise RuntimeError('managed_runtime_timeout')
            # A short wrapper's exit zero is not a terminal runtime. Nonzero is
            # an actual start error; retain state and finalize the partial run.
            if handle is not None and handle.poll() not in (None, 0):
                raise RuntimeError('managed_start_failed')
            sleep(.5)
        update(phase='stopping')
        finalize_started = True
        result = adapter.finalize(state, parent_call=parent_call, run=run)
        update(task_success=result.get('task_success'), runtime_success=result.get('runtime_success'), running=False)
        if result.get('runtime_success') is not True:
            raise RuntimeError('managed_cleanup_unconfirmed')
        update(phase='cleaning')
        adapter.clean(state, run=run)
        update(state='ready' if result.get('task_success') is True else 'failed', phase='cleaned',
               finished=True, pending=False, error_code=None if result.get('task_success') is True else 'task_not_successful')
    except Exception as exc:
        step = getattr(exc, 'managed_step', None)
        if step not in ('init-task', 'preregister', 'configure', 'probe', 'reserve', 'preflight',
                        'start', 'stop', 'scan-diagnostics', 'release', 'retire', 'confirm-retire',
                        'record-mail', 'complete', 'complete-runtime', 'clean'):
            step = 'unknown'
        reason = getattr(exc, 'managed_reason', None)
        if reason not in ('profile_link_unapproved', 'profile_path_mismatch', 'profile_symlink',
                          'current_profile_escape', 'process_failed', 'filesystem_failed',
                          'missing_state', 'runtime_failed', 'validation_failed'):
            reason = 'step_failed'
        failure = {'phase': current.get('phase'), 'step': step, 'reason': reason, 'type': type(exc).__name__}
        save(job / 'worker-failure.json', failure)
        LOG.error("managed worker failed during %s: step=%s reason=%s (%s)",
                  failure['phase'], step, reason, failure['type'])
        # Failure itself never becomes success. Recover ordinary partial starts
        # using the same identity; a cleanup refusal keeps the private state.
        identity, attempt = read(state / 'mail-identity.json', {}), read(state / 'attempt.json', {})
        update(state='failed', pending=False, name=current.get('name') or identity.get('agent_name'),
               runtime_attempt_id=current.get('runtime_attempt_id') or attempt.get('attempt_id'),
               error_phase=current.get('phase'), phase='failed', error='managed_attempt_failed', error_code='managed_attempt_failed')
        if not finalize_started and adapter is not None and run is not None and state.exists():
            try:
                result = adapter.finalize(state, parent_call=parent_call, run=run)
                update(task_success=result.get('task_success'), runtime_success=result.get('runtime_success'), running=False)
                if result.get('runtime_success') is True:
                    update(phase='cleaning')
                    adapter.clean(state, run=run)
                    update(phase='cleaned_after_failure', finished=True)
            except Exception:
                update(error_phase='cleaning' if current.get('phase') == 'cleaning' else current.get('error_phase'),
                       phase='cleanup_unconfirmed', finished=True)
        else:
            update(finished=True)
    return current


def finish_unstarted_job(job, *, adapter=None, run=None):
    """Close saved, confirmed pre-launch cleanup without registration or Mail."""
    job = Path(job)
    current = read(job / 'status.json')
    request = read(job / 'request.json')['request']
    if request.get('harness') != 'hermes' or current.get('state') != 'failed':
        raise ValueError('Only failed Hermes attempts support unstarted cleanup')
    state = job / 'state'
    adapter = adapter or _product_module('integrations.hermes.managed_lifecycle')
    run = run or command_runner(read(state / 'run-config.json'), job / 'runner.log')
    result = adapter.close_unstarted(state, run=run)
    if result.get('runtime_success') is not True or result.get('cleaned') is not True:
        raise RuntimeError('Unstarted cleanup not confirmed')
    current.update(phase='cleaned_after_failure', finished=True, pending=False, running=False,
                   runtime_success=True)
    current['cleanup_success'] = cleanup_success(current)
    save(job / 'status.json', current)
    return current


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--job', type=Path, required=True)
    parser.add_argument('--finish-unstarted', action='store_true',
                        help='Close an already-retired, unstarted Hermes failure without launching or registering')
    args = parser.parse_args()
    if args.finish_unstarted:
        result = finish_unstarted_job(args.job)
        return 0 if result.get('cleanup_success') is True else 1
    result = run_job(args.job)
    return 0 if (result.get('state') != 'failed' and result.get('finished') is True
                 and result.get('phase') == 'cleaned' and result.get('task_success') is True
                 and result.get('runtime_success') is True) else 1


if __name__ == '__main__':
    raise SystemExit(main())
