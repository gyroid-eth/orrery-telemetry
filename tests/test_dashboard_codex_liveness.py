from types import SimpleNamespace

from dashboard import server


def _result(stdout="", returncode=0, stderr=""):
    return SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)


def _tree(live: bool):
    root = 70705
    names = {root: "zsh"}
    children = {root: []}
    if live:
        names[70713] = "codex"
        children[root].append(70713)
    return root, (names, children)


def _patch_agent_inputs(monkeypatch, process_tree):
    root = 70705
    monkeypatch.setattr(
        server,
        "tmux_state",
        lambda: {
            "MintBoltzmann": {
                "name": "MintBoltzmann",
                "created": 100,
                "session_id": "$1",
                "activity": 200,
                "attached": False,
                "client_tty": None,
                "cmd": "zsh",
                "pane_pid": root,
                "title": "launcher: /Applications/ChatGPT.app/Contents/Resources/codex",
            },
        },
    )
    monkeypatch.setattr(
        server,
        "agentmail_state",
        lambda: ({
            "MintBoltzmann": {
                "model": "GPT 5.6",
                "model_raw": "gpt-5.6-sol",
                "program": "codex-cli",
                "task": "test",
                "last_active": 150,
            },
        }, {}),
    )
    monkeypatch.setattr(server, "_process_tree_snapshot", lambda: process_tree)
    monkeypatch.setattr(server, "_codex_app_runtimes", lambda: {})
    monkeypatch.setattr(server, "_deliverables_index", lambda: {})
    monkeypatch.setattr(server, "_retired_names", lambda _project: set())
    monkeypatch.setattr(server, "_name_substitutions", lambda: {})
    monkeypatch.setattr(server, "_agent_runtime", lambda *_args: {})
    monkeypatch.setattr(server, "_project_key", lambda: "")
    monkeypatch.setattr(
        server, "_session_matches_dashboard_project", lambda *_args, **_kwargs: True
    )
    return root


def test_process_snapshot_uses_comm_once_per_ttl(monkeypatch):
    calls = []
    monkeypatch.setattr(server.sys, "platform", "darwin")
    monkeypatch.setattr(server.time, "monotonic", lambda: 100.0)
    server._PROCESS_TREE_CACHE.update(ts=0.0, tree=None)

    def fake_run(argv, **kwargs):
        calls.append(argv)
        assert argv == ["ps", "-axo", "pid=,ppid=,comm="]
        return _result("70705 1 zsh\n70713 70705 codex\n")

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    first = server._process_tree_snapshot()
    second = server._process_tree_snapshot()
    assert first == second
    assert len(calls) == 1


def test_live_wrapper_is_online(monkeypatch):
    _root, process_tree = _tree(True)
    _patch_agent_inputs(monkeypatch, process_tree)
    row = server.build_agents()[0]
    assert row["category"] == "agent"
    assert row["running"] is True


def test_shell_husk_is_finished_even_when_title_mentions_codex(monkeypatch):
    _root, process_tree = _tree(False)
    _patch_agent_inputs(monkeypatch, process_tree)
    row = server.build_agents()[0]
    assert row["category"] == "finished"
    assert row["running"] is False


def test_process_tree_failure_falls_back_to_previous_codex_behavior(monkeypatch):
    _patch_agent_inputs(monkeypatch, None)
    row = server.build_agents()[0]
    assert row["category"] == "agent"
    assert row["running"] is True


def test_graph_uses_the_same_husk_liveness(monkeypatch):
    _root, process_tree = _tree(False)
    _patch_agent_inputs(monkeypatch, process_tree)
    monkeypatch.setattr(
        server,
        "_raw_graph",
        lambda _live_parents=None: {
            "nodes": [{
                "name": "MintBoltzmann",
                "program": "codex-cli",
                "model": "gpt-5.6-sol",
                "last_active": 200,
                "retired": False,
            }],
            "edges": [],
            "spawn": [],
        },
    )
    monkeypatch.setattr(server, "_annotations", lambda: {})
    payload = server.graph_payload(4, True)
    node = payload["nodes"][0]
    assert node["running"] is False
    assert node["state"] == "finished"


def _exit(monkeypatch, process_tree):
    _patch_agent_inputs(monkeypatch, process_tree)
    monkeypatch.setattr(server, "_has_session", lambda _session: True)
    monkeypatch.setattr(server.time, "sleep", lambda _seconds: None)
    sent = []

    def fake_run(argv, **kwargs):
        if argv[:2] == ["tmux", "display-message"]:
            return _result("zsh\n")
        if argv[:2] == ["tmux", "send-keys"]:
            sent.append(argv)
            return _result()
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    return server.do_exit("MintBoltzmann"), sent


def test_exit_uses_shared_live_classification_for_slash_exit(monkeypatch):
    _root, process_tree = _tree(True)
    result, sent = _exit(monkeypatch, process_tree)
    assert result["ok"] is True
    assert "exit-sent" in result["actions"]
    assert any("/exit" in argv for argv in sent)
    assert not any(argv[-2:] == ["exit", "Enter"] for argv in sent)


def test_exit_uses_shared_husk_classification_for_shell_exit(monkeypatch):
    _root, process_tree = _tree(False)
    result, sent = _exit(monkeypatch, process_tree)
    assert result["ok"] is True
    assert "shell-exit-sent" in result["actions"]
    assert any(argv[-2:] == ["exit", "Enter"] for argv in sent)
    assert not any("/exit" in argv for argv in sent)


def test_tmux_state_keeps_legacy_four_field_fake_rows(monkeypatch):
    sep = server.SEP

    def fake_tmux(args):
        if args[0] == "list-sessions":
            return sep.join(("DemoAgent", "100", "200", "$1")) + "\n"
        if args[0] == "list-panes":
            return sep.join(("DemoAgent", "11", "zsh", "demo title")) + "\n"
        if args[0] == "list-clients":
            return ""
        return ""

    monkeypatch.setattr(server, "_tmux", fake_tmux)
    monkeypatch.setattr(server, "_prune_runtime_cache", lambda _sessions: None)
    state = server.tmux_state()["DemoAgent"]
    assert state["cmd"] == "zsh"
    assert state["pane_pid"] == 0
    assert state["title"] == "demo title"
