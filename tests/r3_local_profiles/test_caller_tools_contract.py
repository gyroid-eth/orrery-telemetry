"""Three separate tools contracts; missing real caller binding remains red.

Empty selection preserves captured bound Mail/shell configuration, not proof of
native execution. Nonempty MCP selection must survive every actual entry.
Unknown mail/shell tokens must be rejected by existing grammar, not renamed.
"""
from pathlib import Path
import runpy
import subprocess
import sys
import uuid

import pytest

from .evidence import (CALLERS, CALLER_STARTUP_PROMPT, CALLER_MCP_STARTUP_PROMPT, caller_client_root,
                       pinned_capture_provider, snapshot_tree, assert_tree_unchanged)
from .test_caller_contract import assert_real_callers
from .test_review_controls import oracle_control_binding


def test_nonempty_mcp_selection_survives_all_five_real_callers(caller_probe,workspace):
    workspace.caller['tools'] = {'mcp':['fixture_extra']}
    assert_real_callers(caller_probe,workspace,expected_tools={'mcp':['fixture_extra']},
                        startup_prompt=CALLER_MCP_STARTUP_PROMPT)


@pytest.mark.parametrize('entry',CALLERS)
@pytest.mark.parametrize('invalid',['mail','shell'])
def test_unknown_builtin_names_are_rejected_by_actual_caller(caller_probe,workspace,entry,invalid):
    root = caller_client_root(workspace)
    captures = workspace.tmp/'captures'
    captures.mkdir(mode=0o700)
    nonce = uuid.uuid4().hex
    capture = captures/(entry+'-'+nonce+'.json')
    binary = pinned_capture_provider(workspace,entry,nonce,capture)
    before = []
    def admitted_setup():before.append(snapshot_tree(root))
    def invoke():
        return caller_probe(entry,workspace=workspace,real_entry=CALLERS[entry],
                            provider_binary=binary,owner=root/'profile.json',
                            provider_session_id='native-fixture-session' if entry=='resume-child' else None,
                            invalid_cli_tools=[invalid],invalid_api_tools={invalid:True},
                            on_before_real_entry=admitted_setup,expect_tools_rejection=True)
    if entry == 'shell':
        result = invoke()
        assert isinstance(result,subprocess.CompletedProcess) and result.returncode != 0
        assert 'tools' in result.stderr.lower()
    elif entry == 'dashboard':
        result = invoke()
        assert result == {'ok':False, 'reason':'CHILD_TOOLS_INVALID'}
        assert caller_probe.last_dashboard_http == {'status':400, 'body':result}
    else:
        with pytest.raises((ValueError,RuntimeError),match='(?i)tool'):invoke()
    assert len(before) == 1, 'real entry rejection boundary was not exercised'
    assert_tree_unchanged(root,before[0])
    assert not capture.exists() and not capture.with_suffix('.driver.json').exists()


@pytest.mark.parametrize('invalid',['mail','shell'])
@pytest.mark.parametrize('shape',['cli','api'])
def test_existing_tools_grammar_rejects_builtin_names(invalid,shape):
    api = runpy.run_path(str(Path(__file__).resolve().parents[2]/'hooks/child_tools.py'))
    with pytest.raises(api['ToolsError'],match='(?i)(unknown|tools)'):
        if shape == 'cli':api['parse_cli']([invalid])
        else:api['normalize']('mail-only',{invalid:True})


@pytest.mark.parametrize('drop_tools_entry',[None,*CALLERS])
def test_nonempty_tools_oracle_rejects_dropped_selection(workspace,drop_tools_entry):
    # Oracle-only control, NOT the missing actual caller adapter. Every caller
    # must preserve an independently literal nonempty selection in its owner.
    workspace.caller['tools'] = {'mcp':['fixture_extra']}
    binding, seen = oracle_control_binding(workspace,drop_tools_entry=drop_tools_entry)
    def check():
        assert_real_callers(binding,workspace,expected_tools={'mcp':['fixture_extra']},
                            startup_prompt=CALLER_MCP_STARTUP_PROMPT)
    if drop_tools_entry is None:
        check()
        assert seen == list(CALLERS)
    else:
        with pytest.raises(AssertionError,match='caller changed literal tools'):check()
        assert seen[-1] == drop_tools_entry


@pytest.mark.parametrize('nonempty',[False,True])
@pytest.mark.parametrize('mutation',['remove-mail','remove-shell','remove-extra',
                                     'disable-shell','disable-shell-equals','disable-shell-config-equals'])
def test_tools_oracle_rejects_lost_builtin_or_selected_mcp(workspace,nonempty,mutation):
    # This proves captured configuration is checked, NOT native availability.
    expected = {'mcp':['fixture_extra']} if nonempty else {}
    workspace.caller['tools'] = expected
    binding, _ = oracle_control_binding(workspace,config_mutation=mutation)
    prompt = CALLER_MCP_STARTUP_PROMPT if nonempty else CALLER_STARTUP_PROMPT
    def check():assert_real_callers(binding,workspace,expected_tools=expected,startup_prompt=prompt)
    if mutation == 'remove-extra' and not nonempty:
        check()  # disabling a nonexistent optional server changes no selection
    else:
        message = 'selection removed shell tool' if 'shell' in mutation else 'wrong visible MCP selection'
        with pytest.raises(AssertionError,match=message):check()


@pytest.mark.parametrize('nonempty',[False,True])
@pytest.mark.parametrize('mutation',[None,'missing','other-root','symlink-file','symlink-directory','wrong-bytes'])
def test_tools_oracle_requires_exact_trusted_mail_proxy_script(workspace,nonempty,mutation):
    # Controlled captures only: neither this test nor the driver runs the proxy.
    expected = {'mcp':['fixture_extra']} if nonempty else {}
    workspace.caller['tools'] = expected
    source = Path(__file__).resolve().parents[2]/'bin/lib/runtime_client.py'
    trusted = workspace.tmp/'install/bin/lib/runtime_client.py'
    script = trusted
    if mutation == 'other-root':
        script = workspace.tmp/'alternate-proxy/bin/lib/runtime_client.py'
        script.parent.mkdir(parents=True)
        script.write_bytes(source.read_bytes())  # right bytes at the wrong absolute path
    binding, seen = oracle_control_binding(workspace,proxy_script=script)
    if mutation == 'missing':trusted.unlink()
    elif mutation == 'symlink-file':
        trusted.unlink();trusted.symlink_to(source)
    elif mutation == 'symlink-directory':
        trusted.unlink();trusted.parent.rmdir()
        trusted.parent.symlink_to(source.parent,target_is_directory=True)
    elif mutation == 'wrong-bytes':trusted.write_text('# replaced fixture proxy, never executed\n')
    prompt = CALLER_MCP_STARTUP_PROMPT if nonempty else CALLER_STARTUP_PROMPT
    def check():assert_real_callers(binding,workspace,expected_tools=expected,startup_prompt=prompt)
    if mutation is None:
        check()
        assert seen == list(CALLERS)
    else:
        with pytest.raises(AssertionError,match='wrong bound Mail proxy script'):check()
        assert seen == ['shell']  # rejected on first bad config, before cross-caller comparison


@pytest.mark.parametrize('mutation',[None,'other-command','original-python','wrong-bytes','symlink'])
def test_tools_oracle_pins_fixture_python_path_and_original_executable_bytes(workspace,mutation):
    interpreter=workspace.tmp/'delivery-python/bin/python'
    command={'other-command':'/usr/bin/true','original-python':sys.executable}.get(mutation,interpreter)
    binding,seen=oracle_control_binding(workspace,mail_command=command)
    if mutation=='wrong-bytes':interpreter.write_text('replaced interpreter, never executed\n')
    elif mutation=='symlink':
        interpreter.unlink();interpreter.symlink_to(Path(sys.executable).resolve(strict=True))
    def check():assert_real_callers(binding,workspace,expected_tools={},startup_prompt=CALLER_STARTUP_PROMPT)
    if mutation is None:
        check();assert seen==list(CALLERS)
    else:
        with pytest.raises(AssertionError,match='wrong bound Mail command'):check()
        assert seen==['shell']
