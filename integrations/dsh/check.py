"""Prepare an isolated offline test environment and run all dsh checks serially.

Missing runtimes and unapproved skips are ordinary sources of false green
reports. Neither is accepted as success. No downloads, services or credentials.
"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def summarize(tests_run, failures, errors, skips, allowed):
    unapproved = [row for row in skips if row['reason'] not in allowed]
    return {'success': tests_run > 0 and failures == 0 and errors == 0 and not unapproved,
            'tests_run': tests_run, 'passed': max(0, tests_run - failures - errors - len(skips)),
            'failures': failures, 'errors': errors, 'skips': skips,
            'allowed_skip_reasons': allowed, 'unapproved_skips': unapproved}


def node_result(text, returncode):
    def count(name):
        match = re.search(r'^# ' + name + r' (\d+)\s*$', text, re.M)
        if not match:
            raise ValueError('Node TAP summary missing')
        return int(match[1])
    skips = [{'test': 'node:' + match[0].strip(), 'reason': match[1].strip()}
             for match in re.findall(r'^ok \d+ - (.*?) # SKIP\s*(.*?)\s*$', text, re.M)]
    tests, failures, skipped = count('tests'), count('fail'), count('skipped')
    if skipped != len(skips) or count('cancelled'):
        raise ValueError('Node skipped/cancelled test evidence incomplete')
    return tests, failures, int(returncode != 0 and failures == 0), skips


def prepare(node, runtime, output):
    if sys.version_info < (3, 11):
        raise ValueError('Python 3.11 or newer is required')
    node, runtime, output = [Path(p).resolve() for p in (node, runtime, output)]
    if not node.is_file() or not os.access(node, os.X_OK):
        raise ValueError('Pinned Node executable is required')
    version = subprocess.check_output([str(node), '--version'], env={'PATH': os.defpath}, text=True, timeout=5).strip()
    match = re.fullmatch(r'v(\d+)\.(\d+)\.(\d+)', version)
    if not match or tuple(map(int, match.groups())) < (24, 2, 0):
        raise ValueError('Pinned Node 24.2 or newer is required')
    for name, expected in [('@deepseek-ai/dsh', '0.2.1-rc.2'), ('@deepseek-harness-tui/dsh-tui', '0.16.1')]:
        package = runtime / 'node_modules' / name / 'package.json'
        if json.loads(package.read_text()).get('version') != expected:
            raise ValueError('Pinned dsh/TUI runtime versions are required')
    receipt_file = runtime.parent / 'evidence/upstream-runtime.json'
    if not receipt_file.is_file():
        raise ValueError('Plain dsh/TUI runtime receipt is required')
    source = json.loads(receipt_file.read_text())
    if source.get('variant') not in ('plain', 'plain-datadir'):
        raise ValueError('Plain dsh/TUI runtime receipt is required')
    # Use the installer's same native hash/recipe checks without importing dsh.
    sys.path.insert(0, str(ROOT))
    from scripts.lib.managed_harness_install import validate
    validate(ROOT, 'dsh', {'harness': 'dsh', 'complete': True,
        'variant': source['variant'], 'thin_config_contract': 1,
        'details': {'node': str(node), 'runtime': str(runtime), 'runtime_receipt': str(receipt_file)}})
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    home = output / 'home'
    home.mkdir(mode=0o700, exist_ok=True)
    scratch = output / 'tmp'
    scratch.mkdir(mode=0o700, exist_ok=True)
    # Explicit env: no inherited AgentStack binding, provider key, or live HOME.
    env = {'HOME': str(home), 'PATH': str(node.parent) + os.pathsep + os.defpath,
           'LANG': 'en_US.UTF-8', 'LC_ALL': 'en_US.UTF-8', 'TMPDIR': str(scratch),
           'ORRERY_DSH_TEST_NODE': str(node), 'ORRERY_DSH_TEST_RUNTIME': str(runtime),
           'ORRERY_DSH_TEST_PYTHON': sys.executable}
    return env, {'node': {'path': str(node), 'version': version, 'sha256': hashlib.sha256(node.read_bytes()).hexdigest()},
                 'runtime': str(runtime), 'runtime_receipt': str(receipt_file), 'variant': source['variant'], 'python': sys.executable}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node', required=True)
    parser.add_argument('--runtime', required=True)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--allowed-skips', type=Path, default=HERE / 'allowed_skips.json')
    args = parser.parse_args()
    args.out = args.out.resolve()
    result = summarize(0, 0, 1, [], [])
    args.out.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        allowed = json.loads(args.allowed_skips.read_text())
        if not isinstance(allowed, list) or not all(isinstance(r, str) and r for r in allowed):
            raise ValueError('Allowed skip reasons must be a JSON string array')
        env, proof = prepare(args.node, args.runtime, args.out)
        os.environ.clear()
        os.environ.update(env)
        os.chdir(ROOT)
        sys.path.insert(0, str(ROOT))
        totals, failures, errors, skips = 0, 0, 0, []
        tap = args.out / 'node.tap'
        with tap.open('w') as log:
            run = subprocess.run([env['ORRERY_DSH_TEST_NODE'], '--test', '--test-concurrency=1', '--test-reporter=tap',
                                  str(ROOT / 'tests/test_dsh_capabilities.mjs'),
                                  str(ROOT / 'tests/test_dsh_compaction_preset.mjs'),
                                  str(ROOT / 'tests/test_dsh_thin_tools.mjs'),
                                  str(ROOT / 'tests/test_dsh_thin_credentials.mjs'),
                                  str(ROOT / 'tests/test_dsh_tui_datadir.mjs')], env=env,
                                 stdout=log, stderr=subprocess.STDOUT, timeout=120)
        n, f, e, s = node_result(tap.read_text(), run.returncode)
        totals += n; failures += f; errors += e; skips += s
        loader = unittest.TestLoader()
        suite = loader.discover(str(ROOT / 'tests'), pattern='test_dsh_*.py')
        for module in ('test_managed_catalog', 'test_managed_capabilities', 'test_managed_harness_install'):
            suite.addTests(loader.loadTestsFromName(module))
        with (args.out / 'python-tests.txt').open('w') as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            checked = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
        totals += checked.testsRun; failures += len(checked.failures); errors += len(checked.errors)
        skips += [{'test': test.id(), 'reason': reason} for test, reason in checked.skipped]
        result = {**summarize(totals, failures, errors, skips, allowed), 'environment': proof,
                  'offline': True, 'real_key_reads': 0, 'live_requests': 0}
    except Exception as error:
        result['environment_error'] = type(error).__name__
        result['environment_reason'] = str(error) if isinstance(error, ValueError) else 'Pinned runtime or check environment is incomplete; inspect the saved test logs.'
    (args.out / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
