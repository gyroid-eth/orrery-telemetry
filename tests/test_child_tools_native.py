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
        tools.native_plan(tools.normalize(tools=spec, platform='linux'), cwd=str(tmp_path),
                          claude_json=settings(tmp_path), platform='linux')


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


@pytest.mark.parametrize('base', ['default', 'mail-only'])
@pytest.mark.parametrize('name', ['chrome-devtools', 'custom-chrome'])
def test_named_chrome_selection_copies_codex_transport_without_approvals(tmp_path, base, name):
    spec = tools.normalize(base, {'mcp': ['chrome-devtools']})
    plan = browser_plan(tmp_path, spec, {name: CHROME, 'unrelated': {'command':'/fixture/other'}})
    assert plan['servers'] == {name: CHROME}
    assert plan['approved'] == plan['enabled_tools'] == {}


def test_named_chrome_selection_refuses_to_drop_codex_tool_controls(tmp_path):
    spec = tools.normalize('mail-only', {'mcp': ['chrome-devtools']})
    with pytest.raises(tools.ToolsError, match='native_mcp_selection_unsupported'):
        browser_plan(tmp_path, spec, {'chrome-devtools': {**CHROME, 'enabled_tools': ['list_pages']}})


def test_browser_off_keeps_standard_base_and_built_in_tools(tmp_path):
    spec = tools.normalize(tools=tools.parse_cli(['no-browser', 'mcp:reader']))
    assert spec['base'] == 'default'
    plan = browser_plan(tmp_path, spec, {'chrome-devtools': CHROME})
    assert plan['inherit'] is True and set(plan['servers']) == {'reader'}
    assert 'including web search or fetch' in tools.prompt_text(spec, 'dsh')
    assert '--no-chrome' in tools.claude_plan(spec, cwd=str(tmp_path), chrome=False,
                                               claude_json=settings(tmp_path))['flags']
    codex = {'mcp_servers': {'chrome-devtools': CHROME, 'reader': {'command':'/fixture/reader'}}}
    tools.codex_apply(codex, spec)
    assert not codex['mcp_servers']['chrome-devtools']['enabled']
    assert codex['mcp_servers']['reader']['enabled']


def test_deliberate_mail_only_keeps_built_in_tools_in_the_prompt():
    prompt = tools.prompt_text(tools.normalize('mail-only'), 'dsh')
    assert 'base mail-only limits added MCP servers to authenticated ORRERY Mail' in prompt
    assert 'including web search or fetch' in prompt


@pytest.mark.parametrize('mode, flag', [('off', None), ('temporary', '--isolated'), ('personal', '--autoConnect')])
def test_browser_mode_projects_into_each_provider_without_mixing_profiles(tmp_path, monkeypatch, mode, flag):
    monkeypatch.setattr(tools.shutil, 'which', lambda _: '/fixture/npx')
    monkeypatch.setattr(tools.sys, 'platform', 'darwin')
    spec = tools.normalize(tools=tools.parse_cli(['browser-mode:' + mode]))
    chrome, _ = tools.merge_chrome(spec, False, '')
    assert chrome is (mode == 'personal')
    claude = tools.claude_plan(spec, cwd=str(tmp_path), chrome=chrome, claude_json=settings(tmp_path))
    assert ('--no-chrome' in claude['flags']) is (not chrome)
    assert (next(iter(claude['servers'].values()))['args'][-2] if claude['servers'] else None) == (
        '--isolated' if mode == 'temporary' else None)
    native = browser_plan(tmp_path, spec, {'chrome-devtools': CHROME})
    assert next(iter(native['servers'].values()))['args'][-2] == flag if flag else not native['servers']
    assert native['inherit'] and not native['approved']
    codex = {'mcp_servers': {'chrome-devtools': CHROME, 'other': {'command':'/fixture/other'}}}
    tools.codex_apply(codex, spec)
    rendered = codex['mcp_servers']['chrome-devtools']
    assert rendered['enabled'] is (flag is not None)
    if flag:
        assert flag in rendered['args']
        assert ('--autoConnect' in rendered['args']) is (mode == 'personal')
    assert codex['mcp_servers']['other'] == {'command':'/fixture/other'}
    agy = tools.agy_config('/fixture/proxy', spec, runtime='', project='', child='', mail_env='',
                           home=tmp_path)['mcpServers']
    assert 'orrery-mail' in agy
    assert set(agy) == ({'orrery-mail', 'chrome-devtools'} if flag else {'orrery-mail'})
    if flag:
        assert flag in agy['chrome-devtools']['args']


def test_antigravity_does_not_offer_modes_when_global_chrome_is_active(tmp_path, monkeypatch):
    monkeypatch.setattr(tools.shutil, 'which', lambda _: '/fixture/npx')
    monkeypatch.setattr(tools.sys, 'platform', 'darwin')
    global_config = tmp_path / '.gemini' / 'config' / 'mcp_config.json'
    global_config.parent.mkdir(parents=True)
    global_config.write_text(json.dumps({'mcpServers': {'my-chrome': CHROME}}))
    assert tools.launch_browser_modes({'mcp_servers': {'chrome-devtools': CHROME}}, home=tmp_path)['antigravity'] == []
    with pytest.raises(tools.ToolsError, match='global Antigravity'):
        tools.agy_config('/fixture/proxy', tools.normalize(tools={'browser_mode': 'off'}),
                         runtime='', project='', child='', mail_env='', home=tmp_path)


@pytest.mark.parametrize('mode, flag', [('temporary', '--isolated'), ('personal', '--autoConnect')])
def test_codex_browser_mode_creates_the_mcp_table_if_absent(monkeypatch, mode, flag):
    monkeypatch.setattr(tools.shutil, 'which', lambda _: '/fixture/npx')
    config = {}
    tools.codex_apply(config, tools.normalize(tools={'browser_mode': mode}))
    assert flag in config['mcp_servers']['chrome-devtools']['args']


def test_browser_discovery_ambiguity_is_not_silently_ignored(tmp_path):
    with pytest.raises(tools.ToolsError, match='several match'):
        browser_plan(tmp_path, servers={'first': CHROME, 'second': CHROME})


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_native_screen_has_product_definition_and_no_inherited_cua(tmp_path, platform):
    from agentstack_mail import screen_contract
    spec = tools.normalize(tools={"screen": {"access": "read", "server": "orrery-screen"}},
                           platform=platform)
    definition = {"command": "/fixture/python", "args": ["-m", "agentstack_mail.screen_mcp"],
                  "env": {"AGENTSTACK_SCREEN_BINDING_FILE": "/fixture/binding.json"}}
    config = settings(tmp_path)
    plan = tools.native_plan(spec, cwd=str(tmp_path), claude_json=config,
                             managed_screen=definition, platform=platform)
    assert plan["servers"] == {"orrery-screen": definition}
    assert tuple(plan["managed_screen"]) == screen_contract.TOOLS
    assert plan["approved"] == plan["enabled_tools"] == {}
    assert tools.screen_mode(spec) == "view"
    settings_path = tmp_path / "source.json"
    settings_path.write_text(json.dumps({"mcpServers": {
        "alias": {"command": "/fixture/wrapper", "args": ["/fixture/cua-driver"]}}}))
    conflicting = tools.normalize(tools={"mcp": ["orrery-screen", "alias"]})
    with pytest.raises(tools.ToolsError, match="orrery-screen"):
        tools.native_plan(conflicting, cwd=str(tmp_path), claude_json=str(settings_path),
                          managed_screen=definition, platform=platform)
