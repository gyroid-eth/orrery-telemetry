"""Pure, spike-only launch projection. Ordinary env inheritance can leak keys."""
from pathlib import PurePosixPath


def build_launch(*, node, node_version, runtime, state, worktree, integration, backend_file, backend_id,
                 attempt_id, home):
    version = tuple(map(int, node_version.removeprefix('v').split('.')))
    if version < (24, 2, 0):
        raise ValueError('Node 24.2 or later is required before credentials')
    for value in (node, runtime, state, worktree, integration, backend_file, home):
        if not PurePosixPath(value).is_absolute():
            raise ValueError('absolute paths are required')
    if backend_id not in ('deepseek', 'strata') or not attempt_id:
        raise ValueError('explicit backend and attempt required')
    base = PurePosixPath(integration)
    env = {
        'PATH': str(PurePosixPath(node).parent) + ':/usr/bin:/bin',
        'HOME': home, 'LANG': 'en_US.UTF-8', 'LC_ALL': 'en_US.UTF-8', 'TERM': 'xterm-256color',
        'DSH_HOME': str(PurePosixPath(state) / 'dsh'),
        'NODE_COMPILE_CACHE': str(PurePosixPath(state) / 'node-cache'),
        'DSH_TELEMETRY_DISABLED': '1', 'DSH_TELEMETRY_MODE': 'DISABLED',
        'ORRERY_DSH_STATE_ROOT': state, 'ORRERY_DSH_RUNTIME_ROOT': runtime,
        'ORRERY_DSH_BACKEND_FILE': backend_file, 'ORRERY_DSH_BACKEND_ID': backend_id,
        'ORRERY_DSH_ATTEMPT_ID': attempt_id,
        'ORRERY_DSH_AUDIT_FILE': str(PurePosixPath(state) / 'audit.jsonl'),
        'ORRERY_DSH_OFFLINE': '1',
        'ORRERY_DSH_TUI_DATA_ROOT': str(PurePosixPath(state) / 'tui'),
        'DSH_TUI_LAUNCHER_VERSION': '0.16.1',
    }
    return {'argv': [node, '--import', str(base / 'egress.mjs'), str(base / 'managed_entry.mjs')],
            'cwd': worktree, 'env': env}
