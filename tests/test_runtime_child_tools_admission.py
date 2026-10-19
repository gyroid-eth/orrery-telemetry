"""Direct child API grammar before local/native/lock/network effects."""
from copy import deepcopy
from pathlib import Path
import runpy

import pytest

ROOT = Path(__file__).resolve().parents[1]
API = runpy.run_path(str(ROOT / 'bin/lib/runtime_client.py'))


@pytest.mark.parametrize('entry', ['prepare_child', 'resume_child'])
@pytest.mark.parametrize('invalid', ['mail', 'shell'])
def test_invalid_parsed_tools_are_rejected_before_any_client_effect(entry, invalid):
    child = object.__new__(API['RuntimeClient'])
    child.config = {'client_name': 'parent'}
    def forbidden(*args, **kwargs): pytest.fail('tools rejection reached client effects')
    child.operation_mutex = forbidden
    child.check_local_registration = forbidden
    options = {'child_client_name': 'child_01', 'parsed_tools': {invalid: True}, 'base': 'default'}
    with pytest.raises(API['ClientError'], match='^CHILD_TOOLS_INVALID$'):
        getattr(child, entry)(options)


@pytest.mark.parametrize('selection', [{}, {'mcp': ['b', 'a']}, {'browser': True}])
def test_shared_validation_does_not_replace_the_callers_literal_selection(selection):
    options = {'base': 'mail-only', 'parsed_tools': selection}
    before = deepcopy(options)
    function = API.get('validate_child_tools')
    assert callable(function), 'EARLY_CHILD_TOOLS_VALIDATOR_MISSING'
    assert function(options) is None
    assert options == before
