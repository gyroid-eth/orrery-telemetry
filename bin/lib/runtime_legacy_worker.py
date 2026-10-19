"""Fixed legacy P/Q consumer, not native permission/isolation approval.

Only the saved preview intent determines executable/argv/env. The normal launch
gate is still closed until hooks/public consumers join this internal boundary.
No supervisor, automatic retry, second execution ledger or credential command.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from local_model_auth import LocalModelError, secure_read, source_evidence

FAILED = 'LOCAL_MODEL_EXECUTION_FAILED'


def _contract(intent):
    root = Path(__file__).resolve().parents[2]
    if intent['legacy_context']['install_root'] != str(root): raise LocalModelError(FAILED)
    path = str(root / 'bin/lib/runtime_legacy_proxy.py')
    proof = intent['configuration_evidence']['renderer_sources'][path]
    if source_evidence([path]) != {path: proof}: raise LocalModelError(FAILED)
    content = secure_read(path, os.getuid())
    if hashlib.sha256(content).hexdigest() != proof['sha256']: raise LocalModelError(FAILED)
    proxy = {'__file__': path, '__name__': '_orrery_worker_proxy_contract'}
    exec(compile(content, path, 'exec'), proxy)
    if source_evidence([path]) != {path: proof}: raise LocalModelError(FAILED)
    auth, api = proxy['_auth_contract'](intent, root)
    intent = api['runtime_intent_store'].checked_intent(intent)
    return root, auth, api, proxy['_context'](intent, root)


def _registered(api, intent):
    target = intent['legacy_context']
    store = api['runtime_legacy_registration'].Store(target['runtime_dir'], target['name'])
    value, digest = store._snapshot(); store._unresolved(value)
    if (value['pending'] is not None or 'local_registration' not in value['owner']
            or value['owner']['launch_intent'] != intent or intent['operator_uid'] != os.getuid()):
        raise LocalModelError(FAILED)
    return store, value['owner'], digest


def _projection(api, intent, context):
    projection = api['runtime_launch_intent'].materialize_launch_intent(intent, context, operation='spawn')
    api['runtime_legacy_configuration'].check_legacy_provider_outputs(intent)
    return projection


def _environment(intent, projection, row, *, provider_pid=None):
    # Legacy does NOT fabricate global CLIENT_CONFIG or reuse global runtime
    # keys. This is the common model projection plus this closed transport set.
    target = intent['legacy_context']; home = str(Path(target['owner']).with_suffix('.preview-home'))
    env = {**projection['execution']['env'], 'HOME': home, 'CLAUDE_CONFIG_DIR': home,
        'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C',
        'AGENTSTACK_HOME': target['install_root'], 'AGENTSTACK_RUNTIME_MODE': 'legacy',
        'AGENTSTACK_RUNTIME_DIR': target['runtime_dir'], 'AGENTSTACK_PROJECT_KEY': target['project_key'],
        'AGENTSTACK_MCP_URL': target['mail_url'], 'AGENT_NAME': target['name'],
        'PARENT_AGENT': target['parent']['name'], 'AGENTSTACK_LAUNCH_ID': row['launch_id'],
        'AGENTSTACK_LAUNCH_NONCE': row['receipt_nonce'], 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1'}
    if provider_pid is not None: env['AGENTSTACK_PROVIDER_PID'] = str(provider_pid)
    return env


def _normalize_observation_environment():
    # Saved local birth strings in P/Q/helper must share the same locale/zone.
    # Chronology is still fresh explicit UTC on both sides, never saved lstart.
    os.environ.pop('TZ', None)
    os.environ.update(PATH='/usr/bin:/bin', LANG='C', LC_ALL='C')


def run_preview(intent, *, intent_digest, registration_generation, _process=None, _popen=None):
    """P: same owner, persist unknown BEFORE Popen, stay alive through wait."""
    try:
        _normalize_observation_environment()
        root, auth, api, context = _contract(intent)
        projection = _projection(api, intent, context)
        store, frame, digest = _registered(api, intent)
        producer = api['runtime_legacy_execution'].ExecutionStore(store.runtime, store.name, _process=_process)
        row = producer.plan(expected_state_digest=digest, intent_digest=intent_digest,
            registration_generation=registration_generation)
        reference = {'launch_id': row['launch_id'], 'receipt_nonce': row['receipt_nonce']}
        producer.claim_runner(**reference)
        # The same resources must still hold at the last pre-spawn boundary.
        _projection(api, intent, context)
        row = producer.before_provider(**reference)
        command = [intent['configuration_evidence']['runtime_python']['path'], '-I', '-B',
            str(root / 'bin/lib/runtime_legacy_preview_cli.py'), 'entry', 'provider', store.name,
            '--launch-id=' + row['launch_id'], '--receipt-nonce=' + row['receipt_nonce']]
        child = (_popen if _popen is not None else subprocess.Popen)(command,
            env=_environment(intent, projection, row), cwd=intent['cwd'], close_fds=True)
        try:
            return producer.finish_wait(child, **reference)
        finally:
            # P owns this actual Popen object independently of saved state.
            # A failed pre-wait owner/birth check must not abandon its child.
            # Popen.wait is idempotent after reap; do not signal a saved PID
            # or publish ended/cleanup success from this unconditional wait.
            child.wait()
    except Exception: raise LocalModelError(FAILED) from None


def _parent_proof(api, process, row):
    observer = api['process_observation']; runner = row['execution']['runner']; provider = row['execution']['provider']
    deadline = time.monotonic() + 5
    def snapshot():
        if (process.pid() != provider['pid'] or process.parent() != runner['pid']
                or observer.process_start(runner['pid'], deadline=deadline) != runner['process_start']
                or observer.process_start(provider['pid'], deadline=deadline) != provider['process_start']): return None
        parent = observer.process_birth_utc(runner['pid'], deadline=deadline)
        child = observer.process_birth_utc(provider['pid'], deadline=deadline)
        chain = observer.ancestry(provider['pid'], runner['pid'], os.getuid(), deadline=deadline)
        if chain is None or not observer.birth_not_before(parent, child): return None
        return parent, child, chain
    before = snapshot()
    if before is None or before != snapshot(): raise LocalModelError(FAILED)


def execute_provider(intent, *, launch_id, receipt_nonce, _process=None, _execve=None, _chdir=None):
    """Q: verify its actual direct P twice, claim OWN PID, same-PID execve."""
    try:
        _normalize_observation_environment()
        root, auth, api, context = _contract(intent)
        _projection(api, intent, context)
        store, frame, _ = _registered(api, intent)
        producer = api['runtime_legacy_execution'].ExecutionStore(store.runtime, store.name, _process=_process)
        reference = {'launch_id': launch_id, 'receipt_nonce': receipt_nonce}
        row = producer.claim_provider(**reference)
        _parent_proof(api, producer.process, row)
        projection = _projection(api, intent, context)
        # Re-read the same owner after observation/materialization. A hook or
        # another writer cannot replace the launch/credential while Q starts.
        _, after, _ = _registered(api, intent)
        stable = lambda value: auth.canonical_digest({k: v for k, v in value.items() if k != 'local_execution'})
        current = api['runtime_legacy_execution'].checked_execution(after['local_execution'], intent)
        if stable(frame) != stable(after) or current != row: raise LocalModelError(FAILED)
        body = auth.secure_read(intent['task'], os.getuid())
        if hashlib.sha256(body).hexdigest() != intent['task_sha256']: raise LocalModelError(FAILED)
        target = intent['legacy_context']; home = Path(target['owner']).with_suffix('.preview-home')
        config = json.loads(auth.secure_read(home / 'local-model-mcp.json', os.getuid(), mode=0o600, parent_mode=0o700))
        proxy = config['mcpServers']['orrery-mail']; binary = intent['configuration_evidence']['cli']['path']
        prompt = (f"あなたは {target['name']}（親: {target['parent']['name']}）。この起動は --embed-task mode です。"
            'ORRERY Mail への登録は親が完了済み・儀式不要です。ensure_project・register_agent・fetch_inbox は実行しないでください。'
            f"project_key は {target['project_key']}。以下のタスクが正本です。完了したら ORRERY Mail の send_message で"
            f" {target['parent']['name']} に報告してください:\n\n" + body.decode('utf-8'))
        checked = {'intent': intent, 'projection': projection, 'binary': binary,
            'home': str(home), 'settings': str(home / 'local-model-settings.json')}
        command = api['runtime_local_launch'].local_provider_command(checked,
            {'provider': intent['provider'], 'cwd': intent['cwd'], 'launch_kind': 'startup'},
            proxy=[proxy['command'], *proxy['args']], extra_servers=config['mcpServers'], prompt=prompt)
        env = _environment(intent, projection, row, provider_pid=producer.process.pid())
        _parent_proof(api, producer.process, row)
        _projection(api, intent, context)
        _, final, _ = _registered(api, intent)
        if auth.canonical_digest(final) != auth.canonical_digest(after): raise LocalModelError(FAILED)
        (_chdir if _chdir is not None else os.chdir)(intent['cwd'])
        (_execve if _execve is not None else os.execve)(binary, command, env)
        raise LocalModelError(FAILED)  # exec returning is never success.
    except Exception: raise LocalModelError(FAILED) from None
