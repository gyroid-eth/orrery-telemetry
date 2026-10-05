"""Choosing the tools a child is given: ``base`` and ``tools`` (stage 1).

- ``base`` omitted/"default" keeps today's launch byte for byte.
- ``base: mail-only`` gives only ORRERY Mail plus what ``tools`` names.
- Any selection fails closed: no Mail proxy, a server that cannot be copied,
  or a project where computer use is on for a mail-only child stops the
  launch before tmux starts.
- "Read only" screen access exposes only the classification table's tools.

Every test runs with a temporary HOME, runtime and fake tmux/claude/codex; no
live Mail, dashboard or user configuration is touched.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tomllib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_spawn_child_embed_task import (  # noqa: E402
    _codex_handoff, _executable, _fake_launch_env,
)
from test_claude_child_chrome import (  # noqa: E402
    ARGV_PROMPT, PRE_CHROME_INNER, _calls, _handoff, _new_session, _records, _log_text,
)

import dashboard.server as server  # noqa: E402
from hooks import child_tools  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
SPAWN = ROOT / "hooks" / "spawn_child.sh"
POLICY = ROOT / "hooks" / "claude_chrome_policy.py"
WINDOWS_MCP = {
    "type": "stdio", "command": "/mnt/c/Users/me/.local/bin/uvx.exe",
    "args": ["--python", "3.14", "windows-mcp@0.8.6", "serve", "--transport", "stdio"],
}
READ_FLAGS = [
    "--allowed-tools",
    "mcp__windows-mcp__Screenshot,mcp__windows-mcp__Snapshot",
    "--disallowed-tools",
    ",".join(f"mcp__windows-mcp__{t}" for t in (
        "App", "Click", "Clipboard", "DisplayInventory", "FileSystem", "Move",
        "MultiEdit", "MultiSelect", "Notification", "PowerShell", "Process",
        "Registry", "Scrape", "Scroll", "Shortcut", "Type", "Wait", "WaitFor")),
]


def _spec(base=None, tools=None):
    return child_tools.normalize(base, tools)


def _claude_json(path: pathlib.Path, *, servers=None, projects=None) -> str:
    path.write_text(json.dumps({"mcpServers": servers or {}, "projects": projects or {}}),
                    encoding="utf-8")
    return str(path)


# --------------------------------------------------------------------------- #
# the selection
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("values, expected", [
    (["browser"], {"browser": {"device": ""}}),
    (["browser:win-brave"], {"browser": {"device": "win-brave"}}),
    (["screen"], {"screen": {"access": "operate", "server": None}}),
    (["screen:read:windows-mcp"], {"screen": {"access": "read", "server": "windows-mcp"}}),
    (["mcp:b,mcp:a"], {"mcp": ["a", "b"]}),
])
def test_cli_forms(values, expected):
    assert _spec(None, child_tools.parse_cli(values)) == {"base": "default", "tools": expected}


@pytest.mark.parametrize("base, tools, message", [
    ("everything", {}, "base must be"),
    (None, {"shell": True}, "unknown tools"),
    (None, {"screen": "look"}, "access must be"),
    (None, {"mcp": ["orrery-mail"]}, "is ORRERY Mail"),
    (None, {"mcp": ["mcp_agent_mail"]}, "is ORRERY Mail"),
    (None, {"mcp": ["a b"]}, "must match"),
    (None, {"mcp": []}, "non-empty list"),
    (None, {"mcp": ["x", "x"]}, "twice"),
    (None, {"screen": {"access": "read", "server": "x"}, "mcp": ["x"]}, "only once"),
    (None, {"browser": {"device": "a b"}}, "must match"),
    (None, {"approve_all": ["x"]}, "also in tools.mcp"),
    (None, {"mcp": ["x"], "approve_all": ["y"]}, "also in tools.mcp"),
])
def test_invalid_selections_are_rejected(base, tools, message):
    with pytest.raises(child_tools.ToolsError, match=message):
        _spec(base, tools)


def test_only_a_selection_fails_closed():
    assert not child_tools.restrictive(_spec())
    assert not child_tools.restrictive(_spec("default"))
    assert child_tools.restrictive(_spec("mail-only"))
    assert child_tools.restrictive(_spec(None, {"mcp": ["x"]}))


def test_chrome_fields_and_tools_browser_must_agree():
    spec = _spec(None, {"browser": {"device": "a"}})
    assert child_tools.merge_chrome(spec, True, "a") == (True, "a")
    assert child_tools.merge_chrome(spec, False, "") == (True, "a")
    with pytest.raises(child_tools.ToolsError, match="different browsers"):
        child_tools.merge_chrome(spec, True, "b")


def test_codex_browser_is_chrome_devtools_and_has_no_device_or_builtin_screen():
    child_tools.check_provider(_spec(None, {"browser": True}), "codex")
    with pytest.raises(child_tools.ToolsError, match="deviceId"):
        child_tools.check_provider(_spec(None, {"browser": {"device": "d"}}), "codex")
    with pytest.raises(child_tools.ToolsError, match="no built-in screen"):
        child_tools.check_provider(_spec(None, {"screen": "operate"}), "codex")


# --------------------------------------------------------------------------- #
# Claude plan
# --------------------------------------------------------------------------- #
def test_mail_only_adds_no_chrome_unless_a_browser_was_chosen(tmp_path):
    cj = _claude_json(tmp_path / "c.json")
    plan = child_tools.claude_plan(_spec("mail-only"), cwd=str(tmp_path), chrome=False,
                                   claude_json=cj, platform="darwin")
    assert plan == {"servers": {}, "flags": ["--no-chrome"]}
    plan = child_tools.claude_plan(_spec("mail-only", {"browser": True}), cwd=str(tmp_path),
                                   chrome=True, claude_json=cj, platform="darwin")
    assert plan["flags"] == []


def test_default_base_with_a_server_does_not_touch_chrome(tmp_path):
    cj = _claude_json(tmp_path / "c.json", servers={"notes": {"command": "/usr/bin/notes"}})
    plan = child_tools.claude_plan(_spec(None, {"mcp": ["notes"]}), cwd=str(tmp_path),
                                   chrome=False, claude_json=cj, platform="linux")
    # Selecting a server copies it; its tools are not approved (review of
    # #149: never approve a whole server, and never more than the table knows).
    assert plan == {"servers": {"notes": {"command": "/usr/bin/notes"}}, "flags": []}


@pytest.mark.parametrize("project, stops", [
    ({"enabledMcpServers": ["computer-use"]}, True),
    ({"enabledMcpServers": ["computer-use"], "disabledMcpServers": ["computer-use"]}, False),
    ({"enabledMcpServers": ["other"]}, False),
    ({}, False),
])
def test_mail_only_stops_where_computer_use_is_enabled(tmp_path, project, stops):
    cj = _claude_json(tmp_path / "c.json", projects={str(tmp_path): project})
    call = lambda: child_tools.claude_plan(  # noqa: E731
        _spec("mail-only"), cwd=str(tmp_path), chrome=False, claude_json=cj, platform="darwin")
    if stops:
        with pytest.raises(child_tools.ToolsError, match="computer use is enabled"):
            call()
    else:
        assert call()["flags"] == ["--no-chrome"]


def test_mail_only_on_linux_does_not_read_computer_use(tmp_path):
    cj = _claude_json(tmp_path / "c.json",
                      projects={str(tmp_path): {"enabledMcpServers": ["computer-use"]}})
    plan = child_tools.claude_plan(_spec("mail-only"), cwd=str(tmp_path), chrome=False,
                                   claude_json=cj, platform="linux")
    assert plan["flags"] == ["--no-chrome"]


def test_builtin_screen_needs_macos_operate_and_an_enabled_project(tmp_path):
    enabled = _claude_json(tmp_path / "on.json",
                           projects={str(tmp_path): {"enabledMcpServers": ["computer-use"]}})
    off = _claude_json(tmp_path / "off.json")
    operate = _spec("mail-only", {"screen": "operate"})
    assert child_tools.claude_plan(operate, cwd=str(tmp_path), chrome=False,
                                   claude_json=enabled, platform="darwin")["servers"] == {}
    with pytest.raises(child_tools.ToolsError, match="not enabled for"):
        child_tools.claude_plan(operate, cwd=str(tmp_path), chrome=False,
                                claude_json=off, platform="darwin")
    with pytest.raises(child_tools.ToolsError, match="only on macOS"):
        child_tools.claude_plan(operate, cwd=str(tmp_path), chrome=False,
                                claude_json=enabled, platform="linux")
    with pytest.raises(child_tools.ToolsError, match="not in the classification table"):
        child_tools.claude_plan(_spec(None, {"screen": "read"}), cwd=str(tmp_path),
                                chrome=False, claude_json=enabled, platform="darwin")


def test_read_only_windows_mcp_allows_only_the_table_tools(tmp_path):
    cj = _claude_json(tmp_path / "c.json", servers={"windows-mcp": WINDOWS_MCP})
    plan = child_tools.claude_plan(
        _spec("mail-only", {"screen": {"access": "read", "server": "windows-mcp"}}),
        cwd=str(tmp_path), chrome=False, claude_json=cj, platform="linux")
    assert plan["servers"] == {"windows-mcp": WINDOWS_MCP}
    assert plan["flags"] == ["--no-chrome", *READ_FLAGS]


SCREEN_OPERATE = ("App", "Click", "DisplayInventory", "Move", "MultiEdit", "MultiSelect",
                  "Scroll", "Shortcut", "Type", "Wait", "WaitFor")
NOT_SCREEN = ("Clipboard", "FileSystem", "Notification", "PowerShell", "Process",
              "Registry", "Scrape")


def test_table_splits_every_tool_into_exactly_one_class():
    table = child_tools.TOOL_TABLE["windows-mcp"]["0.8.6"]
    assert set(table["operate"]) == set(SCREEN_OPERATE)
    tiers = [set(table["read"]), set(table["operate"]), set(NOT_SCREEN)]
    assert set().union(*tiers) == set(table["all"]) and sum(map(len, tiers)) == 20


def test_operate_windows_mcp_approves_only_screen_tools(tmp_path):
    """Review P1-1 (a): operate must not approve PowerShell, Registry, ..."""
    cj = _claude_json(tmp_path / "c.json", servers={"windows-mcp": WINDOWS_MCP})
    plan = child_tools.claude_plan(
        _spec(None, {"screen": {"access": "operate", "server": "windows-mcp"}}),
        cwd=str(tmp_path), chrome=False, claude_json=cj, platform="linux")
    allowed = plan["flags"][plan["flags"].index("--allowed-tools") + 1].split(",")
    denied = plan["flags"][plan["flags"].index("--disallowed-tools") + 1].split(",")
    assert set(allowed) == {f"mcp__windows-mcp__{t}" for t in ("Screenshot", "Snapshot", *SCREEN_OPERATE)}
    assert set(denied) == {f"mcp__windows-mcp__{t}" for t in NOT_SCREEN}
    assert "mcp__windows-mcp__PowerShell" not in allowed


def test_everything_needs_its_own_explicit_selection(tmp_path):
    cj = _claude_json(tmp_path / "c.json", servers={"windows-mcp": WINDOWS_MCP})
    spec = _spec(None, child_tools.parse_cli(["mcp:windows-mcp:all"]))
    assert spec["tools"] == {"mcp": ["windows-mcp"], "approve_all": ["windows-mcp"]}
    plan = child_tools.claude_plan(spec, cwd=str(tmp_path), chrome=False,
                                   claude_json=cj, platform="linux")
    allowed = plan["flags"][plan["flags"].index("--allowed-tools") + 1].split(",")
    assert "mcp__windows-mcp__PowerShell" in allowed and len(allowed) == 20
    assert "arbitrary code" in child_tools.prompt_text(spec, "claude")


@pytest.mark.parametrize("definition", [
    {"command": "/usr/bin/uvx", "args": ["windows-mcp@0.9.0"]},
    {"command": "/home/me/windows-mcp.sh"},
])
def test_unknown_version_is_never_approved_as_a_whole(tmp_path, definition):
    """Review P1-1 (c): an upgraded or unknown server gets no approval at
    all, so a resume cannot widen what the launch approved."""
    cj = _claude_json(tmp_path / "c.json", servers={"windows-mcp": definition})
    for tools in ({"screen": {"access": "operate", "server": "windows-mcp"}},
                  {"mcp": ["windows-mcp"]}):
        plan = child_tools.claude_plan(_spec(None, tools), cwd=str(tmp_path),
                                       chrome=False, claude_json=cj, platform="linux")
        assert plan["flags"] == []
    with pytest.raises(child_tools.ToolsError, match="cannot list"):
        child_tools.claude_plan(_spec(None, {"mcp": ["windows-mcp"], "approve_all": ["windows-mcp"]}),
                                cwd=str(tmp_path), chrome=False, claude_json=cj, platform="linux")


def test_codex_operate_exposes_and_approves_only_screen_tools():
    config = {"mcp_servers": {
        "windows-mcp": {"command": "/mnt/c/uvx.exe", "args": ["windows-mcp@0.8.6"]},
        "node_repl": {"command": "/usr/bin/node_repl"}}}
    child_tools.codex_apply(config, _spec("mail-only", {
        "screen": {"access": "operate", "server": "windows-mcp"}, "mcp": ["node_repl"]}))
    windows = config["mcp_servers"]["windows-mcp"]
    screen = {"Screenshot", "Snapshot", *SCREEN_OPERATE}
    assert set(windows["enabled_tools"]) == screen
    assert set(windows["tools"]) == screen
    node = config["mcp_servers"]["node_repl"]
    assert node == {"command": "/usr/bin/node_repl", "enabled": True}
    for server_cfg in config["mcp_servers"].values():
        assert "default_tools_approval_mode" not in server_cfg


CHROME_DEVTOOLS = {"command": "npx", "args": ["-y", "chrome-devtools-mcp@latest", "--autoConnect"]}
ARBITRARY = ("evaluate_script", "upload_file", "take_heapsnapshot",
             "performance_start_trace", "performance_stop_trace", "lighthouse_audit",
             "get_network_request", "take_screenshot", "take_snapshot")


def test_chrome_devtools_table_splits_every_tool_into_one_class():
    table = child_tools.TOOL_TABLE["chrome-devtools-mcp"]["1.10.1"]
    tiers = [set(table["read"]), set(table["operate"]), set(ARBITRARY)]
    assert set().union(*tiers) == set(table["all"]) and sum(map(len, tiers)) == 30
    # the name-classified entry exposes exactly the same names, and no "all"
    star = child_tools.TOOL_TABLE["chrome-devtools-mcp"]["*"]
    assert star["read"] == table["read"] and star["operate"] == table["operate"]
    assert "all" not in star


@pytest.mark.parametrize("args", [
    ["-y", "chrome-devtools-mcp@latest", "--autoConnect"],
    ["chrome-devtools-mcp", "--autoConnect"],
    ["-y", "chrome-devtools-mcp@9.9.9"],
    ["-y", "chrome-devtools-mcp@1.10.1"],
])
def test_codex_browser_exposes_and_approves_only_the_listed_names(args):
    config = {"mcp_servers": {"chrome-devtools": {"command": "npx", "args": args}}}
    child_tools.codex_apply(config, _spec(None, {"browser": True}))
    server = config["mcp_servers"]["chrome-devtools"]
    table = child_tools.TOOL_TABLE["chrome-devtools-mcp"]["*"]
    names = set(table["read"]) | set(table["operate"])
    assert server["enabled"] is True
    assert set(server["enabled_tools"]) == names == set(server["tools"])
    assert all(v == {"approval_mode": "approve"} for v in server["tools"].values())
    assert "default_tools_approval_mode" not in server
    for tool in ARBITRARY:
        assert tool not in server["enabled_tools"]


def test_codex_browser_read_only_through_the_screen_form():
    config = {"mcp_servers": {"chrome-devtools": dict(CHROME_DEVTOOLS)}}
    child_tools.codex_apply(config, _spec(None, {
        "screen": {"access": "read", "server": "chrome-devtools"}}))
    table = child_tools.TOOL_TABLE["chrome-devtools-mcp"]["*"]
    assert set(config["mcp_servers"]["chrome-devtools"]["enabled_tools"]) == set(table["read"])


@pytest.mark.parametrize("access", ["read", "operate"])
def test_browser_selection_cannot_approve_host_file_output_arguments(access):
    config = {"mcp_servers": {"chrome-devtools": dict(CHROME_DEVTOOLS)}}
    child_tools.codex_apply(config, _spec(None, {
        "screen": {"access": access, "server": "chrome-devtools"}}))
    approved = config["mcp_servers"]["chrome-devtools"]["tools"]
    assert not {"take_snapshot", "take_screenshot", "get_network_request"} & approved.keys()


@pytest.mark.parametrize("definition", [
    {"command": "node", "args": ["/tmp/chrome-devtools-mcp"]},
    {"command": "node", "args": ["/tmp/chrome-devtools-mcp@1.10.1"]},
    {"command": "npx", "args": ["--package", "chrome-devtools-mcp@1.10.1",
                                "node", "/tmp/custom-mcp.js"]},
    {"command": "npx", "args": ["-p", "chrome-devtools-mcp@latest", "other-server"]},
    {"command": "npx", "args": ["other-server", "chrome-devtools-mcp@1.10.1"]},
    {"command": "npx", "args": ["--", "-y", "chrome-devtools-mcp@1.10.1"]},
])
def test_chrome_package_in_wrapper_or_dependency_never_gets_approved(definition):
    assert child_tools.table_entry(definition) is None
    for selection in ({"browser": True},
                      {"screen": {"access": "read", "server": "chrome-devtools"}},
                      {"mcp": ["chrome-devtools"], "approve_all": ["chrome-devtools"]}):
        with pytest.raises(child_tools.ToolsError):
            child_tools.codex_apply({"mcp_servers": {"chrome-devtools": definition.copy()}},
                                    _spec(None, selection))


def test_browser_server_name_does_not_classify_a_different_package():
    with pytest.raises(child_tools.ToolsError, match="does not run chrome-devtools"):
        child_tools.codex_apply({"mcp_servers": {"chrome-devtools": {
            "command": "uvx", "args": ["windows-mcp@0.8.6"]}}},
            _spec(None, {"browser": True}))


@pytest.mark.parametrize("access", ["read", "operate"])
def test_codex_browser_and_another_screen_have_independent_approvals(access):
    config = {"mcp_servers": {
        "cdp": dict(CHROME_DEVTOOLS),
        "windows-mcp": {"command": "uvx", "args": ["windows-mcp@0.8.6"]}}}
    child_tools.codex_apply(config, _spec(None, {
        "browser": True, "screen": {"access": access, "server": "windows-mcp"}}))
    chrome = child_tools.TOOL_TABLE["chrome-devtools-mcp"]["*"]
    assert set(config["mcp_servers"]["cdp"]["enabled_tools"]) == \
        set(chrome["read"] + chrome["operate"])
    windows = {"Screenshot", "Snapshot"}
    if access == "operate":
        windows.update(SCREEN_OPERATE)
    assert set(config["mcp_servers"]["windows-mcp"]["enabled_tools"]) == windows


def test_claude_pinned_chrome_read_explicitly_denies_operating_and_file_tools(tmp_path):
    cj = _claude_json(tmp_path / "c.json", servers={"cdp": {
        "command": "/usr/local/bin/npx", "args": ["chrome-devtools-mcp@1.10.1"]}})
    plan = child_tools.claude_plan(_spec(None, {
        "screen": {"access": "read", "server": "cdp"}}), cwd=str(tmp_path),
        chrome=False, claude_json=cj, platform="linux")
    flags = plan["flags"]
    allowed = set(flags[flags.index("--allowed-tools") + 1].split(","))
    denied = set(flags[flags.index("--disallowed-tools") + 1].split(","))
    table = child_tools.TOOL_TABLE["chrome-devtools-mcp"]["1.10.1"]
    assert allowed == {f"mcp__cdp__{tool}" for tool in table["read"]}
    assert denied == {f"mcp__cdp__{tool}" for tool in table["all"] if tool not in table["read"]}


@pytest.mark.parametrize("version", ["latest", "9.9.9", ""])
@pytest.mark.parametrize("access", ["read", "operate"])
def test_claude_unknown_chrome_version_cannot_hide_unknown_tools(tmp_path, version, access):
    package = "chrome-devtools-mcp" + ("@" + version if version else "")
    cj = _claude_json(tmp_path / "c.json", servers={"cdp": {
        "command": "/usr/local/bin/npx", "args": [package]}})
    with pytest.raises(child_tools.ToolsError, match="must pin"):
        child_tools.claude_plan(_spec(None, {
            "screen": {"access": access, "server": "cdp"}}), cwd=str(tmp_path),
            chrome=False, claude_json=cj, platform="linux")


def test_codex_browser_finds_a_renamed_server_and_needs_one():
    config = {"mcp_servers": {"cdp": dict(CHROME_DEVTOOLS)}}
    child_tools.codex_apply(config, _spec(None, {"browser": True}))
    assert config["mcp_servers"]["cdp"]["enabled"] is True
    with pytest.raises(child_tools.ToolsError, match="chrome-devtools MCP server"):
        child_tools.codex_apply({"mcp_servers": {"x": {"command": "/bin/x"}}},
                                _spec(None, {"browser": True}))
    with pytest.raises(child_tools.ToolsError, match="wrapper"):
        child_tools.codex_apply(
            {"mcp_servers": {"chrome-devtools": {"command": "/home/me/cdp.sh"}}},
            _spec(None, {"browser": True}))


def test_codex_browser_all_needs_a_pinned_version_and_adds_evaluate_script():
    unpinned = {"mcp_servers": {"chrome-devtools": dict(CHROME_DEVTOOLS)}}
    with pytest.raises(child_tools.ToolsError, match="cannot list"):
        child_tools.codex_apply(unpinned, _spec(None, {
            "mcp": ["chrome-devtools"], "approve_all": ["chrome-devtools"]}))
    pinned = {"mcp_servers": {"chrome-devtools": {
        "command": "npx", "args": ["-y", "chrome-devtools-mcp@1.10.1"]}}}
    child_tools.codex_apply(pinned, _spec(None, {
        "mcp": ["chrome-devtools"], "approve_all": ["chrome-devtools"]}))
    assert "evaluate_script" in pinned["mcp_servers"]["chrome-devtools"]["tools"]


def test_codex_browser_mail_only_keeps_other_servers_off_and_describes_itself():
    spec = _spec("mail-only", {"browser": True})
    assert "chrome-devtools MCP server" in child_tools.prompt_text(spec, "codex")
    assert "Claude in Chrome" in child_tools.prompt_text(spec, "claude")


def test_codex_all_needs_a_known_version():
    config = {"mcp_servers": {"x": {"command": "/bin/x"}}}
    with pytest.raises(child_tools.ToolsError, match="cannot list"):
        child_tools.codex_apply(config, _spec(None, {"mcp": ["x"], "approve_all": ["x"]}))


def test_mac_codex_screen_through_node_repl_cannot_be_read_only():
    """Mac Codex reaches the screen and Chrome through node_repl (any JS), so
    no tool split exists; it is not in the table and read only is refused."""
    config = {"mcp_servers": {"node_repl": {"command": "/usr/bin/node_repl"}}}
    with pytest.raises(child_tools.ToolsError, match="classification table"):
        child_tools.codex_apply(config, _spec(
            None, {"screen": {"access": "read", "server": "node_repl"}}))


@pytest.mark.parametrize("session, warns", [("0", True), ("2", False)])
def test_wsl_session_zero_is_reported(tmp_path, monkeypatch, session, warns):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _executable(bindir / "powershell.exe", f"#!/bin/bash\nprintf '{session}\\r\\n'\n")
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    spec = _spec(None, {"screen": {"access": "read", "server": "windows-mcp"}})
    warning = child_tools.windows_session_warning(spec, environ={"WSL_DISTRO_NAME": "Ubuntu"})
    assert ("session 0" in warning) is warns
    # Not WSL, or no screen server: nothing is probed.
    assert child_tools.windows_session_warning(spec, environ={}) == "" or \
        child_tools.running_in_wsl({})
    assert child_tools.windows_session_warning(
        _spec(None, {"mcp": ["x"]}), environ={"WSL_DISTRO_NAME": "Ubuntu"}) == ""


@pytest.mark.parametrize("definition", [
    {"command": "/home/me/windows-mcp.sh", "args": []},          # wrapper: version unknown
    {"command": "/usr/bin/uvx", "args": ["windows-mcp"]},        # unpinned
    {"command": "/usr/bin/uvx", "args": ["windows-mcp@0.9.0"]},  # not in the table
])
def test_read_only_needs_a_known_pinned_version(tmp_path, definition):
    cj = _claude_json(tmp_path / "c.json", servers={"windows-mcp": definition})
    with pytest.raises(child_tools.ToolsError, match="classification table"):
        child_tools.claude_plan(
            _spec(None, {"screen": {"access": "read", "server": "windows-mcp"}}),
            cwd=str(tmp_path), chrome=False, claude_json=cj, platform="linux")


def test_project_scope_wins_over_user_scope(tmp_path):
    cj = _claude_json(tmp_path / "c.json", servers={"x": {"command": "/user/x"}},
                      projects={str(tmp_path): {"mcpServers": {"x": {"command": "/project/x"}}}})
    plan = child_tools.claude_plan(_spec(None, {"mcp": ["x"]}), cwd=str(tmp_path),
                                   chrome=False, claude_json=cj, platform="linux")
    assert plan["servers"]["x"]["command"] == "/project/x"


@pytest.mark.parametrize("definition, message", [
    (None, "is not in ~/.claude.json"),
    ({"command": "/bin/x", "env": {"API_KEY": "secret"}}, "has env values"),
    ({"type": "http", "url": "https://x", "headers": {"Authorization": "b"}}, "has headers values"),
    ({"command": "npx", "args": ["x"]}, "not an absolute path"),
    ({"command": "./x"}, "not an absolute path"),
    ({"command": "/bin/x", "cwd": "rel"}, "relative cwd"),
    ({"type": "http"}, "has no url"),
])
def test_servers_that_cannot_be_copied_stop_the_launch(tmp_path, definition, message):
    servers = {} if definition is None else {"x": definition}
    cj = _claude_json(tmp_path / "c.json", servers=servers)
    with pytest.raises(child_tools.ToolsError, match=message):
        child_tools.claude_plan(_spec(None, {"mcp": ["x"]}), cwd=str(tmp_path),
                                chrome=False, claude_json=cj, platform="linux")


def test_config_keeps_every_mail_alias_on_the_proxy(tmp_path):
    cj = _claude_json(tmp_path / "c.json", servers={"mcp-agent-mail": {"url": "http://x"},
                                                    "notes": {"command": "/bin/n"}})
    config = child_tools.claude_config({"command": "/proxy"}, claude_json=cj,
                                       servers={"notes": {"command": "/bin/n"}})
    assert config == {"mcpServers": {
        "mcp-agent-mail": {"command": "/proxy"}, "orrery-mail": {"command": "/proxy"},
        "notes": {"command": "/bin/n"}}}


# --------------------------------------------------------------------------- #
# Codex config
# --------------------------------------------------------------------------- #
def test_codex_read_only_exposes_and_approves_only_the_table_tools():
    config = {"mcp_servers": {"windows-mcp": {
        "command": "/mnt/c/uvx.exe", "args": ["windows-mcp@0.8.6", "serve"],
        "enabled": False, "default_tools_approval_mode": "approve"}}}
    child_tools.codex_apply(config, _spec(
        "mail-only", {"screen": {"access": "read", "server": "windows-mcp"}}))
    server_cfg = config["mcp_servers"]["windows-mcp"]
    assert server_cfg["enabled"] is True
    assert server_cfg["enabled_tools"] == ["Screenshot", "Snapshot"]
    assert "default_tools_approval_mode" not in server_cfg
    assert server_cfg["tools"] == {"Screenshot": {"approval_mode": "approve"},
                                   "Snapshot": {"approval_mode": "approve"}}


def test_codex_selected_server_must_exist():
    with pytest.raises(child_tools.ToolsError, match="not in the user's Codex"):
        child_tools.codex_apply({"mcp_servers": {}}, _spec("mail-only", {"mcp": ["x"]}))


# --------------------------------------------------------------------------- #
# launcher
# --------------------------------------------------------------------------- #
def _env(tmp_path, *, codex=False, proxy=True, claude_json=None):
    env, workdir = _fake_launch_env(tmp_path, codex=codex)
    for key in ("AGENTSTACK_CLAUDE_CHILD_CHROME", "AGENTSTACK_CLAUDE_CHILD_CHROME_DEVICE"):
        env.pop(key, None)
    if proxy:
        runner = tmp_path / "run-mcp.sh"
        _executable(runner, "#!/bin/bash\nexit 0\n")
        env["AGENTSTACK_MCP_PROXY"] = str(runner)
    env["AGENTSTACK_CLAUDE_JSON"] = claude_json or str(tmp_path / "no-claude.json")
    return env, workdir


def _launch(tmp_path, env, workdir, name, *flags, codex=False):
    handoff = _codex_handoff(tmp_path, name) if codex else _handoff(tmp_path, name)
    args = ["/bin/bash", str(SPAWN), "--pre-registered", name,
            "--child-token-file", str(handoff)]
    if codex:
        args.append("--codex")
    return subprocess.run(
        [*args, *flags, "mail task", str(workdir)], cwd=ROOT, env=env, text=True,
        capture_output=True, timeout=60, check=False)


def _inner(env) -> str:
    command = _new_session(env)[-1]
    return command.split(" -lc '", 1)[1][:-1]


def _launched(extra: str = "") -> str:
    """The launch command with tool flags ``extra`` and the first prompt."""
    return PRE_CHROME_INNER.replace(
        '"${MCP_ARGS[@]}";', '"${MCP_ARGS[@]}"' + extra + ARGV_PROMPT + ";")


def _no_launch(env) -> bool:
    return all(c[0] != "new-session" for c in _calls(env))


def test_launcher_default_is_the_command_used_before(tmp_path):
    env, workdir = _env(tmp_path)
    result = _launch(tmp_path, env, workdir, "PlainChild")
    assert result.returncode == 0, result.stderr
    assert _inner(env) == _launched()
    assert _records(env, "PlainChild") == []


def test_launcher_explicit_default_base_is_the_same_launch(tmp_path):
    env, workdir = _env(tmp_path)
    result = _launch(tmp_path, env, workdir, "PlainChild", "--base", "default")
    assert result.returncode == 0, result.stderr
    assert _inner(env) == _launched()


def test_launcher_mail_only_gives_mail_and_no_chrome(tmp_path):
    env, workdir = _env(tmp_path)
    result = _launch(tmp_path, env, workdir, "MailOnly", "--base", "mail-only")
    assert result.returncode == 0, result.stderr
    inner = _inner(env)
    assert inner == _launched(" --no-chrome")
    config = json.loads((pathlib.Path(env["AGENTSTACK_RUNTIME_DIR"]) / "child-agents"
                         / "MailOnly.mcp.json").read_text())
    assert sorted(config["mcpServers"]) == ["orrery-mail"]
    records = _records(env, "MailOnly")
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    assert record["base"] == "mail-only" and record["tools"] == {}
    assert record["claude_chrome"] is False
    assert "base mail-only" in _log_text(env)


def test_launcher_copies_a_selected_server_and_restricts_read_tools(tmp_path):
    cj = _claude_json(tmp_path / "c.json", servers={"windows-mcp": WINDOWS_MCP})
    env, workdir = _env(tmp_path, claude_json=cj)
    result = _launch(tmp_path, env, workdir, "ReadChild",
                     "--base", "mail-only", "--tools", "screen:read:windows-mcp")
    # The built-in computer use is macOS only; a named server works anywhere.
    assert result.returncode == 0, result.stderr
    inner = _inner(env)
    assert inner == _launched(" --no-chrome " + " ".join(READ_FLAGS))
    config = json.loads((pathlib.Path(env["AGENTSTACK_RUNTIME_DIR"]) / "child-agents"
                         / "ReadChild.mcp.json").read_text())
    assert config["mcpServers"]["windows-mcp"] == WINDOWS_MCP
    assert "read only" in _log_text(env)


def test_launcher_browser_tool_is_claude_in_chrome(tmp_path):
    env, workdir = _env(tmp_path)
    result = _launch(tmp_path, env, workdir, "BrowserChild",
                     "--base", "mail-only", "--tools", "browser:win-brave")
    assert result.returncode == 0, result.stderr
    inner = _inner(env)
    assert inner == _launched(" --chrome")
    assert "--no-chrome" not in inner
    assert "deviceId win-brave" in _log_text(env)


def test_launcher_rejects_disagreeing_browser_requests(tmp_path):
    env, workdir = _env(tmp_path)
    result = _launch(tmp_path, env, workdir, "Mismatch", "--claude-chrome-device", "a",
                     "--tools", "browser:b")
    assert result.returncode != 0
    assert "different browsers" in result.stderr
    assert _no_launch(env)


def test_launcher_fails_closed_without_the_mail_proxy(tmp_path):
    env, workdir = _env(tmp_path, proxy=False)
    result = _launch(tmp_path, env, workdir, "NoProxy", "--base", "mail-only")
    assert result.returncode != 0
    assert "ORRERY Mail proxy" in result.stderr
    assert _no_launch(env)
    assert _records(env, "NoProxy") == []


def test_launcher_without_selection_still_falls_back_without_the_proxy(tmp_path):
    env, workdir = _env(tmp_path, proxy=False)
    result = _launch(tmp_path, env, workdir, "Fallback")
    assert result.returncode == 0, result.stderr
    assert "No MCP proxy available" in result.stderr


@pytest.mark.skipif(sys.platform != "darwin", reason="the built-in computer use is macOS only")
def test_launcher_stops_mail_only_where_computer_use_is_enabled(tmp_path):
    env, workdir = _env(tmp_path)
    cj = _claude_json(tmp_path / "c.json",
                      projects={str(workdir): {"enabledMcpServers": ["computer-use"]}})
    env["AGENTSTACK_CLAUDE_JSON"] = cj
    result = _launch(tmp_path, env, workdir, "CuChild", "--base", "mail-only")
    assert result.returncode != 0
    assert "computer use is enabled" in result.stderr
    assert _no_launch(env)


def test_launcher_stops_when_a_server_cannot_be_copied(tmp_path):
    cj = _claude_json(tmp_path / "c.json", servers={"x": {"command": "npx"}})
    env, workdir = _env(tmp_path, claude_json=cj)
    result = _launch(tmp_path, env, workdir, "BadServer", "--tools", "mcp:x")
    assert result.returncode != 0
    assert "not an absolute path" in result.stderr
    assert _no_launch(env)


@pytest.mark.parametrize("flags, message", [
    (["--base", "all"], "base must be"),
    (["--tools", "shell"], "unknown --tools entry"),
    (["--tools", "mcp:orrery-mail"], "is ORRERY Mail"),
    (["--codex", "--tools", "browser:dev"], "deviceId"),
    (["--codex", "--codex-mcp", "inherit", "--base", "mail-only"], "contradicts"),
])
def test_launcher_rejects_invalid_selections_before_launch(tmp_path, flags, message):
    env, workdir = _env(tmp_path)
    result = subprocess.run(
        ["/bin/bash", str(SPAWN), "--pre-registered", "BadSel",
         "--child-token-file", str(_handoff(tmp_path, "BadSel")), *flags,
         "task", str(workdir)],
        cwd=ROOT, env=env, text=True, capture_output=True, timeout=60, check=False)
    assert result.returncode != 0
    assert message in result.stderr
    assert _no_launch(env)


def _codex_env(tmp_path, config_text):
    env, workdir = _env(tmp_path, codex=True)
    source = tmp_path / "source-codex-home"
    source.mkdir()
    (source / "auth.json").write_text("{}\n", encoding="utf-8")
    (source / "config.toml").write_text(config_text, encoding="utf-8")
    env["CODEX_HOME"] = str(source)
    return env, workdir


CODEX_CONFIG = (
    '[mcp_servers.windows-mcp]\ncommand = "/mnt/c/uvx.exe"\n'
    'args = ["windows-mcp@0.8.6", "serve"]\n\n'
    '[mcp_servers.chrome-devtools]\ncommand = "npx"\n'
)


def _codex_child_config(env, name) -> dict:
    home = pathlib.Path(env["AGENTSTACK_RUNTIME_DIR"]) / "child-agents" / f"{name}.codex-home"
    return tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))


def test_codex_mail_only_read_screen(tmp_path):
    env, workdir = _codex_env(tmp_path, CODEX_CONFIG)
    result = _launch(tmp_path, env, workdir, "CodexRead", "--base", "mail-only",
                     "--tools", "screen:read:windows-mcp", codex=True)
    assert result.returncode == 0, result.stderr
    config = _codex_child_config(env, "CodexRead")
    windows = config["mcp_servers"]["windows-mcp"]
    assert windows["enabled"] is True
    assert windows["enabled_tools"] == ["Screenshot", "Snapshot"]
    assert set(windows["tools"]) == {"Screenshot", "Snapshot"}
    assert config["mcp_servers"]["chrome-devtools"]["enabled"] is False
    record = json.loads((pathlib.Path(env["AGENTSTACK_RUNTIME_DIR"]) / "child-agents"
                         / "CodexRead.tools.json").read_text())
    assert record["base"] == "mail-only"
    state = json.loads((pathlib.Path(env["AGENTSTACK_RUNTIME_DIR"]) / "child-agents"
                        / "CodexRead.json").read_text())
    assert state["codex_mcp_profile"] == "orrery-only"
    assert "read only" in _log_text(env)


def test_codex_default_launch_writes_no_record(tmp_path):
    env, workdir = _codex_env(tmp_path, CODEX_CONFIG)
    stale = pathlib.Path(env["AGENTSTACK_RUNTIME_DIR"]) / "child-agents" / "CodexPlain.tools.json"
    stale.parent.mkdir(parents=True)
    stale.write_text("{}", encoding="utf-8")
    result = _launch(tmp_path, env, workdir, "CodexPlain", codex=True)
    assert result.returncode == 0, result.stderr
    assert not stale.exists()
    assert "enabled" not in _codex_child_config(env, "CodexPlain")["mcp_servers"]["windows-mcp"]


def test_codex_fails_closed_on_an_unknown_server(tmp_path):
    env, workdir = _codex_env(tmp_path, CODEX_CONFIG)
    result = _launch(tmp_path, env, workdir, "CodexMissing", "--base", "mail-only",
                     "--tools", "mcp:nope", codex=True)
    assert result.returncode != 0
    assert "not in the user's Codex config.toml" in result.stderr
    assert _no_launch(env)


def test_codex_fails_closed_without_the_mail_proxy(tmp_path):
    env, workdir = _codex_env(tmp_path, CODEX_CONFIG)
    env["AGENTSTACK_MCP_PROXY"] = str(tmp_path / "missing-proxy")
    result = _launch(tmp_path, env, workdir, "CodexNoProxy", "--base", "mail-only", codex=True)
    assert result.returncode != 0
    assert _no_launch(env)


# --------------------------------------------------------------------------- #
# launch record and resume
# --------------------------------------------------------------------------- #
def _policy(*args):
    return subprocess.run([sys.executable, str(POLICY), *args], text=True,
                          capture_output=True, check=False)


def test_record_v3_is_bound_and_announces_tools_at_session_start(tmp_path):
    d = str(tmp_path / "state")
    spec = json.dumps(_spec("mail-only", {"mcp": ["notes"]}))
    launch = "11111111-1111-1111-1111-111111111111"
    sid = "22222222-2222-2222-2222-222222222222"
    assert _policy("prepare", d, "A", launch, "", "0", "0", spec).returncode == 0
    out = _policy("session", d, "A", sid, launch).stdout
    assert "base mail-only" in out and "MCP server notes" in out
    assert "Browser:" not in out


def test_api_passes_base_and_tools_to_the_launcher(monkeypatch):
    captured = []
    monkeypatch.setattr(server, "_spawn_unavailable_error", lambda: None)
    monkeypatch.setattr(server, "_spawn_request", lambda _p: ({}, None))
    monkeypatch.setattr(server, "_claude_catalog", lambda: type(
        "C", (), {"error": None, "source": "local_cache", "models": ()})())
    monkeypatch.setattr(server, "spawn_with_launch_spec",
                        lambda _payload, spec: captured.append(spec) or {"ok": True})
    result = server.do_spawn({
        "task": "t", "model": "claude-opus-5-5", "base": "mail-only",
        "tools": {"browser": {"device": "win-brave"}, "screen": "operate",
                  "mcp": ["notes"]}})
    assert result == {"ok": True}
    assert captured[0].provider_args == (
        "--base", "mail-only", "--tools", "browser:win-brave",
        "--tools", "screen:operate", "--tools", "mcp:notes")
    captured.clear()
    assert server.do_spawn({"task": "t", "model": "claude-opus-5-5",
                            "tools": {"mcp": ["a", "b"], "approve_all": ["b"]}}) == {"ok": True}
    assert captured[0].provider_args == ("--tools", "mcp:a", "--tools", "mcp:b:all")


@pytest.mark.parametrize("payload, message", [
    ({"base": "all"}, "base must be"),
    ({"tools": ["mcp"]}, "tools must be an object"),
    ({"tools": {"mcp": ["orrery-mail"]}}, "is ORRERY Mail"),
    ({"claude_chrome_device": "a", "tools": {"browser": {"device": "b"}}}, "different browsers"),
    ({"provider": "codex", "tools": {"browser": {"device": "d"}}}, "deviceId"),
])
def test_api_rejects_invalid_selections(monkeypatch, payload, message):
    captured = []
    monkeypatch.setattr(server, "_spawn_unavailable_error", lambda: None)
    monkeypatch.setattr(server, "_spawn_request", lambda _p: ({}, None))
    monkeypatch.setattr(server, "_claude_catalog", lambda: type(
        "C", (), {"error": None, "source": "local_cache", "models": ()})())
    monkeypatch.setattr(server, "spawn_with_launch_spec",
                        lambda _payload, spec: captured.append(spec) or {"ok": True})
    result = server.do_spawn({"task": "t", **payload})
    assert result["ok"] is False
    assert message in result["error"]
    assert captured == []


def test_spawn_request_accepts_the_new_fields():
    request, error = server._spawn_request(
        {"task": "t", "standalone": True, "base": "mail-only", "tools": {}})
    assert error is None and request is not None


# --------------------------------------------------------------------------- #
# Claude resume restores the selection (or stops)
# --------------------------------------------------------------------------- #
from test_claude_resume_mail import NAME, SID, child, resume  # noqa: E402,F401

RESUME_LAUNCH = "44444444-4444-4444-4444-444444444444"


def _bound_tools_record(runtime, spec, *, chrome=False):
    d = str(runtime / "child-agents")
    assert _policy("prepare", d, NAME, RESUME_LAUNCH, "", "0",
                   "1" if chrome else "0", json.dumps(spec)).returncode == 0
    assert _policy("session", d, NAME, SID, RESUME_LAUNCH).returncode == 0
    assert (runtime / "child-agents" / f"{NAME}.claude-launch.{SID}.json").exists()


def test_resume_rebuilds_the_selected_config_and_flags(resume, monkeypatch, tmp_path):
    runtime, registration, launches, calls = resume
    cj = _claude_json(tmp_path / "c.json", servers={"notes": {"command": "/bin/notes"}})
    monkeypatch.setenv("AGENTSTACK_CLAUDE_JSON", cj)
    child(runtime, registration)
    _bound_tools_record(runtime, _spec("mail-only", {"mcp": ["notes"]}))
    result = server.do_resume(NAME)
    assert result["ok"], result
    inner = launches[0][-1]
    assert f"--strict-mcp-config --no-chrome --resume {SID} -n {NAME}" in inner
    assert "--chrome" not in inner.replace("--no-chrome", "")
    assert f"export AGENTSTACK_CLAUDE_LAUNCH_ID={RESUME_LAUNCH}; " in inner
    config = json.loads((runtime / "child-agents" / f"{NAME}.mcp.json").read_text())
    assert sorted(config["mcpServers"]) == ["notes", "orrery-mail"]


def test_resume_after_an_upgrade_does_not_widen_approval(resume, monkeypatch, tmp_path):
    """Review P1-1 (c): the server was 0.8.6 at launch and is 0.9.0 now."""
    runtime, registration, launches, calls = resume
    upgraded = {**WINDOWS_MCP, "args": ["windows-mcp@0.9.0", "serve"]}
    monkeypatch.setenv("AGENTSTACK_CLAUDE_JSON",
                       _claude_json(tmp_path / "c.json", servers={"windows-mcp": upgraded}))
    child(runtime, registration)
    _bound_tools_record(runtime, _spec(None, {"screen": {"access": "operate", "server": "windows-mcp"}}))
    result = server.do_resume(NAME)
    assert result["ok"], result
    assert "--allowed-tools" not in launches[0][-1]
    assert "mcp__windows-mcp" not in launches[0][-1]


def test_resume_without_child_state_refuses_a_selection(resume, monkeypatch, tmp_path):
    runtime, registration, launches, calls = resume
    monkeypatch.setenv("AGENTSTACK_CLAUDE_JSON", _claude_json(tmp_path / "c.json"))
    _bound_tools_record(runtime, _spec("mail-only"))
    result = server.do_resume(NAME)
    assert result["ok"] is False
    assert result["resume_capability"] == "config_unrestorable"
    assert "all of the user's MCP servers" in result["error"]
    assert not launches


def test_resume_stops_when_a_selected_server_is_gone(resume, monkeypatch, tmp_path):
    runtime, registration, launches, calls = resume
    monkeypatch.setenv("AGENTSTACK_CLAUDE_JSON", _claude_json(tmp_path / "c.json"))
    child(runtime, registration)
    _bound_tools_record(runtime, _spec(None, {"mcp": ["notes"]}))
    result = server.do_resume(NAME)
    assert result["ok"] is False
    assert "cannot be restored" in result["error"]
    assert not launches and not calls


def test_codex_resume_build_applies_the_record_or_fails(tmp_path):
    """build-home (used by spawn and by the dashboard resume) reads the record."""
    from hooks import child_resume
    runtime = tmp_path / "runtime"
    source = tmp_path / "codex"
    source.mkdir()
    (source / "config.toml").write_text(CODEX_CONFIG, encoding="utf-8")
    runner = tmp_path / "run-mcp.sh"
    _executable(runner, "#!/bin/bash\nexit 0\n")
    token = runtime / "agent_token_CodexResume"
    token.parent.mkdir(parents=True)
    token.write_text("t", encoding="utf-8")
    token.chmod(0o600)
    record = child_resume.tools_record_path(runtime, "CodexResume")
    child_tools.write_codex_record(str(record), _spec(
        "mail-only", {"screen": {"access": "read", "server": "windows-mcp"}}))

    def build(profile):
        return child_resume.build_home(
            home=runtime / "child-agents" / "CodexResume.codex-home", source=source,
            runner=runner, child="CodexResume", project_key="/p", token_file=token,
            mcp_url="http://127.0.0.1:1/mcp", mail_env="", runtime_dir=runtime,
            bearer_mode="auto", python_bin="", mcp_profile=profile)

    with pytest.raises(ValueError, match="mail-only"):
        build("inherit")
    home = build("orrery-only")
    config = tomllib.loads((home / "config.toml").read_text())
    assert config["mcp_servers"]["windows-mcp"]["enabled_tools"] == ["Screenshot", "Snapshot"]
    # A damaged record stops the build instead of widening the child.
    record.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        build("orrery-only")


# --------------------------------------------------------------------------- #
# review P2-1: the resume forecast sees the selection too
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("verify", [True, False])
def test_forecast_says_unrestorable_when_a_selected_server_is_gone(resume, monkeypatch, tmp_path, verify):
    runtime, registration, launches, calls = resume
    cj = tmp_path / "c.json"
    monkeypatch.setenv("AGENTSTACK_CLAUDE_JSON",
                       _claude_json(cj, servers={"notes": {"command": "/bin/notes"}}))
    monkeypatch.setattr(server, "_cached_claude_transcript_path",
                        lambda _n: (True, server._transcript_path(_n), None))
    child(runtime, registration)
    _bound_tools_record(runtime, _spec(None, {"mcp": ["notes"]}))
    assert server._resume_capability(NAME, "claude-code", category="retired",
                                     verify_transcript=verify) == "ready"
    _claude_json(cj)  # the user removed the server
    assert server._resume_capability(NAME, "claude-code", category="retired",
                                     verify_transcript=verify) == "config_unrestorable"
    result = server.do_resume(NAME)
    assert result["resume_capability"] == "config_unrestorable"
    assert not launches


def test_forecast_says_unrestorable_without_child_state(resume, monkeypatch, tmp_path):
    runtime, registration, launches, calls = resume
    monkeypatch.setenv("AGENTSTACK_CLAUDE_JSON", _claude_json(tmp_path / "c.json"))
    _bound_tools_record(runtime, _spec("mail-only"))
    assert server._resume_capability(NAME, "claude-code", category="retired") == "config_unrestorable"


def test_codex_forecast_checks_the_tools_record(monkeypatch, tmp_path):
    from hooks import child_resume
    runtime = tmp_path / "runtime"
    source = tmp_path / "codex"
    source.mkdir()
    (source / "config.toml").write_text(CODEX_CONFIG, encoding="utf-8")
    runner = tmp_path / "run-mcp.sh"
    _executable(runner, "#!/bin/bash\nexit 0\n")
    monkeypatch.setattr(server, "RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(server, "HOOKS_DIR", str(ROOT / "hooks"))
    monkeypatch.setenv("CODEX_HOME", str(source))
    monkeypatch.setenv("AGENTSTACK_MCP_PROXY", str(runner))
    module = server._child_resume_module()
    monkeypatch.setattr(module, "inspect_retained",
                        lambda *a, **k: {"codex_mcp_profile": "orrery-only"})
    monkeypatch.setattr(server, "_child_resume_module", lambda: module)
    registration = {"agent_id": 1, "project_key": "/p", "program": "codex"}
    record = child_resume.tools_record_path(runtime, "CodexCast")
    child_tools.write_codex_record(str(record), _spec("mail-only", {"mcp": ["windows-mcp"]}))
    assert server._codex_resume_child_home("CodexCast", registration)[1] == "orrery-only"
    child_tools.write_codex_record(str(record), _spec("mail-only", {"mcp": ["gone"]}))
    with pytest.raises(server._ResumeCapabilityError) as exc:
        server._codex_resume_child_home("CodexCast", registration)
    assert exc.value.code == "config_unrestorable"
