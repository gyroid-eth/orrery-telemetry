"""Pinned, separately built Hermes TUI; runtime validation never installs tools."""
import hashlib
import json
import os
from pathlib import Path

BASE = 'd923a5eb487a17c0a68801bba9ef0f56c81040a8'
WORKSPACES = ('ui-tui', 'apps/shared')
INPUTS = ('package.json', 'package-lock.json', 'pm/lock.json',
          'ui-tui/package.json', 'apps/shared/package.json',
          'scripts/build/node-deps.mjs', 'scripts/build/tui.mjs',
          'scripts/build/frontend-common.mjs', 'scripts/build/freshness.mjs')


def recipe_id():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def checked(path, root):
    path, root = Path(path), Path(root).resolve()
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
        raise ValueError('Hermes TUI path escapes its generation or is missing')
    return path.resolve()


def fingerprint(path, root):
    path = checked(path, root)
    data = path.read_bytes()
    return {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}


def inputs(source):
    result = {name: fingerprint(source / name, source) for name in INPUTS}
    for name in ('.npmrc', 'tsconfig.json', 'ui-tui/tsconfig.json', 'apps/shared/tsconfig.json',
                 'ui-tui/packages/hermes-ink/package.json', 'ui-tui/packages/hermes-ink/tsconfig.json'):
        path = source / name
        result[name] = fingerprint(path, source) if path.exists() or path.is_symlink() else None
    # Bind the actual source trees consumed by the official compiler too.
    for directory in ('ui-tui/src', 'ui-tui/packages/hermes-ink/src', 'apps/shared/src'):
        base = source / directory
        if not base.is_dir() or base.is_symlink():
            raise ValueError('Hermes TUI source tree is missing')
        for path in sorted(base.rglob('*')):
            if path.is_symlink():
                raise ValueError('Hermes TUI source link is unsupported')
            if path.is_file():
                result[str(path.relative_to(source))] = fingerprint(path, source)
    return result


def tools(root, source, target):
    """Use the existing PM POSIX layout and lock, never ambient node/npm."""
    lock = read(checked(source / 'pm/lock.json', root))['packages']
    facts = read(checked(root / 'tools/facts.json', root))['packages']
    result = {}
    for name, relative in (('node', 'bin/node'), ('npm', 'lib/node_modules/npm/bin/npm-cli.js')):
        fact, pin = facts[name], lock[name]
        entry = Path(fact['entry'])
        artifact = pin['artifacts'].get(target, pin['artifacts'].get('any'))
        if (entry.is_absolute() or '..' in entry.parts or not entry.parts
                or fact.get('target') != target or fact.get('version') != pin['version']
                or fact.get('artifacts') != [artifact['sha256']]):
            raise ValueError('Hermes TUI PM tool identity changed')
        path = checked(root / 'tools' / entry / relative, root / 'tools')
        result[name] = {'path': str(path), 'entry': str(entry), 'version': pin['version'],
                        'artifacts': fact['artifacts'], **fingerprint(path, root)}
    if not os.access(result['node']['path'], os.X_OK):
        raise ValueError('Hermes TUI Node is not executable')
    npm_manifest = checked(Path(result['npm']['path']).parent.parent / 'package.json', root)
    if read(npm_manifest).get('version') != result['npm']['version']:
        raise ValueError('Hermes TUI npm version changed')
    result['npm']['manifest'] = fingerprint(npm_manifest, root)
    return result


def outputs(product):
    for name in ('dist/entry.js', 'dist/hermes-build.json', 'package.json'):
        checked(product / name, product)
    if read(product / 'package.json').get('type') != 'module':
        raise ValueError('Hermes TUI product is not an ES module')
    result = {}
    for path in sorted(product.rglob('*')):
        if path.is_symlink():
            raise ValueError('Hermes TUI product link is unsupported')
        if path.is_file():
            result[str(path.relative_to(product))] = fingerprint(path, product)
    if not result['dist/entry.js']['bytes']:
        raise ValueError('Hermes TUI entry is empty')
    return result


def evidence(root, source, target):
    root, source = Path(root).resolve(), Path(source).resolve()
    product = root / 'tui-product'
    if source != root / 'source' or product.is_symlink():
        raise ValueError('Hermes TUI layout changed')
    selected = tools(root, source, target)
    stamp = read(checked(product / 'dist/hermes-build.json', product))
    platform, arch = target.split('-', 1)
    if (stamp.get('schema') != 1 or stamp.get('product') != 'tui'
            or stamp.get('platform') != platform or stamp.get('arch') != arch
            or stamp.get('node') != selected['node']['version']):
        raise ValueError('Hermes TUI build provenance changed')
    return {'recipe': recipe_id(), 'base_commit': BASE, 'target': target,
            'source': str(source), 'product': str(product), 'tools': selected,
            'inputs': inputs(source), 'outputs': outputs(product)}


def build(root, source, target, env, run):
    root, source = Path(root).resolve(), Path(source).resolve()
    selected, before = tools(root, source, target), inputs(source)
    node, npm = selected['node']['path'], selected['npm']['path']
    clean = dict(env, PATH=str(Path(node).parent) + os.pathsep + os.defpath,
                 npm_execpath=npm, npm_config_cache=str(root / 'cache/npm'),
                 npm_config_registry='https://registry.npmjs.org/',
                 npm_config_fetch_retries='0', npm_config_ignore_scripts='true', CI='1')
    for name in ('userconfig', 'globalconfig'):
        path = root / ('npm-' + name)
        path.write_text('')
        clean['npm_config_' + name] = str(path)
    version = json.loads(run([node, '-p', 'JSON.stringify({version:process.version,execPath:process.execPath})'],
                             cwd=source, env=clean))
    if (version.get('version') != 'v' + selected['node']['version']
            or Path(version['execPath']).resolve() != Path(node)):
        raise ValueError('Hermes TUI Node version/execPath mismatch')
    run([node, str(source / 'scripts/build/node-deps.mjs'), '--source', str(source),
         '--workspace', 'ui-tui', '--workspace', 'apps/shared'], cwd=source, env=clean)
    run([node, str(source / 'scripts/build/tui.mjs'), '--source', str(source),
         '--out', str(root / 'tui-product')], cwd=source, env=clean)
    proof = evidence(root, source, target)
    if proof['inputs'] != before or proof['tools'] != selected:
        raise ValueError('Hermes TUI build inputs changed')
    return {'hermes_tui_dir': proof['product'], 'hermes_node': node, 'tui_evidence': proof}


def validate(details):
    proof = details['tui_evidence']
    actual = evidence(Path(details['managed_root']), Path(details['hermes_source']), proof['target'])
    if (actual != proof or details['hermes_tui_dir'] != actual['product']
            or details['hermes_node'] != actual['tools']['node']['path']):
        raise ValueError('Hermes TUI receipt changed')
    return {'HERMES_TUI_DIR': actual['product'], 'HERMES_NODE': details['hermes_node']}


def launch_environment(config):
    """The management selection supplies the receipt, not model/profile paths."""
    root = Path(config['managed_root']).resolve()
    receipt_path = root / '.orrery-managed-install.json'
    if config.get('generation_receipt') != str(receipt_path):
        raise ValueError('Hermes TUI generation receipt missing')
    receipt = read(checked(receipt_path, root))
    details = receipt['details']
    if (receipt.get('complete') is not True or receipt.get('harness') != 'hermes'
            or receipt.get('hermes_commit') != BASE
            or any(config.get(key) != details[key] for key in
                   ('managed_root', 'hermes_source', 'hermes_executable', 'generation_receipt'))):
        raise ValueError('Hermes TUI selection changed')
    return validate(details)
