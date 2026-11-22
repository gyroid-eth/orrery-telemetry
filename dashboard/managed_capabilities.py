"""Local, keyless effort metadata. Unknown is never a copied trial default."""
import ast
import hashlib
import importlib.util
import json
import os
import runpy
from pathlib import Path
import subprocess
import tempfile
from datetime import datetime, timezone

UNKNOWN = {'efforts': None, 'default_effort': None, 'effort_source': 'unknown'}
CONNECTIONS = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'hooks/native_connections.py'))


def read(path):
    return json.loads(Path(path).read_text())


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def model_input(backend_id, backend):
    model = backend['model']
    if not isinstance(model, str) or not model.strip():
        raise ValueError('Configured model required')
    result = {'model': model, 'model_definition': backend.get('model_definition')}
    if backend.get('thinking') is not None:
        result['thinking'] = backend['thinking']
    # Explicit effort/output overrides do not describe the model's capabilities.
    return {'backend_id': backend_id, **result, 'model_fingerprint': digest(result)}


def runtime_facts(template):
    modules = Path(template['runtime']) / 'node_modules'
    versions, hashes = {}, {}
    for name, label in [('@deepseek-ai/dsh', 'dsh'), ('@deepseek-harness-tui/dsh-tui', 'tui')]:
        versions[label] = read(modules / name / 'package.json')['version']
    for name in ('@deepseek-ai/dsh-llm', '@deepseek-ai/dsh-llm-deepseek'):
        hashes[name] = hashlib.sha256((modules / name / 'lib/index.js').read_bytes()).hexdigest()
    return {'runtime_versions': versions, 'runtime_fingerprint': digest({'versions': versions, 'hashes': hashes})}


def cache_file(template):
    return Path(template['runtime']).parent / 'evidence/effort-capabilities.json'


def _literal_assignment(tree, name):
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            return ast.literal_eval(node.value)
    raise ValueError('Missing native reasoning declaration')


def hermes_effort_capability(template):
    """Read the native custom-route vocabulary and default as data, never import it.

    Named key_cmd chat providers use CustomProfile. Intersect its actual wire
    levels with the CLI parser, including its explicit disabled spellings. An
    internal level clamped to another wire value is not a separate slider step.
    """
    source = Path(template['hermes_source'])
    cli = ast.parse((source / 'hermes_constants.py').read_text())
    wire = ast.parse((source / 'agent/reasoning_effort.py').read_text())
    custom = ast.parse((source / 'plugins/model-providers/custom/__init__.py').read_text())
    levels = _literal_assignment(cli, 'VALID_REASONING_EFFORTS')
    parser = next(node for node in cli.body if isinstance(node, ast.FunctionDef) and node.name == 'parse_reasoning_effort')
    disabled = next(ast.literal_eval(node.test.comparators[0]) for node in parser.body
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name) and node.test.left.id == parser.args.args[0].arg
        and len(node.test.ops) == 1 and isinstance(node.test.ops[0], ast.In)
        and isinstance(node.test.comparators[0], (ast.Set, ast.Tuple, ast.List))
        and len(node.body) == 1 and isinstance(node.body[0], ast.Return)
        and ast.literal_eval(node.body[0].value) == {'enabled': False})
    profile = next(node for node in custom.body if isinstance(node, ast.ClassDef) and node.name == 'CustomProfile')
    methods = {node.name: node for node in profile.body if isinstance(node, ast.FunctionDef)}
    def returned(name):
        body = [node for node in methods[name].body if not (isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str))]
        if len(body) != 1 or not isinstance(body[0], ast.Return):
            raise ValueError('Native reasoning policy changed')
        return body[0].value
    vocabulary = returned('supported_reasoning_efforts')
    if not isinstance(vocabulary, ast.Name):
        raise ValueError('Native wire vocabulary changed')
    supported = _literal_assignment(wire, vocabulary.id)
    default_config = ast.literal_eval(returned('default_reasoning_config'))
    if (not isinstance(levels, (tuple, list)) or not isinstance(supported, (tuple, list))
            or not all(isinstance(value, str) and value for value in (*levels, *supported))
            or not isinstance(default_config, dict) or default_config.get('enabled') is not True):
        raise ValueError('Invalid native reasoning metadata')
    efforts = list(dict.fromkeys(value for value in supported if value in (*levels, *disabled)))
    default = default_config.get('effort')
    if not efforts or default not in efforts:
        raise ValueError('Unknown native reasoning default')
    # The profile can retain a global effort or per-model override. Do not read
    # user state or pretend the native fallback is the effective launch default.
    return {'efforts': efforts, 'default_effort': None, 'effort_source': 'harness'}


# Reviewed Hermes d923a5eb manual policy, without docstrings or source locations.
# A semantic change requires another wire review; formatting/comments do not.
_HERMES_MANUAL_POLICY = {
    'agent/anthropic_adapter.py': {
        '_thinking_kwargs': '34840bc66ceb64e1115adf505d7d0a55862eeb742198b8725022748ff294b042',
        '_supports_adaptive_thinking': '70240bf34f91c1d15b4378d14be8005ef481568d4283dfc7579b6ba616ebdc64',
        '_is_claude_model': '81dcfae56af3d09d3571148272c5bd61efe3e81d031b534eac093c211a98a6f5',
        '_is_pre_thinking_haiku': '937f2597defc085df106c61221705f61005749161fc73514e58808857767204b',
        'build_anthropic_kwargs': '8daacc10dc8f2624b4f1eb2b505fe9e6885891f54e295a6eb2355f8e82fdc909',
    },
    'agent/anthropic_endpoints.py': {
        '_model_name_is_kimi_family': 'd844d2cf8783162b8c6bb786288632a265dea8ef7cf58170edd72d7e774b80bf',
    },
    'hermes_constants.py': {
        'parse_reasoning_effort': 'eebf29640f745d1a9d965860d95b1b7fdd7959687b3da4ecf1037d21616b9b4f',
    },
}


def _native_ast_data(node):
    # ast.dump's empty-field defaults differ between supported Python versions.
    if isinstance(node, ast.AST):
        return [type(node).__name__, [(key, _native_ast_data(value)) for key, value in ast.iter_fields(node)
                                     if value is not None and value != []]]
    if isinstance(node, list):
        return [_native_ast_data(value) for value in node]
    return node


def _native_declaration(tree, name):
    declarations = [node.value for node in tree.body if
        isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
        or isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name]
    if len(declarations) != 1:
        raise ValueError('Ambiguous native reasoning declaration')
    return declarations[0]


def hermes_manual_effort_capability(template, model):
    """Project checked manual budgets, never execute a native policy or read settings.

    Adaptive/Claude and Haiku policies are outside this projection. The selected
    model, not the profile's primary model, decides which branch is eligible.
    """
    source = Path(template['hermes_source'])
    trees = {}
    for filename, policies in _HERMES_MANUAL_POLICY.items():
        tree = trees[filename] = ast.parse((source / filename).read_text())
        for name, expected in policies.items():
            functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
            if len(functions) != 1:
                raise ValueError('Missing native manual policy')
            body = functions[0].body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                body = body[1:]
            normalized = json.dumps(_native_ast_data(body), separators=(',', ':'))
            if hashlib.sha256(normalized.encode()).hexdigest() != expected:
                raise ValueError('Native manual policy changed')
    endpoint = trees['agent/anthropic_endpoints.py']
    prefixes = ast.literal_eval(_native_declaration(endpoint, '_KIMI_FAMILY_MODEL_PREFIXES'))
    exact_node = _native_declaration(endpoint, '_KIMI_FAMILY_EXACT_SLUGS')
    if not (isinstance(exact_node, ast.Call) and isinstance(exact_node.func, ast.Name)
            and exact_node.func.id == 'frozenset' and len(exact_node.args) == 1 and not exact_node.keywords):
        raise ValueError('Native model family policy changed')
    exact = ast.literal_eval(exact_node.args[0])
    if (not isinstance(prefixes, tuple) or not isinstance(exact, (set, tuple, list))
            or not all(isinstance(v, str) and v for v in (*prefixes, *exact))):
        raise ValueError('Invalid native model families')
    # Normalization only strips anthropic/ on non-Claude names; it does not
    # change these predicates. Exclude every Haiku, including future variants.
    name = model.lower()
    slug = model.strip().lower().rsplit('/', 1)[-1]
    if not slug or 'claude' in name or 'haiku' in name or slug in exact or slug.startswith(prefixes):
        return dict(UNKNOWN)
    budgets = ast.literal_eval(_native_declaration(trees['agent/anthropic_adapter.py'], 'THINKING_BUDGET'))
    levels = ast.literal_eval(_native_declaration(trees['hermes_constants.py'], 'VALID_REASONING_EFFORTS'))
    if (not isinstance(budgets, dict) or not budgets or not isinstance(levels, (tuple, list))
            or not all(isinstance(v, str) and v for v in levels)
            or not all(isinstance(k, str) and k in levels and type(v) is int and v > 0 for k, v in budgets.items())
            or len(set(budgets.values())) != len(budgets)):
        raise ValueError('Invalid native manual budgets')
    # No synthesized None/Off: on this path native disabled means omission.
    # Names missing from THINKING_BUDGET fall back to 8000, not extra tiers.
    return {'efforts': sorted(budgets, key=budgets.get), 'default_effort': None, 'effort_source': 'harness'}


def load_effort_capability(template, *, harness, backend_id, model, backend_kind=None):
    """Read checked metadata only. No process, provider or credential calls."""
    if harness == 'hermes':
        try:
            if backend_id not in template.get('profiles', {}) or not isinstance(model, str) or not model:
                return dict(UNKNOWN)
            path = Path(__file__).resolve().parents[1] / 'integrations/hermes/thin_config.py'
            spec = importlib.util.spec_from_file_location('_orrery_hermes_route_projection', path)
            projection = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(projection)
            kind_args = {'kind': backend_kind} if backend_kind is not None else {}
            kind = CONNECTIONS['connection_kind'](backend_id, **kind_args)
            mode = projection.profile_route(template['profiles'][backend_id], kind)['route']['api_mode']
            if mode == 'chat_completions':
                return hermes_effort_capability(template)
            if mode == 'anthropic_messages':
                return hermes_manual_effort_capability(template, model)
            return dict(UNKNOWN)
        except (OSError, ValueError, KeyError, TypeError, SyntaxError, StopIteration, UnicodeError, ImportError, AttributeError):
            return dict(UNKNOWN)
    if harness != 'dsh':
        return dict(UNKNOWN)
    try:
        backend = read(template['backend_file'])['backends'][backend_id]
        row_input = model_input(backend_id, backend)
        facts, cached = runtime_facts(template), read(cache_file(template))
        if (model != row_input['model'] or cached['schema_version'] != 1 or cached['harness'] != harness
                or cached['runtime_fingerprint'] != facts['runtime_fingerprint']):
            return dict(UNKNOWN)
        rows = [r for r in cached['entries'] if r['backend_id'] == backend_id and r['model'] == model
                and r['model_fingerprint'] == row_input['model_fingerprint']]
        if len(rows) != 1 or rows[0]['source'] != 'harness':
            return dict(UNKNOWN)
        efforts, default = rows[0]['efforts'], rows[0]['default_effort']
        if (not isinstance(efforts, list) or not all(isinstance(v, str) and v.strip() for v in efforts)
                or len(set(efforts)) != len(efforts) or default is not None and default not in efforts):
            return dict(UNKNOWN)
        return {'efforts': list(efforts), 'default_effort': default, 'effort_source': 'harness'}
    except (OSError, ValueError, KeyError, TypeError):
        return dict(UNKNOWN)


def refresh_dsh_capabilities(repo, current, config_file=None):
    """Installer-owned runtime evidence. Read only selected non-secret model facts."""
    target = cache_file(current)
    cached = {'schema_version': 1, 'harness': 'dsh', 'entries': [],
              'runtime_fingerprint': None, 'runtime_versions': {},
              'generated_at': datetime.now(timezone.utc).isoformat(), 'extraction_status': 'unknown'}
    try:
        cached.update(runtime_facts(current))
        entries = []
        if config_file:
            manifest = CONNECTIONS['read_manifest'](config_file)
            if manifest.get('enabled') is True and 'dsh' in manifest.get('harnesses', {}):
                template = read(manifest['harnesses']['dsh']['config_file'])
                configured = read(template['backend_file'])['backends']
                for backend_id, kind, public in CONNECTIONS['declared_connections'](manifest):
                    if backend_id not in configured:
                        continue
                    row = model_input(backend_id, configured[backend_id])
                    if row['model'] == public.get('model'):
                        entries.append(row)
        if entries:
            with tempfile.TemporaryDirectory(prefix='effort-capabilities-') as home:
                # Never inherit Keychain hooks, provider keys, NODE_OPTIONS or Mail binding.
                env = {'HOME': home, 'PATH': os.defpath}
                completed = subprocess.run([current['node'], str(Path(repo) / 'integrations/dsh/capabilities.mjs')],
                    input=json.dumps({'runtime': current['runtime'], 'entries': entries}),
                    text=True, capture_output=True, check=True, timeout=30, env=env, cwd=home)
            result = json.loads(completed.stdout)
            if result.get('forwarded') != 0 or result.get('network_attempts') != 0:
                raise ValueError('Capability extraction attempted network')
            cached['entries'] = result['entries']
        cached['extraction_status'] = 'known'
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        # A normal update/import failure must not keep old known metadata alive.
        cached['entries'] = []
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(cached, indent=2) + '\n')
    target.chmod(0o600)
    return cached
