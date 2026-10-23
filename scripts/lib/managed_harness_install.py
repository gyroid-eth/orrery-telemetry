"""Explicit, pinned harness provisioning. No login or provider requests.

Ordinary failed installs must not look ready or claim ownership of user data.
Runtime generations are separate from manual installations and user profiles.
"""
import argparse
import base64
import hashlib
import importlib.util
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
UPSTREAM_PROOF_FILES = ('lib/types/utils/paths.js', 'lib/types/screens/Chat.js')
HERMES_STAMP_SCRIPT = '''import sys
from hermes_cli.version_info import get_version_info
from scripts.write_install_stamp import write_stamp
info = get_version_info()
if info.commit != sys.argv[1]:
    raise ValueError('Hermes version provenance does not match the pinned commit')
if info.dirty is not True:
    raise ValueError('Hermes CA inputs must be recorded as a dirty source build')
write_stamp('install-stamp.json', update_mechanism='external', commit=info.commit,
    branch=info.branch, dirty=info.dirty, commit_date=info.commit_date,
    base_version=None if info.base_version == 'unknown' else info.base_version,
    display_version=info.derived_version, distance=info.distance, source='local')
'''
HERMES_TARGET_SCRIPT = '''import platform,sys
arch = {'arm64':'arm64','aarch64':'arm64','x86_64':'x64','amd64':'x64'}.get(platform.machine().lower())
os_name = 'darwin' if sys.platform == 'darwin' else 'linux' if sys.platform.startswith('linux') and not hasattr(sys,'getandroidapilevel') and platform.libc_ver()[0] == 'glibc' else None
if not arch or not os_name:
    raise SystemExit('Hermes CA recipe supports macOS and GNU/Linux only; Bionic is unsupported')
print(os_name + '-' + arch)
'''
HERMES_CA_PROBE_SCRIPT = '''import certifi,importlib.metadata,json
from pathlib import Path
d=importlib.metadata.distribution('certifi')
metadata=next(f for f in d.files if str(f).endswith('.dist-info/METADATA'))
print(json.dumps({'bundle':str(Path(certifi.where()).resolve()),'metadata':str(Path(d.locate_file(metadata)).resolve())}))
'''


def hermes_ca():
    path = Path(__file__).resolve().parents[2] / 'integrations/hermes/ca_recipe.py'
    spec = importlib.util.spec_from_file_location('_managed_hermes_ca', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def hermes_stamp_details(source):
    path = Path(source) / 'install-stamp.json'
    stamp = read(path)
    if (stamp.get('commit') != HERMES_COMMIT or stamp.get('source') != 'local'
            or stamp.get('updateMechanism') != 'external' or stamp.get('payload') != 'bootstrap'
            or stamp.get('runtimeDir') is not None):
        raise ValueError('Hermes install stamp does not match the owned source layout')
    return {'install_stamp': str(path), 'install_stamp_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


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


def install_dsh(repo, root, node, cache=None, *, datadir=True):
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
    save(evidence / 'node-runtime.json', proof)
    # Isolate only the TUI data root; keep the rest of upstream untouched.
    variant, recipe_sha = 'plain', None
    if datadir:
        recipe_file = repo / 'integrations/dsh/tui-datadir.patch.json'
        recipe = read(recipe_file)
        target = tui / recipe['file']
        text = target.read_text()
        if text.count(recipe['old']) != 1:
            raise ValueError('Pinned TUI data-dir patch preimage mismatch')
        target.write_text(text.replace(recipe['old'], recipe['new'], 1))
        variant = 'plain-datadir'
        recipe_sha = hashlib.sha256(recipe_file.read_bytes()).hexdigest()
    receipt = evidence / 'upstream-runtime.json'
    save(receipt, {'variant': variant, 'source_patch': 'tui-datadir-v1' if datadir else None,
         'recipe_sha256': recipe_sha,
         'files': [{'file': name, 'sha256': hashlib.sha256((tui / name).read_bytes()).hexdigest()}
                   for name in UPSTREAM_PROOF_FILES]})
    return {'node': str(node), 'runtime': str(runtime), 'runtime_receipt': str(receipt),
            'variant': variant, 'thin_config_contract': 1}


def install_hermes(repo, root, python, cache=None):
    env = environment(root)
    save(root / 'config.yaml', {'auth': {'adopt_external_logins': False}, 'updates': {'check': False},
         'telemetry': {'shared_metrics': {'enabled': False, 'send': False}}})
    source = root / 'source'
    run(['git', 'clone', '--no-checkout', str(Path(cache) / 'hermes-agent') if cache else HERMES_URL, str(source)], cwd=root, env=env)
    run(['git', 'checkout', '--detach', HERMES_COMMIT], cwd=source, env=env)
    if run(['git', 'rev-parse', 'HEAD'], cwd=source, env=env) != HERMES_COMMIT:
        raise ValueError('Hermes commit mismatch')
    ca = hermes_ca()
    recipe = ca.apply(repo, source)
    policy, _, _ = ca.load(repo)
    target = run([python, '-I', '-B', '-c', HERMES_TARGET_SCRIPT], cwd=source, env=env)
    ca.target_artifact(policy, target, source)
    # Resolve provenance only at install time using the pinned upstream writer.
    # source=local avoids its git-stamp live-HEAD probe during runtime import;
    # external keeps updates owned by this installer, without changing PM paths.
    run([python, '-c', HERMES_STAMP_SCRIPT, HERMES_COMMIT], cwd=source, env=env)
    stamp = hermes_stamp_details(source)
    # Existing PM owns its own fixed runtime/dependency graph; no general setup,
    # login import, browser, or computer-use installer is invoked.
    # PM rejects --without combined with --extra. First prepare its default
    # closure and persist the optional-tool opt-outs, then add the provider SDK.
    for arguments in (['--without', 'agent-browser', '--without', 'cua-driver'], ['--extra', 'anthropic']):
        ca.verify_inputs(source, policy, 'after')
        run([python, '-m', 'pm.cli', 'install', *arguments], cwd=source, env=env)
        ca.verify_inputs(source, policy, 'after')
    key = hashlib.sha256(str(source.resolve()).encode()).hexdigest()[:16]
    install = root / 'installs' / key
    generation = Path(read(install / 'facts.json')['packages']['venv']['environment']).resolve()
    executable = generation / 'bin/hermes'
    if not generation.is_relative_to((install / 'environments').resolve()) or not executable.is_file():
        raise ValueError('Hermes PM did not publish a committed runtime')
    paths = json.loads(run([str(generation / 'bin/python'), '-I', '-B', '-c', HERMES_CA_PROBE_SCRIPT], cwd=source, env=env))
    proof = ca.runtime_evidence(repo, root, source, generation, target, paths)
    print('Hermes: existing manual homes/logins are untouched; no credentials were installed.')
    return {'hermes_source': str(source), 'managed_root': str(root), 'hermes_executable': str(executable),
            'variant': 'plain', 'thin_config_contract': 1, 'ca_recipe': recipe, 'ca_evidence': proof, **stamp}


def generation_id(repo, harness, *, datadir=True):
    if harness == 'hermes':
        return HERMES_COMMIT[:16] + '-ca1-' + hermes_ca().load(repo)[2][:16]
    inputs = ['runtime-pins.json', 'runtime-package-lock.json', 'ui-package.json', 'ui-package-lock.json']
    if datadir:
        inputs.append('tui-datadir.patch.json')
    digest = hashlib.sha256(b''.join((repo / 'integrations/dsh' / name).read_bytes() for name in inputs)).hexdigest()[:16]
    return 'dsh-0.2.1-rc.2-tui-0.16.1-' + digest + ('-plain-datadir1' if datadir else '-plain1')


def validate(repo, harness, receipt):
    if receipt.get('complete') is not True or receipt.get('harness') != harness:
        raise ValueError('Managed installation receipt is incomplete')
    details = receipt['details']
    if harness == 'dsh':
        if receipt.get('variant') not in ('plain', 'plain-datadir'):
            raise ValueError('Legacy managed dsh runtime is unsupported; existing files were retained')
        runtime = Path(details['runtime'])
        proof = read(runtime.parent / 'evidence/node-runtime.json')
        node = Path(details['node'])
        if not os.access(node, os.X_OK) or hashlib.sha256(node.read_bytes()).hexdigest() != proof['sha256']:
            raise ValueError('Pinned Node executable changed')
        for item in read(repo / 'integrations/dsh/runtime-pins.json'):
            if read(runtime / 'node_modules' / item['name'] / 'package.json')['version'] != item['version']:
                raise ValueError('Installed dsh/TUI version mismatch')
        source = read(details['runtime_receipt'])
        if (receipt.get('thin_config_contract') != 1 or source.get('variant') != receipt['variant']
                or source.get('source_patch') != ('tui-datadir-v1' if receipt['variant'] == 'plain-datadir' else None) or not source.get('files')):
            raise ValueError('Plain runtime receipt is incomplete')
        if receipt['variant'] == 'plain-datadir' and source.get('recipe_sha256') != hashlib.sha256(
                (repo / 'integrations/dsh/tui-datadir.patch.json').read_bytes()).hexdigest():
            raise ValueError('TUI data-dir patch receipt mismatch')
        for row in source['files']:
            relative = Path(row['file'])
            if relative.is_absolute() or '..' in relative.parts:
                raise ValueError('Plain runtime path is invalid')
            target = runtime / 'node_modules/@deepseek-harness-tui/dsh-tui' / relative
            if hashlib.sha256(target.read_bytes()).hexdigest() != row['sha256']:
                raise ValueError('Plain upstream runtime changed')
        return
    else:
        root = Path(details['managed_root'])
        source = Path(details['hermes_source'])
        env = environment(root)
        if run(['git', 'rev-parse', 'HEAD'], cwd=source, env=env) != HERMES_COMMIT:
            raise ValueError('Hermes commit mismatch')
        if any(details.get(key) != value for key, value in hermes_stamp_details(source).items()):
            raise ValueError('Hermes install stamp receipt changed')
        key = hashlib.sha256(str(source.resolve()).encode()).hexdigest()[:16]
        install = root / 'installs' / key
        generation = Path(read(install / 'facts.json')['packages']['venv']['environment']).resolve()
        if (not generation.is_relative_to((install / 'environments').resolve())
                or Path(details['hermes_executable']) != generation / 'bin/hermes'
                or not (generation / 'pyvenv.cfg').is_file()
                or not os.access(generation / 'bin/hermes', os.X_OK)
                or not os.access(generation / 'bin/python', os.X_OK)):
            raise ValueError('Hermes committed runtime is missing or changed')
        hermes_ca().validate_static(repo, details)


def provision(repo, install, harness, choice='keep', node='', python=sys.executable,
              cache=None, dry_run=False, capabilities_manifest=None):
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
            details = (install_dsh(repo, root, node, cache, datadir=True) if harness == 'dsh'
                       else install_hermes(repo, root, python, cache))
            details['generation_receipt'] = str(root / OWNER)
            receipt = {'harness': harness, 'complete': True, 'details': details,
                       **{key: details[key] for key in ('variant', 'thin_config_contract') if key in details},
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
    if harness == 'dsh':
        spec = importlib.util.spec_from_file_location('_managed_capabilities', repo / 'dashboard/managed_capabilities.py')
        capabilities = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(capabilities)
        facts = capabilities.refresh_dsh_capabilities(repo, receipt['details'], capabilities_manifest)
        if facts['extraction_status'] != 'known':
            print('Managed dsh effort capabilities unknown; runtime defaults remain available.')
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
    parser.add_argument('--managed-spawn-config', dest='capabilities_manifest')
    parser.add_argument('--dry-run', action='store_true')
    args = vars(parser.parse_args())
    try:
        provision(**args)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print('Managed harness install failed: ' + str(error), file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError):
            # Acquisition uses a clean environment/private HOME and no keys.
            # PM's command validation and package errors are on stdout, while
            # its runtime bootstrap reports progress on stderr.
            for name in ('stdout', 'stderr'):
                output = getattr(error, name, None)
                if output:
                    if isinstance(output, bytes):
                        output = output.decode('utf-8', errors='replace')
                    print(name + ' (tail):\n' + output[-4000:], file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
