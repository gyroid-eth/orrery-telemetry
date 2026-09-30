"""install.sh --update-mail: replace a running ORRERY Mail and roll back.

Every test here runs real installs against a real isolated Mail server: a
temporary HOME, a temporary state root, free loopback ports, and a label
prefix of the test's own. Candidates are directories whose executables link to
this checkout's dev venv, so switching between them is a real stop/start of a
different runner without building a venv per test.
"""

from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from service_teardown import TEST_LABEL_PREFIX, stop_dashboard
from test_install_agentstack_mail import (
    INSTALLER,
    ROOT,
    _base_env,
    _fake_linux_bin,
    _free_port,
    _generated_env,
    _stop_mail,
    _wait_health,
    _write_command,
)

ENTRYPOINTS = (
    "agentstack-mail",
    "agentstack-mail-service",
    "agentstack-mail-migrate",
    "agentstack-enroll",
)


def _candidate(
    service_root: pathlib.Path, source: str, *, broken: dict[str, str] | None = None
) -> pathlib.Path:
    actual_bin = pathlib.Path(sys.executable).parent
    venv = service_root / "candidates" / source / "venv"
    (venv / "bin").mkdir(parents=True)
    for name in ENTRYPOINTS:
        if broken and name in broken:
            _write_command(venv / "bin", name, broken[name])
        else:
            assert (actual_bin / name).is_file(), name
            (venv / "bin" / name).symlink_to(actual_bin / name)
    return venv


class Stack:
    def __init__(self, tmp_path: pathlib.Path) -> None:
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.env = _base_env(tmp_path, self.home, _fake_linux_bin(tmp_path))
        self.service_root = self.home / ".agentstack" / "mail-service"
        self.state_root = tmp_path / "mail-state"
        self.mail_port = _free_port()
        self.mail_url = f"http://127.0.0.1:{self.mail_port}/mcp"
        self.env.update(
            {
                "AGENTSTACK_MAIL_STATE_ROOT": str(self.state_root),
                "AGENTSTACK_MAIL_SERVICE_ROOT": str(self.service_root),
                "AGENTSTACK_MCP_URL": self.mail_url,
                "AGENTSTACK_PORT": str(_free_port()),
                "AGENTSTACK_LABEL_PREFIX": (
                    f"{TEST_LABEL_PREFIX}.mail-update-{self.mail_port}"
                ),
            }
        )
        self.pidfile = self.service_root / "runtime" / "agentstack-mail.pid"

    def install(
        self,
        source: str,
        *args: str,
        venv: pathlib.Path | None = None,
        extra: dict[str, str] | None = None,
    ):
        env = {**self.env, **(extra or {})}
        env["AGENTSTACK_MAIL_CANDIDATE_ID"] = source
        if venv is not None:
            env["AGENTSTACK_MAIL_SERVICE_VENV"] = str(venv)
        return subprocess.run(
            ["/bin/bash", str(INSTALLER), "--assume-yes", *args],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=100,
        )

    def runner(self) -> str:
        return self.pidfile.read_text(encoding="utf-8").splitlines()[1]

    def pid(self) -> int:
        return int(self.pidfile.read_text(encoding="utf-8").split()[0])

    def render_of(self, source: str) -> pathlib.Path:
        (render,) = (self.service_root / "renders").glob(f"{source}-*")
        return render

    def manifest(self) -> dict:
        return json.loads(
            (self.home / ".agentstack" / "install-state.json").read_text(
                encoding="utf-8"
            )
        )

    def instance_id(self) -> str:
        with sqlite3.connect(self.state_root / "storage.sqlite3") as db:
            return db.execute("SELECT instance_id FROM mail_instances").fetchone()[0]

    def teardown(self) -> None:
        _stop_mail(self.home, self.state_root, self.mail_port)
        stop_dashboard(self.home, label_prefix=self.env["AGENTSTACK_LABEL_PREFIX"])


def _first_install(stack: Stack) -> tuple[dict, pathlib.Path]:
    first = stack.install("fixture-old")
    assert first.returncode == 0, first.stdout + first.stderr
    health = _wait_health(stack.mail_url)
    old_render = stack.render_of("fixture-old")
    assert stack.runner() == str(old_render / "run-agentstack-mail.sh")
    return health, old_render


def test_a_plain_rerun_keeps_the_running_build_and_a_dry_run_changes_nothing(tmp_path):
    stack = Stack(tmp_path)
    _candidate(stack.service_root, "fixture-old")
    _candidate(stack.service_root, "fixture-new")
    try:
        _, old_render = _first_install(stack)
        old_pid = stack.pid()

        kept = stack.install("fixture-new")
        assert kept.returncode == 0, kept.stdout + kept.stderr
        assert "Re-run with --update-mail to switch" in kept.stdout
        assert stack.pid() == old_pid
        assert _generated_env(stack.home)["AGENTSTACK_MAIL_ENV"] == str(
            old_render / "service.env"
        )

        planned = subprocess.run(
            ["/bin/bash", str(INSTALLER), "--update-mail", "--dry-run"],
            cwd=ROOT,
            env={**stack.env, "AGENTSTACK_MAIL_CANDIDATE_ID": "fixture-new"},
            text=True,
            capture_output=True,
            check=False,
            timeout=100,
        )
        assert planned.returncode == 0, planned.stdout + planned.stderr
        assert "will switch ORRERY Mail from fixture-old" in planned.stdout
        assert "DRY-RUN would verify candidate" in planned.stdout
        assert "restore the previous build if the new one is not serving" in planned.stdout
        assert not list((stack.service_root / "renders").glob("fixture-new-*"))
        assert not (stack.service_root / "backups").exists()
        assert stack.pid() == old_pid
    finally:
        stack.teardown()


def test_update_switches_the_running_build_and_keeps_state(tmp_path):
    stack = Stack(tmp_path)
    old_venv = _candidate(stack.service_root, "fixture-old")
    new_venv = _candidate(stack.service_root, "fixture-new")
    try:
        health, old_render = _first_install(stack)
        old_pid = stack.pid()
        instance = stack.instance_id()
        # An enrollment pin must survive the switch: the instance id lives in
        # the shared database, not in the build.
        connection = stack.home / ".agentstack" / "connections" / "local.json"
        pinned = json.loads(connection.read_text(encoding="utf-8"))
        pinned["expected_server_instance_id"] = instance
        connection.write_text(json.dumps(pinned) + "\n", encoding="utf-8")
        connection.chmod(0o600)

        updated = stack.install("fixture-new", "--update-mail")
        assert updated.returncode == 0, updated.stdout + updated.stderr
        assert "candidate verified offline" in updated.stdout
        assert "ORRERY Mail switched from fixture-old to fixture-new" in updated.stdout
        new_render = stack.render_of("fixture-new")
        assert stack.runner() == str(new_render / "run-agentstack-mail.sh")
        assert stack.pid() != old_pid
        assert _wait_health(stack.mail_url)["database_url"] == health["database_url"]
        assert stack.instance_id() == instance
        generated = _generated_env(stack.home)
        assert generated["AGENTSTACK_MAIL_ENV"] == str(new_render / "service.env")
        assert generated["AGENTSTACK_MAIL_ENROLL_BIN"] == str(
            new_venv / "bin" / "agentstack-enroll"
        )
        profile = json.loads(connection.read_text(encoding="utf-8"))
        assert profile["mail_env"] == str(new_render / "service.env")
        assert profile["expected_server_instance_id"] == instance
        manifest = stack.manifest()
        assert manifest["agent_mail"]["service_env"] == str(new_render / "service.env")
        update = manifest["agent_mail"]["update"]
        assert update["result"] == "switched"
        assert update["previous_service_env"] == str(old_render / "service.env")
        backup = pathlib.Path(update["database_backup"])
        assert backup.is_file() and backup.stat().st_mode & 0o777 == 0o600
        assert backup.parent == stack.service_root / "backups"
        with sqlite3.connect(backup) as db:
            assert (
                db.execute("SELECT instance_id FROM mail_instances").fetchone()[0]
                == instance
            )
        # The previous candidate and render are untouched: they are the way back.
        assert (old_venv / "bin" / "agentstack-mail").exists()
        assert (old_render / "run-agentstack-mail.sh").is_file()
        # The autostart sweep is not left held down by the update's stop.
        assert not (stack.service_root / "runtime" / "agentstack-mail.stopped").exists()

        same = stack.install("fixture-new", "--update-mail")
        assert same.returncode == 0, same.stdout + same.stderr
        assert "already uses candidate" in same.stdout
    finally:
        stack.teardown()


def test_update_back_to_the_previous_candidate_reuses_its_render(tmp_path):
    stack = Stack(tmp_path)
    old_venv = _candidate(stack.service_root, "fixture-old")
    _candidate(stack.service_root, "fixture-new")
    try:
        health, old_render = _first_install(stack)
        forward = stack.install("fixture-new", "--update-mail")
        assert forward.returncode == 0, forward.stdout + forward.stderr

        # Going back is the same operation with the previous candidate pinned;
        # its render is immutable and is reused as it was.
        back = stack.install("fixture-old", "--update-mail", venv=old_venv)
        assert back.returncode == 0, back.stdout + back.stderr
        assert "ORRERY Mail switched from fixture-new to fixture-old" in back.stdout
        assert stack.runner() == str(old_render / "run-agentstack-mail.sh")
        assert _wait_health(stack.mail_url)["database_url"] == health["database_url"]
        assert _generated_env(stack.home)["AGENTSTACK_MAIL_ENV"] == str(
            old_render / "service.env"
        )
        assert len(list((stack.service_root / "renders").glob("fixture-old-*"))) == 1
    finally:
        stack.teardown()


def test_update_rolls_back_when_the_new_build_does_not_serve(tmp_path):
    stack = Stack(tmp_path)
    _candidate(stack.service_root, "fixture-old")
    # Passes the offline check (the server entrypoint works) but its
    # supervisor entrypoint never serves, as a build broken in production would.
    _candidate(
        stack.service_root,
        "fixture-broken-start",
        broken={"agentstack-mail-service": "#!/bin/sh\nexit 1\n"},
    )
    try:
        health, old_render = _first_install(stack)
        old_runner = str(old_render / "run-agentstack-mail.sh")

        # How long the update waits for the new build before giving up on it.
        rolled = stack.install(
            "fixture-broken-start",
            "--update-mail",
            extra={"AGENTSTACK_MAIL_UPDATE_START_GRACE": "4"},
        )
        assert rolled.returncode == 1, rolled.stdout + rolled.stderr
        assert "candidate verified offline" in rolled.stdout
        assert "restoring the previous ORRERY Mail build" in rolled.stderr
        assert "restored ORRERY Mail" in rolled.stdout
        assert "ORRERY Mail update rolled-back" in rolled.stderr
        assert stack.runner() == old_runner
        assert _wait_health(stack.mail_url)["database_url"] == health["database_url"]
        assert _generated_env(stack.home)["AGENTSTACK_MAIL_ENV"] == str(
            old_render / "service.env"
        )
        manifest = stack.manifest()
        assert manifest["agent_mail"]["service_env"] == str(old_render / "service.env")
        assert manifest["agent_mail"]["update"]["result"] == "rolled-back"
        assert not (stack.service_root / "runtime" / "agentstack-mail.stopped").exists()
    finally:
        stack.teardown()


def test_update_that_fails_offline_never_stops_the_running_build(tmp_path):
    stack = Stack(tmp_path)
    _candidate(stack.service_root, "fixture-old")
    _candidate(
        stack.service_root,
        "fixture-broken-server",
        broken={"agentstack-mail": "#!/bin/sh\necho broken server >&2\nexit 3\n"},
    )
    try:
        _, old_render = _first_install(stack)
        pid_before = stack.pid()
        refused = stack.install("fixture-broken-server", "--update-mail")
        assert refused.returncode == 1, refused.stdout + refused.stderr
        assert "ORRERY Mail was not switched" in refused.stderr
        assert "ORRERY Mail update not-switched" in refused.stderr
        # Never stopped: same runner process, no backup taken.
        assert stack.pid() == pid_before
        assert stack.runner() == str(old_render / "run-agentstack-mail.sh")
        assert not (stack.service_root / "backups").exists()
        assert "broken server" in (
            stack.service_root / "runtime" / "mail-update-verify.log"
        ).read_text(encoding="utf-8")
        assert stack.manifest()["agent_mail"]["update"]["result"] == "not-switched"
        _wait_health(stack.mail_url)
    finally:
        stack.teardown()


def test_update_refuses_a_deployment_it_cannot_identify(tmp_path):
    """No pidfile and no env.sh: the listener is not ours to stop."""
    stack = Stack(tmp_path)
    _candidate(stack.service_root, "fixture-old")
    _candidate(stack.service_root, "fixture-new")
    try:
        first = stack.install("fixture-old")
        assert first.returncode == 0, first.stdout + first.stderr
        pid = stack.pid()
        # Forget the deployment while it keeps serving.
        runner_line = stack.pidfile.read_text(encoding="utf-8")
        (stack.home / ".agentstack" / "env.sh").unlink()
        stack.pidfile.unlink()
        refused = stack.install("fixture-new", "--update-mail")
        stack.pidfile.write_text(runner_line, encoding="utf-8")
        assert refused.returncode == 1, refused.stdout + refused.stderr
        assert "deployment is not identifiable; nothing was stopped" in refused.stderr
        assert stack.pid() == pid
        _wait_health(stack.mail_url)
    finally:
        stack.teardown()


def test_package_tree_comparison_decides_whether_a_new_build_differs(tmp_path):
    """A new commit that leaves packages/agentstack_mail alone is the same build."""
    repo = tmp_path / "repo"
    (repo / "packages" / "agentstack_mail").mkdir(parents=True)

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            text=True,
            capture_output=True,
            check=True,
            env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
        ).stdout.strip()

    git("init", "-q")
    (repo / "packages" / "agentstack_mail" / "a.py").write_text("1\n")
    (repo / "README").write_text("1\n")
    git("add", "-A")
    git("commit", "-qm", "a")
    first = git("rev-parse", "HEAD")
    (repo / "README").write_text("2\n")
    git("commit", "-qam", "b")
    docs_only = git("rev-parse", "HEAD")
    (repo / "packages" / "agentstack_mail" / "a.py").write_text("2\n")
    git("commit", "-qam", "c")
    package_change = git("rev-parse", "HEAD")

    script = (
        "set -euo pipefail\n"
        f"REPO_ROOT={repo}\n"
        f"NATIVE_MAIL_PACKAGE_SOURCE={repo}/packages/agentstack_mail\n"
        f"eval \"$(sed -n '/^mail_package_unchanged()/,/^}}/p' {INSTALLER})\"\n"
        'mail_package_unchanged "$1" "$2" && echo same || echo different\n'
    )

    def compare(old: str, new: str) -> str:
        return subprocess.run(
            ["/bin/bash", "-c", script, "compare", old, new],
            text=True, capture_output=True, check=True,
        ).stdout.strip()

    assert compare(first, docs_only) == "same"
    assert compare(first, package_change) == "different"
    assert compare("fixture-old", first) == "different"
