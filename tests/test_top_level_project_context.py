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

from _installed_client_payload import copy_client_library

ROOT = Path(__file__).resolve().parents[1]
PROVIDERS = ("claude", "codex", "gemini")
LAUNCHERS = {"claude": "agent-start", "codex": "agent-start-codex", "gemini": "agent-start-gemini"}
KEYS = ("AGENTSTACK_PROJECT_KEY", "PROJECT_KEY", "AGENTSTACK_PROJECT_REPOSITORY",
        "AGENTSTACK_PROJECT_WORK_DIR", "AGENTSTACK_PROJECT_WORKTREE_ROOT",
        "AGENTSTACK_PROTECTED_ROOTS", "AGENTSTACK_EXTRA_PROTECTED_ROOTS", "AGENTSTACK_PROTECTION_CONTEXT", "AGENTSTACK_PROJECT_CONTEXT",
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
if args[0] == "set-environment":
    if len(args) != 5 or args[1:3] != ["-r", "-t"] or not args[3].startswith("="):
        raise SystemExit("non-session environment mutation: " + repr(args))
    if args[4] not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
        raise SystemExit("unexpected environment removal: " + repr(args))
    raise SystemExit(0)
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
env.update(json.loads(os.environ.get("TEST_SHELL_STARTUP_ENV", "{}")))
cwd = os.environ.get("TEST_SHELL_STARTUP_CWD", cwd)
env["TMUX"] = "isolated-double,1,0"
subprocess.run(["/bin/bash", "-c", args[-1]], cwd=cwd, env=env, check=True, timeout=20)
''')
        self.env["TEST_TMUX"] = str(self.tmux)
        self.env["TEST_SNAPSHOT"] = str(self.snapshot)
        # A project key is a coordination namespace, not repository ownership.
        # The live namespace is intentional even when the workspace provenance
        # inherited beside it is stale and points at another repository.
        self.ambient = {
            "AGENTSTACK_PROJECT_KEY": "live-project",
            "PROJECT_KEY": "stale-project-alias",
            "AGENTSTACK_PROJECT_REPOSITORY": str(self.other),
            "AGENTSTACK_PROJECT_WORK_DIR": str(self.other),
            "AGENTSTACK_PROJECT_WORKTREE_ROOT": str(self.other),
            "AGENTSTACK_PROTECTED_ROOTS": str(self.other),
            "AGENTSTACK_PROJECT_CONTEXT": "1",
            "AGENTSTACK_LOOKUP_PROJECT_KEY": "stale-lookup",
            "GIT_DIR": str(self.other / ".git"),
            "GIT_WORK_TREE": str(self.other),
            "GIT_COMMON_DIR": str(self.other / ".git"),
            "AGENT_NAME": "StaleAgent",
            "PARENT_AGENT": "StaleParent",
            "CHILD_REGISTRATION_TOKEN": "stale-token",
            "AGENTSTACK_RESERVED_IDENTITY": "1",
        }
        # A pre-existing tmux server can hold a different namespace and stale
        # repository selectors. Per-session launch context must override these
        # without mutating the server-global environment.
        self.server_stale = {
            **self.ambient,
            "AGENTSTACK_PROJECT_KEY": "server-stale-project",
            "PROJECT_KEY": "server-stale-project",
        }
        self.env["TEST_SERVER_ENV"] = json.dumps(self.server_stale)
        for name in LAUNCHERS.values():
            shutil.copy2(ROOT / "bin" / name, self.bin / name)
        shutil.copy2(ROOT / "hooks/project-context.sh", self.install / "hooks/project-context.sh")
        shutil.copy2(ROOT / "hooks/installed-env.py", self.install / "hooks/installed-env.py")
        copy_client_library(self.install)
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
import os, subprocess, sys, time
subprocess.run([os.environ["TEST_SNAPSHOT"], "provider", *sys.argv[1:]], check=True)
if os.environ.get("TEST_PROVIDER_HOLD") == "1":
    time.sleep(60)
raise SystemExit(int(os.environ.get("TEST_PROVIDER_EXIT", "0")))
''')
            self.env[f"AGENTSTACK_{provider.upper()}_BIN"] = str(executable)
        (self.install / "env.sh").write_text(
            'export AGENTSTACK_PROJECT_KEY=installed-project\n'
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
                     ambient: bool = True,
                     extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        for path in self.output.glob("*.json"):
            path.unlink()
        env = {**self.env, **(self.ambient if ambient else {}),
               "TMUX": "isolated-double,1,0" if inside else "", **(extra or {})}
        return subprocess.run(["/bin/bash", str(self.bin / LAUNCHERS[provider]), *map(str, args)],
                              cwd=self.root, env=env, capture_output=True, text=True, timeout=30)

    def read(self, name: str) -> dict:
        return json.loads((self.output / (name + ".json")).read_text())

    def assert_context(self, actual: dict, target: Path, *, key: str = "live-project",
                       repository: Path | None = None, worktree: Path | None = None) -> None:
        repository = self.repo if repository is None else repository
        worktree = target if worktree is None else worktree
        env = actual["env"]
        self.assertEqual(env["AGENTSTACK_PROJECT_KEY"], key)
        self.assertEqual(env["PROJECT_KEY"], key)
        self.assertEqual(env["AGENTSTACK_PROJECT_REPOSITORY"], str(repository))
        self.assertEqual(env["AGENTSTACK_PROJECT_WORK_DIR"], str(target))
        self.assertEqual(env["AGENTSTACK_PROJECT_WORKTREE_ROOT"], str(worktree))
        self.assertEqual(env["AGENTSTACK_PROTECTED_ROOTS"], str(target))
        self.assertEqual(env["AGENTSTACK_PROTECTION_CONTEXT"], "workspace-v1")
        for name in ("AGENTSTACK_PROJECT_CONTEXT", "AGENTSTACK_LOOKUP_PROJECT_KEY",
                     "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
            self.assertFalse(env[name], (name, env[name]))

    def test_all_launchers_keep_namespace_but_replace_stale_workspace_provenance(self) -> None:
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
                result = self.run_launcher(provider, self.linked, extra={"AGENTSTACK_PROJECT_KEY": key})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_context(self.read("provider"), self.linked, key=key)
        self.assertFalse((self.root / "UNEXPECTED").exists())

    def test_nested_alias_target_protects_only_selected_folder(self) -> None:
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

    def test_independent_clone_keeps_namespace_but_tracks_repository_separately(self) -> None:
        clone = self.root / "clone"
        self.git("clone", "-q", str(self.repo), str(clone))
        result = self.run_launcher("codex", clone)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_context(self.read("provider"), clone, repository=clone)

    def test_installed_project_key_remains_valid_for_a_different_repository(self) -> None:
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                result = self.run_launcher(provider, self.repo, ambient=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_context(self.read("provider"), self.repo, key="installed-project")

    def test_live_project_key_alias_beats_installed_namespace(self) -> None:
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                result = self.run_launcher(
                    provider, self.repo, ambient=False,
                    extra={"PROJECT_KEY": "live-project-alias"},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_context(self.read("provider"), self.repo, key="live-project-alias")

    def test_non_git_keeps_selected_namespace_and_explicit_key_overrides_it(self) -> None:
        for provider in PROVIDERS:
            for key in ("live-project", "plain-key"):
                with self.subTest(provider=provider, key=key):
                    result = self.run_launcher(provider, self.plain, extra={"AGENTSTACK_PROJECT_KEY": key})
                    self.assertEqual(result.returncode, 0, result.stderr)
                    env = self.read("provider")["env"]
                    self.assertEqual(env["AGENTSTACK_PROJECT_KEY"], key)
                    self.assertEqual(env["PROJECT_KEY"], key)
                    self.assertEqual(env["AGENTSTACK_PROJECT_REPOSITORY"], "")
                    self.assertEqual(env["AGENTSTACK_PROJECT_WORK_DIR"], str(self.plain))
                    self.assertEqual(env["AGENTSTACK_PROTECTED_ROOTS"], str(self.plain))

    def test_explicit_physical_project_key_is_namespace_not_repository_authority(self) -> None:
        namespace_alias = self.root / "namespace alias"
        namespace_alias.symlink_to(self.other, target_is_directory=True)
        result = self.run_launcher("codex", self.repo, extra={"AGENTSTACK_PROJECT_KEY": str(namespace_alias)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_context(self.read("provider"), self.repo, key=str(namespace_alias))

    def test_broken_git_is_not_rescued_by_an_explicit_key(self) -> None:
        (self.plain / ".git").write_text("gitdir: /missing/orrery-metadata\n")
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                result = self.run_launcher(provider, self.plain, extra={"AGENTSTACK_PROJECT_KEY": "explicit"})
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(list(self.output.glob("*.json")))

    def test_broken_ancestor_git_metadata_stops_nested_launch(self) -> None:
        nested = self.plain / "nested"
        nested.mkdir()
        (self.plain / ".git").write_text("gitdir: /missing/orrery-metadata\n")
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                result = self.run_launcher(provider, nested, extra={"AGENTSTACK_PROJECT_KEY": "selected"})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("cannot resolve repository metadata", result.stderr)
                self.assertFalse(list(self.output.glob("*.json")))

    def test_empty_ancestor_git_directory_does_not_claim_non_git_workspace(self) -> None:
        nested = self.plain / "nested"
        nested.mkdir()
        (self.plain / ".git").mkdir()
        result = self.run_launcher("codex", nested)
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = self.read("provider")["env"]
        self.assertEqual(actual["AGENTSTACK_PROJECT_WORK_DIR"], str(nested))
        self.assertEqual(actual["AGENTSTACK_PROTECTED_ROOTS"], str(nested))
        self.assertEqual(actual["AGENTSTACK_PROJECT_WORKTREE_ROOT"], "")

    def test_repository_config_cannot_redirect_protection_outside_launch_target(self) -> None:
        self.git("config", "core.worktree", str(self.other), cwd=self.repo)
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                result = self.run_launcher(provider, self.repo)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("outside its resolved Git worktree", result.stderr)
                self.assertFalse(list(self.output.glob("*.json")))

    def test_installed_git_selectors_cannot_redirect_workspace(self) -> None:
        with (self.install / "env.sh").open("a") as stream:
            for name in ("GIT_DIR", "GIT_COMMON_DIR"):
                stream.write(f"export {name}={shlex.quote(str(self.other / '.git'))}\n")
            stream.write(f"export GIT_WORK_TREE={shlex.quote(str(self.other))}\n")
        result = self.run_launcher("codex", self.linked, ambient=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_context(self.read("provider"), self.linked, key="installed-project")

    def test_target_directory_is_protected_without_inventing_a_namespace(self) -> None:
        (self.install / "env.sh").unlink()
        result = self.run_launcher("codex", self.plain, ambient=False, inside=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.read("provider")
        self.assertEqual(record["env"]["AGENTSTACK_PROJECT_KEY"], "")
        self.assertEqual(record["env"]["PROJECT_KEY"], "")
        self.assertEqual(record["env"]["AGENTSTACK_PROJECT_WORK_DIR"], str(self.plain))
        self.assertEqual(record["env"]["AGENTSTACK_PROTECTED_ROOTS"], str(self.plain))

    def test_colon_in_protected_root_is_rejected_without_partial_launch(self) -> None:
        path = self.root / "ambiguous:root"
        path.mkdir()
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                result = self.run_launcher(provider, path)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("legacy environment", result.stderr)
                self.assertFalse(list(self.output.glob("*.json")))

    def test_unsupported_characters_in_workspace_or_extra_roots_stop_before_launch(self) -> None:
        for marker, name in (("[", "folder[ab]"), ("*", "folder*"), ("?", "folder?"), ("\\", "folder\\child")):
            path = self.root / name
            path.mkdir()
            for provider in PROVIDERS:
                for source in ("workspace", "extra"):
                    with self.subTest(marker=marker, provider=provider, source=source):
                        target = path if source == "workspace" else self.repo
                        extras = {"AGENTSTACK_EXTRA_PROTECTED_ROOTS": str(path)} if source == "extra" else {}
                        result = self.run_launcher(provider, target, extra=extras)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn("characters unsupported by ORRERY Mail " + repr(marker), result.stderr)
                        self.assertIn(str(path), result.stderr)
                        self.assertFalse(list(self.output.glob("*.json")))
                        self.assertFalse(list(self.root.glob("agent-start*")))

    def test_character_check_uses_physical_roots_and_allows_standalone_closing_bracket(self) -> None:
        for index, name in enumerate(("folder[ab]", "folder\\child")):
            target = self.root / name
            target.mkdir()
            alias = self.root / f"ordinary-alias-{index}"
            alias.symlink_to(target, target_is_directory=True)
            for source in ("workspace", "extra"):
                with self.subTest(name=name, source=source):
                    extras = {"AGENTSTACK_EXTRA_PROTECTED_ROOTS": str(alias)} if source == "extra" else {}
                    result = self.run_launcher("codex", alias if source == "workspace" else self.repo, extra=extras)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(str(target), result.stderr)
                    self.assertFalse(list(self.output.glob("*.json")))
        allowed = self.root / "folder]"
        allowed.mkdir()
        result = self.run_launcher("codex", allowed, inside=True,
                                   extra={"AGENTSTACK_EXTRA_PROTECTED_ROOTS": str(allowed)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read("provider")["env"]["AGENTSTACK_PROTECTED_ROOTS"], str(allowed))

    def test_explicit_additional_protected_roots_override_installed_roots_and_deduplicate(self) -> None:
        extra_roots = f"{self.plain}:{self.repo}"
        result = self.run_launcher("codex", self.repo, inside=True,
                                   extra={"AGENTSTACK_EXTRA_PROTECTED_ROOTS": extra_roots})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read("provider")["env"]["AGENTSTACK_PROTECTED_ROOTS"], extra_roots)

    def test_ancestor_vault_root_uses_canonical_managed_reservation_path(self) -> None:
        target = self.plain / "project"
        target.mkdir()
        note = target / "note.md"
        result = self.run_launcher("codex", target, ambient=False, inside=True,
                                   extra={"AGENTSTACK_EXTRA_PROTECTED_ROOTS": str(self.plain)})
        self.assertEqual(result.returncode, 0, result.stderr)
        launch_env = self.read("provider")["env"]
        self.assertEqual(launch_env["AGENTSTACK_PROTECTED_ROOTS"], f"{self.plain}:{target}")
        script = '. "$1"; reservation_resolve_tool_context "$2"; printf "%s|%s\\n" "$MATCHED_ROOT" "$REL_PATH"'
        hook = subprocess.run(
            ["/bin/bash", "-e", "-c", script, "test", str(ROOT / "hooks/reservation-common.sh"),
             json.dumps({"tool_input": {"file_path": str(note)}})],
            cwd=target, env={**self.env, **{key: value for key, value in launch_env.items() if value is not None}},
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(hook.returncode, 0, hook.stderr)
        self.assertEqual(hook.stdout.strip(), f"{self.plain}|{note}")

    def test_legacy_physical_namespace_is_not_implicitly_protected(self) -> None:
        (self.install / "env.sh").write_text(
            f"export AGENTSTACK_PROJECT_KEY={shlex.quote(str(self.plain))}\n")
        result = self.run_launcher("codex", self.repo, ambient=False, inside=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        env = self.read("provider")["env"]
        self.assertEqual(env["AGENTSTACK_PROJECT_KEY"], str(self.plain))
        self.assertEqual(env["AGENTSTACK_PROJECT_REPOSITORY"], str(self.repo))
        self.assertEqual(env["AGENTSTACK_PROTECTED_ROOTS"], str(self.repo))

    def test_logical_namespace_without_declared_roots_protects_only_target(self) -> None:
        (self.install / "env.sh").write_text("export AGENTSTACK_PROJECT_KEY=shared-project\n")
        result = self.run_launcher("codex", self.repo, ambient=False, inside=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read("provider")["env"]["AGENTSTACK_PROTECTED_ROOTS"], str(self.repo))

    def test_a_to_b_launch_replaces_derived_roots_but_keeps_intentional_extras(self) -> None:
        for provider in PROVIDERS:
            for inside in (False, True):
                with self.subTest(provider=provider, inside=inside):
                    first = self.run_launcher(provider, self.repo, inside=inside,
                        extra={"AGENTSTACK_EXTRA_PROTECTED_ROOTS": str(self.plain)})
                    self.assertEqual(first.returncode, 0, first.stderr)
                    previous = {key: value for key, value in self.read("provider")["env"].items()
                                if value is not None}
                    second = self.run_launcher(provider, self.other, inside=inside, extra=previous)
                    self.assertEqual(second.returncode, 0, second.stderr)
                    env = self.read("provider")["env"]
                    self.assertEqual(env["AGENTSTACK_PROTECTED_ROOTS"], f"{self.plain}:{self.other}")
                    self.assertEqual(env["AGENTSTACK_EXTRA_PROTECTED_ROOTS"], str(self.plain))
                    self.assertEqual(env["AGENTSTACK_PROJECT_WORK_DIR"], str(self.other))
                    self.assertNotIn(str(self.repo), env["AGENTSTACK_PROTECTED_ROOTS"].split(":"))

    def test_empty_extra_roots_override_installed_and_tmux_server_settings(self) -> None:
        with (self.install / "env.sh").open("a") as stream:
            stream.write(f'export AGENTSTACK_EXTRA_PROTECTED_ROOTS={shlex.quote(str(self.plain))}\n')
        server = {**self.server_stale, "AGENTSTACK_EXTRA_PROTECTED_ROOTS": str(self.other)}
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                result = self.run_launcher(provider, self.repo, extra={
                    "AGENTSTACK_EXTRA_PROTECTED_ROOTS": "", "TEST_SERVER_ENV": json.dumps(server)})
                self.assertEqual(result.returncode, 0, result.stderr)
                env = self.read("provider")["env"]
                self.assertEqual(env["AGENTSTACK_EXTRA_PROTECTED_ROOTS"], "")
                self.assertEqual(env["AGENTSTACK_PROTECTED_ROOTS"], str(self.repo))

    def test_installed_unquoted_empty_extras_are_deliberate_migration(self) -> None:
        with (self.install / "env.sh").open("a") as stream:
            stream.write('export AGENTSTACK_EXTRA_PROTECTED_ROOTS= # intentionally no extras\n')
        result = self.run_launcher("codex", self.repo, ambient=False, inside=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read("provider")["env"]["AGENTSTACK_EXTRA_PROTECTED_ROOTS"], "")
        self.assertEqual(self.read("provider")["env"]["AGENTSTACK_PROTECTED_ROOTS"], str(self.repo))
        self.assertNotIn("legacy AGENTSTACK_PROTECTED_ROOTS ignored", result.stderr)

    def test_installed_extra_roots_survive_namespace_change(self) -> None:
        with (self.install / "env.sh").open("a") as stream:
            stream.write(f'export AGENTSTACK_EXTRA_PROTECTED_ROOTS={shlex.quote(str(self.plain))}\n')
        result = self.run_launcher("codex", self.linked, extra={"AGENTSTACK_PROJECT_KEY": "selected"})
        self.assertEqual(result.returncode, 0, result.stderr)
        env = self.read("provider")["env"]
        self.assertEqual(env["AGENTSTACK_EXTRA_PROTECTED_ROOTS"], str(self.plain))
        self.assertEqual(env["AGENTSTACK_PROTECTED_ROOTS"], f"{self.plain}:{self.linked}")
        self.assertNotIn("legacy AGENTSTACK_PROTECTED_ROOTS ignored", result.stderr)

    def test_legacy_roots_are_visible_in_preview_but_not_reclassified_as_extras(self) -> None:
        result = self.run_launcher("gemini", "--dry-run", self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"legacy AGENTSTACK_PROTECTED_ROOTS ignored for this launch: {self.other}", result.stderr)
        self.assertIn("run scripts/install.sh (install/update) for automatic migration", result.stderr)
        self.assertIn("inherited shell/tmux roots are ignored", result.stderr)
        self.assertIn("finish/release existing reservations before updating", result.stderr)
        self.assertIn("restart cooperating sessions together", result.stderr)
        self.assertFalse(list(self.output.glob("*.json")))

    def test_different_inherited_legacy_roots_warn_even_when_extras_exist(self) -> None:
        with (self.install / "env.sh").open("a") as stream:
            stream.write("export AGENTSTACK_EXTRA_PROTECTED_ROOTS=''\n")
        for extras in ({}, {"AGENTSTACK_EXTRA_PROTECTED_ROOTS": ""},
                       {"AGENTSTACK_EXTRA_PROTECTED_ROOTS": str(self.plain)}):
            with self.subTest(extras=extras):
                result = self.run_launcher("codex", self.repo, inside=True,
                    extra={**extras, "AGENTSTACK_PROTECTED_ROOTS": str(self.linked)})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr.count("legacy AGENTSTACK_PROTECTED_ROOTS ignored"), 1)
                self.assertIn(str(self.linked), result.stderr)
                self.assertNotIn(str(self.linked), self.read("provider")["env"]["AGENTSTACK_PROTECTED_ROOTS"])

    def test_shell_startup_cannot_replace_selected_workspace_or_empty_extras(self) -> None:
        poison = {**self.server_stale, "AGENTSTACK_EXTRA_PROTECTED_ROOTS": str(self.other)}
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                result = self.run_launcher(provider, self.repo, extra={
                    "TEST_SHELL_STARTUP_ENV": json.dumps(poison),
                    "AGENTSTACK_EXTRA_PROTECTED_ROOTS": ""})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_context(self.read("provider"), self.repo)
                self.assertEqual(self.read("provider")["env"]["AGENTSTACK_EXTRA_PROTECTED_ROOTS"], "")

    def test_shell_startup_cannot_move_provider_outside_protected_workspace(self) -> None:
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                result = self.run_launcher(provider, self.repo,
                    extra={"TEST_SHELL_STARTUP_CWD": str(self.other)})
                self.assertEqual(result.returncode, 0, result.stderr)
                actual = self.read("provider")
                self.assert_context(actual, self.repo)
                self.assertEqual(actual["cwd"], str(self.repo))

    def test_shell_startup_restoration_keeps_literal_extra_and_namespace(self) -> None:
        extra = self.root / "shared '$HOME;$(touch UNEXPECTED)`literal`"
        extra.mkdir()
        key = "namespace '$HOME;$(touch UNEXPECTED)`literal`"
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                result = self.run_launcher(provider, self.repo, extra={
                    "AGENTSTACK_EXTRA_PROTECTED_ROOTS": str(extra),
                    "AGENTSTACK_PROJECT_KEY": key,
                    "TEST_SHELL_STARTUP_ENV": json.dumps(self.server_stale)})
                self.assertEqual(result.returncode, 0, result.stderr)
                env = self.read("provider")["env"]
                self.assertEqual(env["AGENTSTACK_PROJECT_KEY"], key)
                self.assertEqual(env["AGENTSTACK_EXTRA_PROTECTED_ROOTS"], str(extra))
                self.assertEqual(env["AGENTSTACK_PROTECTED_ROOTS"], f"{extra}:{self.repo}")
        self.assertFalse((self.repo / "UNEXPECTED").exists())

    def test_same_workspace_context_does_not_repeat_legacy_warning(self) -> None:
        script = ('. "$1"; agentstack_load_top_level_env; '
                  'agentstack_apply_workspace_context "$2" shared; '
                  'agentstack_apply_workspace_context "$2" shared; '
                  'bash "$1" workspace-context-json "$2" shared')
        result = subprocess.run(["/bin/bash", "-eu", "-c", script, "test",
            str(ROOT / "hooks/project-context.sh"), str(self.repo)],
            env={**self.env, **self.ambient}, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr.count("legacy AGENTSTACK_PROTECTED_ROOTS ignored"), 1)
        self.assertEqual(json.loads(result.stdout)["environment"]["AGENTSTACK_PROTECTED_ROOTS"], str(self.repo))

    def test_extra_root_aliases_are_canonicalized_and_deduplicated_in_order(self) -> None:
        alias = self.root / "vault alias"
        alias.symlink_to(self.plain, target_is_directory=True)
        extras = f"{alias}/:{self.plain}:{self.repo}/"
        result = self.run_launcher("codex", self.repo, inside=True,
                                   extra={"AGENTSTACK_EXTRA_PROTECTED_ROOTS": extras})
        self.assertEqual(result.returncode, 0, result.stderr)
        env = self.read("provider")["env"]
        self.assertEqual(env["AGENTSTACK_EXTRA_PROTECTED_ROOTS"], f"{self.plain}:{self.repo}")
        self.assertEqual(env["AGENTSTACK_PROTECTED_ROOTS"], f"{self.plain}:{self.repo}")

    def test_managed_file_alias_matches_canonical_vault_reservation_path(self) -> None:
        target = self.plain / "project"
        target.mkdir()
        alias = self.root / "vault alias"
        alias.symlink_to(self.plain, target_is_directory=True)
        result = self.run_launcher("codex", target, ambient=False, inside=True,
            extra={"AGENTSTACK_EXTRA_PROTECTED_ROOTS": str(alias)})
        self.assertEqual(result.returncode, 0, result.stderr)
        env = {**self.env, **{key: value for key, value in self.read("provider")["env"].items()
                            if value is not None}}
        script = '. "$1"; reservation_resolve_tool_context "$2"; printf "%s|%s\\n" "$MATCHED_ROOT" "$REL_PATH"'
        for path in (alias / "project/note.md", target / "child/../note.md"):
            with self.subTest(path=path):
                result = subprocess.run(["/bin/bash", "-e", "-c", script, "test",
                    str(ROOT / "hooks/reservation-common.sh"),
                    json.dumps({"tool_input": {"file_path": str(path)}})],
                    cwd=target, env=env, capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), f"{self.plain}|{target / 'note.md'}")
        # Current upstream canonicalizes alias roots for legacy sessions too.
        env.pop("AGENTSTACK_PROTECTION_CONTEXT")
        env["AGENTSTACK_PROTECTED_ROOTS"] = str(alias)
        result = subprocess.run(["/bin/bash", "-e", "-c", script, "test",
            str(ROOT / "hooks/reservation-common.sh"),
            json.dumps({"tool_input": {"file_path": str(alias / "project/note.md")}})],
            cwd=target, env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"{self.plain}|project/note.md")

    def test_legacy_literal_glob_roots_still_reach_reservation_guard(self) -> None:
        sibling = self.root / "workspacea"
        sibling.mkdir()
        for name in ("workspace[ab]", "workspace*", "workspace?"):
            workspace = self.root / name
            workspace.mkdir()
            for disable_globbing in (False, True):
                with self.subTest(name=name, noglob=disable_globbing):
                    # Managed launches reject these roots; direct legacy hooks
                    # retain their literal scope matcher and relative identity.
                    script = ('export AGENTSTACK_PROTECTED_ROOTS="$1" AGENTSTACK_PROJECT_KEY=shared; '
                              'unset AGENTSTACK_PROTECTION_CONTEXT; '
                              '. "$2"; before="$-"; before_ifs="$IFS"; '
                              'reservation_resolve_tool_context "$3" || exit 10; '
                              '[ "$MATCHED_ROOT" = "$1" ] && [ "$REL_PATH" = note.md ] || exit 11; '
                              'if reservation_resolve_tool_context "$4"; then exit 12; fi; '
                              '[ "$-" = "$before" ] && [ "$IFS" = "$before_ifs" ]')
                    if disable_globbing:
                        script = "set -f; " + script
                    result = subprocess.run(["/bin/bash", "-eu", "-c", script, "test", str(workspace),
                        str(ROOT / "hooks/reservation-common.sh"),
                        json.dumps({"tool_input": {"file_path": str(workspace / "note.md")}}),
                        json.dumps({"tool_input": {"file_path": str(sibling / "note.md")}})],
                        cwd=self.root, env=self.env,
                        capture_output=True, text=True, timeout=20)
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_filesystem_root_extra_uses_canonical_managed_reservation_path(self) -> None:
        result = self.run_launcher("codex", self.repo, inside=True,
            extra={"AGENTSTACK_EXTRA_PROTECTED_ROOTS": "/"})
        self.assertEqual(result.returncode, 0, result.stderr)
        env = {**self.env, **{key: value for key, value in self.read("provider")["env"].items()
                            if value is not None}}
        note = self.repo / "note.md"
        result = subprocess.run(["bash", "-e", "-c",
            '. "$1"; reservation_resolve_tool_context "$2"; printf "%s" "$REL_PATH"',
            "test", str(ROOT / "hooks/reservation-common.sh"),
            json.dumps({"tool_input": {"file_path": str(note)}})],
            env=env, cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, str(note))

    def test_cli_context_exports_quote_extra_paths_as_data(self) -> None:
        extra = self.root / "shared '$HOME;$(touch UNEXPECTED)"
        extra.mkdir()
        script = ('context="$(bash "$1" workspace-context-exports "$2" shared)" || exit $?; '
                  'eval "$context"; printf "%s\\n" "$AGENTSTACK_PROTECTED_ROOTS"')
        result = subprocess.run(["/bin/bash", "-c", script, "test",
            str(ROOT / "hooks/project-context.sh"), str(self.repo)], cwd=self.root,
            env={**self.env, "AGENTSTACK_EXTRA_PROTECTED_ROOTS": str(extra)},
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"{extra}:{self.repo}")
        self.assertFalse((self.root / "UNEXPECTED").exists())

    def test_relative_or_control_character_extra_root_fails_before_launch(self) -> None:
        for extras in ("relative/shared", str(self.plain) + "\ninvalid", str(self.plain) + "\n"):
            with self.subTest(extras=extras):
                result = self.run_launcher("codex", self.repo,
                    extra={"AGENTSTACK_EXTRA_PROTECTED_ROOTS": extras})
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(list(self.output.glob("*.json")))

    def test_control_character_in_key_is_rejected(self) -> None:
        for key in ("bad\nkey", "bad\n", "bad\r"):
            with self.subTest(key=key):
                result = self.run_launcher("codex", self.repo, extra={"AGENTSTACK_PROJECT_KEY": key})
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(list(self.output.glob("*.json")))

    def test_context_resolution_with_git_only_path_does_not_need_env_or_dirname(self) -> None:
        bindir = self.root / "git-only-bin"
        bindir.mkdir()
        fake_git = bindir / "git"
        fake_git.write_text('#!/bin/sh\nprintf "git\\n" >> "$TEST_GIT_CALLS"\nexit 128\n')
        fake_git.chmod(0o755)
        calls = self.root / "git-calls"
        env = {**self.env, "PATH": str(bindir), "TEST_GIT_CALLS": str(calls)}
        command = ["/bin/bash", str(ROOT / "hooks/project-context.sh"),
                   "resolve-invocation-context", str(self.plain), "shared"]
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["work_dir"], str(self.plain))
        self.assertEqual(calls.read_text().splitlines(), ["git", "git"])
        with (self.install / "env.sh").open("a") as stream:
            stream.write(f'export AGENTSTACK_EXTRA_PROTECTED_ROOTS={shlex.quote(str(self.other))}\n')
        configured = subprocess.run(["/bin/bash", str(ROOT / "hooks/project-context.sh"),
            "workspace-context-json", str(self.plain), "shared"],
            env=env, capture_output=True, text=True, timeout=3)
        self.assertEqual(configured.returncode, 0, configured.stderr)
        configured_env = json.loads(configured.stdout)["environment"]
        self.assertEqual(configured_env["AGENTSTACK_EXTRA_PROTECTED_ROOTS"], str(self.other))
        self.assertEqual(configured_env["AGENTSTACK_PROTECTED_ROOTS"], f"{self.other}:{self.plain}")
        nested = self.plain / "nested"
        nested.mkdir()
        (self.plain / ".git").mkdir()
        (self.plain / ".git/HEAD").write_text("broken metadata")
        command[-2] = str(nested)
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot resolve repository metadata", result.stderr)

    def test_missing_python_cannot_launch_with_stale_context(self) -> None:
        result = self.run_launcher("codex", self.repo, extra={"AGENTSTACK_PYTHON": "/missing/python"})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(list(self.output.glob("*.json")))

    def test_gemini_dry_run_resolves_context_without_external_side_effects(self) -> None:
        result = self.run_launcher("gemini", "--dry-run", self.linked,
            extra={"AGENTSTACK_GEMINI_BIN": "/missing/agy", "AGENTSTACK_PROJECT_KEY": "selected"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("cwd=" + str(self.linked), result.stdout)
        self.assertFalse(list(self.output.glob("*.json")))

    def test_codex_permissions_vault_and_oauth_behavior_do_not_change(self) -> None:
        result = self.run_launcher("codex", self.repo, inside=True,
                                   extra={"AGENTSTACK_VAULT": str(self.plain),
                                          "AGENTSTACK_EXTRA_PROTECTED_ROOTS": str(self.plain),
                                          "OPENAI_API_KEY": "must-strip"})
        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.read("provider")
        self.assertIsNone(record["api_key"])
        self.assertEqual(record["env"]["AGENTSTACK_PROTECTED_ROOTS"], f"{self.plain}:{self.repo}")
        self.assertEqual(record["args"], ["-C", str(self.repo), "--sandbox", "workspace-write",
                                          "--ask-for-approval", "on-request", "-c", "check_for_update_on_startup=false",
                                          "--add-dir", str(self.plain)])

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
    def test_real_tmux_socket_overrides_stale_server_without_changing_globals(self) -> None:
        real = shutil.which("tmux")
        socket = str(self.root / "tmux.sock")
        command = [real, "-S", socket, "-f", os.devnull]
        shared = self.root / "shared vault"
        shared.mkdir()
        stale = {**self.server_stale, "AGENTSTACK_EXTRA_PROTECTED_ROOTS": str(self.other),
                 "AGENTSTACK_PROTECTION_CONTEXT": "workspace-v1"}
        # Only tmux/Git are real. The fake providers record their environment
        # and stay alive until the isolated session is killed; no AI CLI runs.
        server_env = {**self.env, **stale, "SHELL": "/bin/bash",
                      "TEST_REAL_TMUX": real, "TEST_SOCKET": socket, "TEST_PROVIDER_HOLD": "1"}
        try:
            subprocess.run([*command, "new-session", "-d", "-s", "seed", "-c", str(self.other),
                            "sleep 120"], env=server_env, check=True, capture_output=True, timeout=10)
            wrapper = self.executable("tmux-isolated", """
import os, subprocess, sys
args = sys.argv[1:]
if args[0] == "new-session":
    args.insert(1, "-d")
raise SystemExit(subprocess.run([os.environ["TEST_REAL_TMUX"], "-S", os.environ["TEST_SOCKET"],
                                "-f", os.devnull, *args], timeout=15).returncode)
""")
            for provider in PROVIDERS:
                for target in (self.linked, self.plain):
                    for extras in (str(shared), ""):
                        with self.subTest(provider=provider, target=target, extras=extras):
                            result = self.run_launcher(provider, target, extra={
                                "TEST_TMUX": str(wrapper), "TEST_REAL_TMUX": real,
                                "TEST_SOCKET": socket, "TEST_PROVIDER_HOLD": "1",
                                "AGENTSTACK_EXTRA_PROTECTED_ROOTS": extras})
                            self.assertEqual(result.returncode, 0, result.stderr)
                            expected = {
                                "AGENTSTACK_PROJECT_KEY": "live-project", "PROJECT_KEY": "live-project",
                                "AGENTSTACK_PROJECT_REPOSITORY": str(self.repo) if target == self.linked else "",
                                "AGENTSTACK_PROJECT_WORK_DIR": str(target),
                                "AGENTSTACK_PROJECT_WORKTREE_ROOT": str(target) if target == self.linked else "",
                                "AGENTSTACK_PROTECTED_ROOTS": f"{extras}:{target}" if extras else str(target),
                                "AGENTSTACK_EXTRA_PROTECTED_ROOTS": extras,
                                "AGENTSTACK_PROTECTION_CONTEXT": "workspace-v1",
                            }
                            deadline = time.monotonic() + 15
                            while not (self.output / "provider.json").exists() and time.monotonic() < deadline:
                                time.sleep(0.05)
                            self.assertTrue((self.output / "provider.json").exists(), result.stderr)
                            sessions = subprocess.run(
                                [*command, "list-sessions", "-F", "#{session_name}"], env=self.env,
                                check=True, capture_output=True, text=True, timeout=10,
                            ).stdout.splitlines()
                            launched = [session for session in sessions if session != "seed"]
                            self.assertEqual(len(launched), 1, sessions)
                            session = "=" + launched[0]
                            for name, value in expected.items():
                                actual = subprocess.run(
                                    [*command, "show-environment", "-t", session, name], env=self.env,
                                    check=True, capture_output=True, text=True, timeout=10)
                                self.assertEqual(actual.stdout.rstrip("\n"), name + "=" + value)
                            for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
                                removal = subprocess.run(
                                    [*command, "show-environment", "-t", session, name], env=self.env,
                                    check=True, capture_output=True, text=True, timeout=10)
                                self.assertEqual(removal.stdout.strip(), "-" + name)
                            subprocess.run(
                                [*command, "new-window", "-d", "-t", session, "-c", str(target),
                                 shlex.quote(str(self.snapshot)) + " new-window"], env=self.env,
                                check=True, capture_output=True, timeout=10)
                            deadline = time.monotonic() + 15
                            while not (self.output / "new-window.json").exists() and time.monotonic() < deadline:
                                time.sleep(0.05)
                            self.assertTrue((self.output / "new-window.json").exists())
                            for record_name in ("provider", "new-window"):
                                record = self.read(record_name)
                                self.assertEqual(record["cwd"], str(target))
                                for name, value in expected.items():
                                    self.assertEqual(record["env"][name], value, (record_name, name))
                                for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR",
                                             "AGENTSTACK_PROJECT_CONTEXT", "AGENTSTACK_LOOKUP_PROJECT_KEY"):
                                    self.assertFalse(record["env"][name], (record_name, name))
                            # Check after each launch, rather than letting a later
                            # launch accidentally restore a globally leaked value.
                            for name, value in stale.items():
                                unchanged = subprocess.run(
                                    [*command, "show-environment", "-g", name], env=self.env,
                                    check=True, capture_output=True, text=True, timeout=10)
                                self.assertEqual(unchanged.stdout.rstrip("\n"), name + "=" + value)
                            subprocess.run([*command, "kill-session", "-t", session], env=self.env,
                                           check=True, capture_output=True, timeout=10)
        finally:
            subprocess.run([*command, "kill-server"], env=self.env, capture_output=True, timeout=10)

    def test_failed_resolution_leaves_callers_environment_unchanged(self) -> None:
        script = '. "$1"; BIN_DIR="$2"; before="$(export -p)"; '
        script += 'if ags_prepare_top_level_context "$3"; then exit 9; fi; [ "$(export -p)" = "$before" ]'
        result = subprocess.run(["/bin/bash", "-euo", "pipefail", "-c", script, "test",
                                 str(ROOT / "bin/lib/agentstack-launch.sh"), str(self.bin), str(self.root / "missing")],
                                cwd=self.root, env={**self.env, **self.ambient}, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
