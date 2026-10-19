"""Real installer resolution/env/service writers with acquisition stubbed."""
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
INSTALL = (ROOT / 'scripts/install.sh').read_text()


def block(start, end):
    return INSTALL[INSTALL.index(start):INSTALL.index(end, INSTALL.index(start))]


class SavedNodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.install = self.root / 'install'
        self.install.mkdir()
        self.script_dir = self.root / 'fixture-scripts'
        (self.script_dir / 'lib').mkdir(parents=True)
        (self.script_dir / 'lib/managed_harness_install.py').write_text(
            'import json,sys\nfrom pathlib import Path\na=sys.argv[1:]\n'
            'if a[a.index("--harness")+1]=="dsh":\n'
            ' n=a[a.index("--node")+1]\n'
            ' if not n: sys.exit("dsh requires --dsh-node")\n'
            ' Path(a[a.index("--install")+1],"captured-node.json").write_text(json.dumps(n))\n')

    def invoke(self, *, node=None, manifest=None, enabled=False):
        writer = block('write_env_file() {', '\nstop_new_agent_mail() {')
        harness = block('install_managed_harnesses() {', '\ninstall_payload() {')
        defaults = {key: '' for key in re.findall(r'\$\{?([A-Z][A-Z0-9_]*)', writer)
                    if not key.startswith('AGENTSTACK_')}
        defaults.update(INSTALL_DIR=str(self.install), ENV_FILE=str(self.install / 'env.sh'),
            REPO_ROOT=str(ROOT), SCRIPT_DIR=str(self.script_dir), PYTHON_BIN=sys.executable,
            DRY_RUN='false', RESET_SETTINGS='0', OPTION_GIVEN='', HARNESS_CACHE='',
            DSH_OPT_IN='enable' if enabled else '', HERMES_OPT_IN='disable')
        if node is not None:
            defaults['OPTION_GIVEN'] += ' AGENTSTACK_DSH_NODE'
        if manifest is not None:
            defaults['OPTION_GIVEN'] += ' AGENTSTACK_MANAGED_SPAWN_CONFIG'
        defaults.update(DSH_NODE=node or '', MANAGED_SPAWN_CONFIG_SETTING=manifest or '')
        setup = 'set -euo pipefail\nplan() { :; }\n' + '\n'.join(k + '=' + shlex.quote(v) for k, v in defaults.items())
        setup += '\n. ' + shlex.quote(str(ROOT / 'hooks/project-context.sh')) + '\n'
        setup += block('# --- setting resolution', '# --- end setting resolution ---') + '\n'
        for name in ('DSH_NODE', 'MANAGED_SPAWN_CONFIG_SETTING'):
            setup += next(line for line in INSTALL.splitlines() if line.startswith('resolve_setting ' + name + ' ')) + '\n'
        return subprocess.run(['/bin/bash', '-c', setup + writer + harness + '\ninstall_managed_harnesses\nwrite_env_file\n'],
            env={'HOME': str(self.root), 'PATH': '/usr/bin:/bin'}, capture_output=True, text=True)

    def test_enable_then_update_without_node_env_preserves_node_and_manifest(self):
        node = str(self.root / 'fixed Node/bin/node')
        manifest = str(self.root / 'private manifest.json')
        first = self.invoke(node=node, manifest=manifest, enabled=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        written = (self.install / 'env.sh').read_text()
        self.assertIn('export AGENTSTACK_DSH_NODE=', written)
        self.assertIn('export AGENTSTACK_MANAGED_SPAWN_CONFIG=', written)
        update = self.invoke()
        self.assertEqual(update.returncode, 0, update.stderr)
        self.assertEqual(json.loads((self.install / 'captured-node.json').read_text()), node)
        env = (self.install / 'env.sh').read_text()
        self.assertIn(shlex.quote(node), env)
        self.assertIn(shlex.quote(manifest), env)

    def test_existing_install_backfills_node_from_current_receipt(self):
        current = self.install / 'harnesses/dsh/current.json'
        current.parent.mkdir(parents=True)
        current.write_text(json.dumps({'node': '/fixture/existing/node'}))
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('export AGENTSTACK_DSH_NODE=/fixture/existing/node', (self.install / 'env.sh').read_text())

    def test_explicit_node_overrides_saved_and_explicit_empty_does_not_backfill(self):
        self.assertEqual(self.invoke(node='/fixture/old/node').returncode, 0)
        self.assertEqual(self.invoke(node='/fixture/new/node').returncode, 0)
        self.assertEqual(json.loads((self.install / 'captured-node.json').read_text()), '/fixture/new/node')
        result = self.invoke(node='')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('dsh requires --dsh-node', result.stderr)

    def test_new_settings_reach_installer_and_agentctl_launchd_writers(self):
        node, manifest = '/fixture/fixed node/bin/node', '/fixture/Research & Notes/manifest.json'
        for writer in ('installer', 'controller'):
            if writer == 'installer':
                body = block('render_launchd_plist() {', 'render_systemd_unit() {')
                call = 'render_launchd_plist'
            else:
                text = (ROOT / 'dashboard/agentctl.sh').read_text()
                body = text[text.index('sed_escape() {'):text.index('background_pid() {')]
                call = 'render_plist'
            output = self.root / writer / 'Library/LaunchAgents/fixture.plist'
            output.parent.mkdir(parents=True)
            env = {name: '' for name in re.findall(r'\$\{?([A-Z][A-Z0-9_]*)', body)}
            env.update(HOME=str(self.root / writer), PATH=os.defpath, REPO_ROOT=str(ROOT),
                HERE=str(ROOT / 'dashboard'), DASHBOARD_DIR=str(ROOT / 'dashboard'), LABEL='fixture',
                PYTHON_BIN=sys.executable, PYTHON=sys.executable, DRY_RUN='false',
                PLIST_TEMPLATE=str(ROOT / 'dashboard/agentdashboard.plist.template'), PLIST_DST=str(output),
                DSH_NODE=node, DSH_NODE_SETTING=node, MANAGED_SPAWN_CONFIG_SETTING=manifest)
            result = subprocess.run(['/bin/bash', '-c', 'set -euo pipefail\nplan() { :; }\n' + body + '\n' + call],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            values = plistlib.loads(output.read_bytes())['EnvironmentVariables']
            self.assertEqual(values['AGENTSTACK_DSH_NODE'], node)
            self.assertEqual(values['AGENTSTACK_MANAGED_SPAWN_CONFIG'], manifest)


if __name__ == '__main__':
    unittest.main()
