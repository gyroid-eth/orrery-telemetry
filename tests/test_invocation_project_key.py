#!/usr/bin/env python3
"""Top-level invocation resolver; no ambient context or installed services.

Both pytest and direct execution use unittest, so neither fixture injection
nor the position of a custom globals() runner changes which tests execute.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "hooks" / "project-context.sh"
BASH = "/bin/bash"


@unittest.skipIf(os.name == "nt", "POSIX shell helpers; Windows uses its portable lane")
class InvocationProjectKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="orrery-invocation-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(self.home),
            "TMPDIR": str(self.root),
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "AGENTSTACK_HOME": str(self.home / ".agentstack"),
            "AGENTSTACK_LABEL_PREFIX": "org.agentstack.invocation-key." + self.root.name,
            "AGENTSTACK_PYTHON": sys.executable,
        }
        self.repo = self.root / "repo A"
        self.other = self.root / "repo B"
        self.make_repo(self.repo)
        self.make_repo(self.other)
        self.env_file = self.root / "installed-env.sh"
        self.env_file.write_text("", encoding="utf-8")

    def git(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=" + str(self.root / "no-hooks"),
             "-c", "maintenance.auto=false", "-c", "gc.auto=0", *args],
            cwd=cwd or self.root, env=self.env, capture_output=True,
            text=True, check=True, timeout=20,
        )
        return result.stdout.strip()

    def make_repo(self, path: Path, git_dir: Path | None = None) -> None:
        args = ["-c", "init.defaultBranch=main", "init", "-q"]
        if git_dir is not None:
            args.append("--separate-git-dir=" + str(git_dir))
        self.git(*args, str(path))
        self.git("-c", "user.name=Isolation Test", "-c",
                 "user.email=isolation-test@example.invalid", "-c",
                 "commit.gpgsign=false", "commit", "--allow-empty", "-qm",
                 "isolated fixture", cwd=path)

    def linked_worktree(self, repo: Path | None = None) -> Path:
        linked = self.root / "linked tree"
        self.git("worktree", "add", "-q", "--detach", str(linked),
                 cwd=repo or self.repo)
        return linked

    def call(self, function: str, *args: str | Path,
             env: dict[str, str] | None = None, cwd: Path | None = None,
             check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [BASH, "-euo", "pipefail", "-c",
             'source "$1"; shift; "$@"', "identity-test", str(SCRIPT),
             function, *(str(arg) for arg in args)],
            cwd=cwd or self.root, env={**self.env, **(env or {})},
            capture_output=True, text=True, check=check, timeout=20,
        )

    def non_git_workspace(self) -> Path:
        workspace = self.root / "plain workspace"
        workspace.mkdir()
        return workspace

    def test_invocation_selects_target_repository_over_ambient_or_fallback(self) -> None:
        linked = self.linked_worktree()
        stale = {"AGENTSTACK_PROJECT_KEY": str(self.other),
                 "PROJECT_KEY": str(self.other),
                 "AGENTSTACK_PROJECT_CONTEXT": "1",
                 "AGENTSTACK_PROJECT_REPOSITORY": str(self.other),
                 "GIT_DIR": str(self.other / ".git"),
                 "GIT_WORK_TREE": str(self.other),
                 "GIT_COMMON_DIR": str(self.other / ".git")}
        for target in (self.repo, linked):
            with self.subTest(target=target.name):
                self.assertEqual(self.call("agentstack_resolve_invocation_project_key",
                                           target, "", "installed-fallback", env=stale).stdout,
                                 str(self.repo) + "\n")

    def test_invocation_explicit_key_wins_without_changing_repository_binding(self) -> None:
        self.assertEqual(self.call("agentstack_resolve_invocation_project_key", self.repo,
                                   "operator-selected", "fallback").stdout,
                         "operator-selected\n")
        self.assertEqual(self.call("agentstack_repository_key", self.repo).stdout,
                         str(self.repo) + "\n")

    def test_invocation_explicit_linked_key_is_not_collapsed(self) -> None:
        linked = self.linked_worktree()
        self.assertEqual(self.call("agentstack_resolve_invocation_project_key",
                                   linked, linked).stdout, str(linked) + "\n")

    def test_invocation_unvalidated_context_marker_does_not_override_target(self) -> None:
        self.assertEqual(self.call("agentstack_resolve_invocation_project_key", self.repo,
                                   env={"AGENTSTACK_PROJECT_KEY": "claimed-custom-key",
                                        "AGENTSTACK_PROJECT_CONTEXT": "1",
                                        "AGENTSTACK_PROJECT_REPOSITORY": str(self.repo)}).stdout,
                         str(self.repo) + "\n")

    def test_invocation_normalizes_alias_and_nested_target(self) -> None:
        linked = self.linked_worktree()
        nested = linked / "nested"
        nested.mkdir()
        alias = self.root / "linked alias"
        alias.symlink_to(nested, target_is_directory=True)
        self.assertEqual(self.call("agentstack_resolve_invocation_project_key", alias).stdout,
                         str(self.repo) + "\n")

    def test_invocation_non_git_uses_only_explicit_fallback(self) -> None:
        workspace = self.non_git_workspace()
        ambient = {"AGENTSTACK_PROJECT_KEY": "foreign", "PROJECT_KEY": "foreign"}
        self.assertEqual(self.call("agentstack_resolve_invocation_project_key", workspace,
                                   "", "configured-fallback", env=ambient).stdout,
                         "configured-fallback\n")
        self.assertEqual(self.call("agentstack_resolve_invocation_project_key", workspace,
                                   env=ambient).stdout, str(workspace) + "\n")

    def test_invocation_non_git_explicit_selection_beats_fallback(self) -> None:
        workspace = self.non_git_workspace()
        self.assertEqual(self.call("agentstack_resolve_invocation_project_key", workspace,
                                   "selected", "fallback").stdout, "selected\n")

    def test_invocation_fallback_path_is_normalized(self) -> None:
        workspace = self.non_git_workspace()
        alias = self.root / "fallback alias"
        alias.symlink_to(self.other, target_is_directory=True)
        self.assertEqual(self.call("agentstack_resolve_invocation_project_key", workspace,
                                   "", alias).stdout, str(self.other) + "\n")

    def test_invocation_never_reads_or_sources_installed_environment(self) -> None:
        workspace = self.non_git_workspace()
        install = self.home / ".agentstack"
        install.mkdir()
        marker = self.root / "UNEXPECTED_ENV_EXECUTION"
        (install / "env.sh").write_text(
            f'touch "{marker}"\nexport AGENTSTACK_PROJECT_KEY=foreign\n', encoding="utf-8")
        self.assertEqual(self.call("agentstack_resolve_invocation_project_key", workspace).stdout,
                         str(workspace) + "\n")
        self.assertFalse(marker.exists())

    def test_invocation_invalid_target_rejects_even_with_explicit_selection(self) -> None:
        regular = self.root / "regular-file"
        regular.touch()
        for target in ("", self.root / "missing", regular):
            with self.subTest(target=str(target)):
                result = self.call("agentstack_resolve_invocation_project_key", target,
                                   "selected", "fallback", check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")

    def test_invocation_corrupt_metadata_does_not_use_non_git_fallback(self) -> None:
        workspace = self.non_git_workspace()
        (workspace / ".git").write_text("gitdir: /nonexistent/orrery-test-metadata\n",
                                        encoding="utf-8")
        nested = workspace / "nested"
        nested.mkdir()
        result = self.call("agentstack_resolve_invocation_project_key", nested,
                           "", "fallback", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("cannot resolve repository metadata", result.stderr)

    def test_invocation_dangling_git_marker_does_not_use_fallback(self) -> None:
        workspace = self.non_git_workspace()
        (workspace / ".git").symlink_to(self.root / "missing-metadata")
        result = self.call("agentstack_resolve_invocation_project_key", workspace,
                           "", "fallback", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_invocation_missing_git_does_not_use_fallback(self) -> None:
        workspace = self.non_git_workspace()
        empty_bin = self.root / "empty-bin"
        empty_bin.mkdir()
        result = self.call("agentstack_resolve_invocation_project_key", workspace,
                           "", "fallback", env={"PATH": str(empty_bin)}, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("git is required", result.stderr)

    def test_invocation_explicit_selection_does_not_need_git(self) -> None:
        empty_bin = self.root / "empty-bin"
        empty_bin.mkdir()
        self.assertEqual(self.call("agentstack_resolve_invocation_project_key", self.repo,
                                   "logical-key", env={"PATH": str(empty_bin)}).stdout,
                         "logical-key\n")

    def test_invocation_normalization_failure_propagates(self) -> None:
        result = self.call("agentstack_resolve_invocation_project_key", self.repo,
                           self.root / "not-created", env={"AGENTSTACK_PYTHON": "/missing/python"},
                           check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_invocation_bare_and_separate_git_directory(self) -> None:
        bare = self.root / "bare.git"
        self.git("init", "--bare", "-q", str(bare))
        work = self.root / "separate work"
        metadata = self.root / "separate metadata"
        self.make_repo(work, metadata)
        for target, expected in ((bare, bare), (work, metadata)):
            with self.subTest(target=target.name):
                self.assertEqual(self.call("agentstack_resolve_invocation_project_key", target,
                                           "", "fallback").stdout, str(expected) + "\n")

    def test_invocation_cli_returns_the_same_key_as_library(self) -> None:
        result = subprocess.run(
            [BASH, str(SCRIPT), "resolve-invocation-project-key", str(self.repo),
             "chosen", "fallback"], cwd=self.root, env=self.env,
            capture_output=True, text=True, check=True, timeout=20)
        self.assertEqual(result.stdout, self.call("agentstack_resolve_invocation_project_key",
                                                  self.repo, "chosen", "fallback").stdout)

    def test_invocation_cli_rejects_missing_or_excess_arguments(self) -> None:
        for args in ([], [str(self.repo), "key", "fallback", "excess"]):
            with self.subTest(arguments=args):
                result = subprocess.run(
                    [BASH, str(SCRIPT), "resolve-invocation-project-key", *args],
                    cwd=self.root, env=self.env, capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")

    def test_invocation_does_not_mutate_caller_context(self) -> None:
        code = ('source "$1"; before=$PWD; '
                'agentstack_resolve_invocation_project_key "$2" >/dev/null; '
                '[ "$PWD" = "$before" ]; '
                '[ "$AGENTSTACK_PROJECT_KEY" = foreign ]; [ "$PROJECT_KEY" = other ]; '
                '[ "$AGENTSTACK_PROTECTED_ROOTS" = roots ]; '
                '[ "$AGENTSTACK_PROJECT_CONTEXT" = 1 ]')
        result = subprocess.run(
            [BASH, "-euo", "pipefail", "-c", code, "test", str(SCRIPT), str(self.repo)],
            cwd=self.root, env={**self.env, "AGENTSTACK_PROJECT_KEY": "foreign",
                               "PROJECT_KEY": "other", "AGENTSTACK_PROTECTED_ROOTS": "roots",
                               "AGENTSTACK_PROJECT_CONTEXT": "1"},
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")


    def test_invocation_git_execution_failure_is_not_non_git(self) -> None:
        workspace = self.non_git_workspace()
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        fake_git = fake_bin / "git"
        fake_git.write_text("#!/bin/sh\necho 'fatal: synthetic IO failure' >&2\nexit 23\n",
                            encoding="utf-8")
        fake_git.chmod(0o755)
        result = self.call("agentstack_resolve_invocation_project_key", workspace,
                           "", "fallback", env={"PATH": str(fake_bin) + os.pathsep + self.env["PATH"]},
                           check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("git could not inspect", result.stderr)


if __name__ == "__main__":
    unittest.main()
