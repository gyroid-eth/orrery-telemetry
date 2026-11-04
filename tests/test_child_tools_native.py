"""Native selections reuse the standard copy/approval decision, never broaden it."""
import json
import pytest
from hooks import child_tools as tools


def settings(tmp_path):
    path=tmp_path/'user.json'
    path.write_text(json.dumps({'mcpServers':{'reader':{'command':'/fixture/reader','args':[]},
        'windows':{'command':'/fixture/uvx','args':['windows-mcp@0.8.6']}}}))
    return str(path)


def test_default_inherits_and_plain_mcp_copies_same_definition(tmp_path):
    config=settings(tmp_path)
    plan=tools.native_plan(tools.normalize(),cwd=str(tmp_path),claude_json=config)
    assert plan=={'servers':{},'inherit':True,'approved':{},'enabled_tools':{}}
    spec=tools.normalize('mail-only',{'mcp':['reader']})
    plan=tools.native_plan(spec,cwd=str(tmp_path),claude_json=config)
    assert plan['inherit'] is False and plan['servers']['reader']['command']=='/fixture/reader'
    assert plan['approved']=={}


@pytest.mark.parametrize('spec',[
    {'screen':{'access':'read','server':'windows'}},
    {'mcp':['windows'],'approve_all':['windows']},
    {'screen':{'access':'operate','server':None}},
    {'browser':{'device':'specific-device'}},
])
def test_unrepresentable_native_approval_is_rejected(tmp_path,spec):
    with pytest.raises(tools.ToolsError):
        tools.native_plan(tools.normalize(tools=spec),cwd=str(tmp_path),claude_json=settings(tmp_path))


def test_missing_selected_server_is_not_silently_dropped(tmp_path):
    with pytest.raises(tools.ToolsError):
        tools.native_plan(tools.normalize(tools={'mcp':['absent']}),cwd=str(tmp_path),claude_json=settings(tmp_path))


CHROME = {'command': 'npx', 'args': ['-y', 'chrome-devtools-mcp@latest',
          '--autoConnect', '--no-usage-statistics'], 'env': {'npm_config_cache': '/fixture/cache'},
          'cwd': '/fixture/browser'}


def browser_plan(tmp_path, spec=None, servers=None, **kwargs):
    return tools.native_plan(spec or tools.normalize(), cwd=str(tmp_path),
        claude_json=settings(tmp_path), codex_config={'mcp_servers': servers or {}}, **kwargs)


@pytest.mark.parametrize('name', ['chrome-devtools', 'custom-chrome'])
def test_default_inherits_only_codex_browser_with_exact_transport(tmp_path, name):
    source = {name: {**CHROME, 'enabled': True},
              'unrelated': {'command': '/fixture/other'},
              'orrery-mail': {'command': '/never/copy'}}
    plan = browser_plan(tmp_path, servers=source)
    assert plan == {'servers': {name: CHROME}, 'inherit': True, 'approved': {}, 'enabled_tools': {}}
    plan['servers'][name]['args'].append('--fixture-change')
    assert '--fixture-change' not in source[name]['args']


@pytest.mark.parametrize('servers', [{}, {'chrome-devtools': {**CHROME, 'enabled': False}}])
def test_default_without_active_browser_keeps_mail_only(tmp_path, servers):
    assert browser_plan(tmp_path, servers=servers)['servers'] == {}


def test_mail_only_does_not_import_even_unrepresentable_codex_browser(tmp_path):
    plan = browser_plan(tmp_path, tools.normalize('mail-only'),
                        {'chrome-devtools': {**CHROME, 'enabled_tools': ['list_pages']}})
    assert plan == {'servers': {}, 'inherit': False, 'approved': {}, 'enabled_tools': {}}


@pytest.mark.parametrize('restriction', [
    {'enabled_tools': ['list_pages']}, {'disabled_tools': ['evaluate_script']},
    {'tools': {'click': {'approval_mode': 'prompt'}}},
    {'default_tools_approval_mode': 'approve'}, {'env_vars': ['FIXTURE_SETTING']},
])
def test_native_inheritance_never_drops_codex_controls(tmp_path, restriction):
    with pytest.raises(tools.ToolsError, match='native_mcp_selection_unsupported'):
        browser_plan(tmp_path, servers={'chrome-devtools': {**CHROME, **restriction}})


@pytest.mark.parametrize('base', ['default', 'mail-only'])
@pytest.mark.parametrize('enabled', [True, False])
def test_explicit_browser_finds_codex_then_rejects_unrepresentable_approval(tmp_path, base, enabled):
    with pytest.raises(tools.ToolsError, match='native_mcp_approval_unsupported'):
        browser_plan(tmp_path, tools.normalize(base, {'browser': True}),
                     {'chrome-devtools': {**CHROME, 'enabled': enabled}})


def test_default_browser_and_explicit_plain_mcp_are_combined(tmp_path):
    plan = browser_plan(tmp_path, tools.normalize(tools={'mcp': ['reader']}),
                        {'chrome-devtools': CHROME})
    assert set(plan['servers']) == {'reader', 'chrome-devtools'}
    assert plan['approved'] == plan['enabled_tools'] == {}


def test_browser_discovery_ambiguity_is_not_silently_ignored(tmp_path):
    with pytest.raises(tools.ToolsError, match='several match'):
        browser_plan(tmp_path, servers={'first': CHROME, 'second': CHROME})
