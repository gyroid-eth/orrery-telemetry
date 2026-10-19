"""The reserved read/write task subset supported by the managed Hermes entry."""
from pathlib import Path, PurePosixPath
import re
import uuid
import json

from reservation_guard import _matches


def validate(value):
    if not isinstance(value, dict) or set(value) != {'prompt', 'allowed_paths', 'expected'}:
        raise ValueError('Task requires prompt, allowed_paths and expected')
    if not isinstance(value['prompt'], str) or not value['prompt'].strip():
        raise ValueError('Task prompt required')
    paths, expected = value['allowed_paths'], value['expected']
    if not isinstance(paths, list) or not paths or not isinstance(expected, list) or not expected:
        raise ValueError('Reserved paths and expected file checks required')
    for pattern in paths:
        if not isinstance(pattern, str) or not pattern:
            raise ValueError('Relative reservation path required')
        literal = pattern
        rel = PurePosixPath(literal)
        if rel.is_absolute() or '..' in rel.parts or '.git' in rel.parts or not rel.parts or any(c in literal for c in '*?['):
            raise ValueError('Only relative literal file paths supported in this stage')
    if len(set(paths)) != len(paths):
        raise ValueError('Duplicate reservation paths')
    seen = set()
    for spec in expected:
        if not isinstance(spec, dict) or set(spec) - {'path', 'contains', 'sha256'}:
            raise ValueError('Expected file check malformed')
        if ('contains' in spec) == ('sha256' in spec):
            raise ValueError('Expected file requires exactly one contains or sha256 check')
        name = spec.get('path')
        if (not isinstance(name, str) or not name or PurePosixPath(name).is_absolute()
                or any(part in ('..', '.git') for part in PurePosixPath(name).parts)
                or not any(_matches(name, pattern) for pattern in paths) or name in seen):
            raise ValueError('Expected file must be inside reserved paths')
        if 'sha256' in spec:
            if not isinstance(spec['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', spec['sha256']):
                raise ValueError('Expected sha256 must be a lowercase digest')
        elif not isinstance(spec.get('contains'), str):
            raise ValueError('Expected contains or sha256 check required')
        seen.add(name)
    return value


def materialize(state, value, config, attempt, save):
    """Do not seed or alter requested artifacts before the child runs."""
    value = validate(value)
    state = Path(state)
    task = {'attempt_id': attempt['attempt_id'], 'task_id': uuid.uuid4().hex,
            'worktree': config['worktree'], 'report_to': config['parent'],
            'allowed_paths': value['allowed_paths'], 'expected': value['expected'],
            'reservation_source': 'product-bound-proxy', 'reservation_receipts': [],
            'require_read_before_report': False}
    message = {'to': [config['parent']], 'subject': 'Hermes managed task result',
               'body_md': 'path: ' + ', '.join(str(Path(config['worktree']) / row['path']) for row in value['expected'])
                          + ' — completed the requested file checks.'}
    save(state / 'task.json', task)
    save(state / 'requested-task.json', value, exclusive=True)
    save(state / 'message.json', message)
    (state / 'task.md').write_text(value['prompt'] + '\n\n'
        'Registration is complete. Do not register, fetch inboxes, inspect credentials or use another transport. '
        'Use absolute file paths under ' + config['worktree'] + '. Only reserved read_file/write_file and private '
        'memory/skills are available. After all expected files pass these checks: ' + json.dumps(value['expected'])
        + '\ncall mcp__orrery_mail__send_message exactly once with this JSON: ' + json.dumps(message)
        + '\nIf a tool fails, stop and report failure. Do not retry Mail. Finish after it returns.\n')
    return task
