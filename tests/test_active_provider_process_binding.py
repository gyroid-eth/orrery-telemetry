"""Saved-provider wrapper controls; fake lower observer, not real ps proof."""
from copy import deepcopy
import importlib
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin/lib'))
process = importlib.import_module('process_observation')
STAMP = 'Sat Oct 17 12:00:00 2026'


@pytest.mark.parametrize('mutation', [
    'missing', 'extra', 'bool-pid', 'zero-pid', 'unknown-birth', 'absent-birth',
    'empty-birth', 'unknown-before', 'absent-before', 'wrong-before',
    'unknown-after', 'wrong-after', 'ancestry-false', 'observer-error',
])
def test_saved_provider_wraps_the_same_ancestry_and_one_deadline(monkeypatch, mutation):
    provider = {'pid': 20, 'process_start': STAMP}
    runner = {'pid': 10, 'process_start': STAMP}
    calls = []; mode = None; starts = 0
    def start(pid, *, deadline):
        nonlocal starts
        calls.append(('start', pid, deadline)); starts += 1
        if mode == 'observer-error': raise OSError('fixture observation unavailable')
        if (mode in ('unknown-before', 'absent-before', 'wrong-before') and starts == 1
                or mode in ('unknown-after', 'wrong-after') and starts == 2):
            return {'unknown-before': None, 'absent-before': 'absent', 'wrong-before': 'other',
                'unknown-after': None, 'wrong-after': 'other'}[mode]
        return STAMP
    def ancestry(helper, pid, saved_runner, uid, *, deadline):
        calls.append(('ancestry', helper, pid, saved_runner, uid, deadline))
        return mode != 'ancestry-false'
    monkeypatch.setattr(process, 'process_start', start)
    monkeypatch.setattr(process, 'auth_process_binding', ancestry)
    # Same input/observer succeeds first, so all-refuse cannot pass a negative.
    assert process.active_provider_binding(30, provider, runner, 501, deadline=123.0)
    assert calls == [('start', 20, 123.0), ('ancestry', 30, 20, runner, 501, 123.0), ('start', 20, 123.0)]
    calls.clear(); starts = 0; changed = deepcopy(provider); mode = mutation
    if mutation == 'missing': changed.pop('process_start')
    elif mutation == 'extra': changed['other'] = True
    elif mutation == 'bool-pid': changed['pid'] = True
    elif mutation == 'zero-pid': changed['pid'] = 0
    elif mutation == 'unknown-birth': changed['process_start'] = None
    elif mutation == 'absent-birth': changed['process_start'] = 'absent'
    elif mutation == 'empty-birth': changed['process_start'] = ''
    assert process.active_provider_binding(30, changed, runner, 501, deadline=123.0) is False
    if mutation in ('missing', 'extra', 'bool-pid', 'zero-pid', 'unknown-birth', 'absent-birth', 'empty-birth'):
        assert calls == []
    assert all(call[-1] == 123.0 for call in calls)


def test_default_deadline_is_shared_by_all_three_observations(monkeypatch):
    calls = []
    monkeypatch.setattr(process.time, 'monotonic', lambda: 50.0)
    monkeypatch.setattr(process, 'process_start', lambda pid, **kw: calls.append(kw['deadline']) or STAMP)
    monkeypatch.setattr(process, 'auth_process_binding', lambda *a, **kw: calls.append(kw['deadline']) or True)
    assert process.active_provider_binding(30, {'pid': 20, 'process_start': STAMP},
        {'pid': 10, 'process_start': STAMP}, 501)
    assert calls == [55.0, 55.0, 55.0]
