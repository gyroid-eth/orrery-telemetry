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
