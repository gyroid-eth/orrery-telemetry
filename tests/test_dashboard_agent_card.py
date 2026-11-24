"""Card reads use synthetic panes and session/launch metadata."""

import json

import pytest

from dashboard import agent_card, harness_context, server, terminal_state
from hooks import claude_chrome_policy, screen_state


@pytest.mark.parametrize(
    ("pane", "expected"),
    [
        ("› Ask Codex to do anything\n\n"
         "  GPT-6.2-Sol xhigh · Context 16% left · ~/workspace · wee…\n"
         "  ← for agents · ? for shortcuts  ⚠ 1 warning · f2 to view",
         ("GPT-6.2-Sol", "xhigh")),
        ("›\n\n  gpt-5.6-terra high · ~/workspace", ("gpt-5.6-terra", "high")),
        ("›\n\n  gpt-5.6-terra · Context 80% left\n  ? for shortcuts", ("gpt-5.6-terra", None)),
        ("  gpt-5.6-terra high · ~/workspace\n\n› Ask Codex to do anything", (None, None)),
        ("› Draft\n  gpt-5.6-terra high · ~/workspace", (None, None)),
        ("› Draft\n  gpt-5.6-terra high · ~/workspace\n  Context 10% left", (None, None)),
        ("›\n  GPT-6.2-S… xhigh · ~/workspace\n  ? for shortcuts", (None, None)),
        ("›\n  GPT-6.2-Sol xhi…\n  ? for shortcuts", (None, None)),
        ("›\n  gpt-5.6-terra low\n  gpt-5.6-terra high", (None, None)),
    ],
)
def test_codex_card_footer(pane, expected):
    assert terminal_state.codex_model_effort(pane) == expected


def test_codex_model_id_requires_an_exact_or_unique_match():
    assert agent_card.codex_model_id("gpt-7-testing", ()) == "gpt-7-testing"
    assert agent_card.codex_model_id("GPT-6.2-Sol", ("gpt-6.2-sol",)) == "gpt-6.2-sol"
    assert agent_card.codex_model_id("GPT-6.2-Sol", ()) is None
    assert agent_card.codex_model_id("GPT-6.2-Sol", ("gpt-6.2-sol", "gpt-6.2 sol")) is None


@pytest.mark.parametrize(
    ("footer", "expected"),
    [
        ("  local-model…  high  cach…  ctx 32% (84k/262k)", "high"),
        ("  local-model · xhigh · cache 92%  ctx 32% (84k/262k)", "xhigh"),
        ("  local-model…  h…  cach…  ctx 32% (84k/262k)", None),
        ("  local-model…  low-context  cach…  ctx 32% (84k/262k)", None),
    ],
)
def test_dsh_card_effort(footer, expected):
    assert harness_context.dsh_effort("  ⌸ ❯     ⛶\n  ╰─────╯\n" + footer) == expected
    assert harness_context.dsh_effort(footer + "\n  ⌸ ❯     ⛶") is None


@pytest.mark.parametrize(
    ("footer", "expected"),
    [
        (" ─ ready │ local model │ 19.2k/262.1k │ [█░░░░░░░░░]  │ ◎ 74% ─",
         {"ctx_used": 7, "ctx_window": "262.1K"}),
        (" ─ ready │ local model │ 0/128k │ [░░░░░░░░░░] ─",
         {"ctx_used": 0, "ctx_window": "128K"}),
        (" ─ ready │ local model │ 12h │ 1 session ─ ~/.hermes/profiles/helper",
         {"ctx_used": None, "ctx_window": None}),
    ],
)
def test_hermes_ready_context_uses_the_token_pair(footer, expected):
    assert harness_context.parse_context(footer + "\n helper ❯", "hermes") == expected
    assert harness_context.window_tokens("262.1K") == 262100


def _assistant(session_id, model, effort, **fields):
    return dict(type="assistant", sessionId=session_id, message={"model": model},
                effort=effort, **fields)


def test_claude_bound_turn_skips_sidechains_and_refreshes_after_append(tmp_path):
    path = tmp_path / "session.jsonl"
    records = [
        _assistant("session", "claude-opus-5-5", "medium", perTurnEffort="high",
                   cwd=str(tmp_path), padding="a" * 20000),
        _assistant("session", "claude-sonnet-5-5", "low", isSidechain=True),
        _assistant("other-session", "claude-sonnet-5-5", "max"),
        _assistant("session", "<synthetic>", "low"),
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    assert agent_card.claude_turn(path, "session") == ("claude-opus-5-5", "high", str(tmp_path))
    with path.open("a") as handle:
        handle.write(json.dumps(_assistant("session", "claude-sonnet-5-5", "low")) + "\n")
    assert agent_card.claude_turn(path, "session") == ("claude-sonnet-5-5", "low", None)
    assert agent_card.claude_turn(path, "unbound") == (None, None, None)


def test_card_ignores_stale_claude_transcript(tmp_path, monkeypatch):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(json.dumps(_assistant("session", "claude-opus-5-5", "medium")) + "\n")
    monkeypatch.setattr(server, "RUNTIME_DIR", str(tmp_path))
    row = {"name": "Worker", "program": "claude-code", "history_session_id": "session",
           "transcript_path": str(transcript), "history_binding": "stale"}
    registration = {"agent_id": 1, "project_key": "test", "program": "claude-code"}
    assert server._agent_card_fields(row, {}, {}, registration, ())["model_id"] is None
    bound = server._agent_card_fields(dict(row, history_binding="bound"), {}, {}, registration, ())
    assert (bound["model_id"], bound["effort"]) == ("claude-opus-5-5", "medium")


@pytest.fixture
def without_cua_overrides(monkeypatch):
    for key in agent_card.os.environ.copy():
        if key.startswith("CUA_DRIVER_"):
            monkeypatch.delenv(key)


_DIRECT_CUA = {"command": "cua-driver", "args": ["mcp"]}
_WRAPPED_CUA = {
    "command": "/usr/bin/python3",
    "args": ["/fixture/cua_relay.py", "--", "/fixture/cua-driver", "mcp"],
}


@pytest.mark.parametrize('cua', [_DIRECT_CUA, _WRAPPED_CUA], ids=['direct', 'wrapped'])
def test_card_permissions_distinguish_selection_and_configuration(without_cua_overrides, cua):
    spec = {"base": "mail-only", "tools": {}}
    assert agent_card.tool_permissions(None, {}) == (None, None)
    assert agent_card.tool_permissions(spec, {}, isolated=True) == ("off", False)
    assert agent_card.tool_permissions(spec, {"cua-driver": cua}) == ("control", None)
    for restricted in (dict(cua, enabled=False), dict(cua, enabled_tools=[]),
                       dict(cua, env={"CUA_DRIVER_PERMISSION_MODE": "bounded"})):
        assert agent_card.tool_permissions(None, {"cua-driver": restricted}) == (None, None)
    chosen = {"tools": {"screen": {"access": "read", "server": "cua-driver"}, "browser": {}}}
    assert agent_card.tool_permissions(chosen, {"cua-driver": cua}) == ("view", True)


@pytest.mark.parametrize('key', ['enabled_tools', 'disabled_tools', 'tools', 'omit_tools_from'])
def test_card_wrapped_cua_with_filter_stays_unknown(without_cua_overrides, key):
    server = dict(_WRAPPED_CUA, **{key: []})
    assert agent_card.tool_permissions(None, {'cua-driver': server}) == (None, None)


@pytest.mark.parametrize('tail', [
    ['/fixture/not-cua-driver', 'mcp'],
    ['/fixture/cua-driver', 'inspect'],
    ['/fixture/cua-driver', 'mcp', '--extra'],
    ['mcp'],
])
def test_card_wrapped_cua_requires_standard_tail(without_cua_overrides, tail):
    server = dict(_WRAPPED_CUA, args=['/fixture/cua_relay.py', '--', *tail])
    assert agent_card.tool_permissions(None, {'cua-driver': server}) == (None, None)


def test_card_wrapped_cua_with_inherited_override_stays_unknown(without_cua_overrides, monkeypatch):
    monkeypatch.setenv('CUA_DRIVER_PERMISSION_MODE', 'bounded')
    assert agent_card.tool_permissions(None, {'cua-driver': _WRAPPED_CUA}) == (None, None)


def _write_private_json(path, data):
    path.write_text(json.dumps(data))
    path.chmod(0o600)


@pytest.mark.parametrize('program', ['codex', 'claude-code'])
def test_bound_card_reads_wrapped_cua_definition(tmp_path, monkeypatch, without_cua_overrides, program):
    name, sid = 'WrappedCardFixture', '0432dc16-e219-4b20-8bb2-519f2f9d428a'
    monkeypatch.setattr(server, 'RUNTIME_DIR', str(tmp_path))
    children = tmp_path / 'child-agents'
    children.mkdir()
    if program == 'codex':
        _write_private_json(children / f'{name}.json', {
            'agent_id': 1, 'agent_name': name, 'program': program, 'project_key': 'test', 'launch_origin': 'child',
        })
        monkeypatch.setattr(server, '_card_codex_provenance', lambda *_: {'launch_origin': 'child'})
        config = children / f'{name}.codex-home' / 'config.toml'
        config.parent.mkdir()
        config.write_text('[mcp_servers.cua-driver]\n'
                          f'command = {json.dumps(_WRAPPED_CUA["command"])}\n'
                          f'args = {json.dumps(_WRAPPED_CUA["args"])}\n')
        config.chmod(0o600)
    else:
        _write_private_json(children / f'{name}.claude-launch.{sid}.json', {
            'version': claude_chrome_policy.TOOLS_VERSION, 'launch_id': 'card-fixture-launch',
            'agent_name': name, 'session_id': sid, 'claude_chrome': False, 'chrome_device': '',
            'standalone': True, 'base': 'default', 'tools': {'mcp': ['cua-driver']},
        })
        _write_private_json(children / f'{name}.mcp.json', {'mcpServers': {'cua-driver': _WRAPPED_CUA}})
    row = dict(name=name, program=program, running=True, history_binding='bound',
               history_session_id=sid, transcript_path=str(tmp_path / 'transcript.jsonl'))
    registration = dict(agent_id=1, project_key='test', program=program)
    assert server._agent_card_fields(row, {}, {}, registration, ())['computer_use'] == 'control'


@pytest.mark.parametrize(('tools', 'initial'), [
    ({'mcp': ['orrery-screen']}, 'off'),
    ({'screen': {'server': 'orrery-screen', 'access': 'operate'}}, 'control'),
])
def test_claude_child_card_uses_bound_screen_generation(tmp_path, monkeypatch, tools, initial):
    name, sid = 'ScreenCardFixture', '0432dc16-e219-4b20-8bb2-519f2f9d428a'
    monkeypatch.setattr(server, 'RUNTIME_DIR', str(tmp_path))
    children = tmp_path / 'child-agents'
    children.mkdir()
    _write_private_json(children / f'{name}.claude-launch.{sid}.json', {
        'version': claude_chrome_policy.TOOLS_VERSION, 'launch_id': 'card-fixture-launch',
        'agent_name': name, 'session_id': sid, 'claude_chrome': False,
        'chrome_device': '', 'standalone': False, 'base': 'default', 'tools': tools,
    })
    store = screen_state.Store(tmp_path)
    target = dict(project='test-project', runtime=screen_state.launch_runtime(tmp_path), agent=name, generation='a' * 32)
    store.launch(*target.values(), initial)
    _write_private_json(children / f'{name}.mcp.json', {'mcpServers': {
        'orrery-screen': {'env': {'AGENTSTACK_SCREEN_BINDING_FILE': str(screen_state.binding_path(store, target))}},
    }})
    transcript = tmp_path / f'{sid}.jsonl'
    transcript.write_text(json.dumps(_assistant(sid, 'claude-opus-5-5', 'high')) + '\n')
    row = dict(name=name, program='claude-code', running=True, history_binding='bound',
               transcript_path=str(transcript), history_session_id=sid)
    registration = dict(agent_id=1, project_key=target['project'], program=row['program'])

    def mode():
        return server._agent_card_fields(row, {}, {}, registration, (),
                                         screen_launch=server._card_screen_launches()[name])['computer_use']

    assert mode() == initial
    store.change(target, 'view')
    assert mode() == 'view'
    store.launch(target['project'], target['runtime'], name, 'b' * 32, 'control')
    assert mode() is None


def test_card_requires_current_codex_launch_for_retained_parent(tmp_path, monkeypatch):
    children = tmp_path / "child-agents"
    children.mkdir()
    _write_private_json(children / "Worker.json", {
        "agent_id": 1, "agent_name": "Worker", "program": "codex", "project_key": "test",
        "launch_origin": "child", "codex_standalone": True,
    })
    _write_private_json(tmp_path / "1.json", {"launch_id": "known-launch", "expected_at": 100})
    monkeypatch.setattr(server, "RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(server, "CODEX_LAUNCH_DIR", str(tmp_path))
    monkeypatch.setattr(server, "_tmux", lambda _args: "AGENTSTACK_CODEX_LAUNCH_ID=known-launch\n")
    monkeypatch.setattr(server, "_codex_resume_provenance",
                        lambda *args, **kwargs: ({"launch_origin": "child"}, None))
    row = {"name": "Worker", "program": "codex", "history_binding": "bound",
           "transcript_path": str(tmp_path / "rollout.jsonl")}
    registration = {"agent_id": 1, "project_key": "test", "program": "codex"}
    active = {"created": 101, "codex_launch_id": "known-launch", "cwd": str(tmp_path)}
    assert server._agent_card_fields(row, active, {}, registration, ())["parent_state"] == "top"
    for stale in ("", "different-launch"):
        session = dict(active, codex_launch_id=stale)
        assert server._agent_card_fields(row, session, {}, registration, ())["parent_state"] is None


def test_card_rejects_global_tmux_parent_and_reuses_the_local_check(monkeypatch):
    monkeypatch.setattr(server.time, "monotonic", lambda: 100.0)
    server._card_local_tmux_value.cache_clear()
    calls = []
    monkeypatch.setattr(server, "_tmux", lambda args: calls.append(args) or "")
    session = {"server_pid": "1", "session_id": "$2", "created": 3, "parent_agent": "GlobalParent"}
    row = {"name": "Worker", "program": "claude-code"}
    for _ in range(2):
        card = server._agent_card_fields(row, session, {}, None, ())
        assert card["parent_agent"] is card["parent_state"] is None
    assert calls == [["show-environment", "-t", "=Worker", "PARENT_AGENT"]]


def test_card_uses_native_metadata_only_for_the_bound_launch(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RUNTIME_DIR", str(tmp_path))
    children = tmp_path / "child-agents"
    directory = children / "Worker.harness" / ("a" * 32)
    directory.mkdir(parents=True)
    _write_private_json(children / "Worker.json", {
        "agent_id": 4, "agent_name": "Worker", "program": "dsh", "project_key": "test",
        "launch_origin": "child", "harness": "dsh", "harness_backend": "strata",
        "harness_state_dir": str(directory), "parent_agent": "Parent",
    })
    _write_private_json(directory / "native-session.json", {
        "harness": "dsh", "agent_name": "Worker", "native_session_id": "native-id",
        "created_at": "2026-01-01T00:00:00+00:00",
    })
    _write_private_json(directory / "input.json", {
        "agent_name": "Worker", "program": "dsh", "project_key": "test",
        "backend_id": "strata", "state_dir": str(directory), "runtime_dir": str(tmp_path),
        "cwd": str(tmp_path), "model": "local-model-v2", "parent_agent": "Parent",
        "tools": {"base": "default", "tools": {}},
        "mcp_selection": {"servers": {"chrome-devtools": {"command": "chrome-devtools-mcp"}},
                          "inherit": True},
    })
    row = {"name": "Worker", "program": "dsh", "history_binding": "bound",
           "history_session_id": "native-id", "transcript_path": str(tmp_path / "bound.jsonl"),
           "ctx_window": "1.0M"}
    registration = {"agent_id": 4, "project_key": "test", "program": "dsh"}
    card = server._agent_card_fields(row, {}, {"dsh_card_effort": "high"}, registration, ())
    assert card == {"cwd": str(tmp_path), "model_id": "local-model-v2", "effort": "high",
                    "harness_kind": "dsh", "ctx_window_tokens": 1000000, "computer_use": None,
                    "browser_use": True, "parent_state": "child", "parent_agent": "Parent"}
    mismatched = server._agent_card_fields(dict(row, history_session_id="other"), {}, {}, registration, ())
    assert mismatched["model_id"] is mismatched["browser_use"] is mismatched["parent_agent"] is None


def test_roster_exposes_card_fields_from_the_batched_active_pane(monkeypatch, tmp_path):
    sep = server.SEP
    replies = {
        "list-sessions": sep.join(("Worker", "100", "102", "$1", "", "", "Parent", "launch-private", "9")),
        "list-panes": "\n".join([
            sep.join(("Worker", "10", "zsh", "50", "cwd=/other", "Other")),
            sep.join(("Worker", "11", "node", "51", f"cwd={tmp_path}", "Ready")),
        ]),
        "list-clients": "",
        "show-environment": "PARENT_AGENT=Parent\n",
    }
    calls = []
    monkeypatch.setattr(server, "_tmux", lambda args: calls.append(args[0]) or replies[args[0]])
    monkeypatch.setattr(server, "_project_key", lambda: None)
    monkeypatch.setattr(server, "RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(server, "DB_PATH", str(tmp_path / "absent.sqlite3"))
    monkeypatch.setattr(server, "agentmail_state", lambda: ({"Worker": {
        "program": "codex-cli", "model": "GPT-5.6-Terra", "last_active": 100}}, {}))
    monkeypatch.setattr(server, "_process_tree_snapshot", lambda: None)
    monkeypatch.setattr(server, "_agent_runtime", lambda *_: server._parse_runtime(
        "›\n\n  gpt-5.6-terra high · Context 80% left\n  ? for shortcuts"))
    monkeypatch.setattr(server, "_codex_app_runtimes", lambda: {})
    monkeypatch.setattr(server, "_deliverables_index", lambda: {})
    monkeypatch.setattr(server, "_name_substitutions", lambda: {})
    monkeypatch.setattr(server, "_persistent_profiles", lambda: {})
    monkeypatch.setattr(server, "_annotations", lambda: {"Worker": {"role": "Reviewer"}})
    monkeypatch.setattr(server, "_provider_stall_view", lambda *_: None)
    monkeypatch.setattr(server, "_claude_row_mail_fields", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(server, "_resume_capability_for_row", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_observe_activity", lambda *_: None)
    monkeypatch.setattr(server, "_prune_observed", lambda *_: None)
    full = server._AgentsAnswer(server.HISTORY_DAYS_DEFAULT)
    (row,) = json.loads(full.body(""))["agents"]
    assert json.loads(full.body("live"))["agents"] == [row]
    assert row["cwd"] == str(tmp_path)
    assert (row["model_id"], row["effort"], row["harness_kind"], row["ctx_used"]) == (
        "gpt-5.6-terra", "high", "codex", 20)
    assert row["annot"] == {"role": "Reviewer"}
    assert (row["parent_state"], row["parent_agent"]) == ("child", "Parent")
    assert row["computer_use"] is row["browser_use"] is row["parentless"] is None
    assert calls == ["list-sessions", "list-panes", "list-clients", "show-environment"]
    assert "launch-private" not in full.body("").decode()
