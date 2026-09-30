"""Top-level launcher project selection and visibility."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PROVIDERS = ("claude", "codex", "gemini")
LAUNCHERS = {
    "claude": "agent-start",
    "codex": "agent-start-codex",
    "gemini": "agent-start-gemini",
}
KEYS = ("AGENTSTACK_PROJECT_KEY", "PROJECT_KEY", "AGENTSTACK_PROTECTED_ROOTS")


@unittest.skipIf(os.name == "nt", "POSIX top-level launchers")
class TopLevelProjectSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="orrery-top-project-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.install = self.home / "install"
        self.bin = self.install / "bin"
        (self.bin / "lib").mkdir(parents=True)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.protected = self.root / "shared vault"
        self.protected.mkdir()
        self.output = self.root / "output"
        self.output.mkdir()

        self.snapshot = self.executable("snapshot", r'''
import json, os, pathlib, sys
out = pathlib.Path(os.environ["TEST_OUTPUT"]) / (sys.argv[1] + ".json")
out.write_text(json.dumps({
    "cwd": os.getcwd(),
    "env": {key: os.environ.get(key) for key in json.loads(os.environ["TEST_KEYS"])},
}))
''')
        self.tmux = self.executable("tmux-double", r'''
import os, subprocess, sys
args = sys.argv[1:]
if args[0] == "display-message":
    print("TestAgent")
    raise SystemExit(0)
if args[0] == "has-session":
    raise SystemExit(1)
if args[0] == "rename-session":
    raise SystemExit(0)
if args[0] != "new-session":
    raise SystemExit("unexpected tmux mutation: " + repr(args))
env = dict(os.environ)
env.update({
    "AGENTSTACK_PROJECT_KEY": "server-stale-project",
    "PROJECT_KEY": "server-stale-project",
    "AGENTSTACK_PROTECTED_ROOTS": "/server/stale/root",
})
i = 1
cwd = None
while i < len(args) - 1:
    option, value = args[i:i + 2]
    if option == "-e":
        key, value = value.split("=", 1)
        env[key] = value
    elif option == "-c":
        cwd = value
    elif option != "-s":
        raise SystemExit("unexpected option: " + option)
    i += 2
env["TMUX"] = "fake,1,0"
subprocess.run(["/bin/bash", "-c", args[-1]], cwd=cwd, env=env, check=True, timeout=20)
''')

        self.env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(self.home),
            "TMPDIR": str(self.root),
            "LC_ALL": "C",
            "SHELL": "/usr/bin/true",
            "AGENTSTACK_HOME": str(self.install),
            "AGENTSTACK_LABEL_PREFIX": f"org.agentstack.test.top-project.{self.root.name}",
            "TEST_OUTPUT": str(self.output),
            "TEST_KEYS": json.dumps(KEYS),
            "TEST_TMUX": str(self.tmux),
            "TEST_SNAPSHOT": str(self.snapshot),
        }
        for name in LAUNCHERS.values():
            shutil.copy2(ROOT / "bin" / name, self.bin / name)
        (self.bin / "lib" / "agentstack-launch.sh").write_text(
            ". " + shlex.quote(str(ROOT / "bin/lib/agentstack-launch.sh")) + "\n"
            'ags_resolve_tmux() { printf "%s\\n" "$TEST_TMUX"; }\n',
            encoding="utf-8",
        )
        (self.bin / "lib" / "agentstack-register.sh").write_text(
            r'''
ags_pick_adjective_scientist_name() { printf 'TestAgent\n'; }
ags_mail_load_token() { :; }
ags_mcp_call() { :; }
ags_start_mail_watcher() { :; }
ags_record_managed_agent() { :; }
ags_registration_token_file() { return 1; }
ags_register_session() {
  "$TEST_SNAPSHOT" registration
  AGS_REGISTERED_AGENT_NAME=TestAgent
  AGS_AGENT_NAME_SUBSTITUTED=0
}
''',
            encoding="utf-8",
        )
        for provider in ("codex", "gemini"):
            (self.bin / f"agentstack-{provider}-bootstrap").write_text(
                '"$TEST_SNAPSHOT" bootstrap\nexport AGENT_NAME=TestAgent\n',
                encoding="utf-8",
            )
        for provider in PROVIDERS:
            fake = self.executable(
                provider,
                'import os, subprocess\n'
                'subprocess.run([os.environ["TEST_SNAPSHOT"], "provider"], check=True)\n',
            )
            self.env[f"AGENTSTACK_{provider.upper()}_BIN"] = str(fake)

        (self.install / "env.sh").write_text(
            "export AGENTSTACK_PROJECT_KEY=installed-project\n"
            f"export AGENTSTACK_PROTECTED_ROOTS={shlex.quote(str(self.protected))}\n",
            encoding="utf-8",
        )

    def executable(self, name: str, body: str) -> Path:
        path = self.root / name
        path.write_text(f"#!{sys.executable}\n" + body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def run_launcher(
        self,
        provider: str,
        *args: str | Path,
        inside: bool = False,
        extra: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        for path in self.output.glob("*.json"):
            path.unlink()
        env = {**self.env, **(extra or {})}
        if inside:
            env["TMUX"] = "fake,1,0"
        else:
            env.pop("TMUX", None)
        return subprocess.run(
            ["/bin/bash", str(self.bin / LAUNCHERS[provider]), *map(str, args)],
            cwd=self.workspace,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def read(self, name: str) -> dict:
        return json.loads((self.output / f"{name}.json").read_text())

    def assert_project(self, name: str, key: str, roots: Path | str | None = None) -> None:
        env = self.read(name)["env"]
        self.assertEqual(env["AGENTSTACK_PROJECT_KEY"], key)
        self.assertEqual(env["PROJECT_KEY"], key)
        if roots is not None:
            self.assertEqual(env["AGENTSTACK_PROTECTED_ROOTS"], str(roots))

    def test_installed_selection_is_visible_without_rewriting_roots(self) -> None:
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                result = self.run_launcher(provider, self.workspace, inside=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("project=installed-project source=installed env.sh", result.stderr)
                self.assertIn(f"workdir={self.workspace} protected_roots={self.protected}", result.stderr)
                self.assert_project("provider", "installed-project", self.protected)

    def test_live_environment_selectors_beat_installed_project(self) -> None:
        result = self.run_launcher(
            "codex", self.workspace, inside=True,
            extra={"AGENTSTACK_PROJECT_KEY": "live-project"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("project=live-project source=AGENTSTACK_PROJECT_KEY", result.stderr)
        self.assert_project("provider", "live-project", self.protected)

        result = self.run_launcher(
            "codex", self.workspace, inside=True,
            extra={"PROJECT_KEY": "alias-project"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("project=alias-project source=PROJECT_KEY", result.stderr)
        self.assert_project("provider", "alias-project", self.protected)

    def test_project_key_option_reaches_registration_and_existing_tmux(self) -> None:
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                result = self.run_launcher(
                    provider, "--project-key", "explicit-project", self.workspace,
                    extra={"AGENTSTACK_PROJECT_KEY": "live-project"},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("project=explicit-project source=--project-key", result.stderr)
                boundary = "registration" if provider == "claude" else "bootstrap"
                self.assert_project(boundary, "explicit-project", self.protected)
                self.assert_project("provider", "explicit-project", self.protected)

    def test_explicit_project_requirement_stops_before_registration(self) -> None:
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                result = self.run_launcher(
                    provider, self.workspace, inside=True,
                    extra={"AGENTSTACK_REQUIRE_EXPLICIT_PROJECT_KEY": "1"},
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("--project-key is required", result.stderr)
                self.assertIn("usage:", result.stderr)
                self.assertEqual(list(self.output.glob("*.json")), [])

        result = self.run_launcher(
            "codex", "--project-key", "explicit-project", self.workspace, inside=True,
            extra={"AGENTSTACK_REQUIRE_EXPLICIT_PROJECT_KEY": "1"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_project("provider", "explicit-project", self.protected)

    def test_gemini_dry_run_accepts_project_option_in_either_order(self) -> None:
        for args in (
            ("--dry-run", "--project-key", "selected", self.workspace),
            ("--project-key", "selected", "--dry-run", self.workspace),
        ):
            with self.subTest(args=args):
                result = self.run_launcher("gemini", *args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("project=selected source=--project-key", result.stderr)
                self.assertIn(f"dry-run cwd={self.workspace}", result.stdout)
                self.assertEqual(list(self.output.glob("*.json")), [])

    def test_bad_project_option_fails_before_launch(self) -> None:
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                result = self.run_launcher(provider, "--project-key")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("--project-key requires a non-empty value", result.stderr)
                self.assertEqual(list(self.output.glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
