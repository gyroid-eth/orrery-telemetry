"""Human-wait notification: native screen evidence and the legacy Mail route."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from dashboard import harness_activity, human_wait, server


FIXTURES = Path(__file__).parent / "fixtures"
APPROVALS = json.loads((FIXTURES / "native_activity/approval-captures.json").read_text())["cases"]
NATIVE_WAITS = json.loads((FIXTURES / "native_activity/human-wait-captures.json").read_text())
TRUST = (FIXTURES / "codex-0.158-trust-frame.txt").read_text()


def observed(text, program):
    activity = (harness_activity.parse_activity(text, program) if program in {"dsh", "hermes"}
                else server._runtime_activity(text, unknown=None)[0])
    return human_wait.observe(text, program, activity)


def sample(capture, evidence, **changes):
    return {**dict(name="ChildCurie", program="codex", agent_id=2, project_key="/fixture",
                   generation="101:$1:17:23", parent="ParentNewton", capture_id=capture,
                   human_wait=evidence), **changes}


def test_compact_hermes_question_is_a_human_wait_without_quoting_the_body():
    capture = (FIXTURES / 'native_activity/hermes-clarify-compact.txt').read_text()
    assert harness_activity.parse_activity(capture,'hermes')=='question'
    evidence=observed(capture,'hermes')
    assert evidence.kind=='question'
    assert evidence.lines==('↑/↓ to select, Enter to lock, Tab next question',)
    assert not isinstance(observed(capture.replace('Tab next question',''), 'hermes'),human_wait.Screen)
    assert not isinstance(observed(capture + '\n❯ A new ordinary draft\n', 'hermes'),human_wait.Screen)
    assert not isinstance(observed(capture.replace('2. Detailed report','3. Detailed report'), 'hermes'),human_wait.Screen)


@pytest.mark.parametrize("case", APPROVALS, ids=lambda c: c["id"])
def test_real_approval_notifies_once_and_dismissed_or_stale_does_not(case, tmp_path):
    program = case["program"]
    evidence = observed(case["active"], program)
    assert evidence.kind == "approval"
    assert observed(case["dismissed"], program) == "clear"
    assert observed(case["active"] + "\n" + case["dismissed"], program) == "clear"

    mail = []
    monitor = human_wait.Monitor(tmp_path / "human-waits.json",
                                lambda row, screen: mail.append((row["parent"], screen)) or "sent")
    for second in (0, 10, 19):
        monitor.tick([sample(second, evidence)])
    assert not mail
    for second in (20, 30, 40):
        monitor.tick([sample(second, evidence)])
    assert mail == [("ParentNewton", evidence)]
    for second in (50, 60, 70):
        monitor.tick([sample(second, observed(case["dismissed"], program))])
    assert mail == [("ParentNewton", evidence)]


@pytest.mark.parametrize("case", NATIVE_WAITS["cases"], ids=lambda c: c["id"])
def test_native_choices_require_the_current_complete_frame(case, tmp_path):
    text, program = case["active"], case["program"]
    screen = observed(text, program)
    assert screen.kind == case["kind"] and len(screen.lines) in (1, 2)
    assert all(line in text for line in screen.lines)
    assert observed(text + "\n" * 60, program) == screen
    assert not isinstance(observed("\n".join(text.splitlines()[:-1]), program), human_wait.Screen)
    idle = next(row["dismissed"] for row in APPROVALS if row["program"] == program)
    assert not isinstance(observed(text + "\n" + idle, program), human_wait.Screen)

    sent = []
    monitor = human_wait.Monitor(tmp_path / "human-waits.json", lambda *args: sent.append(args) or "sent")
    for capture, evidence in ((0, screen), (10, screen), (20, screen), (30, screen), (40, "clear")):
        monitor.tick([sample(capture, evidence)])
    assert len(sent) == 1 and sent[0][0]["capture_id"] == 20


@pytest.mark.parametrize("case", NATIVE_WAITS["negative"], ids=lambda c: c["id"])
def test_working_waiting_and_drafts_do_not_send(case, tmp_path):
    evidence = observed(case["active"], case["program"])
    assert not isinstance(evidence, human_wait.Screen)
    monitor = human_wait.Monitor(tmp_path / "human-waits.json", lambda *args: pytest.fail("unexpected Mail"))
    for capture in (0, 10, 20, 30):
        monitor.tick([sample(capture, evidence)])
    assert not monitor.path.exists()


def test_agy_browser_approval_survives_wrapped_choices_without_quoting_the_host():
    capture = next(case["active"] for case in NATIVE_WAITS["cases"] if case["id"] == "agy-url-approval")
    wrapped = capture.replace(" access to 'example.com'", "\n    access to 'example.com'")
    screen = observed(wrapped, "agy")
    assert screen == human_wait.Screen("approval", ("Allow access to this URL?", "↑/↓ Navigate · tab Amend"))
    assert "example.com" not in screen.message("ChildCurie")[1]
    incomplete = wrapped.replace("6. No, and always deny", "6. No, and never deny")
    assert not isinstance(observed(incomplete, "agy"), human_wait.Screen)


@pytest.mark.parametrize("program,transcript", [
    ("agy", "⎿  Interrupted · What should Antigravity CLI do instead?"),
    ("claude", "⎿  Interrupted · What should Claude do instead?"),
    ("codex", "■ Conversation interrupted. Tell the model what to do differently."),
    ("dsh", "● Cancelled fixture r"),
    ("hermes", "[Interrupted]"),
])
def test_a_user_interruption_does_not_create_an_episode(program, transcript, tmp_path):
    idle = next(case["dismissed"] for case in APPROVALS if case["program"] == program)
    monitor = human_wait.Monitor(tmp_path / "human-waits.json", lambda *args: pytest.fail("unexpected Mail"))
    for capture in (0, 10, 20, 30):
        monitor.tick([sample(capture, observed(transcript + "\n" + idle, program))])
    assert not monitor.path.exists()


def test_agy_mcp_approval_keeps_tool_name_out_of_the_notice():
    text = next(case["active"] for case in NATIVE_WAITS["cases"] if case["id"] == "agy-mcp-approval")
    assert server._parse_runtime(text)["act_state"] == "ask"
    approval = observed(text, "agy")
    assert approval.kind == "approval"
    assert observed(text.replace("> 1.", "  1.").replace("  6.", "> 6."), "agy") == approval
    for broken in (text.replace("'orrery-mail/whois'", "'other/tool'", 1),
                   text.replace("Allow calling this tool?", "Call the tool"),
                   text.replace("  4. No, deny tool call\n", "")):
        assert observed(broken, "agy") is None
    assert all("orrery-mail/whois" not in part for part in approval.message("ChildCurie"))


def test_codex_folder_trust_requires_the_complete_current_choice():
    expected = human_wait.Screen("trust", ("Trust this folder?", "enter continue · esc quit"))
    assert observed(TRUST, "codex") == expected
    assert observed(TRUST.replace("› 1.", "  1.").replace("  2.", "› 2."), "codex") == expected
    for incomplete in ("Trust this folder?", TRUST.replace("› 1.", "  1."),
                       TRUST.replace("  2. Quit", "  2. Continue"),
                       TRUST + "\n› Draft input\n\n  gpt-5.4 medium · /fixture"):
        assert not isinstance(observed(incomplete, "codex"), human_wait.Screen)


def test_legacy_display_approval_heuristics_alone_do_not_send_mail():
    for program in ("claude", "codex", "agy"):
        for weak in ("Do you want to continue? (y/n)", "Press enter to confirm or esc to cancel"):
            assert server._parse_runtime(weak)["act_state"] == "ask"
            assert observed(weak, program) is None


def test_confirmed_clear_rearms_and_restart_or_unknown_delivery_does_not_duplicate(tmp_path):
    path = tmp_path / "human-waits.json"
    sent = []

    def send(row, screen):
        sent.append(row["capture_id"])
        return "delivery_unknown"

    evidence = observed(TRUST, "codex")
    first = human_wait.Monitor(path, send)
    second = human_wait.Monitor(path, send)
    for monitor in (first, second):
        for capture in range(0, 31, 10):
            monitor.tick([sample(capture, evidence)])
    assert sent == [20]
    for capture in range(40, 61, 10):
        second.tick([sample(capture, None if capture == 40 else evidence)])
    assert sent == [20]

    second.tick([sample(70, "clear")])
    for capture in range(80, 111, 10):
        second.tick([sample(capture, evidence)])
    assert sent == [20, 100]


def test_missed_capture_restarts_timer_without_creating_a_journal(tmp_path):
    path = tmp_path / "human-waits.json"
    sent = []
    monitor = human_wait.Monitor(path, lambda row, _: sent.append(row["capture_id"]) or "sent")
    evidence = observed(TRUST, "codex")
    monitor.tick([sample(0, evidence)])
    monitor.tick([sample(10, evidence)])
    monitor.tick([])
    for capture in (30, 30, 40):
        monitor.tick([sample(capture, evidence)])
    assert not sent and not path.exists()
    monitor.tick([sample(50, evidence)])
    assert sent == [50]


def test_another_dashboard_observing_clear_restarts_the_full_timer(tmp_path):
    path = tmp_path / "human-waits.json"
    sent = []
    send = lambda row, _: sent.append(row["capture_id"]) or "sent"
    first = human_wait.Monitor(path, send)
    second = human_wait.Monitor(path, send)
    evidence = observed(TRUST, "codex")
    for capture in (0, 10, 20):
        first.tick([sample(capture, evidence)])
    second.tick([sample(25, "clear")])
    for capture in (30, 40, 50):
        first.tick([sample(capture, evidence)])
    assert sent == [20, 50]


@pytest.mark.parametrize("program", ["claude", "codex-cli", "dsh", "hermes", "antigravity"])
def test_shared_legacy_sweep_observes_children_before_the_first_turn(monkeypatch, program):
    native = human_wait.PROGRAMS[program]
    text = next(case["active"] for case in NATIVE_WAITS["cases"] if case["program"] == native)
    session = dict(created=101, session_id="$1", pane_pid=17, server_pid="23")
    monkeypatch.setattr(server, "tmux_state", lambda: {"ChildCurie": session, "Standalone": session})
    monkeypatch.setattr(server, "agentmail_state", lambda: ({
        name: {"program": program} for name in ("ChildCurie", "Standalone")}, {}))
    monkeypatch.setattr(server, "_process_tree_snapshot", lambda: {})
    monkeypatch.setattr(server, "_tmux_row_state", lambda *args: (program, "agent", True, None))
    monkeypatch.setattr(server, "_stall_parent", lambda name, gen: "ParentNewton" if name == "ChildCurie" else "")
    monkeypatch.setattr(server, "_resume_registration", lambda *args: {"agent_id": 2, "project_key": "/fixture"})
    monkeypatch.setattr(server, "_rt_cache", {})
    monkeypatch.setattr(server, "_NATIVE_CONTEXT_CACHE", {})
    monkeypatch.setattr(server, "_provider_captures", {})
    monkeypatch.setattr(server, "_human_wait_captures", {})
    captures = []
    monkeypatch.setattr(server, "_tmux_run", lambda args, **kw: captures.append(args)
                        or SimpleNamespace(returncode=0, stdout=text))

    waits = []
    server._provider_stall_samples(waits)

    [child] = waits
    assert child["human_wait"] == observed(text, native)
    assert child["name"] == "ChildCurie" and child["parent"] == "ParentNewton"
    assert sum("ChildCurie" in " ".join(args) for args in captures) == 1


@pytest.mark.parametrize("parent, owner, disabled, generation_ok", [
    ("ParentNewton", "fixture-owner", "", True), ("DifferentParent", "fixture-owner", "", True),
    ("ParentNewton", "other-owner", "", True), ("ParentNewton", "fixture-owner", "1", True),
    ("ParentNewton", "fixture-owner", "", False),
])
def test_sender_revalidates_the_child_parent_and_mail_enabled(tmp_path, monkeypatch,
                                                             parent, owner, disabled, generation_ok):
    state_dir = tmp_path / "child-agents"
    state_dir.mkdir()
    state = state_dir / "ChildCurie.json"
    state.write_text(json.dumps(dict(agent_id=2, agent_name="ChildCurie", project_key="/fixture",
                                     program="codex", launch_origin="child", parent_agent=parent,
                                     registration_token=owner)))
    state.chmod(0o600)
    owner_file = tmp_path / "agent_token_ChildCurie"
    owner_file.write_text("fixture-owner")
    owner_file.chmod(0o600)
    monkeypatch.setattr(server, "RUNTIME_DIR", str(tmp_path))
    session = dict(created=101, session_id="$1", pane_pid=17, server_pid="23" if generation_ok else "24",
                   mail_disabled=disabled)
    monkeypatch.setattr(server, "tmux_state", lambda: {"ChildCurie": session})
    monkeypatch.setattr(server, "_tmux", lambda argv: "PARENT_AGENT=ParentNewton\n")
    monkeypatch.setattr(server, "_resume_registration", lambda name, programs: {
        "agent_id": 2 if name == "ChildCurie" else 1, "project_key": "/fixture"})
    calls = []
    monkeypatch.setattr(server, "_mcp_call", lambda tool, args, **kw: calls.append((tool, args)) or {"ok": True})

    screen = observed(TRUST, "codex")
    result = server._human_wait_send(sample(20, screen), screen)

    allowed = parent == "ParentNewton" and owner == "fixture-owner" and not disabled and generation_ok
    assert result == ("sent" if allowed else "parent_unverified")
    if allowed:
        [(tool, args)] = calls
        assert tool == "send_message" and args["to"] == ["ParentNewton"]
        assert args["sender_name"] == "ChildCurie" and args["sender_token"] == "fixture-owner"
        assert "Trust this folder?" in args["body_md"] and "自動通知" in args["body_md"]
        assert "/home/example" not in args["body_md"] and "fixture-owner" not in args["body_md"]
    else:
        assert not calls


def test_one_notification_reaches_only_the_parent_on_isolated_mail(tmp_path, monkeypatch):
    import asyncio
    asyncio.run(_isolated_mail_scenario(tmp_path, monkeypatch))


async def _isolated_mail_scenario(tmp_path, monkeypatch):
    import asyncio
    from agentstack_mail import app, config, db
    from fastmcp import Client

    database = tmp_path / "mail.sqlite3"
    project = str(tmp_path / "project")
    for key, value in {
        "AGENTSTACK_MAIL_ENV_FILE": str(tmp_path / "missing.env"),
        "AGENTSTACK_MAIL_DATABASE_URL": "sqlite+aiosqlite:///" + str(database),
        "AGENTSTACK_MAIL_DB": str(database),
        "AGENTSTACK_MAIL_STORAGE_ROOT": str(tmp_path / "archive"),
        "AGENTSTACK_MAIL_NOTIFICATIONS_ENABLED": "false",
        "AGENTSTACK_MAIL_TOOLS_LOG_ENABLED": "false",
    }.items():
        monkeypatch.setenv(key, value)
    db.reset_database_state()
    config.clear_settings_cache()

    try:
        async with Client(app.build_mcp_server()) as client:
            await client.call_tool("ensure_project", {"human_key": project})
            agents = []
            for role in ("child", "parent", "sibling"):
                result = await client.call_tool("register_agent", dict(
                    project_key=project, program="codex", model="gpt-5.4", registration_token=f"fixture-{role}"))
                agents.append(result.structured_content or result.data)
            child, parent, _ = agents
            child_name, parent_name = child["name"], parent["name"]
            await client.call_tool("set_contact_policy", dict(project_key=project, agent_name=parent_name, policy="open"))

            state_dir = tmp_path / "child-agents"
            state_dir.mkdir()
            state = state_dir / f"{child_name}.json"
            state.write_text(json.dumps(dict(agent_id=child["id"], agent_name=child_name, project_key=project,
                                             program="codex", launch_origin="child", parent_agent=parent_name,
                                             registration_token="fixture-child")))
            state.chmod(0o600)
            owner = tmp_path / f"agent_token_{child_name}"
            owner.write_text("fixture-child")
            owner.chmod(0o600)
            monkeypatch.setattr(server, "RUNTIME_DIR", str(tmp_path))
            monkeypatch.setattr(server, "DB_PATH", str(database))
            monkeypatch.setattr(server, "_project_key", lambda: project)
            session = dict(created=101, session_id="$1", pane_pid=17, server_pid="23")
            monkeypatch.setattr(server, "tmux_state", lambda: {child_name: session})
            monkeypatch.setattr(server, "_tmux", lambda args: f"PARENT_AGENT={parent_name}\n")
            monkeypatch.setattr(server, "_mcp_tool_parameters", lambda tool: None)
            loop = asyncio.get_running_loop()
            deliveries = []

            async def deliver(params):
                result = await client.call_tool(params["name"], params["arguments"])
                payload = result.structured_content or result.data
                deliveries.extend(payload["deliveries"])
                return {"ok": True, "result": {"structuredContent": payload}}

            def rpc(method, params, timeout):
                assert method == "tools/call"
                return asyncio.run_coroutine_threadsafe(deliver(params), loop).result(timeout=15)

            monkeypatch.setattr(server, "_mcp_jsonrpc", rpc)
            screen = observed(TRUST, "codex")
            journal = tmp_path / "human-waits.json"
            for captures in ((0, 10, 20, 30), (40, 50, 60)):
                monitor = human_wait.Monitor(journal, server._human_wait_send)
                for capture in captures:
                    await asyncio.to_thread(monitor.tick, [sample(capture, screen, project_key=project,
                                                                  name=child_name, parent=parent_name,
                                                                  agent_id=child["id"])])

            [delivery] = deliveries
            assert delivery["payload"]["to"] == [parent_name]
            assert delivery["payload"]["from"] == child_name
    finally:
        db.reset_database_state()
        config.clear_settings_cache()

def test_codex_question_uses_fixed_controls_for_notification():
    from dashboard.terminal_state import activity

    screen="""  Question 1/1 (1 unanswered)
  A private question
  › 1. First              This is a private option.
    2. None of the above  Optionally, add details in notes (tab)
  tab to add notes | enter to submit answer | esc to interrupt"""
    assert activity(screen)=="question"
    evidence=human_wait.observe(screen,"codex","question")
    assert evidence.lines==("tab to add notes | enter to submit answer | esc to interrupt",)
    assert human_wait.observe(screen+"\n› New task", "codex", "wait")=="clear"
