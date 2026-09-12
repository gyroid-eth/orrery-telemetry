"""Issue #16 multi-repository and linked-worktree isolation regressions.

The fixtures deliberately use real Git repositories.  A mock which merely
returns a chosen root cannot catch the failure this issue reported: Git's
linked-worktree ``.git`` file and common directory are the facts that must
drive the shared project identity.
"""

from __future__ import annotations

import asyncio
import http.server
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import threading
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from dashboard import graph_data, server as dashboard_server


ROOT = pathlib.Path(__file__).resolve().parents[1]
PROJECT_CONTEXT = ROOT / "hooks" / "project-context.sh"
RESERVATION_HOOK = ROOT / "hooks" / "check-file-reservation.sh"
RELEASE_RESERVATION_HOOK = ROOT / "hooks" / "release-file-reservation.sh"
RELEASE_ALL_RESERVATIONS_HOOK = ROOT / "hooks" / "release-all-reservations.sh"
TOP_LAUNCHERS = {
    "claude": ROOT / "bin" / "agent-start",
    "codex": ROOT / "bin" / "agent-start-codex",
    "gemini": ROOT / "bin" / "agent-start-gemini",
}
REGISTER_LIB = ROOT / "bin" / "lib" / "agentstack-register.sh"
BOOTSTRAPS = {
    "codex": ROOT / "bin" / "agentstack-codex-bootstrap",
    "gemini": ROOT / "bin" / "agentstack-gemini-bootstrap",
}
SPAWN_CHILD = ROOT / "hooks" / "spawn_child.sh"
CLEANUP_CHILD = ROOT / "hooks" / "cleanup-child-agent.sh"
SESSION_START = ROOT / "hooks" / "session-start-reminder.sh"
REREGISTER = ROOT / "bin" / "agentstack-reregister"
PREREGISTER = ROOT / "bin" / "agentstack-preregister-child"
DOCTOR = ROOT / "scripts" / "doctor.sh"


@dataclass(frozen=True)
class GitLayout:
    main: pathlib.Path
    linked: pathlib.Path
    other: pathlib.Path
    main_subdir: pathlib.Path
    linked_subdir: pathlib.Path
    linked_symlink: pathlib.Path


def _git(*args: str, cwd: pathlib.Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture()
def git_layout(tmp_path: pathlib.Path) -> GitLayout:
    main = tmp_path / "repo-main"
    linked = tmp_path / "repo-linked"
    other = tmp_path / "repo-other"
    main.mkdir()
    other.mkdir()
    for repo in (main, other):
        _git("init", "-q", cwd=repo)
        _git(
            "-c", "user.name=Issue16 Test", "-c",
            "user.email=issue16@example.invalid", "commit", "--allow-empty",
            "-q", "-m", "initial", cwd=repo,
        )
    _git("worktree", "add", "-q", "-b", "issue16-linked", str(linked), cwd=main)
    main_subdir = main / "src" / "nested"
    linked_subdir = linked / "src" / "nested"
    main_subdir.mkdir(parents=True)
    linked_subdir.mkdir(parents=True)
    linked_symlink = tmp_path / "linked-through-symlink"
    linked_symlink.symlink_to(linked, target_is_directory=True)
    return GitLayout(
        main=main.resolve(),
        linked=linked.resolve(),
        other=other.resolve(),
        main_subdir=main_subdir.resolve(),
        linked_subdir=linked_subdir.resolve(),
        linked_symlink=linked_symlink,
    )


def _isolated_env(tmp_path: pathlib.Path) -> dict[str, str]:
    env = os.environ.copy()
    for name in tuple(env):
        if name.startswith("AGENTSTACK_") or name in {
            "PROJECT_KEY",
            "GIT_DIR",
            "GIT_COMMON_DIR",
            "GIT_WORK_TREE",
            "TMUX",
            "TMUX_PANE",
            "PARENT_AGENT",
            "CHILD_REGISTRATION_TOKEN",
        }:
            env.pop(name, None)
    isolated_home = tmp_path / "home"
    isolated_home.mkdir(exist_ok=True)
    env.update(
        {
            "HOME": str(isolated_home),
            "AGENTSTACK_HOME": str(isolated_home / ".agentstack"),
            "AGENTSTACK_LABEL_PREFIX": "org.agentstack.test.issue16",
        }
    )
    return env


def _write_installed_env(home: pathlib.Path, project: pathlib.Path) -> pathlib.Path:
    agentstack_home = home / ".agentstack"
    agentstack_home.mkdir(parents=True, exist_ok=True)
    env_file = agentstack_home / "env.sh"
    env_file.write_text(
        f"export AGENTSTACK_PROJECT_KEY='{project}'\n"
        f"export AGENTSTACK_PROTECTED_ROOTS='{project}'\n",
        encoding="utf-8",
    )
    return env_file


def _resolve_project(
    target: pathlib.Path,
    env: dict[str, str],
    *,
    env_file: pathlib.Path | None = None,
    explicit: str = "",
) -> str:
    result = subprocess.run(
        [
            "/bin/bash",
            str(PROJECT_CONTEXT),
            "resolve-project-key",
            str(target),
            str(env_file or pathlib.Path(env["AGENTSTACK_HOME"]) / "env.sh"),
            "1",
            explicit,
        ],
        cwd=target if target.is_dir() else ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.mark.parametrize(
    "target_name",
    ["main", "main_subdir", "linked", "linked_subdir", "linked_symlink"],
)
def test_git_target_beats_stale_installed_parent_and_git_process_state(
    tmp_path: pathlib.Path, git_layout: GitLayout, target_name: str,
) -> None:
    """A fresh top-level repository selection owns its project context.

    This includes the two easy-to-miss inherited channels from the report:
    normal shell variables and Git's own process-global selectors.
    """
    env = _isolated_env(tmp_path)
    env_file = _write_installed_env(pathlib.Path(env["HOME"]), git_layout.other)
    env.update(
        {
            "AGENTSTACK_PROJECT_KEY": str(git_layout.other),
            "PROJECT_KEY": str(git_layout.other),
            "AGENTSTACK_PROTECTED_ROOTS": str(git_layout.other),
            "GIT_DIR": str(git_layout.other / ".git"),
            "GIT_COMMON_DIR": str(git_layout.other / ".git"),
            "GIT_WORK_TREE": str(git_layout.other),
        }
    )

    target = getattr(git_layout, target_name)
    assert _resolve_project(target, env, env_file=env_file) == str(git_layout.main)


def test_explicit_invocation_key_wins_without_becoming_an_ambient_override(
    tmp_path: pathlib.Path, git_layout: GitLayout,
) -> None:
    env = _isolated_env(tmp_path)
    env_file = _write_installed_env(pathlib.Path(env["HOME"]), git_layout.other)
    env["AGENTSTACK_PROJECT_KEY"] = str(git_layout.other)
    explicit = "operator-selected-namespace"

    assert _resolve_project(
        git_layout.linked_subdir,
        env,
        env_file=env_file,
        explicit=explicit,
    ) == explicit


def test_explicit_nonexistent_path_normalization_propagates_interpreter_failure(
    tmp_path: pathlib.Path,
) -> None:
    target = tmp_path / "missing-parent" / "future-project"
    env = _isolated_env(tmp_path)
    env["AGENTSTACK_PYTHON"] = str(tmp_path / "missing-python")

    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            'source "$1"; agentstack_resolve_project_key "$2" /dev/null 0 "$2"',
            "resolver-failure",
            str(PROJECT_CONTEXT),
            str(target),
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert result.stdout == ""


def test_established_explicit_context_survives_a_linked_worktree(
    tmp_path: pathlib.Path, git_layout: GitLayout,
) -> None:
    env = _isolated_env(tmp_path)
    env.update(
        {
            "AGENTSTACK_PROJECT_KEY": "operator-selected-namespace",
            "PROJECT_KEY": "operator-selected-namespace",
            "AGENTSTACK_PROJECT_CONTEXT": "1",
            "AGENTSTACK_PROJECT_REPOSITORY": str(git_layout.main),
            "AGENTSTACK_PROJECT_WORK_DIR": str(git_layout.linked),
        }
    )

    assert _resolve_project(git_layout.linked_subdir, env) == (
        "operator-selected-namespace"
    )


def test_filesystem_project_alias_matches_resolver_dashboard_and_mail(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastmcp import Client

    from agentstack_mail import app as mail_app
    from agentstack_mail import config as mail_config
    from agentstack_mail import db as mail_db

    physical = tmp_path / "physical-project"
    alias = tmp_path / "project-alias"
    physical.mkdir()
    alias.symlink_to(physical, target_is_directory=True)
    physical_key = str(physical.resolve())
    future_alias = alias / "future-project"
    future_physical_key = str(physical.resolve() / "future-project")

    resolver_env = _isolated_env(tmp_path)
    assert _resolve_project(alias, resolver_env, explicit=str(alias)) == physical_key
    assert _resolve_project(
        physical, resolver_env, explicit=str(future_alias)
    ) == future_physical_key
    resolver_env.update(
        {
            "AGENTSTACK_PROJECT_KEY": str(alias),
            "PROJECT_KEY": str(alias),
            "AGENTSTACK_PROJECT_CONTEXT": "1",
            "AGENTSTACK_PROJECT_WORK_DIR": str(alias),
            "AGENTSTACK_PROTECTED_ROOTS": str(alias),
        }
    )
    assert _resolve_project(alias, resolver_env) == physical_key

    monkeypatch.setattr(dashboard_server, "PROJECT_KEY", str(alias))
    monkeypatch.setattr(dashboard_server, "VAULT", "")
    for name, value in resolver_env.items():
        if name.startswith("AGENTSTACK_PROJECT_") or name == "PROJECT_KEY":
            monkeypatch.setenv(name, value)
    assert dashboard_server._project_key() == physical_key
    monkeypatch.setattr(dashboard_server, "PROJECT_KEY", str(future_alias))
    for name in (
        "AGENTSTACK_PROJECT_KEY",
        "PROJECT_KEY",
        "AGENTSTACK_PROJECT_CONTEXT",
        "AGENTSTACK_PROJECT_REPOSITORY",
        "AGENTSTACK_PROJECT_WORK_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    assert dashboard_server._project_key() == future_physical_key

    monkeypatch.setenv(
        "AGENTSTACK_MAIL_ENV_FILE", str(tmp_path / "missing-alias-mail.env")
    )
    monkeypatch.setenv(
        "AGENTSTACK_MAIL_DATABASE_URL",
        f"sqlite+aiosqlite:///{tmp_path / 'alias-mail.sqlite3'}",
    )
    monkeypatch.setenv(
        "AGENTSTACK_MAIL_STORAGE_ROOT", str(tmp_path / "alias-mail-archive")
    )
    monkeypatch.setenv(
        "AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR",
        str(tmp_path / "alias-mail-signals"),
    )
    monkeypatch.setenv("AGENTSTACK_MAIL_NOTIFICATIONS_ENABLED", "false")
    monkeypatch.setenv("AGENTSTACK_MAIL_TOOLS_LOG_ENABLED", "false")
    monkeypatch.setenv("AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE", "passthrough")
    monkeypatch.setenv("AGENTSTACK_MAIL_WORKTREES_ENABLED", "false")
    monkeypatch.setenv("AGENTSTACK_MAIL_GIT_IDENTITY_ENABLED", "false")
    monkeypatch.setenv("AGENTSTACK_MAIL_PROJECT_IDENTITY_MODE", "dir")
    mail_db.reset_database_state()
    mail_config.clear_settings_cache()

    async def exercise() -> tuple[object, object, object, object, object, object]:
        async with Client(mail_app.build_mcp_server()) as client:
            alias_project = await client.call_tool(
                "ensure_project", {"human_key": str(alias), "format": "json"}
            )
            physical_project = await client.call_tool(
                "ensure_project", {"human_key": physical_key, "format": "json"}
            )
            future_alias_project = await client.call_tool(
                "ensure_project",
                {"human_key": str(future_alias), "format": "json"},
            )
            future_physical_project = await client.call_tool(
                "ensure_project",
                {"human_key": future_physical_key, "format": "json"},
            )
            alias_registration = await client.call_tool(
                "register_agent",
                {
                    "project_key": str(alias),
                    "program": "issue-16-alias-smoke",
                    "model": "test-model",
                    "name": "AliasCurie",
                    "task_description": "filesystem alias identity",
                    "registration_token": "alias-owner-token",
                    "format": "json",
                },
            )
            physical_registration = await client.call_tool(
                "register_agent",
                {
                    "project_key": physical_key,
                    "program": "issue-16-alias-smoke",
                    "model": "test-model",
                    "name": "AliasCurie",
                    "task_description": "physical identity",
                    "registration_token": "alias-owner-token",
                    "format": "json",
                },
            )
        await mail_db.dispose_database_for_shutdown()
        return (
            alias_project,
            physical_project,
            future_alias_project,
            future_physical_project,
            alias_registration,
            physical_registration,
        )

    try:
        (
            alias_project,
            physical_project,
            future_alias_project,
            future_physical_project,
            alias_registration,
            physical_registration,
        ) = asyncio.run(exercise())
    finally:
        mail_db.reset_database_state()
        mail_config.clear_settings_cache()

    for result in (
        alias_project,
        physical_project,
        future_alias_project,
        future_physical_project,
        alias_registration,
        physical_registration,
    ):
        assert result.is_error is False
    assert _mcp_payload(alias_project)["id"] == _mcp_payload(physical_project)["id"]
    assert _mcp_payload(alias_project)["human_key"] == physical_key
    assert _mcp_payload(future_alias_project)["id"] == _mcp_payload(
        future_physical_project
    )["id"]
    assert _mcp_payload(future_alias_project)["human_key"] == future_physical_key
    assert _mcp_payload(alias_registration)["id"] == _mcp_payload(
        physical_registration
    )["id"]


def test_established_context_is_rejected_when_its_repository_binding_is_wrong(
    tmp_path: pathlib.Path, git_layout: GitLayout,
) -> None:
    env = _isolated_env(tmp_path)
    env.update(
        {
            "AGENTSTACK_PROJECT_KEY": "forged-or-stale-namespace",
            "PROJECT_KEY": "forged-or-stale-namespace",
            "AGENTSTACK_PROJECT_CONTEXT": "1",
            "AGENTSTACK_PROJECT_REPOSITORY": str(git_layout.other),
            "AGENTSTACK_PROJECT_WORK_DIR": str(git_layout.other),
        }
    )

    assert _resolve_project(git_layout.linked_subdir, env) == str(git_layout.main)


def test_non_git_context_export_retains_installed_protected_roots(
    tmp_path: pathlib.Path,
) -> None:
    workspace = tmp_path / "plain-workspace"
    extra_root = tmp_path / "operator-shared-root"
    workspace.mkdir()
    extra_root.mkdir()
    env = _isolated_env(tmp_path)
    installed = pathlib.Path(env["AGENTSTACK_HOME"]) / "env.sh"
    installed.parent.mkdir(parents=True)
    installed.write_text(
        "export AGENTSTACK_PROJECT_KEY='/installed/fallback'\n"
        f"export AGENTSTACK_PROTECTED_ROOTS='{workspace}:{extra_root}'\n",
        encoding="utf-8",
    )
    script = (
        f'source "{PROJECT_CONTEXT}"\n'
        f'key="$(agentstack_resolve_project_key "{workspace}" "{installed}" 1 explicit-plain)"\n'
        f'agentstack_export_project_context "$key" "{workspace}"\n'
        "printf '%s\\n' \"$AGENTSTACK_PROTECTED_ROOTS\"\n"
    )

    result = subprocess.run(
        ["/bin/bash", "-c", script],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert result.stdout.strip() == f"{workspace}:{extra_root}"


def test_explicit_non_git_context_drops_unrelated_ambient_roots(
    tmp_path: pathlib.Path,
) -> None:
    workspace = tmp_path / "selected-workspace-b"
    workspace.mkdir()
    project_a_root = tmp_path / "ambient-project-a"
    project_a_root.mkdir()
    env = _isolated_env(tmp_path)
    env.update(
        {
            "AGENTSTACK_PROJECT_KEY": "/ambient/project-a",
            "PROJECT_KEY": "/ambient/project-a",
            "AGENTSTACK_PROTECTED_ROOTS": str(project_a_root),
        }
    )
    script = f"""
source "{PROJECT_CONTEXT}"
agentstack_export_project_context /explicit/project-b "{workspace}"
printf '%s\n' "$AGENTSTACK_PROJECT_KEY"
printf '%s\n' "$AGENTSTACK_PROJECT_WORK_DIR"
printf '%s\n' "$AGENTSTACK_PROTECTED_ROOTS"
"""
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=workspace,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    project_key, work_dir, roots_value = result.stdout.splitlines()
    roots = roots_value.split(":")
    assert project_key == "/explicit/project-b"
    assert work_dir == str(workspace.resolve())
    assert roots == [str(workspace.resolve())]
    assert str(project_a_root.resolve()) not in roots


@pytest.mark.parametrize(
    "resolver",
    ["agentstack_resolve_runtime_protected_roots", "agentstack_resolve_protected_roots"],
)
def test_protected_root_resolvers_propagate_installed_env_interpreter_failure(
    tmp_path: pathlib.Path,
    resolver: str,
) -> None:
    workspace = tmp_path / "plain-root-failure-workspace"
    workspace.mkdir()
    env_file = tmp_path / "installed-root-failure.env"
    env_file.write_text(
        f"export AGENTSTACK_PROJECT_KEY='{workspace}'\n"
        f"export AGENTSTACK_PROTECTED_ROOTS='{workspace}'\n",
        encoding="utf-8",
    )
    env = _isolated_env(tmp_path)
    env["AGENTSTACK_PYTHON"] = str(tmp_path / "missing-configured-python")
    if resolver == "agentstack_resolve_runtime_protected_roots":
        invocation = f'{resolver} "{workspace}" "{workspace}" "{env_file}"'
    else:
        invocation = f'{resolver} "{workspace}" "" "{env_file}"'
    result = subprocess.run(
        ["/bin/bash", "-c", f'source "{PROJECT_CONTEXT}"\n{invocation}'],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert result.stdout == ""


def test_context_export_is_atomic_when_root_resolution_fails_inside_condition(
    tmp_path: pathlib.Path,
) -> None:
    workspace = tmp_path / "conditional-export-workspace"
    workspace.mkdir()
    env = _isolated_env(tmp_path)
    env_file = pathlib.Path(env["AGENTSTACK_HOME"]) / "env.sh"
    env_file.parent.mkdir(parents=True)
    env_file.write_text(
        f"export AGENTSTACK_PROJECT_KEY='{workspace}'\n"
        f"export AGENTSTACK_PROTECTED_ROOTS='{workspace}'\n",
        encoding="utf-8",
    )
    env.update(
        {
            "AGENTSTACK_PYTHON": str(tmp_path / "missing-configured-python"),
            "AGENTSTACK_PROJECT_KEY": "prior-logical-project",
            "PROJECT_KEY": "prior-logical-project",
            "AGENTSTACK_PROJECT_CONTEXT": "prior-context",
            "AGENTSTACK_PROJECT_REPOSITORY": "prior-repository",
            "AGENTSTACK_PROJECT_WORK_DIR": "prior-work-dir",
        }
    )
    script = f'''
source "{PROJECT_CONTEXT}"
if agentstack_export_project_context "{workspace}" "{workspace}"; then
    status=0
else
    status=$?
fi
printf '%s\\n' "$status"
printf '%s\\n' "$AGENTSTACK_PROJECT_KEY"
printf '%s\\n' "$PROJECT_KEY"
printf '%s\\n' "$AGENTSTACK_PROJECT_CONTEXT"
printf '%s\\n' "$AGENTSTACK_PROJECT_REPOSITORY"
printf '%s\\n' "$AGENTSTACK_PROJECT_WORK_DIR"
printf '%s\\n' "${{AGENTSTACK_PROTECTED_ROOTS-unset}}"
'''
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    status, *tuple_values = result.stdout.splitlines()
    assert int(status) != 0
    assert tuple_values == [
        "prior-logical-project",
        "prior-logical-project",
        "prior-context",
        "prior-repository",
        "prior-work-dir",
        "unset",
    ]


def test_protected_roots_need_no_python_without_an_installed_env(
    tmp_path: pathlib.Path,
) -> None:
    workspace = tmp_path / "no-installed-env-workspace"
    workspace.mkdir()
    env = _isolated_env(tmp_path)
    env["AGENTSTACK_PYTHON"] = str(tmp_path / "missing-configured-python")
    absent_env = tmp_path / "does-not-exist.env"
    result = subprocess.run(
        [
            "/bin/bash",
            str(PROJECT_CONTEXT),
            "resolve-runtime-protected-roots",
            str(workspace),
            str(workspace),
            str(absent_env),
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(workspace.resolve())


def test_live_non_git_custom_roots_do_not_require_the_installed_interpreter(
    tmp_path: pathlib.Path,
) -> None:
    workspace = tmp_path / "live-root-workspace"
    shared = tmp_path / "live-shared-root"
    workspace.mkdir()
    shared.mkdir()
    env = _isolated_env(tmp_path)
    env_file = tmp_path / "present-but-unneeded.env"
    env_file.write_text("export AGENTSTACK_PROJECT_KEY='/installed/other'\n")
    env.update(
        {
            "AGENTSTACK_PYTHON": str(tmp_path / "missing-configured-python"),
            "AGENTSTACK_PROJECT_KEY": str(workspace),
            "PROJECT_KEY": str(workspace),
            "AGENTSTACK_PROTECTED_ROOTS": f"{workspace}:{shared}",
        }
    )
    result = subprocess.run(
        [
            "/bin/bash",
            str(PROJECT_CONTEXT),
            "resolve-runtime-protected-roots",
            str(workspace),
            str(workspace),
            str(env_file),
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{workspace.resolve()}:{shared.resolve()}"


def _consumer_install(
    tmp_path: pathlib.Path,
    script: pathlib.Path,
    installed_project: str,
) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    install = tmp_path / f"install-{script.name}"
    (install / "bin").mkdir(parents=True)
    (install / "hooks").mkdir()
    installed_script = install / "bin" / script.name
    shutil.copy2(script, installed_script)
    shutil.copy2(PROJECT_CONTEXT, install / "hooks" / PROJECT_CONTEXT.name)
    home = tmp_path / f"home-{script.name}"
    env_file = home / ".agentstack" / "env.sh"
    env_file.parent.mkdir(parents=True)
    env_file.write_text(
        f"export AGENTSTACK_PROJECT_KEY='{installed_project}'\n"
        f"export PROJECT_KEY='{installed_project}'\n",
        encoding="utf-8",
    )
    return installed_script, home, env_file


@pytest.mark.parametrize("live_project_variable", ["AGENTSTACK_PROJECT_KEY", "PROJECT_KEY"])
def test_reregister_preserves_live_non_git_project_before_loading_install_defaults(
    tmp_path: pathlib.Path,
    live_project_variable: str,
) -> None:
    workspace = tmp_path / "live-non-git-workspace"
    workspace.mkdir()
    script, home, _env_file = _consumer_install(
        tmp_path, REREGISTER, "/installed/project-a"
    )
    capture = tmp_path / "reregister-project"
    register_lib = tmp_path / "reregister-lib.sh"
    register_lib.write_text(
        """#!/bin/bash
ags_mail_load_token() { :; }
ags_load_registration_token() { printf '%s\n' live-owner-token; }
ags_register_session() {
  printf '%s' "$1" > "$ISSUE16_PROJECT_CAPTURE"
  AGS_REGISTERED_AGENT_NAME="$6"
  export AGS_REGISTERED_AGENT_NAME
}
""",
        encoding="utf-8",
    )
    env = _isolated_env(tmp_path)
    env.update(
        {
            "HOME": str(home),
            "AGENTSTACK_HOME": str(home / ".agentstack"),
            "AGENTSTACK_LABEL_PREFIX": "org.agentstack.test.issue16",
            "AGENTSTACK_REGISTER_LIB": str(register_lib),
            "AGENTSTACK_RUNTIME_DIR": str(tmp_path / "runtime-reregister"),
            "CHILD_REGISTRATION_TOKEN": "live-owner-token",
            "ISSUE16_PROJECT_CAPTURE": str(capture),
        }
    )
    env[live_project_variable] = "/live/project-b"

    result = subprocess.run(
        [str(script), "LiveAgent"],
        cwd=workspace,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert capture.read_text(encoding="utf-8") == "/live/project-b"


def test_reregister_accepts_legacy_alias_bound_identity_in_canonical_context(
    tmp_path: pathlib.Path,
) -> None:
    physical = tmp_path / "reregister-physical"
    alias = tmp_path / "reregister-alias"
    physical.mkdir()
    alias.symlink_to(physical, target_is_directory=True)
    runtime = tmp_path / "reregister-alias-runtime"
    (runtime / "child-agents").mkdir(parents=True)
    (runtime / "name-bindings").mkdir(parents=True)
    name = "AliasChild"
    (runtime / f"agent_token_{name}").write_text(
        "alias-owner-token", encoding="utf-8"
    )
    (runtime / f"agent_token_{name}.project").write_text(
        f"{alias}\n", encoding="utf-8"
    )
    (runtime / "child-agents" / f"{name}.json").write_text(
        json.dumps(
            {
                "agent_name": name,
                "project_key": str(alias),
                "registration_token": "alias-owner-token",
            }
        ),
        encoding="utf-8",
    )
    (runtime / "name-bindings" / "aliaschild.json").write_text(
        json.dumps({"agent_name": name, "project_key": str(alias)}),
        encoding="utf-8",
    )
    fake_bin = tmp_path / "reregister-alias-bin"
    fake_bin.mkdir()
    calls = tmp_path / "reregister-alias-calls.jsonl"
    _write_executable(
        fake_bin / "curl",
        """#!/usr/bin/env python3
import json
import os
import pathlib
import sys

payload = json.load(sys.stdin)
pathlib.Path(os.environ["ISSUE16_ALIAS_CALLS"]).open("a", encoding="utf-8").write(
    json.dumps(payload) + "\\n"
)
tool = payload.get("params", {}).get("name")
if tool == "whois":
    data = {"id": 16, "name": "AliasChild"}
elif tool == "register_agent":
    data = {
        "id": 16,
        "name": "AliasChild",
        "registration_token": "alias-owner-token",
    }
else:
    data = {}
print(json.dumps({
    "jsonrpc": "2.0", "id": "1", "result": {"structuredContent": data}
}))
""",
    )
    env = _isolated_env(tmp_path)
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "AGENTSTACK_PROJECT_KEY": str(physical.resolve()),
            "PROJECT_KEY": str(physical.resolve()),
            "AGENTSTACK_PROJECT_CONTEXT": "1",
            "AGENTSTACK_PROJECT_WORK_DIR": str(physical.resolve()),
            "AGENTSTACK_PROTECTED_ROOTS": str(physical.resolve()),
            "AGENTSTACK_RUNTIME_DIR": str(runtime),
            "AGENTSTACK_REGISTER_LIB": str(REGISTER_LIB),
            "AGENTSTACK_MCP_URL": "http://127.0.0.1:1/mcp",
            "AGENTSTACK_MAIL_HTTP_BEARER_MODE": "disabled",
            "CHILD_REGISTRATION_TOKEN": "alias-owner-token",
            "ISSUE16_ALIAS_CALLS": str(calls),
        }
    )

    result = subprocess.run(
        [str(REREGISTER), name],
        cwd=physical,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"registered {name}" in result.stdout
    requests = [json.loads(line) for line in calls.read_text().splitlines()]
    register = next(
        request for request in requests
        if request.get("params", {}).get("name") == "register_agent"
    )
    assert register["params"]["arguments"]["project_key"] == str(
        physical.resolve()
    )
    assert (runtime / f"agent_token_{name}.project").read_text(
        encoding="utf-8"
    ).strip() == str(physical.resolve())


@pytest.mark.parametrize("live_project_variable", ["AGENTSTACK_PROJECT_KEY", "PROJECT_KEY"])
def test_preregister_preserves_live_non_git_project_before_loading_install_defaults(
    tmp_path: pathlib.Path,
    live_project_variable: str,
) -> None:
    workspace = tmp_path / "live-non-git-workspace"
    workspace.mkdir()
    script, home, _env_file = _consumer_install(
        tmp_path, PREREGISTER, "/installed/project-a"
    )
    calls = tmp_path / "preregister-calls"
    token_out = tmp_path / "child-token"
    register_lib = tmp_path / "preregister-lib.sh"
    register_lib.write_text(
        """#!/bin/bash
ags_mail_load_token() { :; }
ags_normalize_project_key() {
  python3 - "$1" <<'PY'
import pathlib
import sys

value = sys.argv[1]
path = pathlib.Path(value)
print(path.resolve() if path.is_absolute() or path.is_dir() else value)
PY
}
ags_has_scientist_suffix() { return 0; }
ags_local_agent_name_conflicts() { return 1; }
ags_acquire_local_name_claim() { :; }
ags_release_local_name_claim() { :; }
ags_update_local_name_claim() { :; }
ags_commit_local_name_claim() { :; }
ags_generate_registration_token() { printf '%s\n' generated-token; }
ags_mcp_call() {
  printf '%s\n' "$*" >> "$ISSUE16_CALLS"
  if [[ "$1" == register_agent ]]; then
    printf '%s\n' '{"result":{"structuredContent":{"name":"LiveChild","registration_token":"server-token"}}}'
  else
    printf '%s\n' '{"result":{"structuredContent":{}}}'
  fi
}
ags_mcp_has_error() { return 1; }
ags_extract_agent_name() { printf '%s\n' LiveChild; }
ags_extract_registration_token() { printf '%s\n' server-token; }
ags_store_registration_token() { :; }
ags_apply_contact_policy() { :; }
""",
        encoding="utf-8",
    )
    env = _isolated_env(tmp_path)
    env.update(
        {
            "HOME": str(home),
            "AGENTSTACK_HOME": str(home / ".agentstack"),
            "AGENTSTACK_LABEL_PREFIX": "org.agentstack.test.issue16",
            "AGENTSTACK_REGISTER_LIB": str(register_lib),
            "AGENTSTACK_RUNTIME_DIR": str(tmp_path / "runtime-preregister"),
            "ISSUE16_CALLS": str(calls),
        }
    )
    env[live_project_variable] = "/live/project-b"

    result = subprocess.run(
        [
            str(script),
            "--name", "LiveChild",
            "--program", "codex",
            "--model", "codex",
            "--task-description", "isolated live project",
            "--token-file-out", str(token_out),
        ],
        cwd=workspace,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert token_out.read_text(encoding="utf-8") == "server-token"
    assert "project_key=/live/project-b" in calls.read_text(encoding="utf-8")


def test_dashboard_default_project_key_canonicalizes_a_linked_worktree(
    git_layout: GitLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in tuple(os.environ):
        if name.startswith("AGENTSTACK_PROJECT_") or name == "PROJECT_KEY":
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(dashboard_server, "PROJECT_KEY", str(git_layout.linked))
    monkeypatch.setattr(dashboard_server, "VAULT", str(git_layout.other))

    assert dashboard_server._project_key() == str(git_layout.main)


def test_dashboard_project_key_preserves_an_established_explicit_binding(
    git_layout: GitLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    explicit_key = "/explicit/logical-project"
    monkeypatch.setattr(dashboard_server, "PROJECT_KEY", explicit_key)
    monkeypatch.setattr(dashboard_server, "VAULT", str(git_layout.other))
    monkeypatch.setenv("AGENTSTACK_PROJECT_KEY", explicit_key)
    monkeypatch.setenv("PROJECT_KEY", explicit_key)
    monkeypatch.setenv("AGENTSTACK_PROJECT_CONTEXT", "1")
    monkeypatch.setenv("AGENTSTACK_PROJECT_REPOSITORY", str(git_layout.main))
    monkeypatch.setenv("AGENTSTACK_PROJECT_WORK_DIR", str(git_layout.linked))
    monkeypatch.setenv(
        "AGENTSTACK_PROTECTED_ROOTS", f"{git_layout.linked}:{git_layout.main}"
    )

    assert dashboard_server._project_key() == explicit_key


def _graph_database(path: pathlib.Path) -> pathlib.Path:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE projects (id INTEGER PRIMARY KEY, human_key TEXT);
            CREATE TABLE agents (
                id INTEGER PRIMARY KEY, project_id INTEGER, name TEXT,
                model TEXT, program TEXT, task_description TEXT,
                last_active_ts INTEGER, inception_ts INTEGER, retired_at TEXT
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY, project_id INTEGER, sender_id INTEGER,
                created_ts INTEGER, importance TEXT
            );
            CREATE TABLE message_recipients (
                message_id INTEGER, agent_id INTEGER, kind TEXT
            );
            INSERT INTO projects VALUES (1, '/first-project');
            INSERT INTO agents VALUES (
                1, 1, 'MustNotLeak', 'model', 'codex', 'private task',
                1789000000000000, 1789000000000000, NULL
            );
            """
        )
    return path


def _linked_dashboard_database(
    path: pathlib.Path,
    canonical_project: pathlib.Path,
    linked_project: pathlib.Path,
) -> pathlib.Path:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE projects (id INTEGER PRIMARY KEY, human_key TEXT);
            CREATE TABLE agents (
                id INTEGER PRIMARY KEY, project_id INTEGER, name TEXT,
                model TEXT, program TEXT, task_description TEXT,
                last_active_ts TEXT, inception_ts TEXT, retired_at TEXT
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY, project_id INTEGER, sender_id INTEGER,
                subject TEXT, created_ts TEXT, importance TEXT
            );
            CREATE TABLE message_recipients (
                message_id INTEGER, agent_id INTEGER, kind TEXT
            );
            """
        )
        connection.executemany(
            "INSERT INTO projects (id, human_key) VALUES (?, ?)",
            [(1, str(canonical_project)), (2, str(linked_project))],
        )
        connection.executemany(
            """INSERT INTO agents VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
            [
                (
                    1, 1, "CanonicalCurie", "model", "codex",
                    "canonical project task", "2026-09-12 12:00:00",
                    "2026-09-12 11:00:00",
                ),
                (
                    2, 2, "LiteralLeak", "model", "codex",
                    "linked literal must not be queried", "2026-09-12 12:00:00",
                    "2026-09-12 11:00:00",
                ),
            ],
        )
    return path


def _configure_linked_dashboard_key(
    git_layout: GitLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in tuple(os.environ):
        if name.startswith("AGENTSTACK_PROJECT_") or name == "PROJECT_KEY":
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(dashboard_server, "PROJECT_KEY", str(git_layout.linked))
    monkeypatch.setattr(dashboard_server, "VAULT", str(git_layout.other))


def test_dashboard_mail_queries_use_canonical_configured_linked_key(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _linked_dashboard_database(
        tmp_path / "dashboard-mail.sqlite3", git_layout.main, git_layout.linked
    )
    _configure_linked_dashboard_key(git_layout, monkeypatch)
    monkeypatch.setattr(dashboard_server, "DB_PATH", str(database))

    agents, instructions = dashboard_server.agentmail_state()

    assert set(agents) == {"CanonicalCurie"}
    assert agents["CanonicalCurie"]["task"] == "canonical project task"
    assert instructions == {}


def test_dashboard_graph_queries_use_canonical_configured_linked_key(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _linked_dashboard_database(
        tmp_path / "dashboard-graph.sqlite3", git_layout.main, git_layout.linked
    )
    _configure_linked_dashboard_key(git_layout, monkeypatch)
    monkeypatch.setattr(graph_data, "DB_PATH", str(database))
    monkeypatch.setattr(graph_data, "PROJECT_HUMAN_KEY", str(git_layout.linked))
    monkeypatch.setattr(graph_data, "_live_parents", lambda: {})
    monkeypatch.setitem(sys.modules, "graph_data", graph_data)
    monkeypatch.setattr(dashboard_server, "_GRAPH_CACHE", {"ts": 0, "data": None})

    graph = dashboard_server._raw_graph()

    assert {node["name"] for node in graph["nodes"]} == {"CanonicalCurie"}


def _same_name_lineage_database(
    path: pathlib.Path,
    git_layout: GitLayout,
    *,
    with_project_a_message: bool,
) -> pathlib.Path:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE projects (id INTEGER PRIMARY KEY, human_key TEXT);
            CREATE TABLE agents (
                id INTEGER PRIMARY KEY, project_id INTEGER, name TEXT,
                model TEXT, program TEXT, task_description TEXT,
                last_active_ts INTEGER, inception_ts INTEGER, retired_at TEXT
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY, project_id INTEGER, sender_id INTEGER,
                created_ts INTEGER, importance TEXT
            );
            CREATE TABLE message_recipients (
                message_id INTEGER, agent_id INTEGER, kind TEXT
            );
            """
        )
        connection.executemany(
            "INSERT INTO projects (id, human_key) VALUES (?, ?)",
            [(1, str(git_layout.main)), (2, str(git_layout.other))],
        )
        connection.executemany(
            "INSERT INTO agents VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            [
                (
                    1, 1, "SharedParent", "model", "codex", "project A parent",
                    1789000200000000, 1788990000000000,
                ),
                (
                    2, 1, "SharedChild", "model", "codex", "project A child",
                    1789000200000000, 1788991000000000,
                ),
                (
                    3, 2, "SharedParent", "model", "codex", "project B parent",
                    1789000200000000, 1788990000000000,
                ),
                (
                    4, 2, "SharedChild", "model", "codex", "project B child",
                    1789000200000000, 1788991000000000,
                ),
            ],
        )
        if with_project_a_message:
            connection.execute(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?)",
                (1, 1, 1, 1789000100000000, "high"),
            )
            connection.execute(
                "INSERT INTO message_recipients VALUES (?, ?, ?)",
                (1, 2, "to"),
            )
    return path


@pytest.mark.parametrize(
    ("lineage_source", "expected_spawn"),
    [
        ("foreign-live-zero-message", []),
        (
            "same-project-live-zero-message",
            [{"source": "SharedParent", "target": "SharedChild", "type": "spawn"}],
        ),
        (
            "historical-message",
            [{"source": "SharedParent", "target": "SharedChild", "type": "spawn"}],
        ),
    ],
)
def test_dashboard_graph_scopes_live_lineage_before_graph_aggregation(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
    monkeypatch: pytest.MonkeyPatch,
    lineage_source: str,
    expected_spawn: list[dict[str, str]],
) -> None:
    """Exercise the server-to-graph boundary with project-colliding names."""
    with_message = lineage_source == "historical-message"
    database = _same_name_lineage_database(
        tmp_path / f"lineage-{lineage_source}.sqlite3",
        git_layout,
        with_project_a_message=with_message,
    )
    session_project = (
        git_layout.other
        if lineage_source == "foreign-live-zero-message"
        else git_layout.main
    )
    sessions = (
        {}
        if with_message
        else {
            "SharedChild": {
                "name": "SharedChild",
                "cwd": str(session_project),
            }
        }
    )
    monkeypatch.setattr(graph_data, "DB_PATH", str(database))
    monkeypatch.setattr(graph_data, "PROJECT_HUMAN_KEY", str(git_layout.main))
    monkeypatch.setattr(
        graph_data,
        "_live_parents",
        lambda: pytest.fail("scoped Dashboard graph consulted host parent cache"),
    )
    monkeypatch.setitem(sys.modules, "graph_data", graph_data)
    monkeypatch.setattr(dashboard_server, "_project_key", lambda: str(git_layout.main))
    monkeypatch.setenv("AGENTSTACK_PROJECT_KEY", str(git_layout.main))
    monkeypatch.setenv("PROJECT_KEY", str(git_layout.main))
    monkeypatch.setenv("AGENTSTACK_PROJECT_CONTEXT", "1")
    monkeypatch.setenv("AGENTSTACK_PROJECT_REPOSITORY", str(git_layout.main))
    monkeypatch.setenv("AGENTSTACK_PROJECT_WORK_DIR", str(git_layout.main))
    monkeypatch.setenv("AGENTSTACK_PROTECTED_ROOTS", str(git_layout.main))
    monkeypatch.setattr(
        dashboard_server,
        "_GRAPH_CACHE",
        {"ts": 0, "project_key": None, "data": None},
    )
    monkeypatch.setattr(dashboard_server, "tmux_state", lambda: sessions)
    monkeypatch.setattr(
        dashboard_server, "_runtime_name_binding_project", lambda _name: ""
    )
    monkeypatch.setattr(dashboard_server, "HOOKS_DIR", str(ROOT / "hooks"))

    def tmux_value(args: list[str]) -> str:
        assert args[:3] == ["show-environment", "-t", "=SharedChild"]
        variable = args[-1]
        values = {
            "PARENT_AGENT": "SharedParent",
            "AGENTSTACK_PROJECT_KEY": str(session_project),
            "AGENTSTACK_PROJECT_CONTEXT": "1",
            "AGENTSTACK_PROJECT_REPOSITORY": str(session_project),
            "AGENTSTACK_PROJECT_WORK_DIR": str(session_project),
        }
        value = values.get(variable, "")
        return f"{variable}={value}" if value else f"-{variable}"

    monkeypatch.setattr(dashboard_server, "_tmux", tmux_value)

    graph = dashboard_server._raw_graph()

    assert {node["name"] for node in graph["nodes"]} == {
        "SharedParent", "SharedChild",
    }
    assert graph["spawn"] == expected_spawn


def test_dashboard_graph_cache_keys_fresh_parent_for_reused_child_name(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _same_name_lineage_database(
        tmp_path / "lineage-reused-child.sqlite3",
        git_layout,
        with_project_a_message=False,
    )
    with sqlite3.connect(database) as connection:
        connection.executemany(
            "INSERT INTO agents VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            [
                (
                    5, 1, "StaleAParent", "model", "codex", "stale parent",
                    1789000200000000, 1788989000000000,
                ),
                (
                    6, 1, "CurrentBParent", "model", "codex", "current parent",
                    1789000200000000, 1788989000000000,
                ),
            ],
        )

    monkeypatch.setattr(graph_data, "DB_PATH", str(database))
    monkeypatch.setitem(sys.modules, "graph_data", graph_data)
    monkeypatch.setattr(
        graph_data,
        "_LIVE_PARENT_CACHE",
        {
            "ts": graph_data.time.monotonic(),
            "value": {"SharedChild": "StaleAParent"},
        },
    )
    monkeypatch.setattr(dashboard_server, "_project_key", lambda: str(git_layout.main))
    monkeypatch.setenv("AGENTSTACK_PROJECT_KEY", str(git_layout.main))
    monkeypatch.setenv("PROJECT_KEY", str(git_layout.main))
    monkeypatch.setenv("AGENTSTACK_PROJECT_CONTEXT", "1")
    monkeypatch.setenv("AGENTSTACK_PROJECT_REPOSITORY", str(git_layout.main))
    monkeypatch.setenv("AGENTSTACK_PROJECT_WORK_DIR", str(git_layout.main))
    monkeypatch.setenv("AGENTSTACK_PROTECTED_ROOTS", str(git_layout.main))
    monkeypatch.setattr(
        dashboard_server,
        "_GRAPH_CACHE",
        {"ts": 0, "project_key": None, "live_parents": None, "data": None},
    )
    monkeypatch.setattr(
        dashboard_server,
        "tmux_state",
        lambda: {"SharedChild": {"name": "SharedChild", "cwd": str(git_layout.main)}},
    )
    monkeypatch.setattr(
        dashboard_server, "_runtime_name_binding_project", lambda _name: ""
    )
    monkeypatch.setattr(dashboard_server, "HOOKS_DIR", str(ROOT / "hooks"))
    current_parent = {"name": "StaleAParent"}

    def tmux_value(args: list[str]) -> str:
        variable = args[-1]
        values = {
            "PARENT_AGENT": current_parent["name"],
            "AGENTSTACK_PROJECT_KEY": str(git_layout.main),
            "AGENTSTACK_PROJECT_CONTEXT": "1",
            "AGENTSTACK_PROJECT_REPOSITORY": str(git_layout.main),
            "AGENTSTACK_PROJECT_WORK_DIR": str(git_layout.main),
        }
        value = values.get(variable, "")
        return f"{variable}={value}" if value else f"-{variable}"

    monkeypatch.setattr(dashboard_server, "_tmux", tmux_value)

    first = dashboard_server._raw_graph()
    assert first["spawn"] == [
        {"source": "StaleAParent", "target": "SharedChild", "type": "spawn"}
    ]
    current_parent["name"] = "CurrentBParent"
    second = dashboard_server._raw_graph()

    assert second["spawn"] == [
        {"source": "CurrentBParent", "target": "SharedChild", "type": "spawn"}
    ]


def test_unknown_dashboard_project_never_falls_back_to_project_one(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _graph_database(tmp_path / "mail.sqlite3")
    monkeypatch.setattr(graph_data, "DB_PATH", str(database))
    monkeypatch.setattr(graph_data, "PROJECT_HUMAN_KEY", "/unknown-project")
    monkeypatch.setattr(graph_data, "PROJECT_ID", 1)
    monkeypatch.setattr(graph_data, "_live_parents", lambda: {})

    graph = graph_data.build_graph()

    assert graph["nodes"] == []
    assert graph["edges"] == []
    assert graph["spawn"] == []
    assert "MustNotLeak" not in json.dumps(graph)


def _mcp_payload(result: object) -> object:
    value = getattr(result, "structured_content", None)
    if value is None:
        value = getattr(result, "data", None)
    while isinstance(value, dict) and set(value) == {"result"}:
        value = value["result"]
    return value


def test_real_mail_service_keeps_same_name_inboxes_and_reservations_project_local(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the bundled service, not a resolver or request-shape mock."""
    from fastmcp import Client

    from agentstack_mail import app as mail_app
    from agentstack_mail import config as mail_config
    from agentstack_mail import db as mail_db

    monkeypatch.setenv(
        "AGENTSTACK_MAIL_ENV_FILE", str(tmp_path / "missing-mail.env")
    )
    monkeypatch.setenv(
        "AGENTSTACK_MAIL_DATABASE_URL",
        f"sqlite+aiosqlite:///{tmp_path / 'isolated-mail.sqlite3'}",
    )
    monkeypatch.setenv(
        "AGENTSTACK_MAIL_STORAGE_ROOT", str(tmp_path / "mail-archive")
    )
    monkeypatch.setenv(
        "AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR", str(tmp_path / "signals")
    )
    monkeypatch.setenv("AGENTSTACK_MAIL_NOTIFICATIONS_ENABLED", "true")
    monkeypatch.setenv("AGENTSTACK_MAIL_TOOLS_LOG_ENABLED", "false")
    monkeypatch.setenv("AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE", "passthrough")
    mail_db.reset_database_state()
    mail_config.clear_settings_cache()

    async def exercise() -> tuple[list[dict], list[dict], dict, dict, dict]:
        project_a = str(git_layout.main)
        project_b = str(git_layout.other)
        async with Client(mail_app.build_mcp_server()) as client:
            for project in (project_a, project_b):
                ensured = await client.call_tool(
                    "ensure_project", {"human_key": project, "format": "json"}
                )
                assert ensured.is_error is False
            for project, parent in (
                (project_a, "ParentAlpha"),
                (project_b, "ParentBeta"),
            ):
                for name in (parent, "SharedCurie"):
                    registered = await client.call_tool(
                        "register_agent",
                        {
                            "project_key": project,
                            "program": "issue-16-test",
                            "model": "test-model",
                            "name": name,
                            "task_description": "isolated namespace fixture",
                            "registration_token": f"token-{parent}-{name}",
                            "format": "json",
                        },
                    )
                    assert registered.is_error is False
            for project, parent, body in (
                (project_a, "ParentAlpha", "only-project-a"),
                (project_b, "ParentBeta", "only-project-b"),
            ):
                sent = await client.call_tool(
                    "send_message",
                    {
                        "project_key": project,
                        "sender_name": parent,
                        "to": ["SharedCurie"],
                        "subject": "project-local handoff",
                        "body_md": body,
                        "format": "json",
                    },
                )
                assert sent.is_error is False
            signal_payloads = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in (tmp_path / "signals").rglob("*.signal")
            ]
            assert len(signal_payloads) == 2
            assert {payload["project_key"] for payload in signal_payloads} == {
                project_a,
                project_b,
            }
            inbox_a = _mcp_payload(
                await client.call_tool(
                    "fetch_inbox",
                    {
                        "project_key": project_a,
                        "agent_name": "SharedCurie",
                        "include_bodies": True,
                        "format": "json",
                    },
                )
            )
            inbox_b = _mcp_payload(
                await client.call_tool(
                    "fetch_inbox",
                    {
                        "project_key": project_b,
                        "agent_name": "SharedCurie",
                        "include_bodies": True,
                        "format": "json",
                    },
                )
            )
            reserve_a = _mcp_payload(
                await client.call_tool(
                    "file_reservation_paths",
                    {
                        "project_key": project_a,
                        "agent_name": "SharedCurie",
                        "paths": ["src/shared.py"],
                        "ttl_seconds": 600,
                        "exclusive": True,
                        "format": "json",
                    },
                )
            )
            reserve_b = _mcp_payload(
                await client.call_tool(
                    "file_reservation_paths",
                    {
                        "project_key": project_b,
                        "agent_name": "SharedCurie",
                        "paths": ["src/shared.py"],
                        "ttl_seconds": 600,
                        "exclusive": True,
                        "format": "json",
                    },
                )
            )
            conflict_a = _mcp_payload(
                await client.call_tool(
                    "file_reservation_paths",
                    {
                        "project_key": project_a,
                        "agent_name": "ParentAlpha",
                        "paths": ["src/shared.py"],
                        "ttl_seconds": 600,
                        "exclusive": True,
                        "format": "json",
                    },
                )
            )
        await mail_db.dispose_database_for_shutdown()
        assert isinstance(inbox_a, list) and isinstance(inbox_b, list)
        assert isinstance(reserve_a, dict) and isinstance(reserve_b, dict)
        assert isinstance(conflict_a, dict)
        return inbox_a, inbox_b, reserve_a, reserve_b, conflict_a

    try:
        inbox_a, inbox_b, reserve_a, reserve_b, conflict_a = asyncio.run(exercise())
    finally:
        mail_db.reset_database_state()
        mail_config.clear_settings_cache()

    assert [message["body_md"] for message in inbox_a] == ["only-project-a"]
    assert [message["body_md"] for message in inbox_b] == ["only-project-b"]
    assert reserve_a["granted"] and reserve_a["conflicts"] == []
    assert reserve_b["granted"] and reserve_b["conflicts"] == []
    assert conflict_a["granted"] == [] and conflict_a["conflicts"]


def test_isolated_mail_accepts_actual_repository_keys_and_explicit_override(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use real repository identities without allowing identity-marker writes."""
    from fastmcp import Client

    from agentstack_mail import app as mail_app
    from agentstack_mail import config as mail_config
    from agentstack_mail import db as mail_db

    actual_values = {
        "studyplanner": os.environ.get("ISSUE16_ACTUAL_STUDYPLANNER_REPO", ""),
        "orrery": os.environ.get("ISSUE16_ACTUAL_ORRERY_REPO", ""),
        "orrery_linked": os.environ.get("ISSUE16_ACTUAL_ORRERY_LINKED", ""),
    }
    if not all(actual_values.values()):
        pytest.skip(
            "set ISSUE16_ACTUAL_STUDYPLANNER_REPO, ISSUE16_ACTUAL_ORRERY_REPO, "
            "and ISSUE16_ACTUAL_ORRERY_LINKED for the host-repository smoke"
        )
    studyplanner = pathlib.Path(actual_values["studyplanner"]).resolve()
    orrery = pathlib.Path(actual_values["orrery"]).resolve()
    orrery_linked = pathlib.Path(actual_values["orrery_linked"]).resolve()
    for repository in (studyplanner, orrery, orrery_linked):
        assert repository.is_dir() and (repository / ".git").exists()
    identity_markers = [
        studyplanner / ".agentstack-project-id",
        orrery / ".agentstack-project-id",
    ]
    marker_state_before = {
        marker: (
            marker.exists(),
            marker.stat().st_mtime_ns if marker.exists() else None,
            marker.stat().st_size if marker.exists() else None,
        )
        for marker in identity_markers
    }
    resolver_env = _isolated_env(tmp_path)
    explicit_key = _resolve_project(ROOT, resolver_env, explicit=str(studyplanner))
    assert explicit_key == str(studyplanner)
    assert _resolve_project(orrery_linked, resolver_env) == str(orrery)

    monkeypatch.setenv(
        "AGENTSTACK_MAIL_ENV_FILE", str(tmp_path / "missing-actual-mail.env")
    )
    monkeypatch.setenv(
        "AGENTSTACK_MAIL_DATABASE_URL",
        f"sqlite+aiosqlite:///{tmp_path / 'actual-keys-mail.sqlite3'}",
    )
    monkeypatch.setenv(
        "AGENTSTACK_MAIL_STORAGE_ROOT", str(tmp_path / "actual-keys-archive")
    )
    monkeypatch.setenv(
        "AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR",
        str(tmp_path / "actual-keys-signals"),
    )
    monkeypatch.setenv("AGENTSTACK_MAIL_NOTIFICATIONS_ENABLED", "false")
    monkeypatch.setenv("AGENTSTACK_MAIL_TOOLS_LOG_ENABLED", "false")
    monkeypatch.setenv("AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE", "passthrough")
    monkeypatch.setenv("AGENTSTACK_MAIL_WORKTREES_ENABLED", "false")
    monkeypatch.setenv("AGENTSTACK_MAIL_GIT_IDENTITY_ENABLED", "false")
    monkeypatch.setenv("AGENTSTACK_MAIL_PROJECT_IDENTITY_MODE", "dir")
    mail_db.reset_database_state()
    mail_config.clear_settings_cache()

    async def exercise() -> tuple[dict, dict, object, list[dict], list[dict]]:
        async with Client(mail_app.build_mcp_server()) as client:
            for project in (str(studyplanner), str(orrery)):
                ensured = await client.call_tool(
                    "ensure_project", {"human_key": project, "format": "json"}
                )
                assert ensured.is_error is False
                registered = await client.call_tool(
                    "register_agent",
                    {
                        "project_key": project,
                        "program": "issue-16-actual-key-smoke",
                        "model": "test-model",
                        "name": "ActualRepoCurie",
                        "task_description": "read-only actual repository identity smoke",
                        "registration_token": f"actual-{project == str(orrery)}-token",
                        "format": "json",
                    },
                )
                assert registered.is_error is False
                parent = await client.call_tool(
                    "register_agent",
                    {
                        "project_key": project,
                        "program": "issue-16-actual-key-smoke",
                        "model": "test-model",
                        "name": "ActualRepoParent",
                        "task_description": "actual repository handoff sender",
                        "registration_token": f"actual-parent-{project == str(orrery)}",
                        "format": "json",
                    },
                )
                assert parent.is_error is False
                sent = await client.call_tool(
                    "send_message",
                    {
                        "project_key": project,
                        "sender_name": "ActualRepoParent",
                        "to": ["ActualRepoCurie"],
                        "subject": "actual repository handoff",
                        "body_md": f"only-{project}",
                        "format": "json",
                    },
                )
                assert sent.is_error is False
            reserve_study = _mcp_payload(
                await client.call_tool(
                    "file_reservation_paths",
                    {
                        "project_key": str(studyplanner),
                        "agent_name": "ActualRepoCurie",
                        "paths": ["README.md"],
                        "ttl_seconds": 600,
                        "exclusive": True,
                        "format": "json",
                    },
                )
            )
            reserve_orrery = _mcp_payload(
                await client.call_tool(
                    "file_reservation_paths",
                    {
                        "project_key": str(orrery),
                        "agent_name": "ActualRepoCurie",
                        "paths": ["README.md"],
                        "ttl_seconds": 600,
                        "exclusive": True,
                        "format": "json",
                    },
                )
            )
            explicit_registration = await client.call_tool(
                "register_agent",
                {
                    "project_key": explicit_key,
                    "program": "issue-16-explicit-override-smoke",
                    "model": "test-model",
                    "name": "ExplicitOverrideCurie",
                    "task_description": "explicit path-valued context survived lookup",
                    "registration_token": "explicit-owner-token",
                    "format": "json",
                },
            )
            inbox_study = _mcp_payload(
                await client.call_tool(
                    "fetch_inbox",
                    {
                        "project_key": str(studyplanner),
                        "agent_name": "ActualRepoCurie",
                        "include_bodies": True,
                        "format": "json",
                    },
                )
            )
            inbox_orrery = _mcp_payload(
                await client.call_tool(
                    "fetch_inbox",
                    {
                        "project_key": str(orrery),
                        "agent_name": "ActualRepoCurie",
                        "include_bodies": True,
                        "format": "json",
                    },
                )
            )
        await mail_db.dispose_database_for_shutdown()
        assert isinstance(reserve_study, dict)
        assert isinstance(reserve_orrery, dict)
        assert isinstance(inbox_study, list) and isinstance(inbox_orrery, list)
        return (
            reserve_study,
            reserve_orrery,
            explicit_registration,
            inbox_study,
            inbox_orrery,
        )

    try:
        (
            reserve_study,
            reserve_orrery,
            explicit_registration,
            inbox_study,
            inbox_orrery,
        ) = asyncio.run(exercise())
    finally:
        mail_db.reset_database_state()
        mail_config.clear_settings_cache()

    assert reserve_study["granted"] and reserve_study["conflicts"] == []
    assert reserve_orrery["granted"] and reserve_orrery["conflicts"] == []
    assert explicit_registration.is_error is False
    assert _mcp_payload(explicit_registration)["name"] == "ExplicitOverrideCurie"
    assert [message["body_md"] for message in inbox_study] == [
        f"only-{studyplanner}"
    ]
    assert [message["body_md"] for message in inbox_orrery] == [f"only-{orrery}"]

    monkeypatch.setattr(dashboard_server, "PROJECT_KEY", str(orrery_linked))
    monkeypatch.setattr(dashboard_server, "VAULT", "")
    for name in (
        "AGENTSTACK_PROJECT_KEY",
        "PROJECT_KEY",
        "AGENTSTACK_PROJECT_CONTEXT",
        "AGENTSTACK_PROJECT_REPOSITORY",
        "AGENTSTACK_PROJECT_WORK_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    assert dashboard_server._project_key() == str(orrery)
    context, error = dashboard_server._resolved_work_dir_context(str(orrery_linked))
    assert error == "" and context is not None
    assert context["project_key"] == str(orrery)
    assert context["repository"] == str(orrery)
    assert context["work_dir"] == str(orrery_linked)
    marker_state_after = {
        marker: (
            marker.exists(),
            marker.stat().st_mtime_ns if marker.exists() else None,
            marker.stat().st_size if marker.exists() else None,
        )
        for marker in identity_markers
    }
    assert marker_state_after == marker_state_before


class _ReadyProcess:
    def wait(self, timeout: float | None = None) -> int:
        return 0


def _configure_dashboard_spawn(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    configured_project: pathlib.Path,
) -> tuple[list[tuple[str, dict]], list[tuple[list[str], dict]]]:
    for name in tuple(os.environ):
        if name.startswith("AGENTSTACK_") or name in {
            "PROJECT_KEY", "PARENT_AGENT", "CHILD_REGISTRATION_TOKEN",
        }:
            monkeypatch.delenv(name, raising=False)
    launcher = tmp_path / "dashboard-spawn-child.sh"
    _write_executable(launcher, "#!/bin/bash\nexit 0\n")
    runtime = tmp_path / "dashboard-runtime"
    calls: list[tuple[str, dict]] = []
    launched: list[tuple[list[str], dict]] = []

    def mcp(method: str, arguments: dict, timeout: int = 15) -> dict:
        calls.append((method, arguments))
        if method == "ensure_project":
            data = {"human_key": os.path.realpath(arguments["human_key"])}
        elif method == "register_agent":
            data = {
                "name": "IsolatedCurie",
                "registration_token": "server-token",
            }
        else:
            data = {}
        return {"ok": True, "data": data}

    real_run = subprocess.run

    def run(args: list[str], *run_args: object, **run_kwargs: object) -> object:
        if args and args[0] == "tmux":
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        return real_run(args, *run_args, **run_kwargs)

    def popen(args: list[str], **kwargs: object) -> _ReadyProcess:
        launched.append((args, kwargs))
        return _ReadyProcess()

    subprocess_proxy = SimpleNamespace(
        run=run,
        Popen=popen,
        SubprocessError=subprocess.SubprocessError,
        TimeoutExpired=subprocess.TimeoutExpired,
    )
    monkeypatch.setattr(dashboard_server, "subprocess", subprocess_proxy)
    monkeypatch.setattr(dashboard_server, "SPAWN_SCRIPT", str(launcher))
    monkeypatch.setattr(dashboard_server, "RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(dashboard_server, "ANNOT_PATH", str(runtime / "annotations.json"))
    monkeypatch.setattr(
        dashboard_server, "SUBST_PATH", str(runtime / "name-substitutions.json")
    )
    monkeypatch.setattr(dashboard_server, "HERE", str(tmp_path))
    monkeypatch.setattr(dashboard_server, "HOOKS_DIR", str(ROOT / "hooks"))
    monkeypatch.setattr(dashboard_server, "PROJECT_KEY", str(configured_project))
    monkeypatch.setattr(dashboard_server, "VAULT", "")
    monkeypatch.setattr(
        dashboard_server, "_project_key", lambda: str(configured_project)
    )
    monkeypatch.setattr(
        dashboard_server,
        "_spawn_name_status",
        lambda _name, _project_key: "available",
    )
    monkeypatch.setattr(dashboard_server, "_mcp_call", mcp)
    monkeypatch.setenv("HOME", str(tmp_path / "dashboard-home"))
    monkeypatch.setenv("AGENTSTACK_HOME", str(tmp_path / "dashboard-home" / ".agentstack"))
    monkeypatch.setenv("AGENTSTACK_PROJECT_KEY", str(configured_project))
    monkeypatch.setenv("PROJECT_KEY", str(configured_project))
    return calls, launched


def test_dashboard_standalone_spawn_uses_selected_linked_repository_namespace(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, launched = _configure_dashboard_spawn(
        tmp_path, monkeypatch, git_layout.other
    )

    result = dashboard_server.do_spawn(
        {
            "standalone": True,
            "name": "IsolatedCurie",
            "task": "work only in the selected repository",
            "dir": str(git_layout.linked_subdir),
        }
    )

    assert result["ok"] is True, result
    assert [method for method, _ in calls] == [
        "ensure_project", "register_agent", "set_contact_policy",
    ]
    assert calls[0][1] == {"human_key": str(git_layout.main)}
    assert {
        arguments["project_key"]
        for _, arguments in calls
        if "project_key" in arguments
    } == {
        str(git_layout.main)
    }
    args, kwargs = launched[0]
    assert args[-1] == str(git_layout.linked_subdir)
    launch_env = kwargs["env"]
    assert launch_env["AGENTSTACK_PROJECT_KEY"] == str(git_layout.main)
    assert launch_env["PROJECT_KEY"] == str(git_layout.main)
    assert launch_env["AGENTSTACK_PROJECT_CONTEXT"] == "1"
    assert launch_env["AGENTSTACK_PROJECT_REPOSITORY"] == str(git_layout.main)
    assert launch_env["AGENTSTACK_PROJECT_WORK_DIR"] == str(git_layout.linked)
    assert launch_env["AGENTSTACK_PROTECTED_ROOTS"] == (
        f"{git_layout.linked}:{git_layout.main}"
    )
    assert str(git_layout.other) not in json.dumps(calls)


@pytest.mark.parametrize("ensure_result", ["error", "different-project"])
def test_dashboard_standalone_project_ensure_fails_before_identity_side_effects(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
    monkeypatch: pytest.MonkeyPatch,
    ensure_result: str,
) -> None:
    calls, launched = _configure_dashboard_spawn(
        tmp_path, monkeypatch, git_layout.other
    )

    def mcp(method: str, arguments: dict, timeout: int = 15) -> dict:
        calls.append((method, arguments))
        assert method == "ensure_project"
        if ensure_result == "error":
            return {"ok": False, "error": "isolated ensure outage"}
        return {"ok": True, "data": {"human_key": str(git_layout.other)}}

    monkeypatch.setattr(dashboard_server, "_mcp_call", mcp)
    result = dashboard_server.do_spawn(
        {
            "standalone": True,
            "name": "IsolatedCurie",
            "task": "never create an identity before project validation",
            "dir": str(git_layout.linked_subdir),
        }
    )

    assert result["ok"] is False
    assert [method for method, _ in calls] == ["ensure_project"]
    assert calls[0][1] == {"human_key": str(git_layout.main)}
    assert launched == []
    runtime = tmp_path / "dashboard-runtime"
    assert not (runtime / "name-bindings").exists()
    assert not (runtime / "annotations.json").exists()
    assert not (runtime / "name-substitutions.json").exists()
    assert not (runtime / "spawn-tokens").exists()


def test_dashboard_standalone_ensures_and_registers_a_fresh_bundled_mail_project(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A never-seen selected repository works against the real bundled tools."""
    from fastmcp import Client

    from agentstack_mail import app as mail_app
    from agentstack_mail import config as mail_config
    from agentstack_mail import db as mail_db

    calls, launched = _configure_dashboard_spawn(
        tmp_path, monkeypatch, git_layout.other
    )
    monkeypatch.setenv(
        "AGENTSTACK_MAIL_ENV_FILE", str(tmp_path / "missing-dashboard-mail.env")
    )
    monkeypatch.setenv(
        "AGENTSTACK_MAIL_DATABASE_URL",
        f"sqlite+aiosqlite:///{tmp_path / 'dashboard-mail.sqlite3'}",
    )
    monkeypatch.setenv(
        "AGENTSTACK_MAIL_STORAGE_ROOT", str(tmp_path / "dashboard-mail-archive")
    )
    monkeypatch.setenv(
        "AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR",
        str(tmp_path / "dashboard-mail-signals"),
    )
    monkeypatch.setenv("AGENTSTACK_MAIL_NOTIFICATIONS_ENABLED", "false")
    monkeypatch.setenv("AGENTSTACK_MAIL_TOOLS_LOG_ENABLED", "false")
    monkeypatch.setenv("AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE", "passthrough")
    monkeypatch.setenv("AGENTSTACK_MAIL_WORKTREES_ENABLED", "false")
    monkeypatch.setenv("AGENTSTACK_MAIL_GIT_IDENTITY_ENABLED", "false")
    monkeypatch.setenv("AGENTSTACK_MAIL_PROJECT_IDENTITY_MODE", "dir")
    mail_db.reset_database_state()
    mail_config.clear_settings_cache()

    def real_mcp(method: str, arguments: dict, timeout: int = 15) -> dict:
        calls.append((method, arguments))

        async def invoke() -> object:
            prepared = dict(arguments)
            # Match Dashboard's live-schema shaping: the bundled strict tool
            # does not accept the compatibility-only owner-token field.
            if method == "set_contact_policy":
                prepared.pop("registration_token", None)
            async with Client(mail_app.build_mcp_server()) as client:
                return await client.call_tool(
                    method, prepared, raise_on_error=False
                )

        called = asyncio.run(invoke())
        if called.is_error:
            return {"ok": False, "error": str(called)}
        return {"ok": True, "data": _mcp_payload(called)}

    monkeypatch.setattr(dashboard_server, "_mcp_call", real_mcp)
    try:
        result = dashboard_server.do_spawn(
            {
                "standalone": True,
                "name": "FreshProjectCurie",
                "task": "register in a newly ensured repository namespace",
                "dir": str(git_layout.linked_subdir),
            }
        )
    finally:
        asyncio.run(mail_db.dispose_database_for_shutdown())
        mail_db.reset_database_state()
        mail_config.clear_settings_cache()

    assert result["ok"] is True, result
    assert [method for method, _ in calls] == [
        "ensure_project", "register_agent", "set_contact_policy",
    ]
    assert calls[0][1] == {"human_key": str(git_layout.main)}
    assert calls[1][1]["project_key"] == str(git_layout.main)
    assert launched


def test_dashboard_delegated_linked_target_uses_canonical_parent_namespace(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_project_key = dashboard_server._project_key
    calls, launched = _configure_dashboard_spawn(
        tmp_path, monkeypatch, git_layout.linked
    )
    monkeypatch.setattr(dashboard_server, "_project_key", real_project_key)
    runtime = pathlib.Path(dashboard_server.RUNTIME_DIR)
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "agent_token_ParentCurie").write_text(
        "canonical-parent-token", encoding="utf-8"
    )
    (runtime / "agent_token_ParentCurie.project").write_text(
        f"{git_layout.main}\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        dashboard_server,
        "_project_has_agent",
        lambda project_key, name: (
            project_key == str(git_layout.main) and name == "ParentCurie"
        ),
    )

    result = dashboard_server.do_spawn(
        {
            "parent": "ParentCurie",
            "name": "IsolatedCurie",
            "task": "delegate within the linked worktree",
            "dir": str(git_layout.linked_subdir),
        }
    )

    assert result["ok"] is True, result
    project_calls = [
        arguments["project_key"]
        for _method, arguments in calls
        if "project_key" in arguments
    ]
    assert project_calls
    assert set(project_calls) == {str(git_layout.main)}
    args, kwargs = launched[0]
    assert str(git_layout.linked_subdir) == args[-1]
    launch_env = kwargs["env"]
    assert launch_env["AGENTSTACK_PROJECT_KEY"] == str(git_layout.main)
    assert launch_env["AGENTSTACK_PROJECT_REPOSITORY"] == str(git_layout.main)
    assert launch_env["AGENTSTACK_PROJECT_WORK_DIR"] == str(git_layout.linked)


def test_dashboard_auto_name_uses_the_selected_repository_namespace(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, launched = _configure_dashboard_spawn(
        tmp_path, monkeypatch, git_layout.other
    )
    suggestion_projects: list[str] = []

    def suggest(project_key: str) -> str:
        suggestion_projects.append(project_key)
        return "IsolatedCurie"

    monkeypatch.setattr(dashboard_server, "_suggest_any_spawn_name", suggest)

    result = dashboard_server.do_spawn(
        {
            "standalone": True,
            "task": "auto-select a name only inside the selected namespace",
            "dir": str(git_layout.linked_subdir),
        }
    )

    assert result["ok"] is True, result
    assert suggestion_projects == [str(git_layout.main)]
    register_call = next(
        arguments for method, arguments in calls if method == "register_agent"
    )
    assert register_call["project_key"] == str(git_layout.main)
    assert launched


def test_dashboard_delegated_cross_repository_spawn_fails_before_mail_or_token(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, launched = _configure_dashboard_spawn(
        tmp_path, monkeypatch, git_layout.main
    )

    result = dashboard_server.do_spawn(
        {
            "parent": "ParentCurie",
            "name": "IsolatedCurie",
            "task": "must not cross repository boundaries",
            "dir": str(git_layout.other),
        }
    )

    assert result["ok"] is False
    assert "project" in result["error"].lower() or "repository" in result["error"].lower()
    assert calls == []
    assert launched == []
    assert not (tmp_path / "dashboard-runtime" / "spawn-tokens").exists()


def test_dashboard_rejects_foreign_parent_credential_before_child_side_effects(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, launched = _configure_dashboard_spawn(
        tmp_path, monkeypatch, git_layout.main
    )
    runtime = pathlib.Path(dashboard_server.RUNTIME_DIR)
    runtime.mkdir(parents=True)
    token = runtime / "agent_token_ParentCurie"
    token.write_text("project-b-owner-token", encoding="utf-8")
    token.with_name(token.name + ".project").write_text(
        str(git_layout.other), encoding="utf-8"
    )
    monkeypatch.setattr(dashboard_server, "_project_has_agent", lambda *_args: True)

    result = dashboard_server.do_spawn(
        {
            "parent": "ParentCurie",
            "name": "IsolatedCurie",
            "task": "must reject the foreign parent token before registration",
            "dir": str(git_layout.main),
        }
    )

    assert result["ok"] is False
    assert "parent" in result["error"].lower()
    assert calls == []
    assert launched == []
    assert not pathlib.Path(dashboard_server.ANNOT_PATH).exists()
    assert not pathlib.Path(dashboard_server.SUBST_PATH).exists()


def test_dashboard_authenticates_legacy_parent_before_registering_child(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, launched = _configure_dashboard_spawn(
        tmp_path, monkeypatch, git_layout.main
    )
    runtime = pathlib.Path(dashboard_server.RUNTIME_DIR)
    runtime.mkdir(parents=True)
    token = runtime / "agent_token_ParentCurie"
    token.write_text("legacy-parent-owner-token", encoding="utf-8")
    monkeypatch.setattr(dashboard_server, "_project_has_agent", lambda *_args: True)
    monkeypatch.setattr(
        dashboard_server,
        "_project_agent_registration",
        lambda *_args: {
            "program": "codex-cli",
            "model": "test-model",
            "task_description": "supported legacy parent",
        },
    )

    def mcp(method: str, arguments: dict, timeout: int = 15) -> dict:
        calls.append((method, arguments))
        if method == "whois":
            return {"ok": True, "data": {"name": "ParentCurie"}}
        if method == "register_agent":
            if arguments.get("name") == "ParentCurie":
                return {"ok": True, "data": {"name": "ParentCurie"}}
            return {
                "ok": True,
                "data": {
                    "name": "IsolatedCurie",
                    "registration_token": "server-token",
                },
            }
        return {"ok": True, "data": {}}

    monkeypatch.setattr(dashboard_server, "_mcp_call", mcp)
    result = dashboard_server.do_spawn(
        {
            "parent": "ParentCurie",
            "name": "IsolatedCurie",
            "task": "authenticate supported legacy ownership before handoff",
            "dir": str(git_layout.main),
        }
    )

    assert result["ok"] is True, result
    child_register_index = next(
        index
        for index, (method, arguments) in enumerate(calls)
        if method == "register_agent" and arguments.get("name") == "IsolatedCurie"
    )
    owner_checks = [
        (index, method, arguments)
        for index, (method, arguments) in enumerate(calls)
        if arguments.get("registration_token") == "legacy-parent-owner-token"
        and arguments.get("project_key") == str(git_layout.main)
    ]
    assert owner_checks and owner_checks[0][0] < child_register_index
    assert owner_checks[0][1] in {"whois", "register_agent"}
    if owner_checks[0][1] == "register_agent":
        assert owner_checks[0][2].get("name") == "ParentCurie"
    assert launched
    assert token.with_name(token.name + ".project").read_text(
        encoding="utf-8"
    ).strip() == str(git_layout.main)


def test_dashboard_spawn_refuses_a_shell_held_normalized_name_claim(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_name_status = dashboard_server._spawn_name_status
    calls, launched = _configure_dashboard_spawn(
        tmp_path, monkeypatch, git_layout.other
    )
    runtime = tmp_path / "dashboard-runtime"
    database = tmp_path / "dashboard-claim.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            f"""
            CREATE TABLE projects (id INTEGER PRIMARY KEY, human_key TEXT);
            CREATE TABLE agents (id INTEGER PRIMARY KEY, project_id INTEGER, name TEXT);
            INSERT INTO projects VALUES (1, '{git_layout.main}');
            """
        )
    env = _isolated_env(tmp_path)
    env.update(
        {
            "AGENTSTACK_RUNTIME_DIR": str(runtime),
            "AGENTSTACK_PYTHON": sys.executable,
        }
    )
    claim = subprocess.run(
        [
            "/bin/bash", "-c",
            'source "$1"; ags_acquire_local_name_claim "$2" SharedCurie candidate',
            "claim-name", str(REGISTER_LIB), str(git_layout.other),
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert claim.returncode == 0, claim.stderr
    assert (runtime / "name-bindings" / "sharedcurie.json").is_file()
    monkeypatch.setattr(dashboard_server, "DB_PATH", str(database))
    monkeypatch.setattr(dashboard_server, "_spawn_name_status", real_name_status)

    result = dashboard_server.do_spawn(
        {
            "standalone": True,
            "name": "Shared-Curie",
            "task": "must lose to the shell's atomic local claim",
            "dir": str(git_layout.linked_subdir),
        }
    )

    assert result["ok"] is False
    assert "name" in result["error"].lower() or "identity" in result["error"].lower()
    assert calls == []
    assert launched == []


def test_dashboard_resolver_failure_cannot_fall_back_to_configured_other_project(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, launched = _configure_dashboard_spawn(
        tmp_path, monkeypatch, git_layout.other
    )
    broken_helper = tmp_path / "broken-project-context.sh"
    _write_executable(broken_helper, "#!/bin/bash\nexit 70\n")
    monkeypatch.setattr(
        dashboard_server, "_project_context_helper", lambda: str(broken_helper)
    )

    result = dashboard_server.do_spawn(
        {
            "standalone": True,
            "name": "IsolatedCurie",
            "task": "a resolver outage must fail closed",
            "dir": str(git_layout.main),
        }
    )

    assert result["ok"] is False
    assert "project" in result["error"].lower() or "resolver" in result["error"].lower()
    assert calls == []
    assert launched == []
    assert not (tmp_path / "dashboard-runtime" / "spawn-tokens").exists()


def test_dashboard_name_lookup_is_project_scoped_but_local_aliases_are_global(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "dashboard-names.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            f"""
            CREATE TABLE projects (id INTEGER PRIMARY KEY, human_key TEXT);
            CREATE TABLE agents (id INTEGER PRIMARY KEY, project_id INTEGER, name TEXT);
            INSERT INTO projects VALUES (1, '{git_layout.main}');
            INSERT INTO projects VALUES (2, '{git_layout.other}');
            INSERT INTO agents VALUES (1, 2, 'SharedCurie');
            """
        )
    runtime = tmp_path / "dashboard-name-runtime"
    runtime.mkdir()
    monkeypatch.setattr(dashboard_server, "DB_PATH", str(database))
    monkeypatch.setattr(dashboard_server, "RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(dashboard_server, "PROJECT_KEY", str(git_layout.main))
    monkeypatch.setattr(dashboard_server, "VAULT", "")
    monkeypatch.setattr(dashboard_server, "_tmux", lambda _args: "")

    # A Mail row in another project is not an occupant in this project's UI.
    assert dashboard_server._spawn_name_status("Shared-Curie") == "available"

    # A local token/tmux spelling is machine-global and must block its
    # separator-normalized alias before Mail registration in any project.
    (runtime / "agent_token_SharedCurie").write_text(
        "other-project-owner-token", encoding="utf-8"
    )
    (runtime / "agent_token_SharedCurie.project").write_text(
        str(git_layout.other), encoding="utf-8"
    )
    assert dashboard_server._spawn_name_status("Shared-Curie") == "occupied"


def test_dashboard_filters_other_project_live_state_from_same_name_history(
    git_layout: GitLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = {
        "name": "SharedCurie",
        "last_active": 100,
        "program": "claude-code",
        "model": "claude-sonnet-5",
        "retired": False,
    }
    other_session = {
        "name": "SharedCurie",
        "created": 90,
        "session_id": "$other",
        "activity": 100,
        "attached": True,
        "cmd": "claude",
        "pane_pid": 1234,
        "cwd": str(git_layout.other),
        "title": "Working in the other repository",
    }
    monkeypatch.setattr(dashboard_server, "_project_key", lambda: str(git_layout.main))
    monkeypatch.setattr(dashboard_server, "_raw_graph", lambda _live_parents=None: {"nodes": [node]})
    monkeypatch.setattr(
        dashboard_server, "tmux_state", lambda: {"SharedCurie": other_session}
    )
    monkeypatch.setattr(
        dashboard_server,
        "_tmux_session_env",
        lambda _name, variable: (
            str(git_layout.other) if variable == "AGENTSTACK_PROJECT_KEY" else ""
        ),
    )
    monkeypatch.setattr(dashboard_server, "_codex_app_runtimes", lambda: {})
    monkeypatch.setattr(dashboard_server, "_process_tree_snapshot", lambda: None)
    monkeypatch.setattr(dashboard_server, "_deliverables_index", lambda: {})
    monkeypatch.setattr(dashboard_server, "_annotations", lambda: {})
    monkeypatch.setattr(dashboard_server, "_name_substitutions", lambda: {})

    result = dashboard_server.graph_payload(days=30, show_all=True)

    assert len(result["nodes"]) == 1
    assert result["nodes"][0]["name"] == "SharedCurie"
    assert result["nodes"][0]["present"] is False
    assert result["nodes"][0]["live"] == ""


def test_dashboard_controls_refuse_a_live_same_name_session_from_another_project(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "SharedCurie"
    other_session = {
        "name": name,
        "created": 90,
        "session_id": "$other",
        "activity": 100,
        "attached": False,
        "cmd": "zsh",
        "pane_pid": 1234,
        "cwd": str(git_layout.other),
        "title": "",
    }
    effects: list[object] = []

    class Result:
        returncode = 0
        stdout = "zsh\n"
        stderr = ""

    subprocess_proxy = SimpleNamespace(
        run=lambda args, **_kwargs: effects.append(("run", args)) or Result(),
        Popen=subprocess.Popen,
        SubprocessError=subprocess.SubprocessError,
        TimeoutExpired=subprocess.TimeoutExpired,
    )
    monkeypatch.setattr(dashboard_server, "subprocess", subprocess_proxy)
    monkeypatch.setattr(dashboard_server, "_project_key", lambda: str(git_layout.main))
    monkeypatch.setattr(dashboard_server, "_agent_program", lambda _name: "codex-cli")
    monkeypatch.setattr(dashboard_server, "_codex_app_runtimes", lambda: {})
    monkeypatch.setattr(dashboard_server, "_has_session", lambda _name: True)
    monkeypatch.setattr(
        dashboard_server, "tmux_state", lambda: {name: other_session}
    )
    monkeypatch.setattr(
        dashboard_server,
        "_tmux_session_env",
        lambda _name, variable: (
            str(git_layout.other) if variable == "AGENTSTACK_PROJECT_KEY" else ""
        ),
    )
    monkeypatch.setattr(dashboard_server, "_terminal_adapter", lambda: "ghostty")
    monkeypatch.setattr(dashboard_server, "_focus_existing_terminal", lambda _name: False)
    monkeypatch.setattr(
        dashboard_server,
        "_open_terminal_tmux",
        lambda args, **_kwargs: effects.append(("open", args)) or {"ok": True},
    )
    monkeypatch.setattr(
        dashboard_server,
        "_mcp_call",
        lambda method, arguments, **_kwargs: (
            effects.append(("mcp", method, arguments)) or {"ok": True}
        ),
    )
    transcript = tmp_path / "rollout-project-a.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(dashboard_server, "_codex_transcript_path", lambda _name: str(transcript))
    monkeypatch.setattr(
        dashboard_server,
        "_codex_meta",
        lambda _path: ("12345678-1234-1234-1234-123456789abc", str(git_layout.main)),
    )

    monkeypatch.setattr(
        dashboard_server,
        "build_agents",
        lambda: [
            {
                "name": name,
                "category": "agent",
                "running": True,
                "attached": False,
            }
        ],
    )
    results = [
        dashboard_server.term_capture(name, 80),
        dashboard_server.do_jump(name),
        dashboard_server.do_exit(name),
        dashboard_server.do_resume(name),
    ]
    monkeypatch.setattr(
        dashboard_server,
        "build_agents",
        lambda: [
            {
                "name": name,
                "category": "finished",
                "running": False,
                "attached": False,
            }
        ],
    )
    results.append(dashboard_server.do_kill(name, mode="tmux"))

    assert all(result["ok"] is False for result in results)
    assert effects == []


def test_dashboard_equal_session_key_requires_repository_corroboration(
    git_layout: GitLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    explicit_key = "explicit-project-a"
    monkeypatch.setattr(dashboard_server, "_project_key", lambda: explicit_key)
    monkeypatch.setenv("AGENTSTACK_PROJECT_KEY", explicit_key)
    monkeypatch.setenv("AGENTSTACK_PROJECT_REPOSITORY", str(git_layout.main))
    monkeypatch.setattr(dashboard_server, "HOOKS_DIR", str(ROOT / "hooks"))
    monkeypatch.setattr(
        dashboard_server, "_runtime_name_binding_project", lambda _name: ""
    )
    values: dict[str, str] = {}
    monkeypatch.setattr(
        dashboard_server,
        "_tmux_session_env",
        lambda _name, variable: values.get(variable, ""),
    )

    # A legacy/pre-fix key is not enough when the live pane is in repository B.
    values.update({"AGENTSTACK_PROJECT_KEY": explicit_key})
    assert not dashboard_server._session_matches_dashboard_project(
        "SharedCurie", {"cwd": str(git_layout.other)}
    )

    # A same-repository linked worktree remains a valid legacy upgrade path.
    assert dashboard_server._session_matches_dashboard_project(
        "SharedCurie", {"cwd": str(git_layout.linked)}
    )

    # New sessions carry a complete binding. An explicit human key may differ
    # from the repository path, but its repository evidence must still agree.
    values.update(
        {
            "AGENTSTACK_PROJECT_CONTEXT": "1",
            "AGENTSTACK_PROJECT_REPOSITORY": str(git_layout.main),
            "AGENTSTACK_PROJECT_WORK_DIR": str(git_layout.linked),
        }
    )
    assert dashboard_server._session_matches_dashboard_project(
        "SharedCurie", {"cwd": str(git_layout.linked_subdir)}
    )
    values["AGENTSTACK_PROJECT_REPOSITORY"] = str(git_layout.other)
    values["AGENTSTACK_PROJECT_WORK_DIR"] = str(git_layout.other)
    assert not dashboard_server._session_matches_dashboard_project(
        "SharedCurie", {"cwd": str(git_layout.other)}
    )


@pytest.mark.parametrize(
    "durable_case",
    [
        "binding-vs-sidecar",
        "binding-vs-state",
        "malformed-binding",
        "misnamed-binding",
        "empty-sidecar",
        "empty-state-project",
    ],
)
def test_dashboard_rejects_contradictory_or_unusable_durable_project_evidence(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
    monkeypatch: pytest.MonkeyPatch,
    durable_case: str,
) -> None:
    name = "SharedCurie"
    runtime = tmp_path / f"dashboard-durable-{durable_case}"
    binding_dir = runtime / "name-bindings"
    state_dir = runtime / "child-agents"
    binding_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    binding = binding_dir / "sharedcurie.json"
    binding.write_text(
        json.dumps({"agent_name": name, "project_key": str(git_layout.main)}),
        encoding="utf-8",
    )
    if durable_case == "binding-vs-sidecar":
        (runtime / f"agent_token_{name}.project").write_text(
            str(git_layout.other), encoding="utf-8"
        )
    elif durable_case == "binding-vs-state":
        (state_dir / f"{name}.json").write_text(
            json.dumps({"agent_name": name, "project_key": str(git_layout.other)}),
            encoding="utf-8",
        )
    elif durable_case == "malformed-binding":
        binding.write_text("{broken", encoding="utf-8")
    elif durable_case == "misnamed-binding":
        binding.write_text(
            json.dumps(
                {"agent_name": "OtherCurie", "project_key": str(git_layout.main)}
            ),
            encoding="utf-8",
        )
    elif durable_case == "empty-sidecar":
        (runtime / f"agent_token_{name}.project").write_text("", encoding="utf-8")
    elif durable_case == "empty-state-project":
        (state_dir / f"{name}.json").write_text(
            json.dumps({"agent_name": name, "project_key": ""}),
            encoding="utf-8",
        )

    monkeypatch.setattr(dashboard_server, "RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(dashboard_server, "_project_key", lambda: str(git_layout.main))
    monkeypatch.setattr(dashboard_server, "HOOKS_DIR", str(ROOT / "hooks"))
    session_values = {
        "AGENTSTACK_PROJECT_KEY": str(git_layout.main),
        "AGENTSTACK_PROJECT_CONTEXT": "1",
        "AGENTSTACK_PROJECT_REPOSITORY": str(git_layout.main),
        "AGENTSTACK_PROJECT_WORK_DIR": str(git_layout.linked),
    }
    monkeypatch.setattr(
        dashboard_server,
        "_tmux_session_env",
        lambda _name, variable: session_values.get(variable, ""),
    )

    assert dashboard_server._runtime_name_binding_project(name) == (
        dashboard_server._DURABLE_PROJECT_CONFLICT
    )
    assert not dashboard_server._session_matches_dashboard_project(
        name, {"cwd": str(git_layout.linked_subdir)}
    )


def test_dashboard_accepts_alias_equivalent_durable_project_evidence(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "AliasCurie"
    physical = tmp_path / "durable-physical"
    alias = tmp_path / "durable-alias"
    linked_subdir = physical / "nested"
    linked_subdir.mkdir(parents=True)
    alias.symlink_to(physical, target_is_directory=True)
    runtime = tmp_path / "dashboard-durable-alias-runtime"
    (runtime / "name-bindings").mkdir(parents=True)
    (runtime / "child-agents").mkdir(parents=True)
    (runtime / "name-bindings" / "aliascurie.json").write_text(
        json.dumps({"agent_name": name, "project_key": str(alias)}),
        encoding="utf-8",
    )
    (runtime / f"agent_token_{name}.project").write_text(
        str(physical.resolve()), encoding="utf-8"
    )
    (runtime / "child-agents" / f"{name}.json").write_text(
        json.dumps({"agent_name": name, "project_key": str(alias)}),
        encoding="utf-8",
    )
    monkeypatch.setattr(dashboard_server, "RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(
        dashboard_server, "_project_key", lambda: str(physical.resolve())
    )
    monkeypatch.setattr(
        dashboard_server,
        "_tmux_session_env",
        lambda _name, variable: (
            str(alias) if variable in {
                "AGENTSTACK_PROJECT_KEY",
                "AGENTSTACK_PROJECT_WORK_DIR",
            } else ""
        ),
    )
    monkeypatch.setattr(
        dashboard_server,
        "_cwd_matches_dashboard_project",
        lambda cwd: os.path.realpath(cwd) == str(physical.resolve()),
    )

    assert dashboard_server._runtime_name_binding_project(name) == str(
        physical.resolve()
    )
    assert dashboard_server._session_matches_dashboard_project(
        name, {"cwd": str(alias)}
    )


@pytest.mark.parametrize(
    ("bootstrap_status", "provider_runs"),
    [(42, False), (0, True)],
    ids=["bootstrap-rejects", "same-project-success"],
)
def test_codex_resume_requires_successful_registration_bootstrap(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
    monkeypatch: pytest.MonkeyPatch,
    bootstrap_status: int,
    provider_runs: bool,
) -> None:
    home = tmp_path / f"resume-home-{bootstrap_status}"
    bootstrap = home / ".codex" / "bin" / "codex_agent_bootstrap.sh"
    bootstrap.parent.mkdir(parents=True)
    bootstrap.write_text(f"return {bootstrap_status}\n", encoding="utf-8")
    fake_bin = tmp_path / f"resume-bin-{bootstrap_status}"
    fake_bin.mkdir()
    sentinel = tmp_path / f"provider-{bootstrap_status}.log"
    _write_executable(
        fake_bin / "codex",
        "#!/bin/bash\n"
        "printf '%s\\n' \"$*\" >> \"$ISSUE16_PROVIDER_SENTINEL\"\n",
    )
    transcript = tmp_path / f"rollout-{bootstrap_status}.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    launches: list[list[str]] = []
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        dashboard_server, "_live_session_conflicts_with_dashboard", lambda _name: False
    )
    monkeypatch.setattr(
        dashboard_server, "_codex_transcript_path", lambda _name: str(transcript)
    )
    monkeypatch.setattr(
        dashboard_server,
        "_codex_meta",
        lambda _path: ("12345678-1234-1234-1234-123456789abc", str(git_layout.main)),
    )
    monkeypatch.setattr(
        dashboard_server, "_tmux_project_context_args", lambda _cwd: []
    )
    monkeypatch.setattr(dashboard_server, "_codex_child_launch_flags", lambda: "")
    monkeypatch.setattr(dashboard_server, "_login_shell", lambda: "/bin/bash")
    monkeypatch.setattr(
        dashboard_server,
        "_open_terminal_tmux",
        lambda args, **_kwargs: launches.append(args) or {"ok": True},
    )

    generated = dashboard_server._do_resume_codex("SharedCurie")
    assert generated["ok"] is True
    assert len(launches) == 1
    inner = launches[0][-1]
    execution_env = _isolated_env(tmp_path)
    execution_env.update(
        {
            "HOME": str(home),
            "PATH": f"{fake_bin}:{execution_env['PATH']}",
            "ISSUE16_PROVIDER_SENTINEL": str(sentinel),
        }
    )
    executed = subprocess.run(
        ["/bin/bash", "-c", inner],
        env=execution_env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert sentinel.exists() is provider_runs
    if provider_runs:
        assert executed.returncode == 0, executed.stderr
        assert "resume 12345678-1234-1234-1234-123456789abc" in sentinel.read_text(
            encoding="utf-8"
        )
    else:
        assert executed.returncode == bootstrap_status


def test_codex_resume_fails_closed_when_registration_bootstrap_is_missing(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "resume-home-missing"
    home.mkdir()
    transcript = tmp_path / "rollout-missing.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    launches: list[list[str]] = []
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        dashboard_server, "_live_session_conflicts_with_dashboard", lambda _name: False
    )
    monkeypatch.setattr(
        dashboard_server, "_codex_transcript_path", lambda _name: str(transcript)
    )
    monkeypatch.setattr(
        dashboard_server,
        "_codex_meta",
        lambda _path: ("12345678-1234-1234-1234-123456789abc", str(git_layout.main)),
    )
    monkeypatch.setattr(
        dashboard_server, "_tmux_project_context_args", lambda _cwd: []
    )
    monkeypatch.setattr(
        dashboard_server,
        "_open_terminal_tmux",
        lambda args, **_kwargs: launches.append(args) or {"ok": True},
    )

    result = dashboard_server._do_resume_codex("SharedCurie")

    assert result["ok"] is False
    assert "bootstrap" in result["error"].lower()
    assert launches == []


def test_dashboard_legacy_metadata_is_visible_only_to_its_verified_project(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "dashboard-metadata-runtime"
    binding_dir = runtime / "name-bindings"
    binding_dir.mkdir(parents=True)
    (binding_dir / "sharedcurie.json").write_text(
        json.dumps(
            {
                "project_key": str(git_layout.main),
                "agent_name": "SharedCurie",
            }
        ),
        encoding="utf-8",
    )
    annotations = runtime / "annotations.json"
    substitutions = runtime / "name-substitutions.json"
    annotations.write_text(
        json.dumps(
            {
                "SharedCurie": {
                    "role": "project-a-role",
                    "emoji": "",
                    "group": "A",
                    "project_key": str(git_layout.main),
                },
                "AmbiguousCurie": {
                    "role": "must-remain-unattributed",
                    "emoji": "",
                    "group": "legacy",
                },
            }
        ),
        encoding="utf-8",
    )
    substitutions.write_text(
        json.dumps(
            {
                "SharedCurie": {
                    "requested": "Shared-Curie",
                    "project_key": str(git_layout.main),
                    "ts": "2026-09-12T00:00:00Z",
                },
                "AmbiguousCurie": {
                    "requested": "Ambiguous-Curie",
                    "ts": "2026-09-12T00:00:00Z",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(dashboard_server, "RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(dashboard_server, "ANNOT_PATH", str(annotations))
    monkeypatch.setattr(
        dashboard_server, "LEGACY_ANNOT_PATH", str(tmp_path / "absent-annotations.json")
    )
    monkeypatch.setattr(dashboard_server, "SUBST_PATH", str(substitutions))
    dashboard_server._ANNOT_CACHE.update(path="", mtime=-1.0, data={})
    dashboard_server._SUBST_CACHE.update(mtime=-1.0, data={})

    monkeypatch.setattr(dashboard_server, "_project_key", lambda: str(git_layout.other))
    assert "SharedCurie" not in dashboard_server._annotations()
    assert "SharedCurie" not in dashboard_server._name_substitutions()
    assert "AmbiguousCurie" not in dashboard_server._annotations()
    assert "AmbiguousCurie" not in dashboard_server._name_substitutions()

    monkeypatch.setattr(dashboard_server, "_project_key", lambda: str(git_layout.main))
    dashboard_server._ANNOT_CACHE.update(path="", mtime=-1.0, data={})
    dashboard_server._SUBST_CACHE.update(mtime=-1.0, data={})
    assert dashboard_server._annotations()["SharedCurie"]["role"] == "project-a-role"
    assert dashboard_server._name_substitutions()["SharedCurie"] == "Shared-Curie"
    # Even a current binding is not provenance for historical unkeyed data.
    assert "AmbiguousCurie" not in dashboard_server._annotations()
    assert "AmbiguousCurie" not in dashboard_server._name_substitutions()


def test_dashboard_metadata_reads_alias_equivalent_scoped_buckets_only(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    physical = tmp_path / "metadata-physical"
    alias = tmp_path / "metadata-alias"
    distinct = tmp_path / "metadata-distinct"
    physical.mkdir()
    distinct.mkdir()
    alias.symlink_to(physical, target_is_directory=True)
    annotations = tmp_path / "alias-annotations.json"
    substitutions = tmp_path / "alias-substitutions.json"
    annotations.write_text(
        json.dumps(
            {
                "projects": {
                    str(alias): {
                        "agents": {
                            "LegacyAliasCurie": {
                                "role": "legacy-alias",
                                "project_key": str(alias),
                            },
                            "UnkeyedCurie": {"role": "must-remain-unattributed"},
                            "ForeignEmbeddedCurie": {
                                "role": "must-not-leak",
                                "project_key": str(distinct),
                            },
                        }
                    },
                    str(physical.resolve()): {
                        "agents": {
                            "CanonicalCurie": {
                                "role": "canonical",
                                "project_key": str(physical.resolve()),
                            }
                        }
                    },
                    str(distinct): {
                        "agents": {
                            "ForeignBucketCurie": {
                                "role": "must-not-leak",
                                "project_key": str(distinct),
                            }
                        }
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    substitutions.write_text(
        json.dumps(
            {
                "projects": {
                    str(alias): {
                        "LegacyAliasCurie": {
                            "requested": "Legacy-Alias-Curie",
                            "project_key": str(alias),
                        },
                        "UnkeyedCurie": {"requested": "Unkeyed-Curie"},
                        "ForeignEmbeddedCurie": {
                            "requested": "Foreign-Embedded-Curie",
                            "project_key": str(distinct),
                        },
                    },
                    str(physical.resolve()): {
                        "CanonicalCurie": {
                            "requested": "Canonical-Curie",
                            "project_key": str(physical.resolve()),
                        }
                    },
                    str(distinct): {
                        "ForeignBucketCurie": {
                            "requested": "Foreign-Bucket-Curie",
                            "project_key": str(distinct),
                        }
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(dashboard_server, "ANNOT_PATH", str(annotations))
    monkeypatch.setattr(
        dashboard_server, "LEGACY_ANNOT_PATH", str(tmp_path / "absent-legacy.json")
    )
    monkeypatch.setattr(dashboard_server, "SUBST_PATH", str(substitutions))
    dashboard_server._ANNOT_CACHE.update(path="", mtime=-1.0, data={})
    dashboard_server._SUBST_CACHE.update(path="", mtime=-1.0, data={})

    observed_annotations = dashboard_server._annotations(str(physical.resolve()))
    observed_substitutions = dashboard_server._name_substitutions(
        str(physical.resolve())
    )

    assert set(observed_annotations) == {"LegacyAliasCurie", "CanonicalCurie"}
    assert observed_annotations["LegacyAliasCurie"]["role"] == "legacy-alias"
    assert observed_annotations["CanonicalCurie"]["role"] == "canonical"
    assert observed_substitutions == {
        "LegacyAliasCurie": "Legacy-Alias-Curie",
        "CanonicalCurie": "Canonical-Curie",
    }


def test_dashboard_metadata_writes_preserve_unattributed_legacy_entries(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "dashboard-preserved-metadata"
    runtime.mkdir()
    annotations = runtime / "annotations.json"
    substitutions = runtime / "name-substitutions.json"
    ambiguous_annotation = {
        "role": "historical-unattributed",
        "emoji": "?",
        "group": "legacy",
    }
    ambiguous_substitution = {
        "requested": "Ambiguous-Curie",
        "ts": "2026-09-12T00:00:00Z",
    }
    annotations.write_text(
        json.dumps({"AmbiguousCurie": ambiguous_annotation}), encoding="utf-8"
    )
    substitutions.write_text(
        json.dumps({"AmbiguousCurie": ambiguous_substitution}), encoding="utf-8"
    )
    monkeypatch.setattr(dashboard_server, "RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(dashboard_server, "ANNOT_PATH", str(annotations))
    monkeypatch.setattr(
        dashboard_server, "LEGACY_ANNOT_PATH", str(tmp_path / "absent-legacy.json")
    )
    monkeypatch.setattr(dashboard_server, "SUBST_PATH", str(substitutions))

    assert dashboard_server._write_annotation(
        "ScopedCurie", "scoped", "", "new", str(git_layout.main)
    )["ok"]
    dashboard_server._record_name_substitution(
        "ScopedCurie", "Scoped-Curie", str(git_layout.main)
    )

    annotation_store = json.loads(annotations.read_text(encoding="utf-8"))
    substitution_store = json.loads(substitutions.read_text(encoding="utf-8"))
    assert annotation_store["AmbiguousCurie"] == ambiguous_annotation
    assert substitution_store["AmbiguousCurie"] == ambiguous_substitution
    assert (
        annotation_store["projects"][str(git_layout.main)]["agents"]["ScopedCurie"]
        ["project_key"]
        == str(git_layout.main)
    )
    assert (
        substitution_store["projects"][str(git_layout.main)]["ScopedCurie"]
        ["project_key"]
        == str(git_layout.main)
    )


def test_dashboard_metadata_cache_never_crosses_project_or_store_path(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_annotations = tmp_path / "annotations-a.json"
    first_substitutions = tmp_path / "substitutions-a.json"
    first_annotations.write_text(
        json.dumps(
            {
                "SharedCurie": {
                    "role": "project-a",
                    "project_key": str(git_layout.main),
                }
            }
        ),
        encoding="utf-8",
    )
    first_substitutions.write_text(
        json.dumps(
            {
                "SharedCurie": {
                    "requested": "Shared-Curie",
                    "project_key": str(git_layout.main),
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(dashboard_server, "ANNOT_PATH", str(first_annotations))
    monkeypatch.setattr(
        dashboard_server, "LEGACY_ANNOT_PATH", str(tmp_path / "absent-legacy.json")
    )
    monkeypatch.setattr(dashboard_server, "SUBST_PATH", str(first_substitutions))
    dashboard_server._ANNOT_CACHE.update(path="", mtime=-1.0, project_key="", data={})
    dashboard_server._SUBST_CACHE.update(
        path="", mtime=-1.0, project_key="", data={}
    )
    assert dashboard_server._annotations(str(git_layout.main))
    assert dashboard_server._name_substitutions(str(git_layout.main))

    # A parse failure under another selected project cannot reuse A's cache.
    first_annotations.write_text("{broken", encoding="utf-8")
    first_substitutions.write_text("{broken", encoding="utf-8")
    assert dashboard_server._annotations(str(git_layout.other)) == {}
    assert dashboard_server._name_substitutions(str(git_layout.other)) == {}

    # Nor may a different store path with the same mtime reuse cached values.
    second_annotations = tmp_path / "annotations-b.json"
    second_substitutions = tmp_path / "substitutions-b.json"
    second_annotations.write_text("{broken", encoding="utf-8")
    second_substitutions.write_text("{broken", encoding="utf-8")
    cached_annotation_mtime = dashboard_server._ANNOT_CACHE["mtime"]
    cached_substitution_mtime = dashboard_server._SUBST_CACHE["mtime"]
    os.utime(second_annotations, (cached_annotation_mtime, cached_annotation_mtime))
    os.utime(
        second_substitutions,
        (cached_substitution_mtime, cached_substitution_mtime),
    )
    monkeypatch.setattr(dashboard_server, "ANNOT_PATH", str(second_annotations))
    monkeypatch.setattr(dashboard_server, "SUBST_PATH", str(second_substitutions))
    assert dashboard_server._annotations(str(git_layout.main)) == {}
    assert dashboard_server._name_substitutions(str(git_layout.main)) == {}


def test_dashboard_metadata_fails_closed_without_a_selected_project(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    annotations = tmp_path / "unconfigured-annotations.json"
    substitutions = tmp_path / "unconfigured-substitutions.json"
    monkeypatch.setattr(dashboard_server, "ANNOT_PATH", str(annotations))
    monkeypatch.setattr(
        dashboard_server, "LEGACY_ANNOT_PATH", str(tmp_path / "absent-legacy.json")
    )
    monkeypatch.setattr(dashboard_server, "SUBST_PATH", str(substitutions))
    monkeypatch.setattr(dashboard_server, "_project_key", lambda: "")

    annotation_shapes = [
        {"HistoricalCurie": {"role": "must-not-leak"}},
        {
            "projects": {
                "": {
                    "agents": {
                        "EmptyBucketCurie": {
                            "role": "must-not-leak",
                            "project_key": "",
                        }
                    }
                }
            }
        },
    ]
    substitution_shapes = [
        {"HistoricalCurie": {"requested": "ForeignCurie"}},
        {
            "projects": {
                "": {
                    "EmptyBucketCurie": {
                        "requested": "ForeignCurie",
                        "project_key": "",
                    }
                }
            }
        },
    ]
    for annotation_shape, substitution_shape in zip(
        annotation_shapes, substitution_shapes, strict=True
    ):
        annotations.write_text(json.dumps(annotation_shape), encoding="utf-8")
        substitutions.write_text(json.dumps(substitution_shape), encoding="utf-8")
        dashboard_server._ANNOT_CACHE.update(
            path=str(annotations),
            mtime=annotations.stat().st_mtime,
            project_key="",
            data={"CachedCurie": {"role": "must-not-leak"}},
        )
        dashboard_server._SUBST_CACHE.update(
            path=str(substitutions),
            mtime=substitutions.stat().st_mtime,
            project_key="",
            data={"CachedCurie": "ForeignCurie"},
        )

        assert dashboard_server._annotations() == {}
        assert dashboard_server._name_substitutions() == {}


def test_dashboard_standalone_metadata_writes_follow_the_selected_project(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, launched = _configure_dashboard_spawn(
        tmp_path, monkeypatch, git_layout.other
    )
    dashboard_server._ANNOT_CACHE.update(path="", mtime=-1.0, data={})
    dashboard_server._SUBST_CACHE.update(mtime=-1.0, data={})

    result = dashboard_server.do_spawn(
        {
            "standalone": True,
            "name": "Isolated-Curie",
            "role": "selected-project-role",
            "group": "selected-project-group",
            "task": "write metadata only in the selected namespace",
            "dir": str(git_layout.linked_subdir),
        }
    )

    assert result["ok"] is True, result
    assert result["child_name"] == "IsolatedCurie"
    register_call = next(
        arguments for method, arguments in calls if method == "register_agent"
    )
    assert register_call["project_key"] == str(git_layout.main)
    assert launched

    # The dashboard itself remains configured for repository A.
    assert "IsolatedCurie" not in dashboard_server._annotations()
    assert "IsolatedCurie" not in dashboard_server._name_substitutions()

    monkeypatch.setattr(dashboard_server, "_project_key", lambda: str(git_layout.main))
    dashboard_server._ANNOT_CACHE.update(path="", mtime=-1.0, data={})
    dashboard_server._SUBST_CACHE.update(mtime=-1.0, data={})
    assert dashboard_server._annotations()["IsolatedCurie"] == {
        "role": "selected-project-role",
        "emoji": "",
        "group": "selected-project-group",
    }
    assert dashboard_server._name_substitutions()["IsolatedCurie"] == (
        "Isolated-Curie"
    )


class _ReservationServer:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
                length = int(self.headers.get("Content-Length", "0"))
                owner.requests.append(json.loads(self.rfile.read(length)))
                body = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": "reservation-guard",
                        "result": {
                            "isError": False,
                            "structuredContent": {"renewed": 1},
                        },
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                return

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address
        return f"http://{host}:{port}/mcp"

    def __enter__(self) -> _ReservationServer:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.httpd.shutdown()
        self.thread.join(timeout=2)
        self.httpd.server_close()


def _run_reservation_hook(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
    server: _ReservationServer,
    *,
    cwd: pathlib.Path,
    file_path: str,
) -> subprocess.CompletedProcess[str]:
    env = _isolated_env(tmp_path)
    isolated_hooks = tmp_path / "isolated-hooks"
    isolated_hooks.mkdir(exist_ok=True)
    env.update(
        {
            "AGENT_NAME": "IssueSixteenTester",
            "AGENTSTACK_HOOKS_DIR": str(isolated_hooks),
            "AGENTSTACK_RUNTIME_DIR": str(tmp_path / "runtime"),
            "AGENTSTACK_MCP_URL": server.url,
            "AGENTSTACK_MAIL_HTTP_BEARER_MODE": "disabled",
            "FILE_RESERVATION_RETRY_DELAY_SECONDS": "0",
            # All three are stale and must lose to payload.cwd's Git context.
            "AGENTSTACK_PROJECT_KEY": str(git_layout.other),
            "PROJECT_KEY": str(git_layout.other),
            "AGENTSTACK_PROTECTED_ROOTS": str(git_layout.other),
        }
    )
    payload = json.dumps(
        {
            "session_id": "issue16-session",
            "cwd": str(cwd),
            "tool_input": {"file_path": file_path},
        }
    )
    return subprocess.run(
        ["/bin/bash", str(RESERVATION_HOOK)],
        cwd=git_layout.other,
        env=env,
        input=payload,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )


def test_reservation_uses_payload_worktree_and_canonical_project_namespace(
    tmp_path: pathlib.Path, git_layout: GitLayout,
) -> None:
    with _ReservationServer() as server:
        result = _run_reservation_hook(
            tmp_path,
            git_layout,
            server,
            cwd=git_layout.linked_subdir,
            file_path="../../tracked-note.md",
        )

    assert result.returncode == 0, result.stderr
    assert len(server.requests) == 1
    call = server.requests[0]["params"]
    assert call["name"] == "renew_file_reservations"
    arguments = call["arguments"]
    assert arguments["project_key"] == str(git_layout.main)
    assert arguments["agent_name"] == "IssueSixteenTester"
    assert arguments["paths"][0] == "tracked-note.md"
    assert arguments["paths"][1] == str(git_layout.linked / "tracked-note.md")
    assert str(git_layout.other) not in json.dumps(arguments)


def _non_git_reservation_env(
    tmp_path: pathlib.Path,
    workspace: pathlib.Path,
    server: _ReservationServer,
    *,
    python: str,
) -> dict[str, str]:
    env = _isolated_env(tmp_path)
    installed = pathlib.Path(env["AGENTSTACK_HOME"]) / "env.sh"
    installed.parent.mkdir(parents=True, exist_ok=True)
    installed.write_text(
        f"export AGENTSTACK_PROJECT_KEY='{workspace}'\n"
        f"export AGENTSTACK_PROTECTED_ROOTS='{workspace}'\n",
        encoding="utf-8",
    )
    env.update(
        {
            "AGENT_NAME": "IssueSixteenTester",
            "AGENTSTACK_HOOKS_DIR": str(ROOT / "hooks"),
            "AGENTSTACK_RUNTIME_DIR": str(tmp_path / "reservation-runtime"),
            "AGENTSTACK_MCP_URL": server.url,
            "AGENTSTACK_MAIL_HTTP_BEARER_MODE": "disabled",
            "AGENTSTACK_PYTHON": python,
            "AGENTSTACK_RELEASE_GRACE_SECONDS": "0",
            "FILE_RESERVATION_RETRY_DELAY_SECONDS": "0",
        }
    )
    return env


def test_reservation_guard_distinguishes_context_failure_from_outside_root_noop(
    tmp_path: pathlib.Path,
) -> None:
    workspace = tmp_path / "reservation-plain-workspace"
    outside = tmp_path / "reservation-outside"
    workspace.mkdir()
    outside.mkdir()
    inside_file = workspace / "inside.py"
    outside_file = outside / "outside.py"

    with _ReservationServer() as server:
        broken_env = _non_git_reservation_env(
            tmp_path,
            workspace,
            server,
            python=str(tmp_path / "missing-configured-python"),
        )
        broken = subprocess.run(
            ["/bin/bash", str(RESERVATION_HOOK)],
            cwd=workspace,
            env=broken_env,
            input=json.dumps(
                {
                    "session_id": "issue16-root-failure",
                    "cwd": str(workspace),
                    "tool_input": {"file_path": str(inside_file)},
                }
            ),
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert broken.returncode == 2
        assert "project context" in (broken.stdout + broken.stderr).lower()
        assert server.requests == []

        valid_env = _non_git_reservation_env(
            tmp_path, workspace, server, python=sys.executable
        )
        outside_result = subprocess.run(
            ["/bin/bash", str(RESERVATION_HOOK)],
            cwd=workspace,
            env=valid_env,
            input=json.dumps(
                {
                    "session_id": "issue16-outside-root",
                    "cwd": str(workspace),
                    "tool_input": {"file_path": str(outside_file)},
                }
            ),
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

    assert outside_result.returncode == 0, outside_result.stderr
    assert server.requests == []


@pytest.mark.parametrize(
    "release_hook",
    [RELEASE_RESERVATION_HOOK, RELEASE_ALL_RESERVATIONS_HOOK],
    ids=["one-file", "all-files"],
)
def test_release_hooks_skip_side_effects_when_project_context_is_unavailable(
    tmp_path: pathlib.Path,
    release_hook: pathlib.Path,
) -> None:
    workspace = tmp_path / f"release-root-failure-{release_hook.stem}"
    workspace.mkdir()
    runtime = tmp_path / "reservation-runtime"
    with _ReservationServer() as server:
        env = _non_git_reservation_env(
            tmp_path,
            workspace,
            server,
            python=str(tmp_path / "missing-configured-python"),
        )
        payload: dict[str, object] = {
            "session_id": "issue16-release-root-failure",
            "cwd": str(workspace),
        }
        if release_hook == RELEASE_RESERVATION_HOOK:
            payload.update(
                {
                    "tool_input": {"file_path": str(workspace / "inside.py")},
                    "tool_result": {"success": True},
                }
            )
        result = subprocess.run(
            ["/bin/bash", str(release_hook)],
            cwd=workspace,
            env=env,
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

    assert result.returncode == 0, result.stderr
    assert server.requests == []
    assert not (runtime / "file_release_debounce").exists()


@pytest.mark.parametrize("escape_kind", ["sibling-prefix", "symlink"])
def test_reservation_root_matching_rejects_prefix_and_symlink_escapes(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
    escape_kind: str,
) -> None:
    outside = git_layout.linked.parent / "repo-linked-outside"
    outside.mkdir()
    if escape_kind == "sibling-prefix":
        escaped = outside / "not-protected.md"
    else:
        link = git_layout.linked / "escape"
        link.symlink_to(outside, target_is_directory=True)
        escaped = link / "not-protected.md"

    with _ReservationServer() as server:
        result = _run_reservation_hook(
            tmp_path,
            git_layout,
            server,
            cwd=git_layout.linked,
            file_path=str(escaped),
        )

    assert result.returncode == 0, result.stderr
    assert server.requests == []


def _write_executable(path: pathlib.Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _launcher_env(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
    provider: str,
    *,
    inside_tmux: bool,
) -> tuple[
    dict[str, str], pathlib.Path, pathlib.Path, pathlib.Path, pathlib.Path, pathlib.Path
]:
    env = _isolated_env(tmp_path)
    _write_installed_env(pathlib.Path(env["HOME"]), git_layout.other)
    install = tmp_path / "isolated-install"
    (install / "bin" / "lib").mkdir(parents=True)
    (install / "hooks").mkdir()
    launcher = install / "bin" / TOP_LAUNCHERS[provider].name
    shutil.copy2(TOP_LAUNCHERS[provider], launcher)
    shutil.copy2(PROJECT_CONTEXT, install / "hooks" / PROJECT_CONTEXT.name)
    _write_executable(
        install / "bin" / "lib" / "agentstack-launch.sh",
        """#!/bin/bash
ags_die() { printf '%s: %s\n' "$AGS_PROG" "$*" >&2; exit 1; }
ags_load_env() { source "$AGENTSTACK_HOME/env.sh"; }
ags_resolve_tmux() { printf '%s\n' "$ISSUE16_TMUX_BIN"; }
ags_choose_dir() { (cd "$1" && pwd -P); }
""",
    )
    _write_executable(
        install / "bin" / "lib" / "agentstack-register.sh",
        """#!/bin/bash
ags_pick_adjective_scientist_name() { printf '%s\n' IssueSixteenTester; }
ags_mail_load_token() { :; }
ags_mcp_call() { return 1; }
ags_start_mail_watcher() { :; }
ags_record_managed_agent() { :; }
""",
    )
    for bootstrap in ("agentstack-codex-bootstrap", "agentstack-gemini-bootstrap"):
        _write_executable(
            install / "bin" / bootstrap,
            "#!/bin/bash\nunset GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR\nreturn 0\n",
        )
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    tmux_log = tmp_path / "tmux-new-session.json"
    tmux_calls = tmp_path / "tmux-calls.jsonl"
    tmux_git_env = tmp_path / "tmux-git-env.json"
    tmux_provider_log = tmp_path / "tmux-provider.log"
    tmux_state = tmp_path / "tmux-state"
    tmux = fake_bin / "tmux"
    _write_executable(
        tmux,
        """#!/usr/bin/env python3
import json
import os
import pathlib
import sys

args = sys.argv[1:]
command = args[0] if args else ""
state = pathlib.Path(os.environ["ISSUE16_TMUX_STATE"])
with pathlib.Path(os.environ["ISSUE16_TMUX_CALLS"]).open("a") as handle:
    handle.write(json.dumps(args) + "\\n")
if command == "new-session":
    pathlib.Path(os.environ["ISSUE16_TMUX_LOG"]).write_text(json.dumps(args))
    pathlib.Path(os.environ["ISSUE16_TMUX_GIT_ENV"]).write_text(json.dumps({
        name: os.environ.get(name)
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")
    }))
    child_env = os.environ.copy()
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
        child_env[name] = os.environ[f"ISSUE16_TMUX_SERVER_{name}"]
    index = 0
    while index + 1 < len(args):
        if args[index] == "-e":
            assignment = args[index + 1]
            key, separator, value = assignment.partition("=")
            if separator:
                child_env[key] = value
            index += 2
            continue
        index += 1
    cwd = args[args.index("-c") + 1]
    executed = __import__("subprocess").run(
        ["/bin/bash", "-c", args[-1]],
        cwd=cwd, env=child_env, text=True, capture_output=True,
    )
    pathlib.Path(os.environ["ISSUE16_TMUX_PROVIDER_LOG"]).write_text(
        executed.stdout + executed.stderr
    )
elif command == "display-message":
    print(state.read_text().strip() if state.exists() else "Issue16Before")
elif command == "has-session":
    raise SystemExit(1)
elif command == "rename-session":
    if os.environ.get("ISSUE16_TMUX_RENAME_FAIL") == "1":
        raise SystemExit(42)
    state.write_text(args[-1])
elif command == "list-sessions":
    raise SystemExit(1)
""",
    )
    provider_path = fake_bin / {
        "claude": "claude",
        "codex": "codex",
        "gemini": "agy",
    }[provider]
    _write_executable(
        provider_path,
        """#!/bin/bash
printf 'provider-project=%s\n' "${AGENTSTACK_PROJECT_KEY:-}"
printf 'provider-legacy-project=%s\n' "${PROJECT_KEY:-}"
printf 'provider-context=%s\n' "${AGENTSTACK_PROJECT_CONTEXT:-}"
printf 'provider-repository=%s\n' "${AGENTSTACK_PROJECT_REPOSITORY:-}"
printf 'provider-work-dir=%s\n' "${AGENTSTACK_PROJECT_WORK_DIR:-}"
printf 'provider-roots=%s\n' "${AGENTSTACK_PROTECTED_ROOTS:-}"
printf 'provider-cwd=%s\n' "$PWD"
printf 'provider-git-dir=%s\n' "${GIT_DIR-unset}"
printf 'provider-git-work-tree=%s\n' "${GIT_WORK_TREE-unset}"
printf 'provider-git-common-dir=%s\n' "${GIT_COMMON_DIR-unset}"
printf 'provider-git-top=%s\n' "$(git -C "$PWD" rev-parse --show-toplevel 2>/dev/null || printf unavailable)"
""",
    )
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "AGENTSTACK_MCP_URL": "http://127.0.0.1:1/mcp",
            "AGENTSTACK_HOOKS_DIR": str(ROOT / "hooks"),
            "AGENTSTACK_RUNTIME_DIR": str(tmp_path / "runtime"),
            "AGENTSTACK_TERMINAL": "none",
            "AGENTSTACK_CLAUDE_BIN": str(provider_path),
            "AGENTSTACK_CODEX_BIN": str(provider_path),
            "AGENTSTACK_GEMINI_BIN": str(provider_path),
            "ISSUE16_TMUX_LOG": str(tmux_log),
            "ISSUE16_TMUX_CALLS": str(tmux_calls),
            "ISSUE16_TMUX_GIT_ENV": str(tmux_git_env),
            "ISSUE16_TMUX_PROVIDER_LOG": str(tmux_provider_log),
            "ISSUE16_TMUX_SERVER_GIT_DIR": str(git_layout.other / ".git"),
            "ISSUE16_TMUX_SERVER_GIT_COMMON_DIR": str(git_layout.other / ".git"),
            "ISSUE16_TMUX_SERVER_GIT_WORK_TREE": str(git_layout.other),
            "ISSUE16_TMUX_STATE": str(tmux_state),
            "ISSUE16_TMUX_BIN": str(tmux),
            "SHELL": "/bin/false",
            # A fresh top-level launch must discard these established markers.
            "AGENTSTACK_PROJECT_CONTEXT": "1",
            "AGENTSTACK_PROJECT_REPOSITORY": str(git_layout.other),
            "AGENTSTACK_PROJECT_WORK_DIR": str(git_layout.other),
            "PARENT_AGENT": "StaleParent",
            "AGENTSTACK_RESERVED_IDENTITY": "1",
            "GIT_DIR": str(git_layout.other / ".git"),
            "GIT_COMMON_DIR": str(git_layout.other / ".git"),
            "GIT_WORK_TREE": str(git_layout.other),
        }
    )
    if inside_tmux:
        env["TMUX"] = "isolated,1,0"
    return env, tmux_log, tmux_calls, tmux_git_env, tmux_provider_log, launcher


@pytest.mark.parametrize("provider", TOP_LAUNCHERS)
@pytest.mark.parametrize("explicit", [False, True], ids=["repository", "explicit"])
def test_top_launchers_pin_project_context_into_new_tmux_session(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
    provider: str,
    explicit: bool,
) -> None:
    (
        env, tmux_log, _tmux_calls, tmux_git_env, tmux_provider_log, launcher
    ) = _launcher_env(
        tmp_path, git_layout, provider, inside_tmux=False
    )
    expected_key = "operator-selected-namespace" if explicit else str(git_layout.main)
    args = [str(launcher)]
    if explicit:
        args.extend(["--project-key", expected_key])
    args.append(str(git_layout.linked_subdir))

    result = subprocess.run(
        args,
        cwd=git_layout.other,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    # The generated wrapper intentionally ends in our /bin/false SHELL after
    # the fake tmux returns.  The tmux argv is the behavior under test.
    assert result.returncode != 0
    assert tmux_log.exists(), result.stdout + result.stderr
    tmux_args = json.loads(tmux_log.read_text(encoding="utf-8"))
    assert f"AGENTSTACK_PROJECT_KEY={expected_key}" in tmux_args
    assert f"PROJECT_KEY={expected_key}" in tmux_args
    assert "AGENTSTACK_PROJECT_CONTEXT=1" in tmux_args
    assert f"AGENTSTACK_PROJECT_REPOSITORY={git_layout.main}" in tmux_args
    assert f"AGENTSTACK_PROJECT_WORK_DIR={git_layout.linked}" in tmux_args
    assert (
        f"AGENTSTACK_PROTECTED_ROOTS={git_layout.linked}:{git_layout.main}"
        in tmux_args
    )
    assert not any(str(git_layout.other) in arg for arg in tmux_args)
    assert json.loads(tmux_git_env.read_text(encoding="utf-8")) == {
        "GIT_DIR": None,
        "GIT_WORK_TREE": None,
        "GIT_COMMON_DIR": None,
    }
    provider_observed = dict(
        line.split("=", 1)
        for line in tmux_provider_log.read_text(encoding="utf-8").splitlines()
        if line.startswith("provider-")
    )
    assert provider_observed["provider-git-dir"] == "unset"
    assert provider_observed["provider-git-work-tree"] == "unset"
    assert provider_observed["provider-git-common-dir"] == "unset"
    assert provider_observed["provider-git-top"] == str(git_layout.linked)


@pytest.mark.parametrize("provider", TOP_LAUNCHERS)
def test_top_launchers_export_repository_context_when_already_inside_tmux(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
    provider: str,
) -> None:
    (
        env, _tmux_log, tmux_calls, _tmux_git_env, _tmux_provider_log, launcher
    ) = _launcher_env(
        tmp_path, git_layout, provider, inside_tmux=True
    )
    result = subprocess.run(
        [str(launcher), str(git_layout.linked_subdir)],
        cwd=git_layout.other,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    observed = dict(
        line.split("=", 1)
        for line in result.stdout.splitlines()
        if line.startswith("provider-")
    )
    assert observed == {
        "provider-project": str(git_layout.main),
        "provider-legacy-project": str(git_layout.main),
        "provider-context": "1",
        "provider-repository": str(git_layout.main),
        "provider-work-dir": str(git_layout.linked),
        "provider-roots": f"{git_layout.linked}:{git_layout.main}",
        "provider-cwd": str(git_layout.linked_subdir),
        "provider-git-dir": "unset",
        "provider-git-work-tree": "unset",
        "provider-git-common-dir": "unset",
        "provider-git-top": str(git_layout.linked),
    }
    tmux_commands = [
        json.loads(line)
        for line in tmux_calls.read_text(encoding="utf-8").splitlines()
    ]
    expected_session_values = {
        "AGENTSTACK_PROJECT_KEY": str(git_layout.main),
        "PROJECT_KEY": str(git_layout.main),
        "AGENTSTACK_PROJECT_CONTEXT": "1",
        "AGENTSTACK_PROJECT_REPOSITORY": str(git_layout.main),
        "AGENTSTACK_PROJECT_WORK_DIR": str(git_layout.linked),
        "AGENTSTACK_PROTECTED_ROOTS": f"{git_layout.linked}:{git_layout.main}",
    }
    observed_session_values = {
        command[-2]: command[-1]
        for command in tmux_commands
        if command[:3] == [
            "set-environment",
            "-t",
            "=IssueSixteenTester" if provider == "claude" else "=Issue16Before",
        ]
    }
    assert observed_session_values == expected_session_values


@pytest.mark.parametrize(
    ("provider", "failure_kind"),
    [
        ("claude", "rename"),
        ("codex", "bootstrap"),
        ("gemini", "bootstrap"),
    ],
)
def test_failed_in_place_launch_preserves_the_existing_tmux_project_context(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
    provider: str,
    failure_kind: str,
) -> None:
    (
        env, _tmux_log, tmux_calls, _tmux_git_env, tmux_provider_log, launcher
    ) = _launcher_env(tmp_path, git_layout, provider, inside_tmux=True)
    if failure_kind == "rename":
        env["ISSUE16_TMUX_RENAME_FAIL"] = "1"
    else:
        bootstrap = pathlib.Path(launcher).with_name(
            f"agentstack-{provider}-bootstrap"
        )
        _write_executable(
            bootstrap,
            "#!/bin/bash\nunset GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR\nreturn 42\n",
        )

    result = subprocess.run(
        [str(launcher), str(git_layout.linked_subdir)],
        cwd=git_layout.other,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0, result.stdout + result.stderr
    commands = (
        [
            json.loads(line)
            for line in tmux_calls.read_text(encoding="utf-8").splitlines()
        ]
        if tmux_calls.exists()
        else []
    )
    assert not any(command[:1] == ["set-environment"] for command in commands)
    assert not tmux_provider_log.exists()


def test_gemini_dry_run_does_not_publish_context_to_inherited_tmux_session(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
) -> None:
    """A dry-run for another repository must leave the live session untouched."""
    (
        env, tmux_log, tmux_calls, _tmux_git_env, _tmux_provider_log, launcher
    ) = _launcher_env(
        tmp_path, git_layout, "gemini", inside_tmux=True
    )
    env["AGENTSTACK_GEMINI_BIN"] = "agy-definitely-not-installed-for-test"
    env["AGENTSTACK_GEMINI_MODEL"] = "gemini-3.8-flash-high"
    env["AGENTSTACK_GEMINI_EFFORT"] = "high"

    result = subprocess.run(
        [str(launcher), "--dry-run", str(git_layout.linked_subdir)],
        cwd=git_layout.other,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == [
        f"agent-start-gemini: dry-run cwd={git_layout.linked_subdir}",
        "agy-definitely-not-installed-for-test --model gemini-3.8-flash-high"
        " --effort high",
    ]
    assert not tmux_calls.exists(), tmux_calls.read_text(encoding="utf-8")
    assert not tmux_log.exists()


def test_candidate_registration_refuses_cross_project_local_name_collision(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
) -> None:
    """Mail names are project-local; token files and tmux names are not.

    A candidate which is free in repository A must not overwrite repository
    B's same-named credential before tmux eventually reports the collision.
    """
    env = _isolated_env(tmp_path)
    runtime = tmp_path / "runtime"
    state_dir = runtime / "child-agents"
    state_dir.mkdir(parents=True)
    token_file = runtime / "agent_token_Shared-Curie"
    token_file.write_text("repository-b-owner-token", encoding="utf-8")
    (state_dir / "Shared-Curie.json").write_text(
        json.dumps(
            {
                "agent_name": "Shared-Curie",
                "project_key": str(git_layout.other),
                "registration_token": "repository-b-owner-token",
            }
        ),
        encoding="utf-8",
    )
    fake_bin = tmp_path / "collision-bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "tmux",
        "#!/bin/bash\n"
        "if [[ \"${1:-}\" == has-session && \"${3:-}\" == '=Shared-Curie' ]]; then exit 0; fi\n"
        "exit 1\n",
    )
    calls = tmp_path / "mail-calls"
    script = f"""
source "{REGISTER_LIB}"
ags_agent_name_status() {{ printf '%s\n' available; }}
ags_pick_adjective_scientist_name() {{ printf '%s\n' Alternate-Curie; }}
ags_note_scientist_used() {{ :; }}
ags_generate_registration_token() {{ printf '%s\n' repository-a-new-token; }}
ags_apply_contact_policy() {{ :; }}
ags_mcp_call() {{
  printf '%s\n' "$*" >> "$ISSUE16_CALLS"
  if [[ "$1" == register_agent ]]; then
    registered=""
    for argument in "$@"; do
      case "$argument" in name=*) registered="${{argument#name=}}" ;; esac
    done
    printf '{{"result":{{"structuredContent":{{"id":2,"name":"%s","registration_token":"repository-a-new-token"}}}}}}\n' "$registered"
  else
    printf '%s\n' '{{"result":{{"structuredContent":{{}}}}}}'
  fi
}}
set +e
ags_register_session "{git_layout.main}" codex model cx "{git_layout.main}" Shared-Curie candidate >/dev/null
status=$?
set -e
printf 'status=%s\n' "$status"
"""
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "AGENTSTACK_RUNTIME_DIR": str(runtime),
            "ISSUE16_CALLS": str(calls),
        }
    )

    result = subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=git_layout.main,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    status = int(result.stdout.strip().split("=", 1)[1])
    assert status in {0, 1}
    assert token_file.read_text(encoding="utf-8") == "repository-b-owner-token"
    mail_calls = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    assert not any(
        line.startswith("register_agent ") and "name=Shared-Curie" in line
        for line in mail_calls
    )


@pytest.mark.parametrize(
    ("case", "server_status", "legacy_token", "expect_success"),
    [
        ("first-session", "available", False, True),
        ("legacy-name-absent-in-target", "available", True, False),
        ("legacy-name-exists-in-target", "occupied", True, True),
    ],
)
def test_reserved_registration_distinguishes_first_claim_from_legacy_reuse(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
    case: str,
    server_status: str,
    legacy_token: bool,
    expect_success: bool,
) -> None:
    runtime = tmp_path / f"registration-{case}"
    runtime.mkdir()
    token_file = runtime / "agent_token_SelectedCurie"
    if legacy_token:
        token_file.write_text("legacy-owner-token", encoding="utf-8")
    calls = tmp_path / f"registration-{case}.calls"
    env = _isolated_env(tmp_path)
    env.update(
        {
            "AGENTSTACK_RUNTIME_DIR": str(runtime),
            "AGENTSTACK_PYTHON": sys.executable,
            "ISSUE16_SERVER_STATUS": server_status,
            "ISSUE16_REGISTRATION_CALLS": str(calls),
        }
    )
    script = f"""
source "{REGISTER_LIB}"
ags_agent_name_status() {{ printf '%s\n' "$ISSUE16_SERVER_STATUS"; }}
ags_generate_registration_token() {{ printf '%s\n' first-owner-token; }}
ags_apply_contact_policy() {{ :; }}
ags_mcp_call() {{
  printf '%s\n' "$*" >> "$ISSUE16_REGISTRATION_CALLS"
  if [[ "$1" == register_agent ]]; then
    printf '%s\n' '{{"result":{{"structuredContent":{{"id":16,"name":"SelectedCurie","registration_token":"server-owner-token"}}}}}}'
  else
    printf '%s\n' '{{"result":{{"structuredContent":{{}}}}}}'
  fi
}}
set +e
ags_register_session "{git_layout.main}" codex test-model cx "{git_layout.main}" SelectedCurie reserved >/dev/null
rc=$?
set -e
printf '%s\n' "$rc"
"""
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=git_layout.main,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert (result.stdout.strip() == "0") is expect_success, result.stderr
    mail_calls = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    register_calls = [line for line in mail_calls if line.startswith("register_agent ")]
    if expect_success:
        assert len(register_calls) == 1
        assert token_file.read_text(encoding="utf-8") == "server-owner-token"
        assert (token_file.with_name(token_file.name + ".project")).read_text(
            encoding="utf-8"
        ).strip() == str(git_layout.main)
    else:
        assert register_calls == []
        assert token_file.read_text(encoding="utf-8") == "legacy-owner-token"
        assert not token_file.with_name(token_file.name + ".project").exists()


def test_shell_substitution_writer_preserves_legacy_and_scopes_verified_entries(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
) -> None:
    runtime = tmp_path / "shell-substitution-runtime"
    runtime.mkdir()
    store = runtime / "name-substitutions.json"
    ambiguous = {
        "requested": "Ambiguous-Curie",
        "ts": "2026-09-12T00:00:00Z",
    }
    verified = {
        "requested": "Verified-Curie",
        "project_key": str(git_layout.other),
        "ts": "2026-09-12T00:00:00Z",
    }
    store.write_text(
        json.dumps({"AmbiguousCurie": ambiguous, "VerifiedCurie": verified}),
        encoding="utf-8",
    )
    env = _isolated_env(tmp_path)
    env.update(
        {
            "AGENTSTACK_RUNTIME_DIR": str(runtime),
            "AGENTSTACK_PYTHON": sys.executable,
        }
    )
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            f'source "{REGISTER_LIB}"\n'
            f'ags_record_name_substitution ScopedCurie Scoped-Curie "{git_layout.main}"',
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(store.read_text(encoding="utf-8"))
    assert data["AmbiguousCurie"] == ambiguous
    assert (
        data["projects"][str(git_layout.other)]["VerifiedCurie"] == verified
    )
    assert (
        data["projects"][str(git_layout.main)]["ScopedCurie"]["project_key"]
        == str(git_layout.main)
    )


@pytest.mark.parametrize("provider", BOOTSTRAPS)
@pytest.mark.parametrize("established", [False, True], ids=["fresh", "established"])
def test_direct_bootstraps_resolve_and_preserve_repository_bound_context(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
    provider: str,
    established: bool,
) -> None:
    env = _isolated_env(tmp_path)
    env.update(
        {
            "AGENTSTACK_MCP_URL": "http://127.0.0.1:1/mcp",
            "AGENTSTACK_RUNTIME_DIR": str(tmp_path / "runtime"),
            "AGENTSTACK_PROJECT_KEY": str(git_layout.other),
            "PROJECT_KEY": str(git_layout.other),
            "AGENTSTACK_PROTECTED_ROOTS": str(git_layout.other),
        }
    )
    expected_key = str(git_layout.main)
    if established:
        expected_key = "operator-selected-namespace"
        env.update(
                {
                    "AGENTSTACK_PROJECT_KEY": expected_key,
                    "PROJECT_KEY": expected_key,
                    "AGENTSTACK_PROJECT_CONTEXT": "1",
                    "AGENTSTACK_PROJECT_REPOSITORY": str(git_layout.main),
                "AGENTSTACK_PROJECT_WORK_DIR": str(git_layout.linked),
                "AGENTSTACK_PROTECTED_ROOTS": (
                    f"{git_layout.linked}:{git_layout.main}"
                ),
            }
        )
    command = f"""
source "{BOOTSTRAPS[provider]}" "{git_layout.linked_subdir}" >/dev/null 2>&1
printf '%s\n' \
  "$AGENTSTACK_PROJECT_KEY" "$PROJECT_KEY" \
  "$AGENTSTACK_PROJECT_CONTEXT" "$AGENTSTACK_PROJECT_REPOSITORY" \
  "$AGENTSTACK_PROJECT_WORK_DIR" "$AGENTSTACK_PROTECTED_ROOTS"
"""

    result = subprocess.run(
        ["/bin/bash", "-c", command],
        cwd=git_layout.other,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=True,
    )

    assert result.stdout.splitlines() == [
        expected_key,
        expected_key,
        "1",
        str(git_layout.main),
        str(git_layout.linked),
        f"{git_layout.linked}:{git_layout.main}",
    ]


@pytest.mark.parametrize("provider", BOOTSTRAPS)
@pytest.mark.parametrize("failure_kind", ["resolver", "export"])
def test_direct_bootstrap_failure_prevents_the_provider_process(
    tmp_path: pathlib.Path,
    provider: str,
    failure_kind: str,
) -> None:
    workspace = tmp_path / f"bootstrap-{provider}-{failure_kind}-workspace"
    workspace.mkdir()
    install = tmp_path / f"bootstrap-{provider}-{failure_kind}-install"
    (install / "bin").mkdir(parents=True)
    (install / "hooks").mkdir()
    bootstrap = install / "bin" / BOOTSTRAPS[provider].name
    shutil.copy2(BOOTSTRAPS[provider], bootstrap)
    shutil.copy2(PROJECT_CONTEXT, install / "hooks" / PROJECT_CONTEXT.name)
    project_key = (
        str(tmp_path / "missing-project-key")
        if failure_kind == "resolver"
        else "logical-project-key"
    )
    (install / "env.sh").write_text(
        f"export AGENTSTACK_PROJECT_KEY={json.dumps(project_key)}\n"
        f"export PROJECT_KEY={json.dumps(project_key)}\n",
        encoding="utf-8",
    )
    provider_called = tmp_path / f"{provider}-{failure_kind}-provider-called"
    fake_provider = tmp_path / f"{provider}-{failure_kind}-provider"
    _write_executable(
        fake_provider,
        "#!/bin/bash\nprintf '%s\\n' called > \"$ISSUE16_PROVIDER_CALLED\"\n",
    )
    env = _isolated_env(tmp_path)
    env.update(
        {
            "AGENTSTACK_HOME": str(install),
            "AGENTSTACK_LABEL_PREFIX": "org.agentstack.test.issue16",
            "AGENTSTACK_RUNTIME_DIR": str(tmp_path / "bootstrap-runtime"),
            "AGENTSTACK_MCP_URL": "http://127.0.0.1:1/mcp",
            "AGENTSTACK_PYTHON": str(tmp_path / "missing-configured-python"),
            "ISSUE16_PROVIDER_CALLED": str(provider_called),
        }
    )
    command = f'source "{bootstrap}" "{workspace}" && "{fake_provider}"'

    result = subprocess.run(
        ["/bin/bash", "-c", command],
        cwd=workspace,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    expected_action = "resolve" if failure_kind == "resolver" else "export"
    assert f"could not {expected_action}" in result.stderr
    assert not provider_called.exists()
    assert not pathlib.Path(env["AGENTSTACK_RUNTIME_DIR"]).exists()


@pytest.mark.parametrize("provider", BOOTSTRAPS)
def test_direct_bootstraps_reject_established_cross_repository_target(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
    provider: str,
) -> None:
    env = _isolated_env(tmp_path)
    env.update(
        {
            "AGENTSTACK_MCP_URL": "http://127.0.0.1:1/mcp",
            "AGENTSTACK_RUNTIME_DIR": str(tmp_path / "runtime"),
            "AGENTSTACK_PROJECT_KEY": "operator-selected-namespace",
            "PROJECT_KEY": "operator-selected-namespace",
            "AGENTSTACK_PROJECT_CONTEXT": "1",
            "AGENTSTACK_PROJECT_REPOSITORY": str(git_layout.main),
            "AGENTSTACK_PROJECT_WORK_DIR": str(git_layout.linked),
            "AGENTSTACK_PROTECTED_ROOTS": (
                f"{git_layout.linked}:{git_layout.main}"
            ),
        }
    )
    command = f"""
set +e
source "{BOOTSTRAPS[provider]}" "{git_layout.other}" >/dev/null 2>&1
rc=$?
set -e
printf '%s\n' "$rc" "$AGENTSTACK_PROJECT_KEY" \
  "$AGENTSTACK_PROJECT_REPOSITORY" "$AGENTSTACK_PROJECT_WORK_DIR"
"""

    result = subprocess.run(
        ["/bin/bash", "-c", command],
        cwd=git_layout.main,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=True,
    )

    observed = result.stdout.splitlines()
    assert int(observed[0]) != 0
    assert observed[1:] == [
        "operator-selected-namespace",
        str(git_layout.main),
        str(git_layout.linked),
    ]


@pytest.mark.parametrize("provider", BOOTSTRAPS)
@pytest.mark.parametrize("live_project_variable", ["AGENTSTACK_PROJECT_KEY", "PROJECT_KEY"])
def test_direct_bootstraps_preserve_live_non_git_alias_over_installed_default(
    tmp_path: pathlib.Path,
    provider: str,
    live_project_variable: str,
) -> None:
    workspace = tmp_path / "bootstrap-non-git"
    workspace.mkdir()
    install = tmp_path / f"bootstrap-{provider}-{live_project_variable}"
    (install / "bin" / "lib").mkdir(parents=True)
    (install / "hooks").mkdir()
    bootstrap = install / "bin" / BOOTSTRAPS[provider].name
    shutil.copy2(BOOTSTRAPS[provider], bootstrap)
    shutil.copy2(PROJECT_CONTEXT, install / "hooks" / PROJECT_CONTEXT.name)
    shutil.copy2(REGISTER_LIB, install / "bin" / "lib" / REGISTER_LIB.name)
    shutil.copy2(
        ROOT / "bin" / "lib" / "agentstack-scientists.sh",
        install / "bin" / "lib" / "agentstack-scientists.sh",
    )
    (install / "env.sh").write_text(
        "export AGENTSTACK_PROJECT_KEY='/installed/project-a'\n"
        "export PROJECT_KEY='/installed/project-a'\n",
        encoding="utf-8",
    )
    env = _isolated_env(tmp_path)
    env.update(
        {
            "AGENTSTACK_MCP_URL": "http://127.0.0.1:1/mcp",
            "AGENTSTACK_RUNTIME_DIR": str(tmp_path / "bootstrap-runtime"),
        }
    )
    env[live_project_variable] = "/live/project-b"
    command = f"""
source "{bootstrap}" "{workspace}" >/dev/null 2>&1
printf '%s\n' "$AGENTSTACK_PROJECT_KEY" "$PROJECT_KEY"
"""

    result = subprocess.run(
        ["/bin/bash", "-c", command],
        cwd=workspace,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=True,
    )

    assert result.stdout.splitlines() == ["/live/project-b", "/live/project-b"]


def test_preregistered_child_rejects_another_repository_before_side_effects(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
) -> None:
    env = _isolated_env(tmp_path)
    runtime = tmp_path / "runtime"
    fake_bin = tmp_path / "child-bin"
    fake_bin.mkdir()
    tmux_calls = tmp_path / "child-tmux-calls"
    _write_executable(
        fake_bin / "tmux",
        "#!/bin/bash\nprintf '%s\\n' \"$*\" >> \"$ISSUE16_TMUX_CALLS\"\nexit 1\n",
    )
    _write_executable(fake_bin / "claude", "#!/bin/bash\nexit 0\n")
    handoff = tmp_path / "one-shot-child-token"
    handoff.write_text("repository-a-child-token", encoding="utf-8")
    handoff.chmod(0o600)
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "PARENT_AGENT": "SharedAgent",
            "PROJECT_KEY": str(git_layout.main),
            "AGENTSTACK_PROJECT_KEY": str(git_layout.main),
            "AGENTSTACK_PROJECT_CONTEXT": "1",
            "AGENTSTACK_PROJECT_REPOSITORY": str(git_layout.main),
            "AGENTSTACK_PROJECT_WORK_DIR": str(git_layout.main),
            "AGENTSTACK_PROTECTED_ROOTS": str(git_layout.main),
            "AGENTSTACK_RUNTIME_DIR": str(runtime),
            "AGENTSTACK_HOOKS_DIR": str(ROOT / "hooks"),
            "AGENTSTACK_HOME": str(tmp_path / "agentstack-home"),
            "AGENTSTACK_LABEL_PREFIX": "org.agentstack.test.issue16",
            "AGENTSTACK_TERMINAL": "none",
            "ISSUE16_TMUX_CALLS": str(tmux_calls),
        }
    )

    result = subprocess.run(
        [
            "/bin/bash",
            str(SPAWN_CHILD),
            "--pre-registered",
            "SharedChild",
            "--child-token-file",
            str(handoff),
            "--embed-task",
            "do isolated work",
            str(git_layout.other),
        ],
        cwd=git_layout.main,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    assert "repository" in result.stderr.lower()
    assert handoff.read_text(encoding="utf-8") == "repository-a-child-token"
    assert not (runtime / "agent_token_SharedChild").exists()
    assert not (runtime / "child-agents" / "SharedChild.json").exists()
    assert not tmux_calls.exists(), "cross-repo rejection must precede tmux launch"


def test_preregistered_child_linked_worktree_preserves_context_token_and_lifecycle(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
) -> None:
    env = _isolated_env(tmp_path)
    runtime = tmp_path / "linked-child-runtime"
    fake_bin = tmp_path / "linked-child-bin"
    fake_bin.mkdir()
    tmux_log = tmp_path / "linked-child-tmux.log"
    tmux_alive = tmp_path / "linked-child-tmux.alive"
    _write_executable(
        fake_bin / "tmux",
        "#!/bin/bash\n"
        "{ printf 'CALL'; for arg in \"$@\"; do printf '\\034%s' \"$arg\"; done; printf '\\035\\n'; } >> \"$ISSUE16_TMUX_LOG\"\n"
        "case \"${1:-}\" in\n"
        "  new-session) : > \"$ISSUE16_TMUX_ALIVE\" ;;\n"
        "  capture-pane) printf '\\n❯ \\n' ;;\n"
        "  has-session) [[ -f \"$ISSUE16_TMUX_ALIVE\" ]] ;;\n"
        "  kill-session) rm -f \"$ISSUE16_TMUX_ALIVE\" ;;\n"
        "  display-message) printf 'ParentCurie\\n' ;;\n"
        "esac\n",
    )
    _write_executable(fake_bin / "sleep", "#!/bin/bash\nexit 0\n")
    _write_executable(fake_bin / "claude", "#!/bin/bash\nexit 0\n")
    handoff = tmp_path / "linked-child-handoff"
    handoff.write_text("linked-child-owner-token", encoding="utf-8")
    handoff.chmod(0o600)
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "PARENT_AGENT": "ParentCurie",
            "PROJECT_KEY": str(git_layout.main),
            "AGENTSTACK_PROJECT_KEY": str(git_layout.main),
            "AGENTSTACK_PROJECT_CONTEXT": "1",
            "AGENTSTACK_PROJECT_REPOSITORY": str(git_layout.main),
            "AGENTSTACK_PROJECT_WORK_DIR": str(git_layout.main),
            "AGENTSTACK_PROTECTED_ROOTS": str(git_layout.main),
            "AGENTSTACK_RUNTIME_DIR": str(runtime),
            "AGENTSTACK_HOOKS_DIR": str(ROOT / "hooks"),
            "AGENTSTACK_REGISTER_LIB": str(REGISTER_LIB),
            "AGENTSTACK_HOME": str(tmp_path / "linked-child-home"),
            "AGENTSTACK_LABEL_PREFIX": "org.agentstack.test.issue16",
            "AGENTSTACK_MCP_PROXY": str(tmp_path / "missing-proxy"),
            "AGENTSTACK_TERMINAL": "none",
            "ISSUE16_TMUX_LOG": str(tmux_log),
            "ISSUE16_TMUX_ALIVE": str(tmux_alive),
        }
    )

    result = subprocess.run(
        [
            "/bin/bash",
            str(SPAWN_CHILD),
            "--pre-registered", "LinkedChild",
            "--child-token-file", str(handoff),
            "--embed-task", "work in the linked checkout",
            str(git_layout.linked_subdir),
        ],
        cwd=git_layout.main,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "LinkedChild"
    assert not handoff.exists(), "the one-shot handoff must be consumed"
    token_file = runtime / "agent_token_LinkedChild"
    project_file = runtime / "agent_token_LinkedChild.project"
    state_file = runtime / "child-agents" / "LinkedChild.json"
    assert token_file.read_text(encoding="utf-8") == "linked-child-owner-token"
    assert project_file.read_text(encoding="utf-8").strip() == str(git_layout.main)
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["project_key"] == str(git_layout.main)
    assert state["registration_token"] == "linked-child-owner-token"
    tmux = tmux_log.read_text(encoding="utf-8")
    assert f"AGENTSTACK_PROJECT_KEY={git_layout.main}" in tmux
    assert f"AGENTSTACK_PROJECT_REPOSITORY={git_layout.main}" in tmux
    assert f"AGENTSTACK_PROJECT_WORK_DIR={git_layout.linked}" in tmux
    assert f"AGENTSTACK_PROTECTED_ROOTS={git_layout.linked}:{git_layout.main}" in tmux
    tmux_calls = [
        record.removeprefix("CALL\034").split("\034")
        for record in tmux.split("\035\n")
        if record
    ]
    new_session = next(call for call in tmux_calls if call[0] == "new-session")
    assert new_session[new_session.index("-c") + 1] == str(git_layout.linked_subdir)


@pytest.mark.parametrize(
    "replacement_owner",
    [False, True],
    ids=["missing-binding", "replacement-owner"],
)
def test_preregistered_child_worktree_git_operations_ignore_inherited_selectors(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
    replacement_owner: bool,
) -> None:
    child_name = "PoisonedGitCurie"
    worktree = (pathlib.Path("/tmp/cc-worktrees") / child_name).resolve()
    branch = f"exp/{child_name}"
    assert not worktree.exists(), f"unexpected stale test worktree: {worktree}"
    env = _isolated_env(tmp_path)
    runtime = tmp_path / "poisoned-worktree-runtime"
    fake_bin = tmp_path / "poisoned-worktree-bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "sleep",
        "#!/bin/bash\nexit 0\n",
    )
    evidence = tmp_path / "poisoned-worktree-evidence.json"
    provider_evidence = tmp_path / "poisoned-child-provider-evidence.json"
    _write_executable(
        fake_bin / "tmux",
        """#!/usr/bin/env python3
import json
import os
import pathlib
import subprocess
import sys

args = sys.argv[1:]
if args and args[0] == "display-message":
    print("ParentCurie")
    raise SystemExit(0)
if args and args[0] == "new-session":
    cwd = args[args.index("-c") + 1]
    clean = {
        key: os.environ.get(key)
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")
    }
    clean["top"] = subprocess.run(
        ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    clean["common"] = subprocess.run(
        ["git", "-C", cwd, "rev-parse", "--git-common-dir"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    pathlib.Path(os.environ["ISSUE16_WORKTREE_EVIDENCE"]).write_text(
        json.dumps(clean)
    )
    child_env = os.environ.copy()
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
        child_env[key] = os.environ[f"ISSUE16_TMUX_SERVER_{key}"]
    index = 0
    while index + 1 < len(args):
        if args[index] == "-e":
            key, separator, value = args[index + 1].partition("=")
            if separator:
                child_env[key] = value
            index += 2
            continue
        index += 1
    subprocess.run(
        ["/bin/bash", "-c", args[-1]],
        cwd=cwd, env=child_env, timeout=10, check=False,
    )
    if os.environ.get("ISSUE16_INSTALL_REPLACEMENT") == "1":
        runtime = pathlib.Path(os.environ["AGENTSTACK_RUNTIME_DIR"])
        name = child_env["AGENT_NAME"]
        project = os.environ["ISSUE16_REPLACEMENT_PROJECT"]
        state_dir = runtime / "child-agents"
        binding_dir = runtime / "name-bindings"
        codex_home = state_dir / f"{name}.codex-home"
        state_dir.mkdir(parents=True, exist_ok=True)
        binding_dir.mkdir(parents=True, exist_ok=True)
        codex_home.mkdir(parents=True, exist_ok=True)
        (runtime / f"agent_token_{name}").write_text("replacement-owner-token")
        (runtime / f"agent_token_{name}.project").write_text(project + "\\n")
        (state_dir / f"{name}.json").write_text(json.dumps({
            "agent_name": name,
            "project_key": project,
            "registration_token": "replacement-owner-token",
        }))
        (state_dir / f"{name}.mcp.json").write_text("replacement-mcp-state")
        (codex_home / "sentinel").write_text("replacement-codex-home")
        (binding_dir / "poisonedgitcurie.json").write_text(json.dumps({
            "project_key": project,
            "agent_name": name,
        }))
        (runtime / "managed_agents.txt").write_text(name + "\\n")
    raise SystemExit(0)
raise SystemExit(1)
""",
    )
    _write_executable(
        fake_bin / "claude",
        """#!/usr/bin/env python3
import json
import os
import pathlib
import subprocess
data = {
    key: os.environ.get(key)
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")
}
data["top"] = subprocess.run(
    ["git", "-C", os.getcwd(), "rev-parse", "--show-toplevel"],
    check=True, capture_output=True, text=True,
).stdout.strip()
pathlib.Path(os.environ["ISSUE16_PROVIDER_EVIDENCE"]).write_text(json.dumps(data))
""",
    )
    handoff = tmp_path / "poisoned-worktree-handoff"
    handoff.write_text("poisoned-worktree-owner-token", encoding="utf-8")
    handoff.chmod(0o600)
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "PARENT_AGENT": "ParentCurie",
            "PROJECT_KEY": str(git_layout.main),
            "AGENTSTACK_PROJECT_KEY": str(git_layout.main),
            "AGENTSTACK_PROJECT_CONTEXT": "1",
            "AGENTSTACK_PROJECT_REPOSITORY": str(git_layout.main),
            "AGENTSTACK_PROJECT_WORK_DIR": str(git_layout.main),
            "AGENTSTACK_PROTECTED_ROOTS": str(git_layout.main),
            "AGENTSTACK_RUNTIME_DIR": str(runtime),
            "AGENTSTACK_HOOKS_DIR": str(ROOT / "hooks"),
            "AGENTSTACK_REGISTER_LIB": str(REGISTER_LIB),
            "AGENTSTACK_HOME": str(tmp_path / "poisoned-worktree-home"),
            "AGENTSTACK_LABEL_PREFIX": "org.agentstack.test.issue16",
            "AGENTSTACK_MCP_PROXY": str(tmp_path / "missing-proxy"),
            "AGENTSTACK_TERMINAL": "none",
            "AGENTSTACK_MCP_URL": "http://127.0.0.1:1/mcp",
            "AGENTSTACK_MAIL_HTTP_BEARER_MODE": "disabled",
            "ISSUE16_WORKTREE_EVIDENCE": str(evidence),
            "ISSUE16_PROVIDER_EVIDENCE": str(provider_evidence),
            "ISSUE16_TMUX_SERVER_GIT_DIR": str(git_layout.other / ".git"),
            "ISSUE16_TMUX_SERVER_GIT_COMMON_DIR": str(git_layout.other / ".git"),
            "ISSUE16_TMUX_SERVER_GIT_WORK_TREE": str(git_layout.other),
            "ISSUE16_INSTALL_REPLACEMENT": "1" if replacement_owner else "0",
            "ISSUE16_REPLACEMENT_PROJECT": str(git_layout.other),
            # Every downstream git operation must ignore this other repository.
            "GIT_DIR": str(git_layout.other / ".git"),
            "GIT_COMMON_DIR": str(git_layout.other / ".git"),
            "GIT_WORK_TREE": str(git_layout.other),
        }
    )

    clean_env = _isolated_env(tmp_path)
    try:
        result = subprocess.run(
            [
                "/bin/bash", str(SPAWN_CHILD),
                "--pre-registered", child_name,
                "--child-token-file", str(handoff),
                "--worktree", "--embed-task", "exercise isolated git worktree",
                str(git_layout.main),
            ],
            cwd=git_layout.main,
            env=env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        assert result.returncode != 0, "the fake tmux deliberately rejects launch"
        observed = json.loads(evidence.read_text(encoding="utf-8"))
        assert observed == {
            "GIT_DIR": None,
            "GIT_WORK_TREE": None,
            "GIT_COMMON_DIR": None,
            "top": str(worktree),
            "common": str(git_layout.main / ".git"),
        }
        assert json.loads(provider_evidence.read_text(encoding="utf-8")) == {
            "GIT_DIR": None,
            "GIT_WORK_TREE": None,
            "GIT_COMMON_DIR": None,
            "top": str(worktree),
        }
        assert not worktree.exists(), (
            "failed spawn must roll its worktree back\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        assert _git("branch", "--list", branch, cwd=git_layout.main) == ""
        if replacement_owner:
            binding = runtime / "name-bindings" / "poisonedgitcurie.json"
            token = runtime / f"agent_token_{child_name}"
            project = runtime / f"agent_token_{child_name}.project"
            state = runtime / "child-agents" / f"{child_name}.json"
            mcp_state = runtime / "child-agents" / f"{child_name}.mcp.json"
            codex_home = (
                runtime / "child-agents" / f"{child_name}.codex-home" / "sentinel"
            )
            assert json.loads(binding.read_text(encoding="utf-8")) == {
                "project_key": str(git_layout.other),
                "agent_name": child_name,
            }
            assert token.read_text(encoding="utf-8") == "replacement-owner-token"
            assert project.read_text(encoding="utf-8").strip() == str(git_layout.other)
            assert json.loads(state.read_text(encoding="utf-8"))["project_key"] == str(
                git_layout.other
            )
            assert mcp_state.read_text(encoding="utf-8") == "replacement-mcp-state"
            assert codex_home.read_text(encoding="utf-8") == "replacement-codex-home"
            assert (runtime / "managed_agents.txt").read_text(
                encoding="utf-8"
            ).splitlines() == [child_name]
    finally:
        # The assertions above are deliberately red against unsafe versions;
        # keep their temporary worktrees out of later runs even then.
        for repository in (git_layout.main, git_layout.other):
            subprocess.run(
                ["git", "-C", str(repository), "worktree", "remove", "--force", str(worktree)],
                env=clean_env,
                capture_output=True,
                check=False,
            )
            subprocess.run(
                ["git", "-C", str(repository), "branch", "-D", branch],
                env=clean_env,
                capture_output=True,
                check=False,
            )


def _cleanup_child_fixture(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
    server: _ReservationServer,
    *,
    token_project: pathlib.Path,
    fake_rm: bool = False,
) -> tuple[subprocess.CompletedProcess[str], pathlib.Path, pathlib.Path]:
    name = "CleanupCurie"
    runtime = tmp_path / "cleanup-runtime"
    state_dir = runtime / "child-agents"
    binding_dir = runtime / "name-bindings"
    state_dir.mkdir(parents=True)
    binding_dir.mkdir()
    token = runtime / f"agent_token_{name}"
    project_file = runtime / f"agent_token_{name}.project"
    state = state_dir / f"{name}.json"
    codex_home = state_dir / f"{name}.codex-home"
    codex_home.mkdir()
    (codex_home / "sentinel").write_text("owned by the child", encoding="utf-8")
    token.write_text("cleanup-owner-token", encoding="utf-8")
    project_file.write_text(f"{token_project}\n", encoding="utf-8")
    state.write_text(
        json.dumps(
            {
                "agent_name": name,
                "project_key": str(git_layout.main),
                "registration_token": "cleanup-owner-token",
            }
        ),
        encoding="utf-8",
    )
    binding = binding_dir / "cleanupcurie.json"
    binding.write_text(
        json.dumps({"project_key": str(git_layout.main), "agent_name": name}),
        encoding="utf-8",
    )
    env = _isolated_env(tmp_path)
    env.update(
        {
            "AGENT_NAME": name,
            "AGENTSTACK_PROJECT_KEY": str(git_layout.main),
            "PROJECT_KEY": str(git_layout.main),
            "AGENTSTACK_PROJECT_CONTEXT": "1",
            "AGENTSTACK_PROJECT_REPOSITORY": str(git_layout.main),
            "AGENTSTACK_PROJECT_WORK_DIR": str(git_layout.main),
            "AGENTSTACK_RUNTIME_DIR": str(runtime),
            "AGENTSTACK_HOOKS_DIR": str(ROOT / "hooks"),
            "AGENTSTACK_MCP_URL": server.url,
            "AGENTSTACK_MAIL_HTTP_BEARER_MODE": "disabled",
        }
    )
    removal_log = tmp_path / "cleanup-rm.log"
    if fake_rm:
        fake_bin = tmp_path / "cleanup-bin"
        fake_bin.mkdir()
        _write_executable(
            fake_bin / "rm",
            "#!/bin/bash\n"
            "if [[ -e \"$ISSUE16_BINDING\" ]]; then state=present; else state=absent; fi\n"
            "printf '%s|%s\\n' \"$state\" \"$*\" >> \"$ISSUE16_RM_LOG\"\n"
            "exec /bin/rm \"$@\"\n",
        )
        env["PATH"] = f"{fake_bin}:{env['PATH']}"
        env["ISSUE16_BINDING"] = str(binding)
        env["ISSUE16_RM_LOG"] = str(removal_log)
    result = subprocess.run(
        ["/bin/bash", str(CLEANUP_CHILD), name],
        cwd=git_layout.main,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    return result, binding, removal_log


def test_child_cleanup_validates_project_ownership_before_mail_side_effects(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
) -> None:
    with _ReservationServer() as server:
        result, binding, _removal_log = _cleanup_child_fixture(
            tmp_path, git_layout, server, token_project=git_layout.other
        )

    assert result.returncode != 0
    assert "mismatch" in result.stderr.lower()
    assert server.requests == []
    assert binding.exists()
    assert (tmp_path / "cleanup-runtime" / "agent_token_CleanupCurie").exists()


def test_child_cleanup_releases_name_binding_after_all_name_keyed_artifacts(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
) -> None:
    with _ReservationServer() as server:
        result, binding, removal_log = _cleanup_child_fixture(
            tmp_path,
            git_layout,
            server,
            token_project=git_layout.main,
            fake_rm=True,
        )

    assert result.returncode == 0, result.stderr
    removals = removal_log.read_text(encoding="utf-8").splitlines()
    assert removals
    assert all(line.startswith("present|") for line in removals)
    assert not binding.exists()
    assert not (tmp_path / "cleanup-runtime" / "agent_token_CleanupCurie").exists()
    assert not (
        tmp_path / "cleanup-runtime" / "child-agents" / "CleanupCurie.codex-home"
    ).exists()


def test_child_cleanup_accepts_legacy_alias_metadata_in_canonical_context(
    tmp_path: pathlib.Path,
) -> None:
    physical = tmp_path / "cleanup-alias-physical"
    alias = tmp_path / "cleanup-alias"
    physical.mkdir()
    alias.symlink_to(physical, target_is_directory=True)
    runtime = tmp_path / "cleanup-alias-runtime"
    state_dir = runtime / "child-agents"
    binding_dir = runtime / "name-bindings"
    state_dir.mkdir(parents=True)
    binding_dir.mkdir()
    name = "AliasCleanupCurie"
    token = runtime / f"agent_token_{name}"
    token_project = runtime / f"agent_token_{name}.project"
    state = state_dir / f"{name}.json"
    binding = binding_dir / "aliascleanupcurie.json"
    token.write_text("alias-cleanup-token", encoding="utf-8")
    token_project.write_text(f"{alias}\n", encoding="utf-8")
    state.write_text(
        json.dumps(
            {
                "agent_name": name,
                "project_key": str(alias),
                "registration_token": "alias-cleanup-token",
            }
        ),
        encoding="utf-8",
    )
    binding.write_text(
        json.dumps({"agent_name": name, "project_key": str(alias)}),
        encoding="utf-8",
    )
    env = _isolated_env(tmp_path)
    env.update(
        {
            "AGENT_NAME": name,
            "AGENTSTACK_PROJECT_KEY": str(physical.resolve()),
            "PROJECT_KEY": str(physical.resolve()),
            "AGENTSTACK_PROJECT_CONTEXT": "1",
            "AGENTSTACK_PROJECT_WORK_DIR": str(physical.resolve()),
            "AGENTSTACK_PROTECTED_ROOTS": str(physical.resolve()),
            "AGENTSTACK_RUNTIME_DIR": str(runtime),
            "AGENTSTACK_HOOKS_DIR": str(ROOT / "hooks"),
            "AGENTSTACK_MAIL_HTTP_BEARER_MODE": "disabled",
        }
    )

    with _ReservationServer() as server:
        env["AGENTSTACK_MCP_URL"] = server.url
        result = subprocess.run(
            ["/bin/bash", str(CLEANUP_CHILD), name],
            cwd=physical,
            env=env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not token.exists()
    assert not token_project.exists()
    assert not state.exists()
    assert not binding.exists()
    assert server.requests


def _session_start_env(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
) -> tuple[dict[str, str], pathlib.Path]:
    env = _isolated_env(tmp_path)
    fake_bin = tmp_path / "session-bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "curl",
        "#!/bin/bash\n"
        "printf '%s\\n' '{\"jsonrpc\":\"2.0\",\"id\":\"session-start\",\"result\":{\"structuredContent\":{\"status\":\"ok\"}}}'\n"
        "printf '%s\\n' '__HTTP_STATUS__200'\n",
    )
    _write_executable(
        fake_bin / "tmux",
        "#!/bin/bash\n"
        "if [[ \"${1:-}\" == display-message ]]; then printf '%s\\n' SharedAgent; fi\n",
    )
    capture = tmp_path / "session-registration.json"
    register_lib = tmp_path / "session-register.sh"
    register_lib.write_text(
        """#!/bin/bash
ags_mail_load_token() { :; }
ags_load_registration_token() { printf '%s\n' isolated-owner-token; }
ags_register_session() {
  python3 - "$1" "$5" "$6" <<'PY'
import json
import os
import pathlib
import sys
pathlib.Path(os.environ["ISSUE16_SESSION_CAPTURE"]).write_text(json.dumps({
    "project_key": sys.argv[1], "work_dir": sys.argv[2], "name": sys.argv[3]
}))
PY
  AGS_REGISTERED_AGENT_NAME="$6"
  AGS_REGISTERED_AGENT_ID=16
  export AGS_REGISTERED_AGENT_NAME AGS_REGISTERED_AGENT_ID
  return 0
}
""",
        encoding="utf-8",
    )
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "AGENT_NAME": "SharedAgent",
            "AGENTSTACK_PROJECT_KEY": str(git_layout.other),
            "PROJECT_KEY": str(git_layout.other),
            "AGENTSTACK_PROTECTED_ROOTS": str(git_layout.other),
            "AGENTSTACK_MCP_URL": "http://127.0.0.1:1/mcp",
            "AGENTSTACK_HOOKS_DIR": str(ROOT / "hooks"),
            "AGENTSTACK_RUNTIME_DIR": str(tmp_path / "runtime"),
            "AGENTSTACK_REGISTER_LIB": str(register_lib),
            "CHILD_REGISTRATION_TOKEN": "isolated-owner-token",
            "ISSUE16_SESSION_CAPTURE": str(capture),
        }
    )
    return env, capture


def test_session_start_uses_payload_cwd_for_registration_and_work_dir(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
) -> None:
    env, capture = _session_start_env(tmp_path, git_layout)
    payload = json.dumps(
        {
            "session_id": "issue16-session",
            "cwd": str(git_layout.linked_subdir),
            "transcript_path": str(tmp_path / "transcript.jsonl"),
        }
    )

    result = subprocess.run(
        ["/bin/bash", str(SESSION_START)],
        cwd=git_layout.other,
        env=env,
        input=payload,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "already registered" in result.stdout
    registration = json.loads(capture.read_text(encoding="utf-8"))
    assert registration == {
        "project_key": str(git_layout.main),
        "work_dir": str(git_layout.linked_subdir),
        "name": "SharedAgent",
    }


def test_session_start_preserves_project_key_only_non_git_context(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
) -> None:
    env, capture = _session_start_env(tmp_path, git_layout)
    workspace = tmp_path / "session-start-non-git"
    workspace.mkdir()
    _write_installed_env(pathlib.Path(env["HOME"]), pathlib.Path("/installed/project-a"))
    env.pop("AGENTSTACK_PROJECT_KEY", None)
    env["PROJECT_KEY"] = "/live/project-b"
    payload = json.dumps(
        {"session_id": "issue16-session", "cwd": str(workspace)}
    )

    result = subprocess.run(
        ["/bin/bash", str(SESSION_START)],
        cwd=workspace,
        env=env,
        input=payload,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(capture.read_text(encoding="utf-8"))["project_key"] == (
        "/live/project-b"
    )


def test_session_start_accepts_legacy_alias_session_index_for_canonical_project(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
) -> None:
    env, capture = _session_start_env(tmp_path, git_layout)
    physical = tmp_path / "session-index-physical"
    alias = tmp_path / "session-index-alias"
    physical.mkdir()
    alias.symlink_to(physical, target_is_directory=True)
    env.pop("AGENT_NAME", None)
    env.update(
        {
            "AGENTSTACK_PROJECT_KEY": str(physical.resolve()),
            "PROJECT_KEY": str(physical.resolve()),
            "AGENTSTACK_PROTECTED_ROOTS": str(physical.resolve()),
        }
    )
    index = pathlib.Path(env["AGENTSTACK_RUNTIME_DIR"]) / "session_index"
    index.mkdir(parents=True)
    (index / "16.json").write_text(
        json.dumps(
            {
                "agent_id": 16,
                "agent_name": "AliasIndexedCurie",
                "session_id": "alias-session",
                "project_key": str(alias),
                "registered_by": "",
                "schema_version": 2,
                "binding_kind": "self",
                "ts": "2026-09-12T00:00:00",
            }
        ),
        encoding="utf-8",
    )
    payload = json.dumps(
        {"session_id": "alias-session", "cwd": str(physical.resolve())}
    )

    result = subprocess.run(
        ["/bin/bash", str(SESSION_START)],
        cwd=physical,
        env=env,
        input=payload,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "already registered" in result.stdout
    registration = json.loads(capture.read_text(encoding="utf-8"))
    assert registration == {
        "project_key": str(physical.resolve()),
        "work_dir": str(physical.resolve()),
        "name": "AliasIndexedCurie",
    }


def test_session_start_stops_before_foreign_identity_when_project_is_unresolved(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
) -> None:
    env, capture = _session_start_env(tmp_path, git_layout)
    workspace = tmp_path / "session-start-unresolved"
    workspace.mkdir()
    _write_installed_env(pathlib.Path(env["HOME"]), pathlib.Path("/foreign/project"))
    env.pop("AGENT_NAME", None)
    env.pop("AGENTSTACK_PROJECT_KEY", None)
    env.pop("PROJECT_KEY", None)
    env["AGENTSTACK_PYTHON"] = str(tmp_path / "missing-configured-python")

    runtime = pathlib.Path(env["AGENTSTACK_RUNTIME_DIR"])
    index = runtime / "session_index"
    index.mkdir(parents=True)
    index_record = index / "99.json"
    index_record.write_text(
        json.dumps(
            {
                "agent_id": 99,
                "agent_name": "ForeignIndexedCurie",
                "session_id": "issue16-foreign-index",
                "project_key": "/foreign/project",
                "registered_by": "",
                "schema_version": 2,
                "binding_kind": "self",
                "ts": "2026-09-12T00:00:00",
            }
        ),
        encoding="utf-8",
    )
    before = {
        path.relative_to(runtime): path.read_bytes()
        for path in runtime.rglob("*")
        if path.is_file()
    }
    curl_called = tmp_path / "session-start-curl-called"
    fake_bin = pathlib.Path(env["PATH"].split(":", 1)[0])
    _write_executable(
        fake_bin / "curl",
        "#!/bin/bash\nprintf '%s\\n' called > \"$ISSUE16_CURL_CALLED\"\nexit 97\n",
    )
    env["ISSUE16_CURL_CALLED"] = str(curl_called)
    payload = json.dumps(
        {"session_id": "issue16-foreign-index", "cwd": str(workspace)}
    )

    result = subprocess.run(
        ["/bin/bash", str(SESSION_START)],
        cwd=workspace,
        env=env,
        input=payload,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0
    assert "PROJECT CONTEXT UNRESOLVED" in result.stdout
    assert "PROJECT CONTEXT UNRESOLVED" in result.stderr
    assert "ForeignIndexedCurie" not in result.stdout + result.stderr
    assert "ensure_project" not in result.stdout + result.stderr
    assert not capture.exists()
    assert not curl_called.exists()
    assert not (runtime / "session-start-resolve.log").exists()
    after = {
        path.relative_to(runtime): path.read_bytes()
        for path in runtime.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_session_start_rejects_an_established_cross_repository_binding(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
) -> None:
    env, capture = _session_start_env(tmp_path, git_layout)
    env.update(
        {
            "AGENTSTACK_PROJECT_KEY": "operator-selected-namespace",
            "PROJECT_KEY": "operator-selected-namespace",
            "AGENTSTACK_PROJECT_CONTEXT": "1",
            "AGENTSTACK_PROJECT_REPOSITORY": str(git_layout.other),
            "AGENTSTACK_PROJECT_WORK_DIR": str(git_layout.other),
        }
    )
    payload = json.dumps(
        {"session_id": "issue16-session", "cwd": str(git_layout.linked)}
    )

    result = subprocess.run(
        ["/bin/bash", str(SESSION_START)],
        cwd=git_layout.other,
        env=env,
        input=payload,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0
    assert "PROJECT CONTEXT MISMATCH" in result.stderr
    assert not capture.exists()


def _run_doctor_project_fixture(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
    *,
    state_project: pathlib.Path,
    state_work_dir: pathlib.Path,
    current_work_dir: pathlib.Path,
    protected_roots: str,
    extra_record: tuple[str, pathlib.Path, pathlib.Path] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = _isolated_env(tmp_path)
    runtime = tmp_path / "doctor-runtime"
    child_dir = runtime / "child-agents"
    child_dir.mkdir(parents=True, exist_ok=True)
    (child_dir / "CrossProjectCurie.json").write_text(
        json.dumps(
            {
                "agent_name": "CrossProjectCurie",
                "project_key": str(state_project),
                "work_dir": str(state_work_dir),
                "registration_token": "doctor-owner-token-canary",
            }
        ),
        encoding="utf-8",
    )
    if extra_record is not None:
        name, project_key, work_dir = extra_record
        (child_dir / f"{name}.json").write_text(
            json.dumps(
                {
                    "agent_name": name,
                    "project_key": str(project_key),
                    "work_dir": str(work_dir),
                    "registration_token": "second-doctor-token-canary",
                }
            ),
            encoding="utf-8",
        )
    fake_bin = tmp_path / "doctor-bin"
    fake_bin.mkdir(exist_ok=True)
    for command in ("curl", "launchctl", "lsof", "tmux"):
        _write_executable(fake_bin / command, "#!/bin/bash\nexit 1\n")
    install = tmp_path / "doctor-install"
    (install / "hooks").mkdir(parents=True, exist_ok=True)
    shutil.copy2(PROJECT_CONTEXT, install / "hooks" / PROJECT_CONTEXT.name)
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "AGENT_NAME": "CrossProjectCurie",
            "AGENTSTACK_RUNTIME_DIR": str(runtime),
            "AGENTSTACK_HOOKS_DIR": str(install / "hooks"),
            "AGENTSTACK_MCP_URL": "http://127.0.0.1:1/mcp",
            "AGENTSTACK_PROJECT_KEY": str(git_layout.main),
            "PROJECT_KEY": str(git_layout.main),
            "AGENTSTACK_PROJECT_CONTEXT": "1",
            "AGENTSTACK_PROJECT_REPOSITORY": str(git_layout.main),
            "AGENTSTACK_PROJECT_WORK_DIR": str(current_work_dir),
            "AGENTSTACK_PROTECTED_ROOTS": protected_roots,
        }
    )
    return subprocess.run(
        ["/bin/bash", str(DOCTOR), "--install-dir", str(install)],
        cwd=current_work_dir,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def test_doctor_reports_agent_workdir_and_protected_root_project_mismatches(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
) -> None:
    result = _run_doctor_project_fixture(
        tmp_path,
        git_layout,
        state_project=git_layout.main,
        state_work_dir=git_layout.other,
        current_work_dir=git_layout.main,
        protected_roots=str(git_layout.other),
    )

    output = result.stdout + result.stderr
    agent_warnings = [
        line for line in output.splitlines()
        if "warn:" in line.lower() and "CrossProjectCurie" in line
    ]
    assert agent_warnings, output
    assert any(
        "project" in line.lower() or "work" in line.lower()
        for line in agent_warnings
    )
    assert any(
        "warn:" in line.lower() and "protected" in line.lower()
        for line in output.splitlines()
    )
    assert "doctor-owner-token-canary" not in output


def test_doctor_accepts_same_repository_linked_worktree_context(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
) -> None:
    result = _run_doctor_project_fixture(
        tmp_path,
        git_layout,
        state_project=git_layout.main,
        state_work_dir=git_layout.linked,
        current_work_dir=git_layout.linked_subdir,
        protected_roots=f"{git_layout.linked}:{git_layout.main}",
    )

    output = result.stdout + result.stderr
    assert not any(
        "warn:" in line.lower() and "CrossProjectCurie" in line
        for line in output.splitlines()
    ), output
    assert "doctor-owner-token-canary" not in output


def test_doctor_accepts_healthy_records_from_two_projects_and_main_subdir(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
) -> None:
    result = _run_doctor_project_fixture(
        tmp_path,
        git_layout,
        state_project=git_layout.main,
        state_work_dir=git_layout.main_subdir,
        current_work_dir=git_layout.main_subdir,
        protected_roots=str(git_layout.main),
        extra_record=("OtherProjectCurie", git_layout.other, git_layout.other),
    )

    output = result.stdout + result.stderr
    assert not any(
        "warn:" in line.lower()
        and ("CrossProjectCurie" in line or "OtherProjectCurie" in line)
        for line in output.splitlines()
    ), output
    assert "doctor-owner-token-canary" not in output
    assert "second-doctor-token-canary" not in output


def test_doctor_reports_ambient_project_mismatch_with_current_repository(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
) -> None:
    env = _isolated_env(tmp_path)
    runtime = tmp_path / "doctor-ambient-runtime"
    (runtime / "child-agents").mkdir(parents=True)
    fake_bin = tmp_path / "doctor-ambient-bin"
    fake_bin.mkdir()
    for command in ("curl", "launchctl", "lsof", "tmux"):
        _write_executable(fake_bin / command, "#!/bin/bash\nexit 1\n")
    install = tmp_path / "doctor-ambient-install"
    (install / "hooks").mkdir(parents=True)
    shutil.copy2(PROJECT_CONTEXT, install / "hooks" / PROJECT_CONTEXT.name)
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "AGENTSTACK_RUNTIME_DIR": str(runtime),
            "AGENTSTACK_HOOKS_DIR": str(install / "hooks"),
            "AGENTSTACK_MCP_URL": "http://127.0.0.1:1/mcp",
            "AGENTSTACK_PROJECT_KEY": str(git_layout.main),
            "PROJECT_KEY": str(git_layout.main),
        }
    )

    result = subprocess.run(
        ["/bin/bash", str(DOCTOR), "--install-dir", str(install)],
        cwd=git_layout.other,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    output = result.stdout + result.stderr
    warnings = [line.lower() for line in output.splitlines() if "warn:" in line.lower()]
    assert any(
        ("current" in line or "cwd" in line)
        and ("project" in line or "repository" in line)
        for line in warnings
    ), output


def _run_doctor_context_boundary(
    tmp_path: pathlib.Path,
    *,
    case: str,
    cwd: pathlib.Path,
    project_key: str,
    repository: str,
    work_dir: pathlib.Path,
    established: bool,
) -> subprocess.CompletedProcess[str]:
    case_root = tmp_path / case
    case_root.mkdir()
    env = _isolated_env(case_root)
    runtime = case_root / "runtime"
    (runtime / "child-agents").mkdir(parents=True)
    fake_bin = case_root / "bin"
    fake_bin.mkdir()
    for command in ("curl", "launchctl", "lsof", "tmux"):
        _write_executable(fake_bin / command, "#!/bin/bash\nexit 1\n")
    install = case_root / "install"
    (install / "hooks").mkdir(parents=True)
    shutil.copy2(PROJECT_CONTEXT, install / "hooks" / PROJECT_CONTEXT.name)
    (install / "env.sh").write_text(
        f"export AGENTSTACK_PROJECT_KEY={json.dumps(project_key)}\n"
        f"export PROJECT_KEY={json.dumps(project_key)}\n"
        f"export AGENTSTACK_PROJECT_REPOSITORY={json.dumps(repository)}\n"
        f"export AGENTSTACK_PROJECT_WORK_DIR={json.dumps(str(work_dir))}\n"
        f"export AGENTSTACK_PROTECTED_ROOTS={json.dumps(str(work_dir))}\n",
        encoding="utf-8",
    )
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "AGENTSTACK_RUNTIME_DIR": str(runtime),
            "AGENTSTACK_HOOKS_DIR": str(install / "hooks"),
            "AGENTSTACK_MCP_URL": "http://127.0.0.1:1/mcp",
        }
    )
    if established:
        env.update(
            {
                "AGENTSTACK_PROJECT_KEY": project_key,
                "PROJECT_KEY": project_key,
                "AGENTSTACK_PROJECT_CONTEXT": "1",
                "AGENTSTACK_PROJECT_REPOSITORY": repository,
                "AGENTSTACK_PROJECT_WORK_DIR": str(work_dir),
                "AGENTSTACK_PROTECTED_ROOTS": str(work_dir),
            }
        )
    return subprocess.run(
        ["/bin/bash", str(DOCTOR), "--install-dir", str(install)],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def test_doctor_established_non_git_cwd_must_remain_inside_bound_workspace(
    tmp_path: pathlib.Path,
    git_layout: GitLayout,
) -> None:
    outside = tmp_path / "doctor-established-outside"
    outside.mkdir()
    negative = _run_doctor_context_boundary(
        tmp_path,
        case="negative",
        cwd=outside,
        project_key=str(git_layout.main),
        repository=str(git_layout.main),
        work_dir=git_layout.main,
        established=True,
    )
    diagnostic = (
        "established project context work_dir does not contain current non-Git cwd"
    )
    assert diagnostic in (negative.stdout + negative.stderr)

    workspace = tmp_path / "doctor-established-workspace"
    descendant = workspace / "nested"
    descendant.mkdir(parents=True)
    healthy_bound = _run_doctor_context_boundary(
        tmp_path,
        case="healthy-bound",
        cwd=descendant,
        project_key=str(workspace),
        repository="",
        work_dir=workspace,
        established=True,
    )
    assert diagnostic not in healthy_bound.stdout + healthy_bound.stderr

    healthy_installed = _run_doctor_context_boundary(
        tmp_path,
        case="healthy-installed",
        cwd=outside,
        project_key=str(git_layout.main),
        repository=str(git_layout.main),
        work_dir=git_layout.main,
        established=False,
    )
    assert diagnostic not in healthy_installed.stdout + healthy_installed.stderr
