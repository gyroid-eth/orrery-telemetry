"""Refresh repository dsh pins and locks from explicit npm versions."""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from urllib.parse import quote

from lib.managed_harness_install import (
    UPSTREAM_PROOF_FILES, dsh_node, dsh_npm_command, read, run, tarball_integrity,
)

INTEGRATION = Path(__file__).resolve().parents[1] / 'integrations/dsh'
REGISTRY = 'https://registry.npmjs.org/'
TUI = '@deepseek-harness-tui/dsh-tui'
DSH = '@deepseek-ai/dsh'
EXA = '@deepseek-ai/dsh-web-search-exa'


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def fetch(url):
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read()


def download_pin(pin, version, directory):
    name = pin['name']
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:-[0-9A-Za-z.\-]+)?', version):
        raise ValueError(f'{name} requires an exact npm version')
    package = json.loads(fetch(f'{REGISTRY}{quote(name, safe="@")}/{version}'))
    dist = package['dist']
    tarball = f'{REGISTRY}{name}/-/{name.split("/")[-1]}-{version}.tgz'
    if package['name'] != name or package['version'] != version or dist['tarball'] != tarball:
        raise ValueError(f'{name}@{version}: npm metadata mismatch')
    data = fetch(tarball)
    if tarball_integrity(data) != dist['integrity']:
        raise ValueError(f'{name}@{version}: tarball sha512 mismatch')
    (directory / pin['file']).write_bytes(data)
    return {**pin, 'version': version, 'integrity': dist['integrity'], 'url': tarball}


def verify_tui(file, recipe):
    with tarfile.open(file) as archive:
        for path in UPSTREAM_PROOF_FILES:
            archive.getmember('package/' + path)
        target = archive.extractfile('package/' + recipe['file']).read().decode()
        if target.count(recipe['old']) != 1:
            raise ValueError(f"TUI DATA_DIR patch preimage mismatch in {recipe['file']}")
        manifest = json.load(archive.extractfile('package/package.json'))
    # Published workspace dependencies are provided by the host or TUI bundle.
    return {name: version for name, version in manifest['dependencies'].items()
            if not version.startswith('workspace:')}


def refresh(node, versions):
    with tempfile.TemporaryDirectory(prefix='orrery-dsh-pins-') as directory:
        root = Path(directory)
        node, env, _ = dsh_node(root, node)
        npm = dsh_npm_command(root, node, 'install') + ['--package-lock-only']
        evidence, runtime, exa, ui = (root / name for name in ('evidence', 'runtime', 'exa', 'ui'))
        for path in (evidence, runtime, exa, ui):
            path.mkdir()
        pins = [download_pin(pin, versions[pin['name']], evidence)
                for pin in read(INTEGRATION / 'runtime-pins.json')]
        tui = next(pin for pin in pins if pin['name'] == TUI)
        recipe = read(INTEGRATION / 'tui-datadir.patch.json')
        dependencies = verify_tui(evidence / tui['file'], recipe)
        for directory, name in ((runtime, DSH), (exa, EXA)):
            pin = next(pin for pin in pins if pin['name'] == name)
            write(directory / 'package.json', {'dependencies': {name: 'file:../evidence/' + pin['file']}})
            print(f'Resolving {directory.name} lock', flush=True)
            run(npm, cwd=directory, env=env)
            packages = read(directory / 'package-lock.json')['packages']
            resolved = packages['node_modules/' + name]
            if resolved['version'] != pin['version'] or resolved['integrity'] != pin['integrity']:
                raise ValueError(f'npm lock mismatch for {name}')
        ui_manifest = read(INTEGRATION / 'ui-package.json')
        shutil.copyfile(INTEGRATION / 'ui-package-lock.json', ui / 'package-lock.json')
        write(ui / 'package.json', {**ui_manifest, 'dependencies': dependencies})
        ui_npm = npm + ['--legacy-peer-deps']
        print('Resolving dsh-TUI UI lock', flush=True)
        run(ui_npm, cwd=ui, env=env)
        resolved = read(ui / 'package-lock.json')['packages']
        ui_manifest['dependencies'] = {name: resolved['node_modules/' + name]['version']
                                       for name in sorted(dependencies)}
        write(ui / 'package.json', ui_manifest)
        run(ui_npm, cwd=ui, env=env)
        for src, dest in [(runtime / 'package-lock.json', 'runtime-package-lock.json'),
                          (exa / 'package-lock.json', 'exa-package-lock.json'),
                          (ui / 'package.json', 'ui-package.json'),
                          (ui / 'package-lock.json', 'ui-package-lock.json')]:
            shutil.copyfile(src, INTEGRATION / dest)
        write(INTEGRATION / 'runtime-pins.json', pins)
    print('Refreshed dsh pins and locks. The TUI DATA_DIR patch preimage matched.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node', required=True, help='Absolute Node executable (>=24.9)')
    parser.add_argument('--dsh', required=True, help='Exact @deepseek-ai/dsh npm version')
    parser.add_argument('--tui', required=True, help='Exact dsh-TUI npm version')
    parser.add_argument('--exa', required=True, help='Exact dsh-web-search-exa npm version')
    args = parser.parse_args()
    try:
        refresh(args.node, {DSH: args.dsh, TUI: args.tui, EXA: args.exa})
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr, file=sys.stderr)
        parser.exit(1, f'Could not refresh dsh pins: {error}\n')


if __name__ == '__main__':
    main()
