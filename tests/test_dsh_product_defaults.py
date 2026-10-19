"""Profile projection must leave output policy to the harness unless opted in."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / 'integrations/dsh'


class OutputDefaultsTests(unittest.TestCase):
    def test_real_profile_projection_omits_trial_caps_and_preserves_explicit_limit(self):
        sys.path.insert(0, str(INTEGRATION))
        self.addCleanup(lambda: sys.path.remove(str(INTEGRATION)))
        spec = importlib.util.spec_from_file_location('dsh_default_launch', INTEGRATION / 'launch.py')
        launch = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launch)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / 'runtime'
            for package, version in [('@deepseek-ai/dsh', '0.2.1-rc.2'), ('@deepseek-harness-tui/dsh-tui', '0.16.1')]:
                file = runtime / 'node_modules' / package / 'package.json'
                file.parent.mkdir(parents=True)
                file.write_text(json.dumps({'version': version}))
            node = root / 'fixed-node'
            node.write_text('synthetic node')
            work = root / 'work'
            work.mkdir()
            template = json.loads((INTEGRATION / 'backend.json').read_text())
            self.assertNotIn('session_requests', template['limits'])
            for backend in template['backends'].values():
                self.assertNotIn('max_output_tokens', backend)
                self.assertNotIn('maxTokens', backend.get('model_definition') or {})
            for index, explicit in enumerate([None, 1000000000]):
                backend = template['backends']['strata']
                backend['base_url'] = 'https://strata.invalid'
                if explicit is not None:
                    backend['max_output_tokens'] = explicit
                config = root / 'backend.json'
                config.write_text(json.dumps(template))
                args = argparse.Namespace(node=str(node), runtime=str(runtime), state=str(root / f'state-{index}'),
                    worktree=str(work), backend_file=str(config), backend='strata', patch_receipt=str(root / 'receipt.json'), guard_probe=False)
                with patch.object(launch.subprocess, 'check_output', return_value='v24.3.0'), patch.dict(os.environ, HOME=str(root)):
                    state, _ = launch.prepare(args)
                selected = json.loads((state / 'selected.patch.json').read_text())
                provider = next(row['config'] for row in selected if row.get('id') == 'llm-deepseek')
                self.assertEqual(provider.get('maxTokens'), explicit)
                if explicit is None:
                    self.assertNotIn('maxTokens', provider)
                self.assertNotIn('maxTokens', provider['models'][0])


if __name__ == '__main__':
    unittest.main()
