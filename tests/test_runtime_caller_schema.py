"""Caller validation is shared; operator/transport inputs cannot leak into it."""
from copy import deepcopy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'bin/lib'))
from runtime_caller_schema import canonical_caller, LocalModelError


def literal():
    return dict(provider='codex', model='local:fixture', cwd='/fixture/cwd',
                task='/fixture/task.md', name='FixtureChild', parent='FixtureParent',
                tools=[], base='mail-only', clear_tools=False, detached=True,
                resources=[], worktree=False, chrome=False)


@pytest.mark.parametrize('provider,expected', [('codex','codex'),('codex-cli','codex'),
                                            ('claude','claude'),('claude-code','claude')])
def test_provider_alias_is_only_structural_normalization(provider, expected):
    caller = {**literal(), 'provider':provider}
    before = deepcopy(caller)
    assert canonical_caller(caller) == {**caller, 'provider':expected}
    assert caller == before


def test_omission_empty_false_and_clear_do_not_collapse():
    original = canonical_caller(literal())
    for key, value in [('effort',''), ('effort',None), ('task_delivery','mail'),
                       ('clear_tools',True), ('tools',['mail'])]:
        different = canonical_caller({**literal(),key:value})
        assert different != original
    assert 'effort' not in original
    assert original['tools'] == [] and original['clear_tools'] is False


@pytest.mark.parametrize('key,value', [('local_models_root','/foreign'),('identity',{}),
    ('auth_context',{}),('tmux_socket','/foreign'),('effective_scope',{}),
    ('provider','gemini'),('name','bad/name'),('task','relative'),('cwd','/a/../b'),
    ('chrome',1),('resources','file'),('task_delivery','unknown'),('effort',False),
    ('strict_output_cap',1),('model','bad\nmodel')])
def test_invalid_or_operator_only_inputs_are_rejected_without_resolution(key, value):
    with pytest.raises(LocalModelError, match='LOCAL_MODEL_CALLER_INVALID'):
        canonical_caller({**literal(),key:value})
