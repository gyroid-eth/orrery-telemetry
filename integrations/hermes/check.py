"""Prepare an isolated offline environment and run every Hermes fixture."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def report(result, allowed):
    if not isinstance(allowed, list) or any(not isinstance(reason, str) for reason in allowed):
        raise ValueError('Allowed skip reasons must be a string array')
    skips = [{'test': test.id(), 'reason': reason} for test, reason in result.skipped]
    unapproved = [row for row in skips if row['reason'] not in allowed]
    return {'success': result.wasSuccessful() and result.testsRun > 0 and not unapproved,
            'tests_run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
            'skips': skips, 'allowed_skip_reasons': allowed, 'unapproved_skips': unapproved}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True, help='Trusted private Hermes run-config reference')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--allowed-skips', type=Path, default=HERE / 'check-skip-reasons.json')
    args = parser.parse_args()
    output = args.out.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    outcome = {'success': False, 'tests_run': 0, 'failures': 0, 'errors': 1, 'skips': [],
               'allowed_skip_reasons': [], 'unapproved_skips': []}
    try:
        from runner import committed_python
        config = json.loads(args.config.read_text())
        python = committed_python(config)
        source = Path(config['hermes_source']).resolve()
        if not (source / 'hermes_cli/oneshot.py').is_file():
            raise ValueError('Installed Hermes source unavailable')
        allowed = json.loads(args.allowed_skips.read_text())
        with tempfile.TemporaryDirectory(prefix='hermes-check-', dir=output.parent) as temporary:
            home = Path(temporary) / 'home'
            home.mkdir()
            previous = dict(os.environ)
            try:
                os.environ.clear()
                os.environ.update(HOME=str(home), TMPDIR=str(Path(temporary).resolve()), PATH='/usr/bin:/bin',
                    PYTHONDONTWRITEBYTECODE='1', HERMES_DISABLE_LAZY_INSTALLS='1', UV_OFFLINE='1',
                    UV_CACHE_DIR=str(Path(temporary) / 'uv-cache'),
                    ORRERY_HERMES_TEST_PYTHON=python, ORRERY_HERMES_TEST_SOURCE=str(source))
                sys.dont_write_bytecode = True
                suite = unittest.defaultTestLoader.discover(str(HERE.parents[1] / 'tests'), pattern='test_hermes*.py')
                result = unittest.TextTestRunner(verbosity=2).run(suite)
                outcome = report(result, allowed)
            finally:
                os.environ.clear()
                os.environ.update(previous)
    except Exception as error:
        outcome['setup_failure'] = type(error).__name__
    output.write_text(json.dumps(outcome, indent=2) + '\n')
    print(json.dumps(outcome))
    return 0 if outcome['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
