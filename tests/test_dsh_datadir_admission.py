"""Check only the shared static admission function; no launcher/provider imports."""
import ast
import json
import os
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class AdmissionTests(unittest.TestCase):
    def test_datadir_receipt_is_dsh_only_and_plain_is_not_relabelled(self):
        source = Path(os.environ.get('ORRERY_DSH_ADMISSION_SOURCE', ROOT / 'hooks/harness_child.py'))
        function = next(node for node in ast.parse(source.read_text()).body
                        if isinstance(node, ast.FunctionDef) and node.name == 'static_check')
        namespace = {'Path': Path, 'os': os, 'ROOT': ROOT,
                     'read': lambda file: json.loads(Path(file).read_text()),
                     'config_generator': lambda *args: None}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), namespace)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            node = root / 'node'
            node.write_text('synthetic executable')
            node.chmod(0o700)
            runtime = root / 'runtime'
            for pin in namespace['read'](ROOT / 'integrations/dsh/runtime-pins.json'):
                target = runtime / 'node_modules' / pin['name'] / 'package.json'
                target.parent.mkdir(parents=True)
                target.write_text(json.dumps({'version': pin['version']}))
            config = {'node': str(node), 'runtime': str(runtime)}
            receipt = {'harness': 'dsh', 'variant': 'plain-datadir', 'complete': True,
                       'thin_config_contract': 1, 'details': config}
            check = namespace['static_check']
            self.assertTrue(check(receipt, config, harness='dsh')['available'])
            self.assertFalse(check({**receipt, 'harness': 'hermes'}, config, harness='hermes')['available'])
            plain = {**receipt, 'variant': 'plain'}
            self.assertTrue(check(plain, config, harness='dsh')['available'])
            self.assertEqual(plain['variant'], 'plain')
            # The dsh generator separately requires exact plain-datadir: its
            # old-plain negative is in test_dsh_thin_config.py.
