"""Private TUI products and PM facts; no npm acquisition or Hermes import."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('fixture_tui', ROOT / 'integrations/hermes/tui_product.py')
tui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tui)


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def pins(target='darwin-arm64'):
    return {name: {'version': version, 'artifacts': {('any' if name == 'npm' else target): {'sha256': str(index) * 64}}}
            for index, (name, version) in enumerate((('node', '26.7.0'), ('npm', '12.0.2')), 1)}


def add_source_inputs(source):
    for name in tui.INPUTS:
        path = source / name
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{}' if name.endswith('.json') else '// fixed synthetic builder\n')
    for name in ('ui-tui/src', 'ui-tui/packages/hermes-ink/src', 'apps/shared/src'):
        path = source / name / 'entry.ts'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('export const fixture = 1;\n')


def add_tools(root, source, target='darwin-arm64'):
    facts_path = root / 'tools/facts.json'
    facts = tui.read(facts_path) if facts_path.exists() else {'packages': {}}
    lock = tui.read(source / 'pm/lock.json')['packages']
    for name, relative in (('node', 'bin/node'), ('npm', 'lib/node_modules/npm/bin/npm-cli.js')):
        path = root / 'tools' / (name + '-fixture') / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('synthetic ' + name)
        path.chmod(0o700)
        facts['packages'][name] = {'entry': name + '-fixture', 'version': lock[name]['version'],
            'target': target, 'artifacts': [lock[name]['artifacts'].get(target, lock[name]['artifacts'].get('any'))['sha256']]}
        if name == 'npm':
            save(path.parent.parent / 'package.json', {'version': lock[name]['version']})
    save(facts_path, facts)


def make_product(root, target='darwin-arm64'):
    product = root / 'tui-product'
    (product / 'dist').mkdir(parents=True, exist_ok=True)
    (product / 'dist/entry.js').write_text("console.log('synthetic prebuilt ready');\n")
    save(product / 'package.json', {'type': 'module'})
    platform, arch = target.split('-', 1)
    save(product / 'dist/hermes-build.json', {'schema': 1, 'product': 'tui',
        'platform': platform, 'arch': arch, 'node': '26.7.0'})


def details(root, target='darwin-arm64'):
    proof = tui.evidence(root, root / 'source', target)
    return {'managed_root': str(root), 'hermes_source': str(root / 'source'),
            'hermes_tui_dir': proof['product'], 'hermes_node': proof['tools']['node']['path'],
            'tui_evidence': proof}


@pytest.fixture
def product(tmp_path):
    root = tmp_path / 'generation'
    source = root / 'source'
    add_source_inputs(source)
    save(source / 'pm/lock.json', {'packages': pins()})
    add_tools(root, source)
    make_product(root)
    return root


@pytest.mark.parametrize('damage', ['entry', 'package', 'stamp', 'source', 'builder', 'lock',
                                   'node', 'npm', 'npm-version', 'tool-selection', 'link', 'extra'])
def test_static_validation_rejects_missing_or_changed_product_inputs(product, damage):
    saved = details(product)
    paths = {'entry': 'tui-product/dist/entry.js', 'package': 'tui-product/package.json',
             'stamp': 'tui-product/dist/hermes-build.json', 'source': 'source/ui-tui/src/entry.ts',
             'builder': 'source/scripts/build/tui.mjs', 'lock': 'source/package-lock.json',
             'node': 'tools/node-fixture/bin/node', 'npm': 'tools/npm-fixture/lib/node_modules/npm/bin/npm-cli.js',
             'npm-version': 'tools/npm-fixture/lib/node_modules/npm/package.json',
             'tool-selection': 'tools/facts.json', 'link': 'tui-product/dist/entry.js',
             'extra': 'tui-product/dist/extra.js'}
    path = product / paths[damage]
    if damage == 'entry':path.unlink()
    elif damage == 'link':
        outside = product.parent / 'external-entry.js'
        outside.write_bytes(path.read_bytes());path.unlink();path.symlink_to(outside)
    else:path.write_text('{"changed":true}' if path.suffix == '.json' else 'changed')
    with pytest.raises((ValueError, KeyError, OSError)):
        tui.validate(saved)


@pytest.mark.parametrize('target', ['darwin-arm64', 'linux-x64'])
def test_locked_official_build_uses_only_pm_node_npm_and_private_configuration(tmp_path, target):
    root = tmp_path / 'generation';source = root / 'source'
    add_source_inputs(source);save(source / 'pm/lock.json', {'packages': pins(target)})
    add_tools(root, source, target)
    calls = []
    def run(argv, *, cwd, env):
        calls.append(argv)
        assert cwd == source
        assert env['npm_config_ignore_scripts'] == 'true'
        assert env['npm_execpath'] == str(root / 'tools/npm-fixture/lib/node_modules/npm/bin/npm-cli.js')
        assert Path(env['npm_config_userconfig']).read_text() == ''
        assert Path(env['npm_config_globalconfig']).read_text() == ''
        assert env['npm_config_cache'].startswith(str(root))
        if '-p' in argv:return json.dumps({'version': 'v26.7.0', 'execPath': argv[0]})
        if argv[1].endswith('/tui.mjs'):make_product(root, target)
        return ''
    result = tui.build(root, source, target, {'HOME': str(root / 'home'), 'PATH': os.defpath}, run)
    assert calls[1][1:] == [str(source / 'scripts/build/node-deps.mjs'), '--source', str(source),
                          '--workspace', 'ui-tui', '--workspace', 'apps/shared']
    assert calls[2][1:] == [str(source / 'scripts/build/tui.mjs'), '--source', str(source),
                          '--out', str(root / 'tui-product')]
    assert all(argv[0] == result['hermes_node'] for argv in calls)
    assert tui.validate({**details(root, target), **result})['HERMES_TUI_DIR'] == str(root / 'tui-product')


def test_pinned_launcher_runs_prebuilt_node_without_any_python_workspace(product, tmp_path):
    """Exercise the real fixed upstream branches, with imports/PM deliberately absent."""
    import ast
    # The host check supplies the upstream file; this copy is the exact reviewed
    # fixed launcher functions, not a replacement implementation.
    source = os.environ.get('HERMES_TEST_SOURCE')
    if not source:pytest.skip('set HERMES_TEST_SOURCE for the fixed upstream launcher probe')
    launcher = Path(source) / 'hermes_cli/main_tui_launch.py'
    tree = ast.parse(launcher.read_text())
    names = {'_make_tui_argv', '_tui_node_bin'}
    code = compile(ast.Module(body=[node for node in tree.body if isinstance(node, ast.FunctionDef)
                                    and node.name in names], type_ignores=[]), str(launcher), 'exec')
    node = shutil.which('node')
    if not node:pytest.skip('Node unavailable for private prebuilt probe')
    env = {**os.environ, 'HERMES_TUI_DIR': str(product / 'tui-product'), 'HERMES_NODE': node}
    namespace = {'Path': Path, 'os': type('Env', (), {'environ': env, 'path': os.path, 'access': os.access, 'X_OK': os.X_OK}),
                 'sys': sys, '_find_bundled_tui': lambda: pytest.fail('prebuilt did not win'),
                 '_ensure_tui_workspace': lambda _: pytest.fail('workspace requested')}
    exec(code, namespace)
    missing = tmp_path / 'absent-python-snapshot/ui-tui'
    argv, cwd = namespace['_make_tui_argv'](missing, False)
    assert not missing.exists()
    result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'synthetic prebuilt ready'
