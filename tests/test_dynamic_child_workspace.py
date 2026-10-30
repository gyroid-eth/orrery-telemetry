"""Covered child launcher and rendered runner paths derive protection from their target.

Dashboard resume, persistent launch, and warm-pool reclaims are out of scope.
Only temporary repositories, homes and mocked Mail/tmux are used here.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest



ROOT = Path(__file__).resolve().parents[1]
CONTEXT = ROOT / "hooks" / "project-context.sh"
FIELDS = ("PROJECT_KEY", "AGENTSTACK_PROJECT_KEY", "AGENTSTACK_PROJECT_REPOSITORY",
          "AGENTSTACK_PROJECT_WORK_DIR", "AGENTSTACK_PROJECT_WORKTREE_ROOT",
          "AGENTSTACK_PROTECTED_ROOTS", "AGENTSTACK_EXTRA_PROTECTED_ROOTS",
          "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")
DUMP = shlex.join([sys.executable, "-c", "import json,os; print(json.dumps({k:os.environ.get(k) for k in " + repr(FIELDS) + "}))"])


def git(*args):
    subprocess.run(["git", *map(str, args)], check=True, capture_output=True, text=True)


def repository(path):
    path.mkdir()
    git("init", path)
    git("-C", path, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "--allow-empty", "-m", "fixture")
    return path


def environment(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return {"PATH": os.environ["PATH"], "HOME": str(home), "AGENTSTACK_HOME": str(home / ".agentstack"),
            "AGENTSTACK_LABEL_PREFIX": "org.agentstack.test.dynamic-child",
            "AGENTSTACK_PROTECTED_ROOTS": "/old/runtime/root", "GIT_DIR": "/old/.git",
            "GIT_WORK_TREE": "/old", "GIT_COMMON_DIR": "/old/.git"}




def test_child_target_replaces_parent_and_recomputes_worktree_before_forwarding(tmp_path):
    source = repository(tmp_path / "source")
    linked = tmp_path / "child-worktree"
    git("-C", source, "worktree", "add", "-b", "child", linked)
    vault = linked / "vault"
    vault.mkdir()
    text = (ROOT / "hooks" / "spawn_child.sh").read_text()
    functions = ""
    for name in ("prepare_child_workspace_context", "append_child_workspace_environment"):
        start = text.index(name + "() {")
        functions += text[start:text.index("\n}\n", start) + 3]
    command = (f"source {shlex.quote(str(CONTEXT))}; " + functions +
               f"PROJECT_KEY=shared-mail; WORK_DIR={shlex.quote(str(source))}; "
               "prepare_child_workspace_context || exit $?; "
               f"WORK_DIR={shlex.quote(str(vault))}; "
               "prepare_child_workspace_context || exit $?; "
               "TMUX_ENV_ARGS=(); append_child_workspace_environment; " + DUMP +
               "; printf '%s\\n' \"${TMUX_ENV_ARGS[@]}\"")
    env = environment(tmp_path)
    env["AGENTSTACK_EXTRA_PROTECTED_ROOTS"] = str(vault)
    result = subprocess.run(["bash", "-c", command], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    context = json.loads(lines[0])
    assert context["PROJECT_KEY"] == "shared-mail"
    assert context["AGENTSTACK_PROJECT_REPOSITORY"] == str(source)
    assert context["AGENTSTACK_PROJECT_WORK_DIR"] == str(vault)
    assert context["AGENTSTACK_PROJECT_WORKTREE_ROOT"] == str(linked)
    assert context["AGENTSTACK_PROTECTED_ROOTS"] == str(vault)
    assert all(context[key] is None for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"))
    assert f"AGENTSTACK_PROTECTED_ROOTS={vault}" in lines
    assert f"AGENTSTACK_EXTRA_PROTECTED_ROOTS={vault}" in lines
    assert "/old/runtime/root" not in result.stdout


@pytest.mark.parametrize("codex", [False, True], ids=["claude", "codex"])
@pytest.mark.parametrize("source", ["workspace", "extra"])
@pytest.mark.parametrize("marker,name", [("[", "folder[ab]"), ("*", "folder*"), ("?", "folder?"), ("\\", "folder\\child")])
def test_child_unsupported_roots_stop_before_runtime_writes_or_tmux(tmp_path, codex, source, marker, name):
    from test_claude_child_chrome import _launch_env, _spawn

    env, target = _launch_env(tmp_path, codex=codex)
    root = tmp_path / name
    root.mkdir()
    if source == "workspace":
        target = root
    else:
        env["AGENTSTACK_EXTRA_PROTECTED_ROOTS"] = str(root)
    result = _spawn(tmp_path, env, target, "WorkspaceChild", codex=codex)
    assert result.returncode != 0
    assert "characters unsupported by ORRERY Mail " + repr(marker) in result.stderr
    assert str(root) in result.stderr
    assert not Path(env["AGENTSTACK_RUNTIME_DIR"]).exists()
    assert not Path(env["FAKE_TMUX_LOG"]).exists()


@pytest.mark.parametrize("codex", [False, True], ids=["claude", "codex"])
def test_child_provider_receives_pinned_target_after_login_defaults(tmp_path, codex):
    from test_claude_child_chrome import _launch_env, _spawn, _new_session, _codex_inner

    env, _ = _launch_env(tmp_path, codex=codex)
    env["AGENTSTACK_PYTHON"] = sys.executable
    # A supported hooks override may customize helpers without duplicating
    # launch policy. The resolver must come from spawn_child.sh's own version.
    custom_hooks = tmp_path / "custom-hooks"
    custom_hooks.mkdir()
    for entry in (ROOT / "hooks").iterdir():
        if entry.name != "project-context.sh":
            (custom_hooks / entry.name).symlink_to(entry)
    env["AGENTSTACK_HOOKS_DIR"] = str(custom_hooks)
    target = repository(tmp_path / "approved")
    nested = target / "nested"
    nested.mkdir()
    vault = target / "shared vault"
    vault.mkdir()
    env["AGENTSTACK_EXTRA_PROTECTED_ROOTS"] = str(vault)
    env["AGENTSTACK_PROTECTED_ROOTS"] = "/previous/agent/root"
    result = _spawn(tmp_path, env, nested, "WorkspaceChild", codex=codex)
    assert result.returncode == 0, result.stderr
    session = _new_session(env)
    tmux_env = dict(session[i + 1].split("=", 1) for i, arg in enumerate(session[:-1]) if arg == "-e")
    assert tmux_env["AGENTSTACK_LAUNCH_CONTEXT_HELPER"] == str(CONTEXT)
    inner = _codex_inner(env) if codex else shlex.split(session[-1])[-1]
    dump = tmp_path / "provider-env.json"
    cli = Path(env["HOME"]) / ".local" / "bin" / ("codex" if codex else "claude")
    cli.parent.mkdir(parents=True, exist_ok=True)
    cli.write_text("#!/bin/bash\n" + shlex.join([sys.executable, "-c",
        "import json,os,pathlib; pathlib.Path(" + repr(str(dump)) + ").write_text(json.dumps({'cwd':os.getcwd(),'env':dict(os.environ)}))"]) + "\n")
    cli.chmod(0o700)
    hooks = tmp_path / "noop-hooks"
    hooks.mkdir()
    (hooks / "cleanup-child-agent.sh").write_text("exit 0\n")
    # Simulate a login profile changing cwd and reloading another install's
    # namespace/protection. The child-scoped snapshots remain authoritative.
    run_env = {**env, **tmux_env, "AGENTSTACK_CODEX_BIN": str(cli), "CLAUDE_CHILD_BIN": str(cli), "AGENTSTACK_HOOKS_DIR": str(hooks),
               "PROJECT_KEY": "stale-login", "AGENTSTACK_PROJECT_KEY": "stale-login",
               "AGENTSTACK_EXTRA_PROTECTED_ROOTS": "/stale/login/extra",
               "AGENTSTACK_PROTECTED_ROOTS": "/stale/login/root", "GIT_DIR": "/stale/.git"}
    result = subprocess.run(["bash", "-c", inner], env=run_env, cwd=tmp_path,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    seen = json.loads(dump.read_text())
    assert seen["cwd"] == str(nested)
    assert seen["env"]["PROJECT_KEY"] == "/shared/project"
    assert seen["env"]["AGENTSTACK_PROTECTED_ROOTS"] == f"{vault}:{nested}"
    assert seen["env"]["AGENTSTACK_EXTRA_PROTECTED_ROOTS"] == str(vault)
    assert seen["env"]["AGENTSTACK_PROTECTION_CONTEXT"] == "workspace-v1"
    assert "GIT_DIR" not in seen["env"]
    assert "AGENTSTACK_LAUNCH_WORK_DIR" not in seen["env"]


@pytest.mark.parametrize("launcher", ["spawn_gemini_child.sh", "spawn_gemini_preregistered.sh"])
def test_gemini_runner_recomputes_its_isolated_worktree_context(tmp_path, launcher):
    source = repository(tmp_path / "source")
    linked = tmp_path / "child worktree"
    git("-C", source, "worktree", "add", "-b", "child", linked)
    vault = tmp_path / "extra root"
    vault.mkdir()
    script = (ROOT / "hooks" / launcher).read_text()
    # Exercise the real generated runner prefix; Mail/provider processes are
    # outside this test. Its target and explicit extras are pinned at render.
    prefix = script.split('cat > "$RUNNER_FILE" <<EOF\n', 1)[1].split('git_excludes_file=', 1)[0]
    if launcher == "spawn_gemini_child.sh":
        prefix = prefix.split('RESOURCES=', 1)[0]
    runner = tmp_path / "runner.sh"
    render_env = environment(tmp_path)
    render_env.update({
        "WORKTREE_DIR": str(linked), "CHILD_NAME": "WorkspaceGemini", "PARENT_NAME": "Parent",
        "PARENT_AGENT": "Parent", "PROJECT_KEY": "shared-namespace", "AGENTSTACK_HOME_DIR": str(tmp_path),
        "HOOKS_DIR": str(ROOT / "hooks"), "PROJECT_CONTEXT_HELPER": str(CONTEXT),
        "RUNTIME_DIR": str(tmp_path / "runtime"), "MCP_URL": "http://127.0.0.1:9/mcp",
        "MAIL_ENV": str(tmp_path / "mail.env"), "HTTP_BEARER_MODE": "disabled",
        "PYTHON_BIN": sys.executable, "AGENTSTACK_EXTRA_PROTECTED_ROOTS": str(vault),
    })
    rendered = subprocess.run(["bash", "-c", "cat <<EOF\n" + prefix + "EOF\n"],
                              env=render_env, capture_output=True, text=True)
    assert rendered.returncode == 0, rendered.stderr
    runner.write_text(rendered.stdout + DUMP + "\n")
    stale = {**render_env, "AGENTSTACK_PROJECT_KEY": "wrong-tmux-namespace",
             "AGENTSTACK_EXTRA_PROTECTED_ROOTS": "/old/tmux/extra", "AGENTSTACK_PROTECTED_ROOTS": "/old/root"}
    launched = subprocess.run(["bash", str(runner)], env=stale, cwd=source,
                              capture_output=True, text=True)
    assert launched.returncode == 0, launched.stderr
    context = json.loads(launched.stdout)
    assert context["AGENTSTACK_PROJECT_KEY"] == "shared-namespace"
    assert context["AGENTSTACK_PROJECT_REPOSITORY"] == str(source)
    assert context["AGENTSTACK_PROJECT_WORK_DIR"] == str(linked)
    assert context["AGENTSTACK_PROJECT_WORKTREE_ROOT"] == str(linked)
    assert context["AGENTSTACK_PROTECTED_ROOTS"] == f"{vault}:{linked}"
    assert context["AGENTSTACK_EXTRA_PROTECTED_ROOTS"] == str(vault)
    assert all(context[key] is None for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"))
