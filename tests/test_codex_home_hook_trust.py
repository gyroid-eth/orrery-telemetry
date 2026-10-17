"""A Codex child keeps the hook trust its user gave to their own CODEX_HOME hooks.

The child's CODEX_HOME links the user's hooks.json at
`<runtime>/child-agents/<name>.codex-home/hooks.json`, and Codex keys hook trust
by that path, so the user's own hooks, unchanged, stopped every new child on
"N hooks need review" (2026-10-16: four user hooks, on each spawn). The child's
config.toml therefore carries the user's trust over to the child's path. The
hash is of the hook's content and Codex checks it again, so a changed hook
stays in review. The user's own config is not touched.
"""
from __future__ import annotations

import json
import tomllib
from pathlib import Path

from hooks import child_resume
from test_claude_resume_mail import NAME, TOKEN, private


TRUSTED = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64


def _user_home(tmp_path: Path, extra: str = "") -> Path:
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "hooks.json").write_text('{"hooks": {}}\n', encoding="utf-8")
    (home / "config.toml").write_text(
        'model = "gpt-6-luna"\n\n[hooks.state]\n\n'
        f'[hooks.state."{home}/hooks.json:pre_tool_use:0:0"]\ntrusted_hash = "{TRUSTED}"\n\n'
        f'[hooks.state."{home}/hooks.json:session_start:1:0"]\ntrusted_hash = "{OTHER}"\n\n'
        f'[hooks.state."{tmp_path}/repo/.codex/hooks.json:stop:0:0"]\ntrusted_hash = "{TRUSTED}"\n\n'
        '[hooks.state."agentstack-codex-app@agentstack-local:hooks/hooks.json:stop:0:0"]\n'
        f'trusted_hash = "{TRUSTED}"\n' + extra,
        encoding="utf-8",
    )
    return home


def _child_home(tmp_path: Path) -> Path:
    return tmp_path / "runtime" / "child-agents" / f"{NAME}.codex-home"


def _build(tmp_path: Path, source_home: Path) -> tuple[Path, dict]:
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
        home=_child_home(tmp_path), source=source_home, runner=runner,
        child=NAME, project_key="/p", token_file=runtime / f"agent_token_{NAME}",
        mcp_url="http://127.0.0.1:9/mcp", mail_env="", runtime_dir=runtime, bearer_mode="disabled",
        python_bin="", mcp_profile="inherit", work_dir=None,
    )
    config = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    return home, config.get("hooks", {}).get("state", {})


def test_the_users_home_hook_trust_reaches_the_childs_home(tmp_path):
    user = _user_home(tmp_path)
    before = (user / "config.toml").read_text(encoding="utf-8")
    home, state = _build(tmp_path, user)
    assert state[f"{home}/hooks.json:pre_tool_use:0:0"]["trusted_hash"] == TRUSTED
    assert state[f"{home}/hooks.json:session_start:1:0"]["trusted_hash"] == OTHER
    # The user's own entries and the plugin's are kept as they were.
    assert state[f"{user}/hooks.json:pre_tool_use:0:0"]["trusted_hash"] == TRUSTED
    assert "agentstack-codex-app@agentstack-local:hooks/hooks.json:stop:0:0" in state
    # The user's own config is never written.
    assert (user / "config.toml").read_text(encoding="utf-8") == before


def test_only_the_users_home_hooks_are_carried(tmp_path):
    user = _user_home(tmp_path)
    home, state = _build(tmp_path, user)
    added = {key for key in state if key.startswith(str(home))}
    assert added == {f"{home}/hooks.json:pre_tool_use:0:0",
                     f"{home}/hooks.json:session_start:1:0"}


def test_trust_already_recorded_for_the_childs_path_wins(tmp_path):
    child = _child_home(tmp_path)
    user = _user_home(tmp_path, extra=(
        f'\n[hooks.state."{child}/hooks.json:pre_tool_use:0:0"]\ntrusted_hash = "{OTHER}"\n'))
    home, state = _build(tmp_path, user)
    assert home == child
    assert state[f"{home}/hooks.json:pre_tool_use:0:0"]["trusted_hash"] == OTHER


def test_a_user_without_hook_trust_gets_nothing_extra(tmp_path):
    user = tmp_path / "codex-home"
    user.mkdir()
    (user / "config.toml").write_text('model = "gpt-6-luna"\n', encoding="utf-8")
    _home, state = _build(tmp_path, user)
    assert state == {}
