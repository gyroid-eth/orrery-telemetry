"""Generated provider-resource drift before auth-plan release; fake OS only."""
import os
import sys
from types import SimpleNamespace

import pytest

from test_runtime_legacy_auth_binding import (
    bound, configure_fixture, context_fixture, observation, read,
)
import local_model_auth as auth


def change_instruction(bound, mutation, output_name):
    path = bound.store.owner.with_suffix('.preview-home') / output_name
    if mutation == 'bytes':path.write_bytes(b'changed instructions\n')
    elif mutation == 'missing':path.unlink()
    elif mutation == 'symlink':path.unlink(); path.symlink_to(bound.ws.task)
    elif mutation == 'mode':path.chmod(0o644)
    else:os.link(path, path.with_name('instruction-alias'))


@pytest.mark.parametrize('moment', ['before', 'during-observation'])
@pytest.mark.parametrize('mutation', ['bytes', 'missing', 'symlink', 'mode', 'hardlink'])
@pytest.mark.parametrize('output_name', ['CLAUDE.md', 'local-model-mcp.json', 'local-model-settings.json'])
def test_instruction_drift_cannot_release_auth_plan(bound, monkeypatch, moment, mutation, output_name):
    calls = observation(bound, monkeypatch)
    # Establish a valid binding first: an unrelated failure must not satisfy
    # the negative. This returns an auth plan; it never executes its command.
    assert read(bound)['argv'] == [str(bound.key)]
    previous = len(calls)
    changed = []
    if moment == 'before':
        change_instruction(bound, mutation, output_name)
    else:
        observed_run = auth.subprocess.run
        def during(argv, **options):
            if not changed:
                changed.append(True)
                change_instruction(bound, mutation, output_name)
            return observed_run(argv, **options)
        monkeypatch.setattr(auth.subprocess, 'run', during)
    with pytest.raises(auth.LocalModelError, match='^LOCAL_MODEL_AUTH_FAILED$'):
        read(bound)
    if moment == 'before':assert len(calls) == previous
    else:assert changed == [True] and len(calls) > previous
    assert not bound.ws.tmp.joinpath('key-executed').exists()


@pytest.mark.parametrize('drift', [False, True])
@pytest.mark.parametrize('output_name', ['CLAUDE.md', 'local-model-mcp.json', 'local-model-settings.json'])
def test_auth_instruction_check_does_not_use_cached_product_modules(bound, monkeypatch, drift, output_name):
    calls = observation(bound, monkeypatch)
    def poison(*args, **options):pytest.fail('cached unverified instruction code used')
    monkeypatch.setitem(sys.modules, 'instruction_blocks', SimpleNamespace(render=poison))
    monkeypatch.setitem(sys.modules, 'runtime_legacy_configuration',
                        SimpleNamespace(check_legacy_provider_outputs=poison))
    monkeypatch.setitem(sys.modules, 'runtime_provider_projection',
                        SimpleNamespace(_build_provider_projection=poison))
    monkeypatch.setitem(sys.modules, 'local_model_profiles', SimpleNamespace(selector_name=poison))
    monkeypatch.setitem(sys.modules, 'runtime_model_resources', SimpleNamespace(skill_outputs=poison))
    if drift:
        change_instruction(bound, 'bytes', output_name)
        with pytest.raises(auth.LocalModelError, match='^LOCAL_MODEL_AUTH_FAILED$'):read(bound)
        assert calls == []
    else:
        assert read(bound)['argv'] == [str(bound.key)]
        assert calls
