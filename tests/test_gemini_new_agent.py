"""Regression coverage for Dashboard NEW AGENT Antigravity integration."""
from __future__ import annotations

import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import threading
import time
import types

import pytest

import dashboard.provider_server as server
import dashboard.server as canonical_server
import dashboard.service_runner as service_runner


ROOT = pathlib.Path(__file__).resolve().parent.parent
ADAPTER = ROOT / "hooks" / "spawn_gemini_preregistered.sh"
gemini_runtime = sys.modules["_orrery_provider_dashboard_gemini"]
_REAL_RUN = subprocess.run
_REAL_POPEN = subprocess.Popen


def _git(*args: str, cwd: pathlib.Path) -> str:
    result = _REAL_RUN(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", *args],
        cwd=cwd, text=True, capture_output=True, check=True,
    )
    return result.stdout.strip()


def _executable(path: pathlib.Path, body: str) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _make_repo(tmp_path: pathlib.Path, links: dict[str, str] | None = None,
               root_escape: bool = True) -> pathlib.Path:
    """A committed repository with one escaping and one internal symlink.

    ``links`` adds more symlinks (relative path -> target); ``{outside}`` in a
    target is the absolute outside directory, so it survives a worktree checkout.
    ``root_escape=False`` omits the top-level escape, which would otherwise
    already match a leading wildcard.
    """
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside\n", encoding="utf-8")
    (outside / "x").write_text("outside\n", encoding="utf-8")
    for relative in ("src/app.py", "tests/test_app.py", "docs/guide.md"):
        (repo / relative).parent.mkdir(parents=True, exist_ok=True)
        (repo / relative).write_text("x\n", encoding="utf-8")
    _git("init", "-q", str(repo), cwd=tmp_path)
    if root_escape:
        (repo / "escape").symlink_to(outside, target_is_directory=True)
    (repo / "src" / "shared").symlink_to("../docs", target_is_directory=True)
    for relative, target in (links or {}).items():
        (repo / relative).parent.mkdir(parents=True, exist_ok=True)
        (repo / relative).symlink_to(target.format(outside=outside))
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "fixture", cwd=repo)
    return repo


class _Launches:
    """Fake launcher processes keyed by script, with real git subprocesses."""

    def __init__(self) -> None:
        self.launched: list[tuple[list[str], dict]] = []
        self.registrations: dict[str, dict] = {}
        self.mcp_calls: list[tuple[str, dict]] = []
        self.blockers: dict[str, threading.Event] = {}
        self.started: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def block(self, script: str) -> threading.Event:
        release = threading.Event()
        self.blockers[script] = release
        self.started[script] = threading.Event()
        return release

    def popen(self, args, **kwargs):
        args = list(args)
        if os.path.basename(str(args[0])) == "git":
            return _REAL_POPEN(args, **kwargs)
        with self._lock:
            self.launched.append((args, kwargs))
        release = self.blockers.get(args[0])
        if release is not None:
            self.started[args[0]].set()

        class Proc:
            pid = 424242

            def wait(self, timeout=None):
                if release is not None:
                    assert release.wait(timeout=10), "launcher was never released"
                return 0

        return Proc()

    def run(self, args, *a, **k):
        if args and args[0] == "tmux":
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return _REAL_RUN(args, *a, **k)

    def mcp(self, method, args, timeout=15):
        with self._lock:
            self.mcp_calls.append((method, dict(args)))
            if method == "register_agent":
                self.registrations[args["name"]] = dict(args)
        data = {"name": args["name"], "registration_token": "server-child-token"} \
            if method == "register_agent" else {}
        return {"ok": True, "data": data}

    def for_script(self, script: str) -> list[tuple[list[str], dict]]:
        return [item for item in self.launched if item[0][0] == str(script)]


@pytest.fixture
def gemini_env(monkeypatch, tmp_path):
    repo = _make_repo(tmp_path)
    hooks = tmp_path / "agentstack" / "hooks"
    adapter = _executable(hooks / "spawn_gemini_preregistered.sh", "#!/bin/bash\n")
    home = tmp_path / "agentstack"
    for name in ("agentstack-gemini-child-mail", "agentstack-gemini-stream",
                 "agentstack-gemini-mcp"):
        _executable(home / "bin" / name, "#!/bin/sh\n")
    fake_bin = tmp_path / "fake-bin"
    _executable(fake_bin / "agy", "#!/bin/sh\n")
    _executable(fake_bin / "tmux", "#!/bin/sh\n")
    claude_launcher = _executable(tmp_path / "spawn_child.sh", "#!/bin/bash\n")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "agent_token_Parent").write_text("parent-owner-token", encoding="utf-8")
    launches = _Launches()

    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
    monkeypatch.setenv("AGENTSTACK_HOME", str(home))
    monkeypatch.setenv("AGENTSTACK_PYTHON", sys.executable)
    monkeypatch.delenv("AGENTSTACK_GEMINI_BIN", raising=False)
    monkeypatch.delenv("AGENTSTACK_GEMINI_MODELS", raising=False)
    monkeypatch.setattr(server, "HOOKS_DIR", str(hooks))
    monkeypatch.setattr(server, "SPAWN_SCRIPT", str(claude_launcher))
    monkeypatch.setattr(server, "RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(server, "HERE", str(tmp_path))
    monkeypatch.setattr(server, "_project_key", lambda: "/project")
    monkeypatch.setattr(server, "_spawn_name_status", lambda _: "available")
    monkeypatch.setattr(server, "_mcp_call", launches.mcp)
    monkeypatch.setattr(server.time, "sleep", lambda _: None)
    monkeypatch.setattr(server.subprocess, "Popen", launches.popen)
    monkeypatch.setattr(server.subprocess, "run", launches.run)
    return types.SimpleNamespace(
        repo=repo, adapter=adapter, home=home, runtime=runtime, tmp=tmp_path,
        claude_launcher=claude_launcher, launches=launches,
    )


def _gemini_payload(env, **extra) -> dict:
    payload = {
        "parent": "Parent",
        "name": "Sunny-Curie",
        "task": "implement the requested change",
        "dir": str(env.repo),
        "provider": "gemini",
        "model": "gemini-3.8-flash-high",
        "effort": "medium",
        "resources": "src/**, tests/**",
    }
    payload.update(extra)
    return payload


# --------------------------------------------------------------------------- #
# Catalog and the single explicit-effort policy owner
# --------------------------------------------------------------------------- #
def test_spawn_catalog_exposes_gemini_provider(monkeypatch):
    monkeypatch.delenv("AGENTSTACK_GEMINI_MODELS", raising=False)
    monkeypatch.setattr(
        server,
        "_spawn_scientist_statuses",
        lambda _adjectives, scientists: {name: "unknown" for name in scientists},
    )
    data = server.spawn_names_payload()
    gemini = next(provider for provider in data["providers"] if provider["id"] == "gemini")
    assert gemini == {
        "id": "gemini",
        "label": "Antigravity",
        "program": "antigravity",
        "models": ["gemini-3.8-flash-high", "gemini-3.8-flash-medium"],
        "default_model": "gemini-3.8-flash-high",
        "efforts": ["low", "medium", "high"],
        "effort_default": "",
        "provider_key": "google",
        "capabilities": {
            "effort": True,
            "effort_required": True,
            "mcp": True,
            "resume": False,
            "runtime": True,
            "transcript": False,
            "standalone": False,
            "worktree_required": True,
            "resources_required": True,
        },
    }


def test_spawn_catalog_honors_gemini_model_override(monkeypatch):
    monkeypatch.setenv("AGENTSTACK_GEMINI_MODELS", "gemini-test-a, gemini-test-b")
    gemini = next(
        provider for provider in server.spawn_names_payload()["providers"]
        if provider["id"] == "gemini"
    )
    assert gemini["models"] == ["gemini-test-a", "gemini-test-b"]
    assert gemini["default_model"] == "gemini-test-a"


def test_effort_policy_has_one_owner(monkeypatch, tmp_path):
    entrypoint = (ROOT / "dashboard" / "provider_server.py").read_text(encoding="utf-8")
    assert "effort_required" not in entrypoint
    assert "effort_default" not in entrypoint

    # Flipping the one declaration moves catalog, validation, and UI together.
    monkeypatch.setitem(gemini_runtime.CAPABILITIES, "effort_required", False)
    gemini = next(
        provider for provider in server.spawn_names_payload()["providers"]
        if provider["id"] == "gemini"
    )
    assert gemini["capabilities"]["effort_required"] is False
    assert gemini["effort_default"] == "high"
    result = server.do_spawn({
        "parent": "Parent", "task": "work", "dir": str(tmp_path),
        "provider": "gemini", "model": "gemini-3.8-flash-high",
    })
    assert result == {"ok": False, "error": "resources required for provider gemini"}


def test_direct_cli_launchers_keep_historical_high_effort_fallback():
    assert 'GEMINI_EFFORT="${AGENTSTACK_GEMINI_EFFORT:-high}"' in (
        ROOT / "bin" / "agent-start-gemini").read_text(encoding="utf-8")
    for path in (ROOT / "hooks" / "spawn_gemini_child.sh", ADAPTER):
        assert 'EFFORT="${AGENTSTACK_GEMINI_EFFORT:-high}"' in path.read_text(encoding="utf-8")


def test_gemini_spawn_requires_declared_resources(tmp_path):
    result = server.do_spawn({
        "parent": "Parent-Curie",
        "task": "inspect the dashboard",
        "dir": str(tmp_path),
        "provider": "gemini",
        "model": "gemini-3.8-flash-high",
        "effort": "high",
    })
    assert result == {"ok": False, "error": "resources required for provider gemini"}


def test_gemini_spawn_requires_explicit_effort(tmp_path):
    result = server.do_spawn({
        "parent": "Parent-Curie",
        "task": "inspect the dashboard",
        "dir": str(tmp_path),
        "provider": "gemini",
        "model": "gemini-3.8-flash-high",
        "resources": "src/**",
    })
    assert result == {"ok": False, "error": "effort required for provider gemini"}


def test_gemini_spawn_rejects_standalone_and_invalid_engine_options(tmp_path):
    common = {
        "parent": "Parent-Curie",
        "task": "inspect the dashboard",
        "dir": str(tmp_path),
        "provider": "gemini",
        "resources": "src/**",
    }
    # Standalone is rejected before the explicit-effort requirement.
    assert server.do_spawn({**common, "standalone": True}) == {
        "ok": False,
        "error": "standalone not supported for provider gemini",
    }
    assert server.do_spawn({**common, "standalone": "yes"}) == {
        "ok": False, "error": "standalone must be boolean",
    }
    bad_model = server.do_spawn({**common, "model": "gemini-not-allowed", "effort": "high"})
    assert bad_model["error"] == "model not allowed for provider gemini: gemini-not-allowed"
    bad_effort = server.do_spawn({**common, "effort": "xhigh"})
    assert bad_effort["error"] == "effort not allowed for provider gemini: xhigh"
    assert server.do_spawn({**common, "effort": "high", "parent": ""}) == {
        "ok": False, "error": "parent name invalid",
    }


def test_native_providers_keep_canonical_validation():
    for module in (server, canonical_server):
        assert module.do_spawn({"parent": "P", "task": "w", "provider": "claude",
                                "model": "claude-opus-5", "effort": "high"}) == {
            "ok": False, "error": "effort not supported for provider: claude",
        }
        assert module.do_spawn({"parent": "P", "task": "w", "provider": "codex",
                                "model": "gemini-3.8-flash-high"})["error"] == (
            "model not allowed for provider codex: gemini-3.8-flash-high")
    assert canonical_server.do_spawn({"parent": "P", "task": "w", "provider": "gemini"}) == {
        "ok": False, "error": "provider not allowed: gemini",
    }


# --------------------------------------------------------------------------- #
# Model collisions and platform precedence
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("override, provider", [
    ("gemini-test-a,claude-opus-5", "claude"),
    ("gpt-5.6-sol", "codex"),
])
def test_gemini_model_collision_fails_closed(monkeypatch, tmp_path, override, provider):
    monkeypatch.setenv("AGENTSTACK_GEMINI_MODELS", override)
    ids = [item["id"] for item in server.spawn_names_payload()["providers"]]
    assert "gemini" not in ids and {"claude", "codex"} <= set(ids)
    colliding = override.split(",")[-1]
    result = server.do_spawn({
        "parent": "Parent", "task": "work", "dir": str(tmp_path),
        "provider": "gemini", "model": colliding, "effort": "high", "resources": "src/**",
    })
    assert result == {
        "ok": False,
        "error": "provider gemini unavailable: model allow-list for provider gemini "
                 f"collides with provider {provider}: {colliding}",
    }


def test_platform_unavailable_takes_precedence_for_gemini(monkeypatch):
    monkeypatch.setattr(
        server, "_spawn_unavailable_error",
        lambda: {"ok": False, "error": "unavailable on this platform"},
    )
    assert server.do_spawn({"provider": "gemini", "standalone": "bad"}) == {
        "ok": False, "error": "unavailable on this platform",
    }

    base = types.SimpleNamespace(
        spawn_names_payload=lambda: {"unavailable": "unavailable on this platform", "names": []},
    )
    gemini_runtime._install_catalog(base, gemini_runtime._Integration(base))
    assert base.spawn_names_payload() == {
        "unavailable": "unavailable on this platform", "names": [],
    }


# --------------------------------------------------------------------------- #
# Resource isolation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw", [
    "/etc/passwd",
    "src/**,/tmp/**",
    "../outside/**",
    "src/../../outside",
    "~/secrets",
    "~root/.ssh",
    "src/\x00x",
    "src/**\n,tests/**",
    "src\\..\\x",
    ".git/config",
    "src/.GIT/hooks",
    "-rf",
    "./-rf",
    ".//--ttl=1",
    "src/**,./-rf",
    ".",
    "./",
])
def test_unsafe_resource_declarations_are_rejected(raw):
    with pytest.raises(ValueError):
        gemini_runtime.normalize_resources(raw)


@pytest.mark.parametrize("raw", ["./-rf", ".//--ttl=1", "src/**,./-rf"])
def test_leading_dash_is_rejected_after_normalization_by_both_validators(tmp_path, raw):
    item = raw.split(",")[-1]
    with pytest.raises(ValueError, match=re.escape(f"resource must not start with '-': {item}")):
        gemini_runtime.normalize_resources(raw)
    result = _adapter_validator(raw, _make_repo(tmp_path))
    assert (result.returncode, result.stdout) == (2, "")
    assert result.stderr == f"spawn_gemini_preregistered.sh: unsafe resource declaration: {item}\n"


def test_resource_declarations_have_one_canonical_form():
    assert gemini_runtime.normalize_resources(
        " src/** , ./tests//unit/ ,src/**,docs/*.md") == "src/**,tests/unit,docs/*.md"
    with pytest.raises(ValueError, match="resources required"):
        gemini_runtime.normalize_resources(" , ")


def test_resource_boundary_rejects_symlink_escape_and_accepts_safe_globs(tmp_path):
    repo = _make_repo(tmp_path)
    check = gemini_runtime.resource_boundary_error
    for escaping in ("escape", "escape/**", "escape/secret.txt", "e*/**"):
        assert "escapes the repository root" in check(str(repo), escaping), escaping
    for safe in ("src/**", "tests/**,docs/*.md", "src/shared/**", "src/*",
                 "not-yet-created/**"):
        assert check(str(repo), safe) == "", safe


@pytest.mark.parametrize("resource", [".g*", "[.]git", "*", "**"])
def test_resource_boundary_rejects_globs_covering_source_git_metadata(tmp_path, resource):
    repo = _make_repo(tmp_path)
    assert gemini_runtime.resource_boundary_error(str(repo), resource) == (
        f"resource must not cover git metadata: {resource} (.git)"
    )


def test_resource_boundary_rejects_globs_covering_git_metadata_symlink(tmp_path):
    repo = _make_repo(tmp_path)
    git_dir = repo / ".git"
    git_store = repo / ".git-store"
    git_dir.rename(git_store)
    git_dir.symlink_to(git_store, target_is_directory=True)
    for resource in (".g*", "[.]git", "*", "**"):
        assert gemini_runtime.resource_boundary_error(str(repo), resource) == (
            f"resource must not cover git metadata: {resource} (.git)"
        )


# A tracked ``src/meta -> ../.git`` aliases Git metadata under names that never
# spell ``.git``; each resource reaches the alias through a different check.
_GIT_ALIAS_RESOURCES = ["src/meta", "src/meta/*", "src/*/HEAD", "src/**"]


def _git_alias_root(tmp_path: pathlib.Path, kind: str) -> pathlib.Path:
    """A checkout whose ``.git`` is a directory, a gitdir file, or a symlink.

    ``file`` is a real ``git worktree add`` checkout of the source repository;
    ``symlink`` renames the source ``.git`` to a store and links it back.
    """
    repo = _make_repo(tmp_path, {"src/meta": "../.git"}, root_escape=False)
    if kind == "file":
        worktree = tmp_path / "linked"
        _git("worktree", "add", "-q", str(worktree), cwd=repo)
        assert (worktree / ".git").is_file()
        assert (worktree / "src" / "meta").is_symlink()
        return worktree
    if kind == "symlink":
        (repo / ".git").rename(repo / ".git-store")
        (repo / ".git").symlink_to(".git-store", target_is_directory=True)
    return repo


def _adapter_validator(resources: str, root: pathlib.Path) -> subprocess.CompletedProcess:
    """Run the adapter's embedded resource validator exactly as the script does."""
    match = re.search(
        r"validate_resources\(\) \{\n  \"\$PYTHON_BIN\" - \"\$RESOURCES\" \"\$\{1:-\}\" "
        r"<<'PY'\n(.*?)\nPY\n",
        ADAPTER.read_text(encoding="utf-8"), re.S,
    )
    assert match, "embedded validator not found"
    return _REAL_RUN([sys.executable, "-", resources, str(root)], input=match.group(1),
                     text=True, capture_output=True, timeout=60, check=False)


@pytest.mark.parametrize("kind", ["directory", "file", "symlink"])
@pytest.mark.parametrize("resource", _GIT_ALIAS_RESOURCES)
def test_resource_boundary_rejects_symlink_aliases_of_git_metadata(tmp_path, kind, resource):
    root = _git_alias_root(tmp_path, kind)
    error = f"resource must not cover git metadata: {resource} (src/meta)"
    assert gemini_runtime.resource_boundary_error(str(root), resource) == error
    result = _adapter_validator(resource, root)
    assert result.returncode == 2, result.stdout
    assert result.stderr == f"spawn_gemini_preregistered.sh: {error}\n"


def test_git_alias_checks_keep_escape_precedence_and_safe_symlinks(tmp_path):
    repo = _make_repo(tmp_path, {
        "src/meta": "../.git",
        "src/hub": "../.github",
        "src/ignore": "../.gitignore",
        "src/loop": "../src",
        "src/dangling": "../.git-not-here",
    }, root_escape=False)
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
    (repo / ".git" / "leak").symlink_to(tmp_path / "outside", target_is_directory=True)
    runtime = gemini_runtime.resource_boundary_error
    for resource in ("src/hub/**", "src/h*/workflows", "src/ignore", "src/loop/app.py",
                     "src/s*/**", "src/dangling/new", "not-yet-created/**",
                     "src/meta-new/**"):
        assert runtime(str(repo), resource) == "", resource
        result = _adapter_validator(resource, repo)
        assert (result.returncode, result.stderr) == (0, ""), resource
    # An outside symlink reached through the alias still reports the escape.
    resource = "src/meta/leak/*"
    assert runtime(str(repo), resource) == (
        f"resource escapes the repository root through a symlink: {resource}"
    )
    result = _adapter_validator(resource, repo)
    assert result.returncode == 2
    assert result.stderr == (
        f"spawn_gemini_preregistered.sh: resource escapes the worktree through a symlink: {resource}\n"
    )
    # The alias is still found below safe internal symlinks, whichever path reaches it.
    for resource in ("src/**", "src/loop/*/HEAD"):
        assert runtime(str(repo), resource).startswith(
            f"resource must not cover git metadata: {resource} ("), resource
        assert _adapter_validator(resource, repo).returncode == 2, resource


# Symlinks below the literal prefix and first wildcard level. Each escape is
# reached only through a deeper wildcard, ``**``, a matched directory, or an
# internal symlink; the checks used to stop at the first wildcard level.
_DEEP_ESCAPES = [
    ("src/**", {"src/sub/escape": "{outside}"}, "src/sub/escape"),
    ("*/x", {"a/x": "{outside}/x"}, "a/x"),
    ("src/*/*/x", {"src/a/b": "{outside}"}, "src/a/b"),
    ("src/**/*.py", {"src/deep/er/leak.py": "{outside}/x"}, "src/deep/er/leak.py"),
    ("src/*", {"src/sub/escape": "{outside}"}, "src/sub/escape"),
    ("src", {"src/sub/escape": "{outside}"}, "src/sub/escape"),
    ("src/**", {"docs/deep/escape": "{outside}"}, "src/shared/deep/escape"),
]


@pytest.mark.parametrize("resource, links, where", _DEEP_ESCAPES)
def test_resource_boundary_rejects_deep_symlink_escape(tmp_path, resource, links, where):
    repo = _make_repo(tmp_path, links, root_escape=False)
    assert gemini_runtime.resource_boundary_error(str(repo), resource) == (
        f"resource escapes the repository root through a symlink: {resource} ({where})"
    )


def test_resource_boundary_accepts_deep_internal_symlinks_and_cycles(tmp_path):
    repo = _make_repo(tmp_path, {
        "src/pkg/deep/docs": "../../../docs",
        "src/pkg/deep/loop": "../..",
        "src/pkg/self": ".",
        "src/pkg/dangling": "missing-inside",
        "tests/unit/fixture.py": "../test_app.py",
    })
    (repo / "src" / "pkg" / "deep" / "mod.py").write_text("x\n", encoding="utf-8")
    check = gemini_runtime.resource_boundary_error
    for safe in ("src/**", "src/**/*.py", "src/*/deep/**", "tests/**", "src",
                 "src/pkg/deep/docs/*.md", "src/pkg/*/loop/**"):
        assert check(str(repo), safe) == "", safe
    # An escape behind an internal link is still found, whichever path reaches it.
    (repo / "docs" / "leak").symlink_to(tmp_path / "outside")
    error = check(str(repo), "src/pkg/**")
    assert error.startswith("resource escapes the repository root through a symlink: src/pkg/** (")
    assert error.endswith("/leak)")


def test_resource_boundary_walk_is_bounded_and_fails_closed(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    for index in range(20):
        (repo / "src" / "wide" / f"f{index}").mkdir(parents=True)
    check = gemini_runtime.resource_boundary_error
    monkeypatch.setattr(gemini_runtime, "_MAX_BOUNDARY_ENTRIES", 10)
    assert check(str(repo), "src/**").startswith("resource covers too many paths")
    assert check(str(repo), "docs/*.md") == ""
    monkeypatch.undo()
    if os.geteuid() == 0:
        pytest.skip("root can read unreadable directories")
    locked = repo / "src" / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        assert check(str(repo), "src/**") == (
            "could not verify the repository boundary for resource: src/** (src/locked)"
        )
        assert check(str(repo), "tests/**") == ""
    finally:
        locked.chmod(0o755)


# Default APFS is case- and normalization-insensitive: ``.GI?`` opens ``.git``
# and ``SR?`` opens ``src``.  Components are matched on names alone, so every
# case below holds on case-sensitive filesystems as well.
@pytest.mark.parametrize("resource, where", [
    (".GI?/config", ".git/config"),
    (".G*/config", ".git/config"),
    ("[.]GIT/HEAD", ".git/HEAD"),
    (".Gi[t]/objects/**", ".git/objects"),
    (".G?T", ".git"),
])
def test_case_variant_globs_cannot_cover_git_metadata(tmp_path, resource, where):
    repo = _make_repo(tmp_path, root_escape=False)
    error = f"resource must not cover git metadata: {resource} ({where})"
    assert gemini_runtime.resource_boundary_error(str(repo), resource) == error
    result = _adapter_validator(resource, repo)
    assert result.returncode == 2, result.stdout
    assert result.stderr == f"spawn_gemini_preregistered.sh: {error}\n"


def test_case_variant_globs_cover_linked_worktree_gitdir_file(tmp_path):
    repo = _make_repo(tmp_path, root_escape=False)
    worktree = tmp_path / "linked"
    _git("worktree", "add", "-q", str(worktree), cwd=repo)
    assert (worktree / ".git").is_file()
    for resource in (".G?T", ".g?T/**", "[.]Git"):
        error = f"resource must not cover git metadata: {resource} (.git)"
        assert gemini_runtime.resource_boundary_error(str(worktree), resource) == error
        result = _adapter_validator(resource, worktree)
        assert result.returncode == 2, resource
        assert result.stderr == f"spawn_gemini_preregistered.sh: {error}\n", resource


# Each escape is reachable only through a name the filesystem folds together:
# a case variant, a class that excludes one case (``[!s]`` admits ``S``), a
# character that folds to two (``ß`` opens ``ss``), or another normal form.
@pytest.mark.parametrize("resource, links, where", [
    ("SR?/**", {"src/sub/escape": "{outside}"}, "src/sub/escape"),
    ("[!s]RC/**", {"src/sub/escape": "{outside}"}, "src/sub/escape"),
    ("*/SUB/ESCAPE", {"src/sub/escape": "{outside}"}, "src/sub/escape"),
    ("docs/?/**", {"docs/ss/escape": "{outside}"}, "docs/ss/escape"),
    ("doc?/cafe\u0301/**", {"docs/caf\u00e9/escape": "{outside}"}, "docs/caf\u00e9/escape"),
])
def test_folded_globs_cannot_reach_symlink_escape(tmp_path, resource, links, where):
    repo = _make_repo(tmp_path, links, root_escape=False)
    error = f"{resource} ({where})"
    assert gemini_runtime.resource_boundary_error(str(repo), resource) == (
        f"resource escapes the repository root through a symlink: {error}"
    )
    result = _adapter_validator(resource, repo)
    assert result.returncode == 2, result.stdout
    assert result.stderr == (
        f"spawn_gemini_preregistered.sh: resource escapes the worktree through a symlink: {error}\n"
    )


def test_case_variant_globs_keep_safe_neighbours_allowed(tmp_path):
    repo = _make_repo(tmp_path, root_escape=False)
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
    for resource in (".GI?HUB/**", ".GIT?*", ".GITHUB/**", "[.]GITHUB/*", "SR?/**",
                     "S[!r]C/*.PY", "DOC[S]/*.MD", "[A-Z]*/**", "TESTS/**"):
        assert gemini_runtime.resource_boundary_error(str(repo), resource) == "", resource
        result = _adapter_validator(resource, repo)
        assert (result.returncode, result.stderr) == (0, ""), resource


@pytest.mark.parametrize("resources, error", [
    ("/etc/**", "resource must be relative to the repository: /etc/**"),
    ("../outside/**", "resource must not contain parent traversal: ../outside/**"),
    ("escape/**", "resource escapes the repository root through a symlink: escape/**"),
    ("./-rf", "resource must not start with '-': ./-rf"),
    (".//--ttl=1", "resource must not start with '-': .//--ttl=1"),
    ("src/**,./-rf", "resource must not start with '-': ./-rf"),
    (".GI?/config", "resource must not cover git metadata: .GI?/config (.git/config)"),
    ("E?CAPE/**", "resource escapes the repository root through a symlink: E?CAPE/** (escape)"),
])
def test_unsafe_resources_fail_before_registration(gemini_env, resources, error):
    result = server.do_spawn(_gemini_payload(gemini_env, resources=resources))
    assert result == {"ok": False, "error": error}
    assert gemini_env.launches.mcp_calls == []
    assert gemini_env.launches.launched == []
    assert not list(gemini_env.runtime.glob("provider-gemini-task-*"))
    assert _git("worktree", "list", "--porcelain", cwd=gemini_env.repo).count("worktree ") == 1
    assert _git("branch", "--list", "exp/*", cwd=gemini_env.repo) == ""


# --------------------------------------------------------------------------- #
# Pre-registration preflight
# --------------------------------------------------------------------------- #
def test_preflight_failures_do_not_register_junk_identities(gemini_env, monkeypatch, tmp_path):
    checks = []

    def attempt(label, **extra):
        result = server.do_spawn(_gemini_payload(gemini_env, **extra))
        checks.append((label, result.get("error", "")))
        assert gemini_env.launches.mcp_calls == [], label

    helper = gemini_env.home / "bin" / "agentstack-gemini-stream"
    helper.chmod(0o644)
    attempt("helper")
    helper.chmod(0o755)

    monkeypatch.setenv("AGENTSTACK_GEMINI_BIN", "agy-not-installed")
    attempt("agy")
    monkeypatch.delenv("AGENTSTACK_GEMINI_BIN")

    plain = tmp_path / "plain"
    plain.mkdir()
    attempt("git", dir=str(plain))
    attempt("base", worktree_base="no-such-rev")
    attempt("base-option", worktree_base="--output=/tmp/x")
    attempt("parent", parent="Missing")

    gemini_env.adapter.chmod(0o644)
    attempt("adapter")

    assert [label for label, error in checks if not error] == []
    errors = dict(checks)
    assert errors["helper"].startswith("Gemini provider helper is not installed")
    assert errors["agy"] == "Antigravity CLI not found for provider gemini (expected agy)"
    assert errors["git"] == "provider gemini requires a git repository"
    assert errors["base"] == "could not resolve worktree base for provider gemini: no-such-rev"
    assert errors["base-option"].startswith("worktree base must not start with '-'")
    assert errors["parent"] == "parent registration token unavailable for 'Missing'"
    assert errors["adapter"].startswith("spawn adapter missing for provider gemini")


# --------------------------------------------------------------------------- #
# Dispatch contract, identity, and task authority
# --------------------------------------------------------------------------- #
def test_gemini_spawn_dispatches_existing_preregistered_adapter(gemini_env):
    result = server.do_spawn(_gemini_payload(gemini_env))

    assert result["ok"] is True
    assert result["provider"] == "gemini"
    assert result["model"] == "gemini-3.8-flash-high"
    assert result["effort"] == "medium"
    assert result["worktree"] is True
    registration = gemini_env.launches.registrations["Sunny-Curie"]
    assert registration["program"] == "antigravity"
    assert registration["model"] == "gemini-3.8-flash-high"
    # One task authority: the launcher's full-task handoff, not a task mail
    # asking the child to reply while the launcher also reports.
    assert [method for method, _ in gemini_env.launches.mcp_calls] == [
        "register_agent", "set_contact_policy",
    ]

    [(args, kwargs)] = gemini_env.launches.launched
    assert args[0] == str(gemini_env.adapter)
    assert args[1:4] == ["--pre-registered", "Sunny-Curie", "--child-token-file"]
    assert args[5:] == ["--model", "gemini-3.8-flash-high", "--worktree",
                        "implement the requested change", str(gemini_env.repo)]
    env = kwargs["env"]
    assert env["PARENT_AGENT"] == "Parent"
    assert env["AGENTSTACK_GEMINI_MODEL"] == "gemini-3.8-flash-high"
    assert env["AGENTSTACK_GEMINI_EFFORT"] == "medium"
    assert env["AGENTSTACK_GEMINI_RESOURCES"] == "src/**,tests/**"
    assert env["AGENTSTACK_HOME"] == str(gemini_env.home)
    assert kwargs["start_new_session"] is True
    # The adapter consumed the handoff before exiting; nothing is left behind.
    assert not pathlib.Path(env["AGENTSTACK_GEMINI_TASK_FILE"]).exists()
    assert not list(gemini_env.runtime.glob("provider-gemini-*"))


def test_native_spawn_still_sends_task_mail(gemini_env):
    result = server.do_spawn({"parent": "Parent", "name": "Zesty-Bohr", "task": "work",
                              "dir": str(gemini_env.repo)})
    assert result["ok"] is True and result["provider"] == "claude"
    assert [method for method, _ in gemini_env.launches.mcp_calls] == [
        "register_agent", "set_contact_policy", "send_message",
    ]


def _wait_for_state(name: str, state: str) -> dict:
    deadline = time.monotonic() + 5
    status = server.spawn_launch_status(name)
    while status.get("state") != state and time.monotonic() < deadline:
        time.sleep(0.02)
        status = server.spawn_launch_status(name)
    assert status.get("state") == state, status
    return status


def test_async_gemini_status_and_log_report_gemini_identity(gemini_env):
    release = gemini_env.launches.block(str(gemini_env.adapter))
    pending = server.do_spawn(_gemini_payload(gemini_env, name="Brisk-Hopper", **{"async": True}))

    assert pending["ok"] is True and pending["pending"] is True
    assert (pending["provider"], pending["model"], pending["effort"]) == (
        "gemini", "gemini-3.8-flash-high", "medium")
    launching = server.spawn_launch_status("Brisk-Hopper")
    assert launching["state"] == "launching"
    task_file = pathlib.Path(
        gemini_env.launches.for_script(gemini_env.adapter)[0][1]["env"]["AGENTSTACK_GEMINI_TASK_FILE"])
    assert task_file.read_text(encoding="utf-8") == "implement the requested change"

    release.set()
    status = _wait_for_state("Brisk-Hopper", "ready")
    assert status["result"]["provider"] == "gemini"
    assert status["result"]["model"] == "gemini-3.8-flash-high"
    assert status["result"]["effort"] == "medium"
    assert not task_file.exists()

    log = (gemini_env.tmp / "logs" / "spawn.log").read_text(encoding="utf-8")
    assert ("spawn child=Brisk-Hopper parent=Parent provider=gemini "
            "model=gemini-3.8-flash-high effort=medium") in log
    assert "provider=claude" not in log


def test_public_payload_cannot_spoof_launch_identity(gemini_env, monkeypatch):
    monkeypatch.delenv("AGENTSTACK_GEMINI_TASK_FILE", raising=False)
    result = server.do_spawn({
        "parent": "Parent", "name": "Zesty-Bohr", "task": "work", "dir": str(gemini_env.repo),
        "provider": "claude", "model": "claude-sonnet-5",
        "program": "antigravity", "script": str(gemini_env.adapter),
        "launcher_env": {"AGENTSTACK_GEMINI_TASK_FILE": "/tmp/spoof"},
        "handoff_paths": [str(gemini_env.claude_launcher)],
        "spec": {"provider": "gemini"}, "provider_identity": "gemini",
    })
    assert result["ok"] is True
    assert result["provider"] == "claude"
    assert gemini_env.launches.registrations["Zesty-Bohr"]["program"] == "claude-code"
    [(args, kwargs)] = gemini_env.launches.launched
    assert args[0] == str(gemini_env.claude_launcher)
    assert "AGENTSTACK_GEMINI_TASK_FILE" not in kwargs["env"]
    assert gemini_env.claude_launcher.exists()

    # A Claude request naming a Gemini model never borrows Gemini's program.
    assert server.do_spawn({
        "parent": "Parent", "task": "work", "provider": "claude",
        "model": "gemini-3.8-flash-high",
    }) == {"ok": False, "error": "model not allowed for provider claude: gemini-3.8-flash-high"}
    with pytest.raises(TypeError):
        server.spawn_with_launch_spec({"task": "work"}, {"provider": "gemini"})


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #
def test_concurrent_gemini_and_native_spawns_do_not_cross_contaminate(gemini_env):
    launches = gemini_env.launches
    release = launches.block(str(gemini_env.adapter))
    results: dict[str, dict] = {}
    original_script = server.SPAWN_SCRIPT
    original_models = dict(server._SPAWN_MODELS)

    def spawn(key: str, payload: dict) -> threading.Thread:
        thread = threading.Thread(target=lambda: results.__setitem__(key, server.do_spawn(payload)))
        thread.start()
        return thread

    gemini = spawn("gemini", _gemini_payload(gemini_env))
    assert launches.started[str(gemini_env.adapter)].wait(timeout=5)
    # The Gemini launcher is waiting on readiness right now.
    assert server.SPAWN_SCRIPT == original_script
    assert server._SPAWN_MODELS == original_models

    native = [
        spawn("claude", {"parent": "Parent", "name": "Zesty-Bohr", "task": "claude work",
                         "dir": str(gemini_env.repo), "provider": "claude",
                         "model": "claude-opus-5"}),
        spawn("codex", {"parent": "Parent", "name": "Quiet-Noether", "task": "codex work",
                        "dir": str(gemini_env.repo), "provider": "codex",
                        "model": "gpt-5.6-sol", "effort": "high"}),
        spawn("borrowed", {"parent": "Parent", "task": "claude work", "provider": "claude",
                           "model": "gemini-3.8-flash-high"}),
    ]
    for thread in native:
        thread.join(timeout=5)
        assert not thread.is_alive(), "native spawn was serialized behind Gemini readiness"
    assert "gemini" not in results

    release.set()
    gemini.join(timeout=5)
    assert not gemini.is_alive()

    assert results["gemini"]["provider"] == "gemini"
    assert results["claude"]["provider"] == "claude"
    assert results["claude"]["model"] == "claude-opus-5"
    assert results["codex"]["provider"] == "codex"
    assert results["borrowed"] == {
        "ok": False, "error": "model not allowed for provider claude: gemini-3.8-flash-high",
    }
    registrations = launches.registrations
    assert registrations["Sunny-Curie"]["program"] == "antigravity"
    assert registrations["Zesty-Bohr"]["program"] == "claude-code"
    assert registrations["Quiet-Noether"]["program"] == "codex-cli"

    by_child = {args[2]: (args, kwargs) for args, kwargs in launches.launched}
    assert by_child["Sunny-Curie"][0][0] == str(gemini_env.adapter)
    for child, model in (("Zesty-Bohr", "claude-opus-5"), ("Quiet-Noether", "gpt-5.6-sol")):
        args, kwargs = by_child[child]
        assert args[0] == str(gemini_env.claude_launcher)
        assert args[args.index("--model") + 1] == model
        assert not any(key.startswith("AGENTSTACK_GEMINI_") for key in kwargs["env"])
    assert "--codex" in by_child["Quiet-Noether"][0]
    assert "--codex" not in by_child["Zesty-Bohr"][0]
    assert not hasattr(gemini_runtime, "_PATCH_LOCK")


def test_readiness_timeout_signals_gemini_process_group_and_cleans_handoff(
        gemini_env, monkeypatch):
    signals = []

    class SlowProc:
        pid = 31337

        def __init__(self):
            self.calls = 0

        def wait(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("launcher", timeout)
            return -15

        def terminate(self):
            signals.append(("terminate", None))

        def kill(self):
            signals.append(("kill", None))

    launched = []

    def popen(args, **kwargs):
        if os.path.basename(str(args[0])) == "git":
            return _REAL_POPEN(args, **kwargs)
        launched.append((list(args), kwargs))
        return SlowProc()

    monkeypatch.setattr(server.subprocess, "Popen", popen)
    monkeypatch.setattr(server.os, "killpg", lambda pid, signum: signals.append((pid, signum)))

    result = server.do_spawn(_gemini_payload(gemini_env))
    assert result["ok"] is False
    assert "did not finish readiness checks within 120s" in result["error"]
    assert signals == [(31337, signal.SIGTERM)]
    assert not pathlib.Path(launched[0][1]["env"]["AGENTSTACK_GEMINI_TASK_FILE"]).exists()

    signals.clear()
    server.do_spawn({"parent": "Parent", "name": "Zesty-Bohr", "task": "work",
                     "dir": str(gemini_env.repo)})
    assert signals == [("terminate", None)]


# --------------------------------------------------------------------------- #
# Capability-driven modal controls
# --------------------------------------------------------------------------- #
def _rendered_index() -> str:
    source = (ROOT / "dashboard" / "index.html").read_bytes()
    return server._render_dashboard_index(source).decode("utf-8")


def test_rendered_dashboard_adds_capability_driven_resource_controls():
    rendered = _rendered_index()
    assert 'id="spm-resources-row"' in rendered
    assert 'id="spm-resources"' in rendered
    assert "applySpawnProviderCapabilities(provider);" in rendered
    assert "const providerReady=spawnProviderRequirementsMet();" in rendered
    assert "resetSpawnProviderCapabilities();" in rendered
    injected = "".join(new for _label, _old, new in gemini_runtime._UI_PATCHES)
    assert "gemini" not in injected.lower()
    assert "antigravity" not in injected.lower()


def test_rendered_dashboard_keeps_server_language_defaults(monkeypatch):
    source = (ROOT / "dashboard" / "index.html").read_bytes()
    monkeypatch.setattr(server, "DASHBOARD_LANG", "en")
    rendered = server._render_dashboard_index(source, "en").decode("utf-8")
    assert 'const AGENTSTACK_SERVER_DEFAULTS={"language":"en","murmur":null};' in rendered


_UI_FUNCTIONS = (
    "normalizeSpawnProviders", "spawnModelTone", "renderSpawnProviders",
    "selectSpawnProvider", "renderSpawnModels", "selectSpawnModel",
    "renderSpawnEfforts", "selectSpawnEffort", "renderSpawnEngineNote",
    "updateSpawnButton", "buildSpawnPayload",
)

_UI_HARNESS = r"""
const els={};
function makeEl(id){
  const classes=new Set();
  return {id,value:'',checked:false,disabled:false,hidden:false,textContent:'',
    style:{},dataset:{},children:[],_html:'',
    classList:{toggle(c,on){if(on===undefined?!classes.has(c):on)classes.add(c);else classes.delete(c);},
      add(c){classes.add(c)},remove(c){classes.delete(c)},contains(c){return classes.has(c)}},
    setAttribute(){},
    get innerHTML(){return this._html},
    set innerHTML(v){this._html=v;this.children=[...v.matchAll(/data-(provider|model|effort)="([^"]*)"/g)]
      .map(m=>({dataset:{[m[1]]:m[2]},classList:{toggle(){}},setAttribute(){}}));},
    querySelectorAll(){return this.children;}};
}
const SPM=id=>els[id]||(els[id]=makeEl(id));
const esc=s=>String(s);
let spmSelectedName='',spmSelectedProvider='',spmSelectedModel='';
let spmSelectedEffort='',spmProviders=[];
let spmBusy=false,spmReady=true,spmIdentityState='auto',spmSuggestedName='';
const spmNameStatus=new Map();
const state=()=>({
  worktree:SPM('spm-worktree').checked,locked:SPM('spm-worktree').disabled,
  resourcesShown:SPM('spm-resources-row').style.display==='grid',
  resources:SPM('spm-resources').value,effort:spmSelectedEffort,
  launchDisabled:SPM('spm-spawn').disabled,payload:buildSpawnPayload()});
const out={};
SPM('spm-parent').value='Parent';SPM('spm-task').value='work';SPM('spm-dir').value='/repo';
resetSpawnProviderCapabilities();
renderSpawnProviders(normalizeSpawnProviders(CATALOG));
out.initialClaude=state();
selectSpawnProvider('gemini');
out.geminiSelected=state();
SPM('spm-resources').value='src/**';SPM('spm-resources').oninput();
out.geminiResourcesOnly=state();
selectSpawnEffort('medium');
out.geminiReady=state();
selectSpawnProvider('claude');
out.claudeAfterGemini=state();
selectSpawnProvider('gemini');
SPM('spm-resources').value='tests/**';selectSpawnEffort('high');
selectSpawnProvider('codex');
out.codexAfterGemini=state();
selectSpawnProvider('claude');
SPM('spm-worktree').checked=true;
selectSpawnProvider('gemini');
selectSpawnProvider('claude');
out.claudeChoiceRestored=state();
process.stdout.write(JSON.stringify(out));
"""


def _extract_ui_script(rendered: str) -> str:
    helpers = re.search(
        r"/\* provider-capabilities:start \*/.*?/\* provider-capabilities:end \*/",
        rendered, re.DOTALL)
    assert helpers, "provider capability helpers missing"
    parts = [helpers.group(0)]
    for constant in ("SPM_MODEL_TONES", "SPM_EFFORT_HINTS"):
        match = re.search(rf"const {constant}=Object\.freeze\(\{{.*?\}}\);", rendered, re.DOTALL)
        assert match, constant
        parts.append(match.group(0))
    for name in _UI_FUNCTIONS:
        match = re.search(rf"\nfunction {name}\(.*?\)\{{.*?\n\}}", rendered, re.DOTALL)
        assert match, name
        parts.append(match.group(0))
    return "\n".join(parts)


def test_provider_switching_does_not_leak_capability_state(monkeypatch):
    monkeypatch.delenv("AGENTSTACK_GEMINI_MODELS", raising=False)
    catalog = {"providers": [
        {"id": "claude", "label": "Claude", "program": "claude-code",
         "models": ["claude-sonnet-5"], "default_model": "claude-sonnet-5", "efforts": None},
        {"id": "codex", "label": "Codex", "program": "codex-cli",
         "models": ["gpt-5.6-sol"], "default_model": "gpt-5.6-sol",
         "efforts": ["low", "medium", "high", "xhigh"], "effort_default": "xhigh"},
        gemini_runtime._catalog_item(["gemini-3.8-flash-high"]),
    ]}
    script = (f"const CATALOG={json.dumps(catalog)};\n"
              + _extract_ui_script(_rendered_index()) + "\n" + _UI_HARNESS)
    result = _REAL_RUN(["node", "-e", script], capture_output=True, text=True,
                       timeout=20, check=False)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)

    base_payload = {"task": "work", "dir": "/repo", "role": "", "group": "", "parent": "Parent"}
    assert out["initialClaude"]["payload"] == {**base_payload, "provider": "claude",
                                               "model": "claude-sonnet-5"}
    assert out["geminiSelected"] | {"payload": None} == {
        "worktree": True, "locked": True, "resourcesShown": True, "resources": "",
        "effort": "", "launchDisabled": True, "payload": None,
    }
    assert out["geminiResourcesOnly"]["launchDisabled"] is True
    assert out["geminiReady"]["launchDisabled"] is False
    assert out["geminiReady"]["payload"] == {
        **base_payload, "provider": "gemini", "model": "gemini-3.8-flash-high",
        "effort": "medium", "resources": "src/**", "worktree": True,
    }
    for key, provider, model, effort in (
            ("claudeAfterGemini", "claude", "claude-sonnet-5", None),
            ("codexAfterGemini", "codex", "gpt-5.6-sol", "xhigh")):
        state = out[key]
        expected = {**base_payload, "provider": provider, "model": model}
        if effort:
            expected["effort"] = effort
        assert state["payload"] == expected, key
        assert (state["worktree"], state["locked"], state["resourcesShown"],
                state["resources"], state["launchDisabled"]) == (False, False, False, "", False), key
    assert out["claudeChoiceRestored"]["worktree"] is True
    assert out["claudeChoiceRestored"]["locked"] is False
    assert out["claudeChoiceRestored"]["payload"]["worktree"] is True


# --------------------------------------------------------------------------- #
# Render-marker drift and extension failure
# --------------------------------------------------------------------------- #
def test_ui_marker_drift_serves_canonical_page_without_partial_injection():
    source = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
    drifted = source.replace("hint.textContent=copy?", "hint.innerText=copy?")
    assert drifted != source
    # An early patch target is still present; none of the patches may apply.
    patched, error = gemini_runtime.apply_ui_patches(drifted)
    assert error == "dashboard UI patch target missing: effort button refresh"
    assert patched == drifted

    rendered = server._render_dashboard_index(drifted.encode("utf-8")).decode("utf-8")
    assert rendered == canonical_server._render_dashboard_index(
        drifted.encode("utf-8"), server.DASHBOARD_LANG, server.DASHBOARD_MURMUR).decode("utf-8")
    assert "spm-resources-row" not in rendered
    assert "provider-capabilities" not in rendered


def test_ui_marker_drift_disables_gemini_catalog_and_spawn(monkeypatch, tmp_path):
    source = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
    drifted = tmp_path / "index.html"
    drifted.write_text(source.replace("button.disabled=spmBusy||!spmReady||!identityReady;",
                                      "button.disabled=spmBusy;"), encoding="utf-8")
    monkeypatch.setattr(server, "INDEX_HTML", str(drifted))
    ids = [item["id"] for item in server.spawn_names_payload()["providers"]]
    assert ids == ["claude", "codex"]
    result = server.do_spawn({"parent": "Parent", "task": "work", "dir": str(tmp_path),
                              "provider": "gemini", "effort": "high", "resources": "src/**"})
    assert result == {
        "ok": False,
        "error": "provider gemini unavailable: dashboard UI patch target missing: launch readiness gate",
    }


def test_broken_extension_keeps_canonical_dashboard_available(tmp_path):
    dashboard = tmp_path / "dashboard"
    dashboard.mkdir()
    for name in ("provider_server.py", "server.py", "index.html"):
        (dashboard / name).write_bytes((ROOT / "dashboard" / name).read_bytes())
    (dashboard / "gemini_provider_runtime.py").write_text(
        "import module_that_is_not_installed\n", encoding="utf-8")
    probe = f"""
import importlib.util, json, sys
sys.path.insert(0, {str(ROOT)!r})
spec = importlib.util.spec_from_file_location("probe_provider_server", {str(dashboard / "provider_server.py")!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
core = sys.modules["_orrery_provider_dashboard_core"]
print(json.dumps({{
    "failures": core._PROVIDER_EXTENSION_FAILURES,
    "gemini": core.do_spawn({{"parent": "P", "task": "w", "provider": "gemini"}}),
    "claude": core.do_spawn({{"parent": "P", "task": "w", "provider": "claude", "effort": "x"}}),
}}))
"""
    result = _REAL_RUN([sys.executable, "-c", probe], capture_output=True, text=True,
                       timeout=60, check=False, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert out["failures"] == ["gemini_provider_runtime.py"]
    assert out["gemini"] == {"ok": False, "error": "provider not allowed: gemini"}
    assert out["claude"] == {"ok": False, "error": "effort not supported for provider: claude"}
    assert "optional dashboard provider extension disabled" in result.stderr


def test_provider_entrypoint_has_no_ambient_module_fallback():
    text = (ROOT / "dashboard" / "provider_server.py").read_text(encoding="utf-8")
    assert "ModuleNotFoundError" not in text
    assert "from gemini_provider_runtime import" not in text


# --------------------------------------------------------------------------- #
# Real adapter boundary and termination
# --------------------------------------------------------------------------- #
_FAKE_MAIL_HELPER = """import os, sys, time
with open(os.environ["FAKE_MAIL_LOG"], "a", encoding="utf-8") as log:
    log.write(sys.argv[1] + "\\n")
if sys.argv[1] == "reserve" and os.environ.get("FAKE_BLOCK_RESERVE"):
    open(os.environ["FAKE_BLOCK_RESERVE"], "w").close()
    time.sleep(30)
"""


def _adapter_run(tmp_path: pathlib.Path, resources: str, block_reserve: bool = False,
                 links: dict[str, str] | None = None, root_escape: bool = True):
    repo = _make_repo(tmp_path, links, root_escape)
    home = tmp_path / "home"
    _executable(home / "bin" / "agentstack-gemini-child-mail", _FAKE_MAIL_HELPER)
    _executable(home / "bin" / "agentstack-gemini-stream", "#!/bin/sh\n")
    _executable(home / "bin" / "agentstack-gemini-mcp", "#!/bin/sh\n")
    fake_bin = tmp_path / "fake-bin"
    _executable(fake_bin / "agy", "#!/bin/sh\n")
    _executable(fake_bin / "tmux", "#!/bin/sh\n")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    token = runtime / "one-shot.token"
    token.write_text("child-token", encoding="utf-8")
    task = runtime / "task.txt"
    task.write_text("full task", encoding="utf-8")
    marker = tmp_path / "reserve-started"
    env = {
        **{key: value for key, value in os.environ.items()
           if not key.startswith("AGENTSTACK_")},
        "AGENTSTACK_MANAGED_AGENTS_FILE": str(runtime / "managed_agents.txt"),
        "AGENTSTACK_MAIL_ENV": str(tmp_path / "mail.env"),
        "AGENTSTACK_MCP_URL": "http://127.0.0.1:9/mcp",
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "AGENTSTACK_HOME": str(home),
        "AGENTSTACK_LABEL_PREFIX": "org.agentstack.test.gemini-adapter",
        "AGENTSTACK_HOOKS_DIR": str(tmp_path / "hooks"),
        "AGENTSTACK_RUNTIME_DIR": str(runtime),
        "AGENTSTACK_WORKTREE_ROOT": str(tmp_path / "worktrees"),
        "AGENTSTACK_PROJECT_KEY": "/project",
        "AGENTSTACK_PYTHON": sys.executable,
        "AGENTSTACK_GEMINI_RESOURCES": resources,
        "AGENTSTACK_GEMINI_TASK_FILE": str(task),
        "AGENTSTACK_GEMINI_EFFORT": "medium",
        "PARENT_AGENT": "Parent",
        "FAKE_MAIL_LOG": str(tmp_path / "mail.log"),
    }
    if block_reserve:
        env["FAKE_BLOCK_RESERVE"] = str(marker)
    proc = _REAL_POPEN(
        ["/bin/bash", str(ADAPTER), "--pre-registered", "Child", "--child-token-file",
         str(token), "--model", "gemini-3.8-flash-high", "--worktree", "task", str(repo)],
        env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    return types.SimpleNamespace(proc=proc, repo=repo, task=task, marker=marker,
                                 runtime=runtime, worktree=tmp_path / "worktrees" / "Child",
                                 mail_log=tmp_path / "mail.log")


def _mail_calls(run) -> list[str]:
    try:
        return run.mail_log.read_text(encoding="utf-8").split()
    except FileNotFoundError:
        return []


def test_adapter_rejects_absolute_resource_before_worktree(tmp_path):
    run = _adapter_run(tmp_path, "src/**,/etc/**")
    _stdout, stderr = run.proc.communicate(timeout=30)
    assert run.proc.returncode == 2, stderr
    assert "unsafe resource declaration: /etc/**" in stderr
    assert not run.worktree.exists()
    assert _mail_calls(run) == []


@pytest.mark.parametrize("resources, item", [
    ("./-rf", "./-rf"),
    (".//--ttl=1", ".//--ttl=1"),
    ("src/**,./-rf", "./-rf"),
])
def test_adapter_rejects_normalized_leading_dash_before_worktree(tmp_path, resources, item):
    run = _adapter_run(tmp_path, resources)
    _stdout, stderr = run.proc.communicate(timeout=30)
    assert run.proc.returncode == 2, stderr
    assert stderr == f"spawn_gemini_preregistered.sh: unsafe resource declaration: {item}\n"
    assert _mail_calls(run) == []
    assert not run.worktree.exists()
    assert "exp/Child" not in _git("branch", "--list", cwd=run.repo)
    assert not (run.runtime / "agent_token_Child").exists()


@pytest.mark.parametrize("resource", [".G?T", ".g?T/**", "[.]GIT"])
def test_adapter_rejects_case_variant_globs_covering_worktree_gitdir_file(tmp_path, resource):
    run = _adapter_run(tmp_path, resource, root_escape=False)
    _stdout, stderr = run.proc.communicate(timeout=60)
    assert run.proc.returncode == 2, stderr
    assert f"resource must not cover git metadata: {resource} (.git)" in stderr
    assert _mail_calls(run) == ["retire"]
    assert not run.worktree.exists()
    assert "exp/Child" not in _git("branch", "--list", cwd=run.repo)
    assert not run.task.exists()
    assert not (run.runtime / "agent_token_Child").exists()


def test_adapter_rejects_worktree_symlink_escape_before_reservation(tmp_path):
    run = _adapter_run(tmp_path, "src/**,escape/**")
    _stdout, stderr = run.proc.communicate(timeout=60)
    assert run.proc.returncode == 2, stderr
    assert "resource escapes the worktree through a symlink: escape/**" in stderr
    assert _mail_calls(run) == ["retire"]
    assert not run.worktree.exists()
    assert "exp/Child" not in _git("branch", "--list", cwd=run.repo)
    assert not run.task.exists()
    assert not (run.runtime / "agent_token_Child").exists()


@pytest.mark.parametrize("resource", [".g*", "[.]git", "*", "**"])
def test_adapter_rejects_globs_covering_worktree_git_metadata(tmp_path, resource):
    run = _adapter_run(tmp_path, resource, root_escape=False)
    _stdout, stderr = run.proc.communicate(timeout=60)
    assert run.proc.returncode == 2, stderr
    assert f"resource must not cover git metadata: {resource} (.git)" in stderr
    assert _mail_calls(run) == ["retire"]
    assert not run.worktree.exists()
    assert not run.task.exists()
    assert not (run.runtime / "agent_token_Child").exists()


@pytest.mark.parametrize("resource", _GIT_ALIAS_RESOURCES)
def test_adapter_rejects_symlink_aliases_of_worktree_gitdir_file(tmp_path, resource):
    run = _adapter_run(tmp_path, resource, links={"src/meta": "../.git"}, root_escape=False)
    _stdout, stderr = run.proc.communicate(timeout=60)
    assert run.proc.returncode == 2, stderr
    assert f"resource must not cover git metadata: {resource} (src/meta)" in stderr
    assert _mail_calls(run) == ["retire"]
    assert not run.worktree.exists()
    assert "exp/Child" not in _git("branch", "--list", cwd=run.repo)
    assert not run.task.exists()
    assert not (run.runtime / "agent_token_Child").exists()


@pytest.mark.parametrize("resource, links, where", [
    ("src/**", {"src/sub/escape": "{outside}"}, "src/sub/escape"),
    ("*/x", {"a/x": "{outside}/x"}, "a/x"),
    ("src/**", {"docs/deep/escape": "{outside}"}, "src/shared/deep/escape"),
])
def test_adapter_rejects_deep_worktree_symlink_escape(tmp_path, resource, links, where):
    run = _adapter_run(tmp_path, resource, links=links, root_escape=False)
    _stdout, stderr = run.proc.communicate(timeout=60)
    assert run.proc.returncode == 2, stderr
    assert (
        f"resource escapes the worktree through a symlink: {resource} ({where})" in stderr
    )
    assert _mail_calls(run) == ["retire"]
    assert not run.worktree.exists()


def test_adapter_accepts_deep_internal_symlinks_and_reaches_reservation(tmp_path):
    run = _adapter_run(tmp_path, "src/**,tests/**/*.py,src/*/deep/**", block_reserve=True, links={
        "src/pkg/deep/docs": "../../../docs",
        "src/pkg/loop": "..",
        "tests/unit/fixture.py": "../test_app.py",
    })
    deadline = time.monotonic() + 60
    while not run.marker.exists() and run.proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert run.marker.exists(), run.proc.stderr.read() if run.proc.poll() is not None else ""
    os.killpg(run.proc.pid, signal.SIGTERM)
    _stdout, stderr = run.proc.communicate(timeout=30)
    assert "resource" not in stderr, stderr
    assert _mail_calls(run) == ["reserve", "retire"]
    assert not run.worktree.exists()


def test_adapter_termination_cleans_up_launch_artifacts(tmp_path):
    run = _adapter_run(tmp_path, "src/**", block_reserve=True)
    deadline = time.monotonic() + 60
    while not run.marker.exists() and run.proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert run.marker.exists(), run.proc.stderr.read() if run.proc.poll() is not None else ""
    os.killpg(run.proc.pid, signal.SIGTERM)
    _stdout, stderr = run.proc.communicate(timeout=30)
    assert run.proc.returncode == 143, stderr
    assert _mail_calls(run) == ["reserve", "retire"]
    assert not run.worktree.exists()
    assert not run.task.exists()
    assert not (run.runtime / "agent_token_Child").exists()


# --------------------------------------------------------------------------- #
# Packaging
# --------------------------------------------------------------------------- #
def test_service_runner_prefers_optional_provider_entrypoint(monkeypatch, tmp_path):
    core = tmp_path / "server.py"
    provider = tmp_path / "provider_server.py"
    core.write_text("# core\n", encoding="utf-8")
    monkeypatch.setattr(service_runner, "HERE", tmp_path)
    assert service_runner._default_server_path() == core
    provider.write_text("# provider\n", encoding="utf-8")
    assert service_runner._default_server_path() == provider


def test_provider_installer_ships_dashboard_extension():
    text = (ROOT / "scripts" / "install-gemini-provider.sh").read_text(encoding="utf-8")
    assert '"dashboard/provider_server.py"' in text
    assert '"dashboard/gemini_provider_runtime.py"' in text
    assert "service runner cannot load optional" in text


def test_gemini_dashboard_adapter_is_shell_parseable():
    result = _REAL_RUN(
        ["bash", "-n", str(ADAPTER)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
