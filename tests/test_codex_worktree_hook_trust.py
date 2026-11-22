"""A Codex child in a git worktree keeps the hook trust its user already gave.

Codex records hook trust in config.toml as
`[hooks.state."<path>/.codex/hooks.json:<event>:<i>:<j>"] trusted_hash = ...`.
A `--worktree` child runs in a checkout at another path, so the same project
hooks, unchanged, waited on Codex's "N hooks need review" screen (4.5 minutes in
the 2026-10-01 run-through) and none of them ran meanwhile. The child's own
config.toml therefore carries the source checkout's trust over to the worktree
path. The hash is of the hook's content (checked on Codex 0.159.2: a copied
hash makes an identical hook active and leaves a changed one in review), so
Codex still refuses any hook that differs. The user's own config is not touched.
"""
from __future__ import annotations

import json
import fcntl
import os
import tomllib
from pathlib import Path

import pytest

from hooks import child_resume
from test_claude_resume_mail import NAME, TOKEN, private


TRUSTED = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64


def _checkouts(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "vault"
    (source / ".git" / "worktrees" / NAME).mkdir(parents=True)
    (source / ".git" / "worktrees" / NAME / "commondir").write_text("../..\n", encoding="utf-8")
    worktree = tmp_path / "worktrees" / NAME
    (worktree / "sub").mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {source}/.git/worktrees/{NAME}\n", encoding="utf-8")
    # git's own back-reference from the metadata to this worktree.
    (source / ".git" / "worktrees" / NAME / "gitdir").write_text(f"{worktree}/.git\n", encoding="utf-8")
    return source, worktree


def _user_config(tmp_path: Path, source: Path, extra: str = "") -> Path:
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text(
        'model = "gpt-6-luna"\n\n[hooks.state]\n\n'
        f'[hooks.state."{source}/.codex/hooks.json:pre_tool_use:0:0"]\ntrusted_hash = "{TRUSTED}"\n\n'
        f'[hooks.state."{source}/.codex/hooks.json:session_start:0:0"]\ntrusted_hash = "{OTHER}"\n\n'
        '[hooks.state."agentstack-codex-app@agentstack-local:hooks/hooks.json:stop:0:0"]\n'
        f'trusted_hash = "{TRUSTED}"\n' + extra,
        encoding="utf-8",
    )
    return home


def _build(tmp_path: Path, source_home: Path, work_dir: Path | None) -> dict:
    runtime = tmp_path / "runtime"
    private(runtime / f"agent_token_{NAME}", TOKEN)
    private(runtime / "child-agents" / f"{NAME}.json", json.dumps({
        "agent_id": 73, "agent_name": NAME, "project_key": "/p", "program": "codex",
        "registration_token": TOKEN,
    }))
    child_resume.prepare_active_state(runtime, NAME, project_key="/p", program="codex")
    runner = tmp_path / "run-mcp.sh"
    runner.write_text("#!/bin/bash\nexit 0\n")
    runner.chmod(0o755)
    home = child_resume.build_home(
        home=runtime / "child-agents" / f"{NAME}.codex-home", source=source_home, runner=runner,
        child=NAME, project_key="/p", token_file=runtime / f"agent_token_{NAME}",
        mcp_url="http://127.0.0.1:9/mcp", mail_env="", runtime_dir=runtime, bearer_mode="disabled",
        python_bin="", mcp_profile="inherit", work_dir=work_dir,
    )
    return tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))["hooks"]["state"]


def test_the_source_checkouts_hook_trust_reaches_the_worktree_path(tmp_path):
    source, worktree = _checkouts(tmp_path)
    user = _user_config(tmp_path, source)
    before = (user / "config.toml").read_text(encoding="utf-8")
    state = _build(tmp_path, user, worktree / "sub")
    assert state[f"{worktree}/.codex/hooks.json:pre_tool_use:0:0"]["trusted_hash"] == TRUSTED
    assert state[f"{worktree}/.codex/hooks.json:session_start:0:0"]["trusted_hash"] == OTHER
    # The source's own trust and the plugin's are kept as they were.
    assert state[f"{source}/.codex/hooks.json:pre_tool_use:0:0"]["trusted_hash"] == TRUSTED
    assert "agentstack-codex-app@agentstack-local:hooks/hooks.json:stop:0:0" in state
    # The user's own config is never written.
    assert (user / "config.toml").read_text(encoding="utf-8") == before


def test_nothing_the_user_did_not_trust_is_trusted(tmp_path):
    source, worktree = _checkouts(tmp_path)
    user = _user_config(tmp_path, source)
    state = _build(tmp_path, user, worktree)
    added = {key for key in state if key.startswith(str(worktree))}
    assert added == {f"{worktree}/.codex/hooks.json:pre_tool_use:0:0",
                     f"{worktree}/.codex/hooks.json:session_start:0:0"}


def test_trust_the_user_already_gave_the_worktree_path_wins(tmp_path):
    source, worktree = _checkouts(tmp_path)
    user = _user_config(tmp_path, source, extra=(
        f'\n[hooks.state."{worktree}/.codex/hooks.json:pre_tool_use:0:0"]\ntrusted_hash = "{OTHER}"\n'))
    state = _build(tmp_path, user, worktree)
    assert state[f"{worktree}/.codex/hooks.json:pre_tool_use:0:0"]["trusted_hash"] == OTHER


def test_a_main_checkout_gets_nothing_extra(tmp_path):
    source, _worktree = _checkouts(tmp_path)
    user = _user_config(tmp_path, source)
    state = _build(tmp_path, user, source)
    assert len(state) == 3


def test_a_submodule_is_not_a_worktree(tmp_path):
    source, _worktree = _checkouts(tmp_path)
    module = tmp_path / "vault" / "lib"
    module.mkdir()
    (source / ".git" / "modules" / "lib").mkdir(parents=True)
    (module / ".git").write_text(f"gitdir: {source}/.git/modules/lib\n", encoding="utf-8")
    user = _user_config(tmp_path, source)
    assert len(_build(tmp_path, user, module)) == 3


def test_no_work_dir_keeps_the_previous_behaviour(tmp_path):
    source, _worktree = _checkouts(tmp_path)
    user = _user_config(tmp_path, source)
    assert len(_build(tmp_path, user, None)) == 3


def test_both_launch_paths_pass_the_work_dir():
    root = Path(__file__).resolve().parents[1]
    spawn = (root / "hooks" / "spawn_child.sh").read_text(encoding="utf-8")
    start = spawn.index("write_child_codex_home() {")
    assert '--work-dir "$WORK_DIR"' in spawn[start:spawn.index("\n}\n", start)]
    server = (root / "dashboard" / "server.py").read_text(encoding="utf-8")
    start = server.index("def _rebuild_codex_child_home(")
    assert '"--work-dir"' in server[start:server.index("\ndef ", start + 10)]


def test_a_checkout_whose_git_directory_is_kept_elsewhere(tmp_path):
    """The vault's `.git` is a file naming a git directory outside it, and git
    records no path for the vault itself; its worktree must still find it."""
    store = tmp_path / "Obsidian_Git" / "obsidian.git"
    (store / "worktrees" / NAME).mkdir(parents=True)
    (store / "worktrees" / NAME / "commondir").write_text("../..\n", encoding="utf-8")
    source = tmp_path / "vault"
    source.mkdir()
    (source / ".git").write_text(f"gitdir: {store}\n", encoding="utf-8")
    worktree = tmp_path / "worktrees" / NAME
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {store}/worktrees/{NAME}\n", encoding="utf-8")
    (store / "worktrees" / NAME / "gitdir").write_text(f"{worktree}/.git\n", encoding="utf-8")
    user = _user_config(tmp_path, source)
    state = _build(tmp_path, user, worktree)
    assert state[f"{worktree}/.codex/hooks.json:pre_tool_use:0:0"]["trusted_hash"] == TRUSTED


def test_another_repositorys_trust_is_not_carried(tmp_path):
    source, worktree = _checkouts(tmp_path)
    other = tmp_path / "other-repo"
    (other / ".git").mkdir(parents=True)
    user = _user_config(tmp_path, other)
    state = _build(tmp_path, user, worktree)
    assert not any(key.startswith(str(worktree)) for key in state)


# --- review of #174 ----------------------------------------------------------


def _carried(tmp_path: Path, worktree: Path, source: Path) -> list[str]:
    user = _user_config(tmp_path, source)
    state = _build(tmp_path, user, worktree)
    return [key for key in state if key.startswith(str(worktree))]


def test_a_git_file_naming_no_real_worktree_borrows_nothing(tmp_path):
    """P2-1: `gitdir: <trusted>/.git/worktrees/does-not-exist` alone is not a worktree."""
    source, _worktree = _checkouts(tmp_path)
    stranger = tmp_path / "unrelated"
    stranger.mkdir()
    (stranger / ".git").write_text(f"gitdir: {source}/.git/worktrees/does-not-exist\n", encoding="utf-8")
    assert _carried(tmp_path, stranger, source) == []


def test_metadata_that_points_at_another_worktree_borrows_nothing(tmp_path):
    source, worktree = _checkouts(tmp_path)
    (source / ".git" / "worktrees" / NAME / "gitdir").write_text(
        f"{tmp_path}/somewhere-else/.git\n", encoding="utf-8")
    assert _carried(tmp_path, worktree, source) == []


@pytest.mark.parametrize("commondir", [None, "", "../../no-such-dir"], ids=["missing", "empty", "dangling"])
def test_without_a_usable_commondir_nothing_is_borrowed(tmp_path, commondir):
    source, worktree = _checkouts(tmp_path)
    marker = source / ".git" / "worktrees" / NAME / "commondir"
    if commondir is None:
        marker.unlink()
    else:
        marker.write_text(commondir + "\n", encoding="utf-8")
    assert _carried(tmp_path, worktree, source) == []


def _alias_config(tmp_path: Path, source: Path, alias_hash: str) -> Path:
    alias = tmp_path / "alias"
    alias.symlink_to(source)
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text(
        "[hooks.state]\n\n"
        f'[hooks.state."{source}/.codex/hooks.json:session_start:0:0"]\ntrusted_hash = "{TRUSTED}"\n\n'
        f'[hooks.state."{alias}/.codex/hooks.json:session_start:0:0"]\ntrusted_hash = "{alias_hash}"\n',
        encoding="utf-8",
    )
    return home


def test_trust_given_under_a_symlinked_path_too_is_written_once(tmp_path):
    """P2-2: two trust entries for one checkout must not declare one table twice."""
    source, worktree = _checkouts(tmp_path)
    state = _build(tmp_path, _alias_config(tmp_path, source, TRUSTED), worktree)
    assert state[f"{worktree}/.codex/hooks.json:session_start:0:0"]["trusted_hash"] == TRUSTED


def test_disagreeing_trust_for_one_hook_is_left_to_review(tmp_path):
    source, worktree = _checkouts(tmp_path)
    state = _build(tmp_path, _alias_config(tmp_path, source, OTHER), worktree)
    assert f"{worktree}/.codex/hooks.json:session_start:0:0" not in state


def test_metadata_elsewhere_cannot_point_its_commondir_at_a_trusted_repository(tmp_path):
    """Re-check of #174, P2-R1: real metadata with a correct back-reference, but
    under another repository's worktrees/, naming the trusted repository as its
    commondir. Nothing is written under the trusted repository."""
    source, _worktree = _checkouts(tmp_path)
    other_repo = tmp_path / "other-repo"
    metadata = other_repo / ".git" / "worktrees" / "other"
    metadata.mkdir(parents=True)
    stranger = tmp_path / "stranger"
    stranger.mkdir()
    (stranger / ".git").write_text(f"gitdir: {metadata}\n", encoding="utf-8")
    (metadata / "gitdir").write_text(f"{stranger}/.git\n", encoding="utf-8")
    (metadata / "commondir").write_text(f"{source}/.git\n", encoding="utf-8")
    assert _carried(tmp_path, stranger, source) == []


PARENT = "ParentCurie"
PLUGIN = "agentstack-codex-app@fixture"
PARENT_HOOK = f"{PLUGIN}:hooks/hooks.json:pre_tool_use:1:0"


@pytest.fixture
def parent_case(tmp_path):
    """Two private generated homes; the ordinary source lacks the new approval."""
    runtime = tmp_path / "runtime"
    source = tmp_path / "source-home"
    source.mkdir()
    private(source / "config.toml", child_resume._emit_toml({
        "model": "source-model", "sandbox_mode": "read-only",
        "plugins": {PLUGIN: {"enabled": True}},
        "mcp_servers": {"source-only": {"command": "source-only"}},
    }))
    parent_source = tmp_path / "parent-source"
    parent_source.mkdir()
    private(parent_source / "config.toml", child_resume._emit_toml({
        "model": "parent-model", "sandbox_mode": "danger-full-access",
        "plugins": {PLUGIN: {"enabled": True}},
        "mcp_servers": {"parent-only": {"command": "parent-only"}},
        "hooks": {"state": {PARENT_HOOK: {"trusted_hash": TRUSTED}}},
    }))
    runner = tmp_path / "run-mcp.sh"
    runner.write_text("#!/bin/bash\nexit 0\n")
    runner.chmod(0o700)

    def build(name, source_home, parent="", work_dir=None, overlay="", use_recorded_parent=False):
        private(runtime / f"agent_token_{name}", TOKEN)
        private(runtime / "child-agents" / f"{name}.json", json.dumps({
            "agent_id": 74 if name == PARENT else 73, "agent_name": name,
            "project_key": "/p", "program": "codex", "registration_token": TOKEN,
        }))
        child_resume.prepare_active_state(runtime, name, project_key="/p", program="codex",
                                         parent_agent=parent)
        home = child_resume.build_home(
            home=runtime / "child-agents" / f"{name}.codex-home", source=source_home,
            runner=runner, child=name, project_key="/p", token_file=runtime / f"agent_token_{name}",
            mcp_url="http://127.0.0.1:9/mcp", mail_env="", runtime_dir=runtime,
            bearer_mode="disabled", python_bin="", mcp_profile="inherit",
            parent_agent="" if use_recorded_parent else parent,
            work_dir=work_dir, overlay_setting=overlay,
        )
        return tomllib.loads((home / "config.toml").read_text())

    build(PARENT, parent_source)
    return {"runtime": runtime, "source": source, "build": build,
            "parent_home": runtime / "child-agents" / f"{PARENT}.codex-home",
            "parent_state": runtime / "child-agents" / f"{PARENT}.json",
            "parent_lock": runtime / "child-agents" / f".{PARENT}.resume.lock"}


def test_verified_parent_approval_reaches_child_without_changing_source_home(parent_case):
    case = parent_case
    watched = [case["source"] / "config.toml", case["parent_state"],
               case["parent_home"] / "config.toml", case["parent_lock"]]
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode) for p in watched}
    result = case["build"](NAME, case["source"], PARENT)
    assert result.get("hooks", {}).get("state", {}).get(PARENT_HOOK) == {"trusted_hash": TRUSTED}
    assert result["model"] == "source-model" and result["sandbox_mode"] == "read-only"
    assert result["mcp_servers"]["source-only"] == {"command": "source-only"}
    assert "parent-only" not in result["mcp_servers"]
    assert all((p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode) == before[p] for p in watched)


def test_parent_approval_keeps_the_original_hash_for_native_change_detection(parent_case):
    result = parent_case["build"](NAME, parent_case["source"], PARENT)
    copied = result.get("hooks", {}).get("state", {}).get(PARENT_HOOK, {})
    # The launcher carries the approval; it must not manufacture approval for
    # a different definition. Codex performs the current-hash check itself.
    assert copied.get("trusted_hash") == TRUSTED
    assert copied.get("trusted_hash") != OTHER


def _set_case_config(path, change):
    config = tomllib.loads(path.read_text())
    change(config)
    private(path, child_resume._emit_toml(config))


def _child_hook_state(case, parent=PARENT):
    return case["build"](NAME, case["source"], parent).get("hooks", {}).get("state", {})


@pytest.mark.parametrize("parent", ["", "UnknownParent", NAME])
def test_no_formal_verified_parent_does_not_borrow_another_agents_approval(parent_case, parent):
    assert PARENT_HOOK not in _child_hook_state(parent_case, parent)


@pytest.mark.parametrize("field,value", [
    ("agent_name", "AnotherParent"), ("project_key", "/other-project"),
    ("program", "claude-code"), ("agent_id", True), ("provider", "claude"),
    ("retired_at", "2026-10-01T00:00:00Z"), ("resume_in_progress_at", "pending"),
    ("tools_change_generation", "a" * 32),
])
def test_foreign_or_incomplete_parent_falls_back_to_native_review(parent_case, field, value):
    path = parent_case["parent_state"]
    state = json.loads(path.read_text())
    state[field] = value
    private(path, json.dumps(state))
    assert PARENT_HOOK not in _child_hook_state(parent_case)


@pytest.mark.parametrize("damage", [
    "missing-state", "missing-lock", "invalid-state", "invalid-config",
    "state-symlink", "config-symlink", "lock-symlink", "home-symlink",
    "config-readable", "state-readable", "lock-readable", "home-readable", "config-hardlink",
])
def test_unsafe_parent_paths_are_not_repaired_or_used(parent_case, damage):
    case = parent_case
    paths = {"state": case["parent_state"], "lock": case["parent_lock"],
             "config": case["parent_home"] / "config.toml", "home": case["parent_home"]}
    operation, item = damage.split("-", 1)
    if operation == "missing":
        paths[item].unlink()
    elif operation == "invalid":
        paths[item].write_text("[invalid")
    elif item == "symlink":
        original = paths[operation]
        saved = original.with_name(original.name + ".saved")
        original.rename(saved)
        original.symlink_to(saved)
    elif item == "readable":
        paths[operation].chmod(0o755 if operation == "home" else 0o644)
    elif item == "hardlink":
        os.link(paths[operation], paths[operation].with_name("extra-link"))
    assert PARENT_HOOK not in _child_hook_state(case)
    if operation == "missing":
        assert not paths[item].exists()


def test_a_parent_directory_with_another_owner_is_not_used(parent_case, monkeypatch):
    original = Path.lstat

    def foreign(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == parent_case["parent_home"]:
            fields = list(info)
            fields[4] = os.getuid() + 1
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(Path, "lstat", foreign)
    assert PARENT_HOOK not in _child_hook_state(parent_case)


@pytest.mark.timeout(5)
def test_busy_parent_does_not_wait_while_the_child_lock_is_held(parent_case):
    with parent_case["parent_lock"].open("rb") as writer:
        fcntl.flock(writer.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert PARENT_HOOK not in _child_hook_state(parent_case)


@pytest.mark.parametrize("pending", ["tools-change", "legacy-migration", "registration-pending"])
def test_dangling_parent_transaction_marker_still_requires_review(parent_case, pending):
    marker = parent_case["parent_state"].with_name(f".{PARENT}.{pending}.json")
    marker.symlink_to(marker.with_name("missing-transaction"))
    assert PARENT_HOOK not in _child_hook_state(parent_case)
    assert marker.is_symlink()


@pytest.mark.parametrize("field,value", [
    ("AGENTSTACK_PROXY_AGENT_NAME", "AnotherParent"),
    ("AGENTSTACK_PROJECT_KEY", "/foreign"), ("AGENTSTACK_PROXY_PROGRAM", "claude"),
    ("AGENTSTACK_PROXY_TOKEN_FILE", "/unrelated/token"),
    ("AGENTSTACK_RUNTIME_DIR", "/unrelated/runtime"),
])
def test_parent_proxy_binding_must_match_the_canonical_identity(parent_case, field, value):
    _set_case_config(parent_case["parent_home"] / "config.toml",
                     lambda c: c["mcp_servers"]["orrery-mail"]["env"].update({field: value}))
    assert PARENT_HOOK not in _child_hook_state(parent_case)


@pytest.mark.parametrize("hooks", [
    [], {"state": []}, {"state": {PARENT_HOOK: "not-a-record"}},
    {"state": {PARENT_HOOK: {"trusted_hash": 42}}},
    {"state": {PARENT_HOOK: {"trusted_hash": "unapproved"}}},
    {"state": {PARENT_HOOK: {"trusted_hash": TRUSTED, "enabled": 1}}},
    {"state": {PARENT_HOOK: {"enabled": True}}},
])
def test_malformed_or_unapproved_parent_records_add_no_trust(parent_case, hooks):
    _set_case_config(parent_case["parent_home"] / "config.toml", lambda c: c.update(hooks=hooks))
    assert PARENT_HOOK not in _child_hook_state(parent_case)


def test_explicit_source_disable_wins_over_parent_approval(parent_case):
    _set_case_config(parent_case["source"] / "config.toml",
                     lambda c: c.update(hooks={"state": {PARENT_HOOK: {"enabled": False}}}))
    assert _child_hook_state(parent_case)[PARENT_HOOK] == {"enabled": False}


def test_explicit_disable_in_existing_child_is_not_reenabled_on_rebuild(parent_case):
    case = parent_case
    case["build"](NAME, case["source"], "")
    target = case["runtime"] / "child-agents" / f"{NAME}.codex-home" / "config.toml"
    _set_case_config(target, lambda c: c.update(hooks={"state": {PARENT_HOOK: {"enabled": False}}}))
    assert _child_hook_state(case)[PARENT_HOOK] == {"enabled": False}


@pytest.mark.parametrize("entry", [{"trusted_hash": TRUSTED, "enabled": False}, {"enabled": False}])
def test_parent_disabled_or_unapproved_hook_is_not_inherited(parent_case, entry):
    _set_case_config(parent_case["parent_home"] / "config.toml",
                     lambda c: c["hooks"]["state"].update({PARENT_HOOK: entry}))
    assert PARENT_HOOK not in _child_hook_state(parent_case)


def test_parent_disabled_entry_does_not_replace_a_baseline_decision(parent_case):
    _set_case_config(parent_case["source"] / "config.toml",
                     lambda c: c.update(hooks={"state": {PARENT_HOOK: {"trusted_hash": OTHER}}}))
    _set_case_config(parent_case["parent_home"] / "config.toml",
                     lambda c: c["hooks"]["state"][PARENT_HOOK].update(enabled=False))
    assert _child_hook_state(parent_case)[PARENT_HOOK] == {"trusted_hash": OTHER}


def test_parent_approval_replaces_stale_source_hash_without_enabling_anything(parent_case):
    _set_case_config(parent_case["source"] / "config.toml",
                     lambda c: c.update(hooks={"state": {PARENT_HOOK: {"trusted_hash": OTHER}}}))
    assert _child_hook_state(parent_case)[PARENT_HOOK] == {"trusted_hash": TRUSTED}


@pytest.mark.parametrize("approval", [{"trusted_hash": TRUSTED, "enabled": False}, {"enabled": False}])
def test_source_project_disable_survives_worktree_rekey(parent_case, tmp_path, approval):
    checkout, worktree = _checkouts(tmp_path)
    key = f"{checkout}/.codex/hooks.json:pre_tool_use:0:0"
    _set_case_config(parent_case["source"] / "config.toml",
                     lambda c: c.update(hooks={"state": {key: approval}}))
    result = parent_case["build"](NAME, parent_case["source"], PARENT, worktree)
    assert result["hooks"]["state"][f"{worktree}/.codex/hooks.json:pre_tool_use:0:0"] == approval


def test_overlay_disable_is_not_lost_when_adding_parent_approval(parent_case, tmp_path):
    overlay = tmp_path / "overlay.toml"
    private(overlay, child_resume._emit_toml({"hooks": {"state": {PARENT_HOOK: {"enabled": False}}}}))
    result = parent_case["build"](NAME, parent_case["source"], PARENT, overlay=str(overlay))
    assert result["hooks"]["state"][PARENT_HOOK].get("enabled") is False


def test_overlay_project_disable_applies_before_worktree_aliases(parent_case, tmp_path):
    checkout, worktree = _checkouts(tmp_path)
    key = f"{checkout}/.codex/hooks.json:pre_tool_use:0:0"
    _set_case_config(parent_case["parent_home"] / "config.toml",
                     lambda c: c["hooks"]["state"].update({key: {"trusted_hash": TRUSTED}}))
    overlay = tmp_path / "overlay.toml"
    private(overlay, child_resume._emit_toml({"hooks": {"state": {key: {"enabled": False}}}}))
    result = parent_case["build"](NAME, parent_case["source"], PARENT, worktree, str(overlay))
    assert result["hooks"]["state"][f"{worktree}/.codex/hooks.json:pre_tool_use:0:0"]["enabled"] is False


def test_rebuild_uses_the_parent_in_the_existing_child_record(parent_case):
    result = parent_case["build"](NAME, parent_case["source"], PARENT, use_recorded_parent=True)
    assert result["hooks"]["state"][PARENT_HOOK] == {"trusted_hash": TRUSTED}
