"""Explicit, pinned harness provisioning. No login or provider requests.

Ordinary failed installs must not look ready or claim ownership of user data.
Runtime generations are separate from manual installations and user profiles.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request

HERMES_COMMIT = 'd923a5eb487a17c0a68801bba9ef0f56c81040a8'
HERMES_URL = 'https://github.com/NousResearch/hermes-agent.git'
OWNER = '.orrery-managed-install.json'


def read(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + '\n')
    path.chmod(0o600)


def run(argv, *, cwd, env):
    return subprocess.run(argv, cwd=cwd, env=env, check=True, capture_output=True,
                          text=True, timeout=1800).stdout.strip()


def environment(root):
    # Provider/login variables and inherited npm/git configuration are excluded.
    home = root / 'installer-home'
    home.mkdir(parents=True, exist_ok=True)
    return {'HOME': str(home), 'PATH': os.defpath, 'LANG': 'en_US.UTF-8',
            'HERMES_HOME': str(root), 'UV_CACHE_DIR': str(root / 'cache/uv'),
            'UV_PYTHON_INSTALL_DIR': str(root / 'python'),
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_TERMINAL_PROMPT': '0'}


def install_dsh(repo, root, node, cache=None):
    node = Path(node).expanduser()
    if not node.is_absolute() or not node.is_file() or not os.access(node, os.X_OK):
        raise ValueError('dsh requires --dsh-node with an absolute Node executable (>=24.2)')
    node = node.resolve()
    env = environment(root)
    env['PATH'] = str(node.parent) + os.pathsep + os.defpath
    proof = json.loads(run([str(node), '-p', 'JSON.stringify({version:process.version,execPath:process.execPath})'], cwd=root, env=env))
    match = re.fullmatch(r'v(\d+)\.(\d+)\.(\d+)', proof['version'])
    if not match or tuple(map(int, match.groups())) < (24, 2, 0) or Path(proof['execPath']).resolve() != node:
        raise ValueError('Node version/execPath does not match the pinned executable')
    proof['sha256'] = hashlib.sha256(node.read_bytes()).hexdigest()
    pins = read(repo / 'integrations/dsh/runtime-pins.json')
    evidence = root / 'evidence'
    evidence.mkdir()
    for item in pins:
        target = evidence / item['file']
        if cache:
            shutil.copy2(Path(cache) / item['file'], target)
        else:
            with urllib.request.urlopen(item['url'], timeout=60) as response:
                target.write_bytes(response.read())
        digest = 'sha512-' + base64.b64encode(hashlib.sha512(target.read_bytes()).digest()).decode()
        if digest != item['integrity']:
            raise ValueError('dsh tarball sha512 mismatch')
    runtime = root / 'runtime'
    runtime.mkdir()
    lock = repo / 'integrations/dsh/runtime-package-lock.json'
    shutil.copy2(lock, runtime / 'package-lock.json')
    save(runtime / 'package.json', {'dependencies': read(lock)['packages']['']['dependencies']})
    user_config, global_config = root / 'empty-user.npmrc', root / 'empty-global.npmrc'
    user_config.write_text('')
    global_config.write_text('')
    npm = node.parent.parent / 'lib/node_modules/npm/bin/npm-cli.js'
    common = [str(node), str(npm), 'ci', '--ignore-scripts', '--no-audit', '--no-fund',
              '--registry=https://registry.npmjs.org/', '--fetch-retries=0',
              '--userconfig=' + str(user_config), '--globalconfig=' + str(global_config),
              '--cache=' + str(root / 'npm-cache')]
    if cache:
        common += ['--offline', '--cache=' + str(Path(cache) / 'npm-cache')]
    run(common, cwd=runtime, env=env)
    # The TUI bundles workspace:* packages. Asking npm to resolve its entire
    # manifest fails; preserve the verified bundle and add only pinned UI deps.
    tui = runtime / 'node_modules/@deepseek-harness-tui/dsh-tui'
    tui.mkdir(parents=True)
    tui_pin = next(item for item in pins if item['name'] == '@deepseek-harness-tui/dsh-tui')
    with tarfile.open(evidence / tui_pin['file']) as archive:
        archive.extractall(tui.parent / '.tui-unpack', filter='data')
    shutil.copytree(tui.parent / '.tui-unpack/package', tui, dirs_exist_ok=True, symlinks=True)
    shutil.rmtree(tui.parent / '.tui-unpack')
    ui = root / 'ui-deps'
    ui.mkdir()
    for name in ('package.json', 'package-lock.json'):
        shutil.copy2(repo / ('integrations/dsh/ui-' + name), ui / name)
    run(common + ['--legacy-peer-deps'], cwd=ui, env=env)
    for package in read(ui / 'package.json')['dependencies']:
        destination = tui / 'node_modules' / package
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.symlink_to(ui / 'node_modules' / package, target_is_directory=True)
    for item in pins:
        package = runtime / 'node_modules' / item['name']
        if read(package / 'package.json')['version'] != item['version']:
            raise ValueError('Installed dsh/TUI version mismatch')
    recipe_file = repo / 'integrations/dsh/tui-managed.patch'
    changes = {}
    for row in read(recipe_file)['replacements']:
        target = tui / row['file']
        text = target.read_text()
        if text.count(row['old']) != 1:
            raise ValueError('Pinned TUI patch preimage mismatch')
        target.write_text(text.replace(row['old'], row['new'], 1))
        changes[row['file']] = hashlib.sha256(target.read_bytes()).hexdigest()
    save(evidence / 'tui-patch-applied.json', {
        'recipe_sha256': hashlib.sha256(recipe_file.read_bytes()).hexdigest(),
        'changes': [{'file': name, 'after': digest} for name, digest in changes.items()]})
    save(evidence / 'node-runtime.json', proof)
    return {'node': str(node), 'runtime': str(runtime), 'patch_receipt': str(evidence / 'tui-patch-applied.json')}


def install_hermes(repo, root, python, cache=None):
    env = environment(root)
    save(root / 'config.yaml', {'auth': {'adopt_external_logins': False}, 'updates': {'check': False},
         'telemetry': {'shared_metrics': {'enabled': False, 'send': False}}})
    source = root / 'source'
    run(['git', 'clone', '--no-checkout', str(Path(cache) / 'hermes-agent') if cache else HERMES_URL, str(source)], cwd=root, env=env)
    run(['git', 'checkout', '--detach', HERMES_COMMIT], cwd=source, env=env)
    if run(['git', 'rev-parse', 'HEAD'], cwd=source, env=env) != HERMES_COMMIT:
        raise ValueError('Hermes commit mismatch')
    # Existing PM owns its own fixed runtime/dependency graph; no general setup,
    # login import, browser, or computer-use installer is invoked.
    run([python, '-m', 'pm.cli', 'install', '--without', 'agent-browser',
         '--without', 'cua-driver', '--extra', 'anthropic'], cwd=source, env=env)
    key = hashlib.sha256(str(source.resolve()).encode()).hexdigest()[:16]
    install = root / 'installs' / key
    generation = Path(read(install / 'facts.json')['packages']['venv']['environment']).resolve()
    executable = generation / 'bin/hermes'
    if not generation.is_relative_to((install / 'environments').resolve()) or not executable.is_file():
        raise ValueError('Hermes PM did not publish a committed runtime')
    print('Hermes: existing manual homes/logins are untouched; no credentials were installed.')
    return {'hermes_source': str(source), 'managed_root': str(root), 'hermes_executable': str(executable)}


def generation_id(repo, harness):
    if harness == 'hermes':
        return HERMES_COMMIT[:16] + '-v1'
    inputs = ['runtime-pins.json', 'runtime-package-lock.json', 'ui-package.json',
              'ui-package-lock.json', 'tui-managed.patch']
    digest = hashlib.sha256(b''.join((repo / 'integrations/dsh' / name).read_bytes() for name in inputs)).hexdigest()[:16]
    return 'dsh-0.2.1-rc.2-tui-0.16.1-' + digest


def validate(repo, harness, receipt):
    if receipt.get('complete') is not True or receipt.get('harness') != harness:
        raise ValueError('Managed installation receipt is incomplete')
    details = receipt['details']
    if harness == 'dsh':
        runtime = Path(details['runtime'])
        proof = read(runtime.parent / 'evidence/node-runtime.json')
        node = Path(details['node'])
        if not os.access(node, os.X_OK) or hashlib.sha256(node.read_bytes()).hexdigest() != proof['sha256']:
            raise ValueError('Pinned Node executable changed')
        for item in read(repo / 'integrations/dsh/runtime-pins.json'):
            if read(runtime / 'node_modules' / item['name'] / 'package.json')['version'] != item['version']:
                raise ValueError('Installed dsh/TUI version mismatch')
        patch = read(details['patch_receipt'])
        recipe_file = repo / 'integrations/dsh/tui-managed.patch'
        files = {row['file'] for row in read(recipe_file)['replacements']}
        if (patch['recipe_sha256'] != hashlib.sha256(recipe_file.read_bytes()).hexdigest()
                or sorted(row['file'] for row in patch['changes']) != sorted(files)):
            raise ValueError('Managed TUI patch receipt mismatch')
        for row in patch['changes']:
            target = runtime / 'node_modules/@deepseek-harness-tui/dsh-tui' / row['file']
            if hashlib.sha256(target.read_bytes()).hexdigest() != row['after']:
                raise ValueError('Managed TUI patch changed')
    else:
        root = Path(details['managed_root'])
        source = Path(details['hermes_source'])
        env = environment(root)
        if run(['git', 'rev-parse', 'HEAD'], cwd=source, env=env) != HERMES_COMMIT:
            raise ValueError('Hermes commit mismatch')
        key = hashlib.sha256(str(source.resolve()).encode()).hexdigest()[:16]
        install = root / 'installs' / key
        generation = Path(read(install / 'facts.json')['packages']['venv']['environment']).resolve()
        if (not generation.is_relative_to((install / 'environments').resolve())
                or Path(details['hermes_executable']) != generation / 'bin/hermes'
                or not (generation / 'pyvenv.cfg').is_file()
                or not os.access(generation / 'bin/hermes', os.X_OK)
                or not os.access(generation / 'bin/python', os.X_OK)):
            raise ValueError('Hermes committed runtime is missing or changed')


def provision(repo, install, harness, choice='keep', node='', python=sys.executable,
              cache=None, dry_run=False):
    repo, install = Path(repo).resolve(), Path(install).resolve()
    setting = install / 'dashboard' / (harness + '-provider.json')
    enabled = choice == 'enable' or choice == 'keep' and setting.is_file() and read(setting).get('enabled') is True
    if dry_run:
        print(f'DRY-RUN managed {harness}: enabled={bool(enabled)}; no credentials')
        return
    if not enabled:
        if choice == 'disable':
            save(setting, {'enabled': False})
        return
    base = install / 'harnesses' / harness
    root = base / generation_id(repo, harness)
    if root.exists():
        if not (root / OWNER).is_file() or read(root / OWNER).get('harness') != harness:
            raise ValueError('Runtime generation exists without installer ownership')
        receipt = read(root / OWNER)
        validate(repo, harness, receipt)
    else:
        root.mkdir(parents=True, mode=0o700)
        try:
            profiles = base / 'profiles'
            profiles.mkdir(mode=0o700, exist_ok=True)
            # User data is outside the owned runtime generation. PM/runner still
            # see the expected HERMES_HOME/profiles path.
            (root / 'profiles').symlink_to(profiles, target_is_directory=True)
            details = (install_dsh(repo, root, node, cache) if harness == 'dsh'
                       else install_hermes(repo, root, python, cache))
            receipt = {'harness': harness, 'complete': True, 'details': details,
                       'hermes_commit': HERMES_COMMIT if harness == 'hermes' else None}
            validate(repo, harness, receipt)
            save(root / OWNER, receipt)
        except Exception:
            shutil.rmtree(root)
            raise
    destination = install / 'integrations' / harness
    destination.mkdir(parents=True, exist_ok=True)
    for file in (repo / 'integrations' / harness).iterdir():
        if file.is_file() and file.suffix in ('.py', '.mjs', '.json', '.patch', '.yml', '.md'):
            shutil.copy2(file, destination / file.name)
    save(base / 'current.json', {'generation': root.name, **receipt['details']})
    save(setting, {'enabled': True})
    print(f'Managed {harness} installed; configure backend and Keychain service/account separately (no key installed).')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--install', required=True)
    parser.add_argument('--harness', choices=['dsh', 'hermes'], required=True)
    parser.add_argument('--choice', choices=['enable', 'disable', 'keep'], default='keep')
    parser.add_argument('--node', default='')
    parser.add_argument('--python', default=sys.executable)
    parser.add_argument('--cache')
    parser.add_argument('--dry-run', action='store_true')
    args = vars(parser.parse_args())
    try:
        provision(**args)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print('Managed harness install failed: ' + str(error), file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            # Acquisition uses a clean environment/private HOME and no keys.
            print(error.stderr[-4000:], file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
