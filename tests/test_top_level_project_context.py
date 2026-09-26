"""Top-level launcher boundary with real Git and isolated external-command doubles."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
PROVIDERS = ("claude", "codex", "gemini")
LAUNCHERS = {"claude": "agent-start", "codex": "agent-start-codex", "gemini": "agent-start-gemini"}
KEYS = ("AGENTSTACK_PROJECT_KEY", "PROJECT_KEY", "AGENTSTACK_PROJECT_REPOSITORY",
        "AGENTSTACK_PROJECT_WORK_DIR", "AGENTSTACK_PROJECT_WORKTREE_ROOT",
        "AGENTSTACK_PROTECTED_ROOTS", "AGENTSTACK_PROJECT_CONTEXT",
        "AGENTSTACK_LOOKUP_PROJECT_KEY", "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")


@unittest.skipIf(os.name == "nt", "POSIX top-level launchers")
class TopLevelProjectContextTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="orrery-top-launch-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.install = self.home / "install"
        self.bin = self.install / "bin"
        (self.bin / "lib").mkdir(parents=True)
        (self.install / "hooks").mkdir()
        self.output = self.root / "output"
        self.output.mkdir()
        self.env = {
            "PATH": os.environ.get("PATH", os.defpath), "HOME": str(self.home),
            "TMPDIR": str(self.root), "LC_ALL": "C", "SHELL": "/usr/bin/true",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0", "AGENTSTACK_PYTHON": sys.executable,
            "AGENTSTACK_HOME": str(self.install),
            "AGENTSTACK_LABEL_PREFIX": f"org.agentstack.top-level-context.{self.root.name}",
            "TEST_OUTPUT": str(self.output), "TEST_KEYS": json.dumps(KEYS),
        }
        self.repo = self.root / "repo A"
        self.other = self.root / "repo B"
        for path in (self.repo, self.other):
            self.git("-c", "init.defaultBranch=main", "init", "-q", str(path))
            self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                     "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-qm", "fixture", cwd=path)
        self.linked = self.root / "linked tree"
        self.git("worktree", "add", "-q", "--detach", str(self.linked), cwd=self.repo)
        self.plain = self.root / "plain workspace"
        self.plain.mkdir()
        self.snapshot = self.executable("snapshot", '''
import json, os, pathlib, sys
out = pathlib.Path(os.environ["TEST_OUTPUT"]) / (sys.argv[1] + ".json")
temporary = out.with_suffix(".tmp")
temporary.write_text(json.dumps({"cwd": os.getcwd(), "args": sys.argv[2:],
    "env": {key: os.environ.get(key) for key in json.loads(os.environ["TEST_KEYS"])},
    "identity": {key: os.environ.get(key) for key in ("AGENT_NAME", "PARENT_AGENT", "CHILD_REGISTRATION_TOKEN", "AGENTSTACK_RESERVED_IDENTITY")},
    "api_key": os.environ.get("OPENAI_API_KEY")}))
temporary.replace(out)
''')
        self.tmux = self.executable("tmux-double", '''
import json, os, pathlib, subprocess, sys
args = sys.argv[1:]
out = pathlib.Path(os.environ["TEST_OUTPUT"])
if args[0] == "display-message":
    print("TestAgent")
    raise SystemExit(0)
if args[0] == "has-session":
    raise SystemExit(1)
if args[0] != "new-session":
    raise SystemExit("unexpected tmux mutation: " + repr(args))
keys = json.loads(os.environ["TEST_KEYS"])
(out / "tmux-client.json").write_text(json.dumps({key: os.environ.get(key) for key in keys}))
env = dict(os.environ)
env.update(json.loads(os.environ["TEST_SERVER_ENV"]))
i = 1
cwd = None
while i < len(args) - 1:
    option, value = args[i:i+2]
    if option == "-e":
        key, value = value.split("=", 1)
        env[key] = value
    elif option == "-c":
        cwd = value
    elif option != "-s":
        raise SystemExit("unexpected option: " + option)
    i += 2
env["TMUX"] = "isolated-double,1,0"
subprocess.run(["/bin/bash", "-c", args[-1]], cwd=cwd, env=env, check=True, timeout=20)
''')
        self.env["TEST_TMUX"] = str(self.tmux)
        self.env["TEST_SNAPSHOT"] = str(self.snapshot)
        self.stale = {
            "AGENTSTACK_PROJECT_KEY": str(self.other), "PROJECT_KEY": "ambient-namespace",
            "AGENTSTACK_PROJECT_REPOSITORY": str(self.other),
            "AGENTSTACK_PROJECT_WORK_DIR": str(self.other),
            "AGENTSTACK_PROJECT_WORKTREE_ROOT": str(self.other),
            "AGENTSTACK_PROTECTED_ROOTS": str(self.other), "AGENTSTACK_PROJECT_CONTEXT": "1",
            "AGENTSTACK_LOOKUP_PROJECT_KEY": "stale-lookup",
            "GIT_DIR": str(self.other / ".git"), "GIT_WORK_TREE": str(self.other),
            "GIT_COMMON_DIR": str(self.other / ".git"),
            "AGENT_NAME": "StaleAgent", "PARENT_AGENT": "StaleParent",
            "CHILD_REGISTRATION_TOKEN": "stale-token", "AGENTSTACK_RESERVED_IDENTITY": "1",
        }
        self.env["TEST_SERVER_ENV"] = json.dumps(self.stale)
        for name in LAUNCHERS.values():
            shutil.copy2(ROOT / "bin" / name, self.bin / name)
        shutil.copy2(ROOT / "hooks/project-context.sh", self.install / "hooks/project-context.sh")
        # Only external command discovery is replaced; argument/context helpers are real.
        (self.bin / "lib/agentstack-launch.sh").write_text(
            '. ' + shlex.quote(str(ROOT / "bin/lib/agentstack-launch.sh")) + '\n'
            'ags_resolve_tmux() { printf "%s\\n" "$TEST_TMUX"; }\n'
            'ags_pick_dir() { printf "%s\\n" "$TEST_PICK_DIR"; }\n')
        (self.bin / "lib/agentstack-register.sh").write_text('''
ags_pick_adjective_scientist_name() { printf 'TestAgent\\n'; }
ags_mail_load_token() { :; }
ags_mcp_call() { :; }
ags_start_mail_watcher() { :; }
ags_record_managed_agent() { :; }
ags_registration_token_file() { :; }
ags_register_session() {
  "$TEST_SNAPSHOT" registration "$@"
  AGS_REGISTERED_AGENT_NAME=TestAgent
}
''')
        for provider in ("codex", "gemini"):
            (self.bin / f"agentstack-{provider}-bootstrap").write_text('''
"$TEST_SNAPSHOT" bootstrap "$@"
[[ "${TEST_BOOTSTRAP_FAIL:-0}" != 1 ]] || return 1
export AGENT_NAME=TestAgent
''')
        for provider in PROVIDERS:
            executable = self.executable(provider, '''
import os, subprocess, sys
subprocess.run([os.environ["TEST_SNAPSHOT"], "provider", *sys.argv[1:]], check=True)
raise SystemExit(int(os.environ.get("TEST_PROVIDER_EXIT", "0")))
''')
            self.env[f"AGENTSTACK_{provider.upper()}_BIN"] = str(executable)
        (self.install / "env.sh").write_text(
            'export AGENTSTACK_PROJECT_KEY=installed-foreign\n'
            'export PROJECT_KEY=installed-foreign\n'
            f'export AGENTSTACK_PROTECTED_ROOTS={shlex.quote(str(self.other))}\n')

    def executable(self, name: str, source: str) -> Path:
        path = self.root / name
        path.write_text(f"#!{sys.executable}\n" + source)
        path.chmod(0o755)
        return path

    def git(self, *args: str, cwd: Path | None = None) -> None:
        subprocess.run(["git", "-c", "core.hooksPath=" + str(self.root / "no-hooks"),
                        "-c", "maintenance.auto=false", "-c", "gc.auto=0", *args],
                       cwd=cwd or self.root, env=self.env, capture_output=True,
                       text=True, check=True, timeout=20)

    def run_launcher(self, provider: str, *args: str | Path, inside: bool = False,
                     extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        for path in self.output.glob("*.json"):
            path.unlink()
        env = {**self.env, **self.stale, "TMUX": "isolated-double,1,0" if inside else "",
               **(extra or {})}
        return subprocess.run(["/bin/bash", str(self.bin / LAUNCHERS[provider]), *map(str, args)],
                              cwd=self.root, env=env, capture_output=True, text=True, timeout=30)

    def read(self, name: str) -> dict:
        return json.loads((self.output / (name + ".json")).read_text())

    def assert_context(self, actual: dict, target: Path, *, key: str | None = None,
                       repository: Path | None = None, worktree: Path | None = None) -> None:
        repository = self.repo if repository is None else repository
        worktree = target if worktree is None else worktree
        env = actual["env"]
        self.assertEqual(env["AGENTSTACK_PROJECT_KEY"], key or str(repository))
        self.assertEqual(env["PROJECT_KEY"], key or str(repository))
        self.assertEqual(env["AGENTSTACK_PROJECT_REPOSITORY"], str(repository))
        self.assertEqual(env["AGENTSTACK_PROJECT_WORK_DIR"], str(target))
        self.assertEqual(env["AGENTSTACK_PROJECT_WORKTREE_ROOT"], str(worktree))
        self.assertEqual(env["AGENTSTACK_PROTECTED_ROOTS"], str(worktree))
        for name in ("AGENTSTACK_PROJECT_CONTEXT", "AGENTSTACK_LOOKUP_PROJECT_KEY",
                     "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
            self.assertFalse(env[name], (name, env[name]))

    def test_all_launchers_replace_stale_context_inside_and_outside_tmux(self) -> None:
        for provider in PROVIDERS:
            for inside in (False, True):
                for target in (self.repo, self.linked):
                    with self.subTest(provider=provider, inside=inside, target=target):
                        result = self.run_launcher(provider, target, inside=inside)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assert_context(self.read("provider"), target)
                        boundary = self.read("registration" if provider == "claude" else "bootstrap")
                        self.assert_context(boundary, target)
                        self.assertFalse(boundary["identity"]["CHILD_REGISTRATION_TOKEN"])
                        self.assertFalse(boundary["identity"]["AGENTSTACK_RESERVED_IDENTITY"])
                        if not inside:
                            self.assertTrue(all(value is None for value in self.read("tmux-client").values()))

    def test_explicit_namespace_is_literal_and_never_selects_protected_roots(self) -> None:
        key = 'custom:$(touch UNEXPECTED)-"quoted";value'
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                result = self.run_launcher(provider, "--project-key", key, self.linked)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_context(self.read("provider"), self.linked, key=key)
        self.assertFalse((self.root / "UNEXPECTED").exists())

    def test_nested_alias_target_keeps_cwd_but_protects_the_whole_worktree(self) -> None:
        nested = self.linked / "nested"
        nested.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(nested, target_is_directory=True)
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                result = self.run_launcher(provider, alias)
                self.assertEqual(result.returncode, 0, result.stderr)
                record = self.read("provider")
                self.assertEqual(record["cwd"], str(nested))
                self.assert_context(record, nested, worktree=self.linked)

    def test_independent_clone_is_a_separate_project(self) -> None:
        clone = self.root / "clone"
        self.git("clone", "-q", str(self.repo), str(clone))
        result = self.run_launcher("codex", clone)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_context(self.read("provider"), clone, repository=clone)

    def test_non_git_defaults_to_target_and_explicit_key_only_changes_namespace(self) -> None:
        for provider in PROVIDERS:
            for args, key in (((self.plain,), str(self.plain)),
                              (("--project-key", "plain-key", self.plain), "plain-key")):
                with self.subTest(provider=provider, key=key):
                    result = self.run_launcher(provider, *args)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    env = self.read("provider")["env"]
                    self.assertEqual(env["AGENTSTACK_PROJECT_KEY"], key)
                    self.assertEqual(env["PROJECT_KEY"], key)
                    self.assertEqual(env["AGENTSTACK_PROJECT_REPOSITORY"], "")
                    self.assertEqual(env["AGENTSTACK_PROJECT_WORK_DIR"], str(self.plain))
                    self.assertEqual(env["AGENTSTACK_PROTECTED_ROOTS"], str(self.plain))

    def test_bad_arguments_fail_before_registration_or_tmux(self) -> None:
        invalid = (("--project-key",), ("--project-key", "", self.repo),
                   ("--project-key", "a", "--project-key", "b", self.repo),
                   ("--unknown",), (self.repo, self.other), (self.root / "missing",), ("",))
        for provider in PROVIDERS:
            for args in invalid:
                with self.subTest(provider=provider, args=args):
                    result = self.run_launcher(provider, *args)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(list(self.output.glob("*.json")))

    def test_broken_git_is_not_rescued_by_an_explicit_key(self) -> None:
        (self.plain / ".git").write_text("gitdir: /missing/orrery-metadata\n")
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                result = self.run_launcher(provider, "--project-key", "explicit", self.plain)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(list(self.output.glob("*.json")))

    def test_colon_in_protected_root_is_rejected_without_partial_launch(self) -> None:
        path = self.root / "ambiguous:root"
        path.mkdir()
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                result = self.run_launcher(provider, path)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("legacy environment", result.stderr)
                self.assertFalse(list(self.output.glob("*.json")))

    def test_control_character_in_key_is_rejected(self) -> None:
        for key in ("bad\nkey", "bad\n", "bad\r"):
            with self.subTest(key=key):
                result = self.run_launcher("codex", "--project-key", key, self.repo)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(list(self.output.glob("*.json")))

    def test_missing_python_cannot_launch_with_stale_context(self) -> None:
        result = self.run_launcher("codex", self.repo, extra={"AGENTSTACK_PYTHON": "/missing/python"})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(list(self.output.glob("*.json")))

    def test_gemini_dry_run_resolves_context_without_external_side_effects(self) -> None:
        for args in (("--dry-run", "--project-key", "selected", self.linked),
                     ("--project-key", "selected", "--dry-run", self.linked)):
            with self.subTest(args=args):
                result = self.run_launcher("gemini", *args, extra={"AGENTSTACK_GEMINI_BIN": "/missing/agy"})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("project=selected", result.stdout)
                self.assertIn("repository=" + str(self.repo), result.stdout)
                self.assertFalse(list(self.output.glob("*.json")))

    def test_codex_permissions_vault_and_oauth_behavior_do_not_change(self) -> None:
        result = self.run_launcher("codex", self.repo, inside=True,
                                   extra={"AGENTSTACK_VAULT": str(self.plain), "OPENAI_API_KEY": "must-strip"})
        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.read("provider")
        self.assertIsNone(record["api_key"])
        self.assertEqual(record["args"], ["-C", str(self.repo), "--sandbox", "workspace-write",
                                          "--ask-for-approval", "on-request", "--add-dir", str(self.plain)])

    def test_gemini_repl_exit_status_and_model_effort_are_preserved(self) -> None:
        result = self.run_launcher("gemini", self.repo, inside=True,
                                   extra={"TEST_PROVIDER_EXIT": "7", "AGENTSTACK_GEMINI_MODEL": "fixture-model",
                                          "AGENTSTACK_GEMINI_EFFORT": "low"})
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(self.read("provider")["args"], ["--model", "fixture-model", "--effort", "low"])

    def test_bootstrap_failure_never_runs_provider(self) -> None:
        for provider in ("codex", "gemini"):
            with self.subTest(provider=provider):
                result = self.run_launcher(provider, self.repo, inside=True, extra={"TEST_BOOTSTRAP_FAIL": "1"})
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.output / "provider.json").exists())

    @unittest.skipUnless(shutil.which("tmux"), "real tmux is exercised in macOS CI")
    def test_real_tmux_socket_overrides_stale_server_without_changing_its_global_key(self) -> None:
        real = shutil.which("tmux")
        socket = str(self.root / "tmux.sock")
        command = [real, "-S", socket, "-f", os.devnull]
        # The outer launcher uses true to avoid an interactive shell; tmux needs a real command shell.
        server_env = {**self.env, **self.stale, "SHELL": "/bin/bash"}
        subprocess.run([*command, "new-session", "-d", "-s", "seed", "-c", str(self.other),
                        "sleep 60"], env=server_env, check=True, capture_output=True, timeout=10)
        try:
            wrapper = self.executable("tmux-isolated", """
import os, subprocess, sys
args = sys.argv[1:]
if args[0] == "new-session":
    args.insert(1, "-d")
raise SystemExit(subprocess.run([os.environ["TEST_REAL_TMUX"], "-S", os.environ["TEST_SOCKET"],
                                "-f", os.devnull, *args], timeout=15).returncode)
""")
            for provider in PROVIDERS:
                with self.subTest(provider=provider):
                    result = self.run_launcher(provider, self.linked, extra={
                        "TEST_TMUX": str(wrapper), "TEST_REAL_TMUX": real, "TEST_SOCKET": socket})
                    self.assertEqual(result.returncode, 0, result.stderr)
                    deadline = time.monotonic() + 15
                    while not (self.output / "provider.json").exists() and time.monotonic() < deadline:
                        time.sleep(0.05)
                    self.assert_context(self.read("provider"), self.linked)
            result = subprocess.run([*command, "show-environment", "-g", "AGENTSTACK_PROJECT_KEY"],
                                    env=self.env, check=True, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.stdout.strip(), "AGENTSTACK_PROJECT_KEY=" + str(self.other))
        finally:
            subprocess.run([*command, "kill-server"], env=self.env, capture_output=True, timeout=10)

    def test_failed_resolution_leaves_callers_environment_unchanged(self) -> None:
        script = '. "$1"; BIN_DIR="$2"; before="$(export -p)"; '
        script += 'if ags_prepare_top_level_context "$3"; then exit 9; fi; [ "$(export -p)" = "$before" ]'
        result = subprocess.run(["/bin/bash", "-euo", "pipefail", "-c", script, "test",
                                 str(ROOT / "bin/lib/agentstack-launch.sh"), str(self.bin), str(self.root / "missing")],
                                cwd=self.root, env={**self.env, **self.stale}, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
