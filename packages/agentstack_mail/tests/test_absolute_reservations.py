from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agentstack_mail.reservation_activity import Activity, probe_activity
from agentstack_mail.reservation_candidate import ReservationServer
from agentstack_mail.reservation_clients import (
    Binding,
    ReservationClient,
    hook_guard,
    hook_release,
)
from agentstack_mail.reservation_paths import (
    ReservationError,
    normalize_path,
    paths_overlap,
)

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = json.loads(
    (Path(__file__).parents[1] / "fixtures/absolute-reservations-v1.json").read_text()
)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("TMUX", raising=False)


def authenticate(name, token):
    if name not in {"alpha", "beta"} or token != "test-" + name:
        raise ReservationError("AUTHENTICATION_REQUIRED")
    return name


@pytest.fixture
def server():
    now = [10_000.0]
    candidate = ReservationServer(
        authenticate, clock=lambda: now[0], inactivity=100, grace=100
    )
    return candidate, now


def client(server, cwd, owner="alpha"):
    return ReservationClient(
        lambda: Binding(owner, "test-" + owner), server.dispatch, cwd=cwd
    )


@pytest.mark.parametrize("case", FIXTURE["overlap_cases"])
def test_overlap_fixture(case, tmp_path):
    left = normalize_path(str(tmp_path / case["left"]))
    right = normalize_path(str(tmp_path / case["right"]))
    assert paths_overlap(left, right) is case["conflict"]
    assert paths_overlap(right, left) is case["conflict"]


def test_two_cwds_and_ignored_scope(server, tmp_path):
    service, _ = server
    a = client(service, tmp_path / "a")
    b = client(service, tmp_path / "b", "beta")
    first = a.call("reserve_files", paths=["src/file.py"], project_key="wrong")
    second = b.call("reserve_files", paths=["src/file.py"], project_key="wrong")
    assert first[0].path != second[0].path
    with pytest.raises(ReservationError, match="CONFLICT"):
        b.call(
            "reserve_files",
            paths=[str(tmp_path / "a/src/file.py")],
            project_key="different",
        )
    with pytest.raises(ReservationError, match="ABSOLUTE_PATH_REQUIRED"):
        service.dispatch(
            "reserve_files",
            {
                "agent_name": "alpha",
                "registration_token": "test-alpha",
                "project_key": str(tmp_path),
                "paths": ["relative.py"],
            },
        )


def test_symlink_dotdot_new_file_and_lifecycle(server, tmp_path):
    target = tmp_path / "real"
    target.mkdir()
    (tmp_path / "alias").symlink_to(target, target_is_directory=True)
    assert normalize_path(str(tmp_path / "alias/sub/../new.py")) == normalize_path(
        str(target / "new.py")
    )
    service, _ = server
    a, b = client(service, tmp_path), client(service, tmp_path, "beta")
    lease = a.call("reserve_files", paths=["alias/new.py"])[0]
    assert a.call("check_reservations", paths=["real/new.py"])
    with pytest.raises(ReservationError, match="CONFLICT"):
        b.call("reserve_files", paths=["real/new.py"])
    assert a.call("renew_reservations", paths=["real/new.py"])[0].id == lease.id
    a.call("release_reservations", paths=["alias/new.py"])
    assert b.call("reserve_files", paths=["real/new.py"])


@pytest.mark.parametrize("glob", ["*/new.py", "**/new.py", "**"])
def test_glob_symlink_directory_conflicts_for_future_file(server, tmp_path, glob):
    scope, target = tmp_path / "scope", tmp_path / "target"
    scope.mkdir()
    target.mkdir()
    (scope / "link").symlink_to(target, target_is_directory=True)
    service, _ = server
    client(service, scope).call("reserve_files", paths=[glob])
    with pytest.raises(ReservationError, match="CONFLICT"):
        client(service, target, "beta").call("reserve_files", paths=["new.py"])


def test_glob_symlink_file_and_bounded_unknown(tmp_path):
    scope = tmp_path / "scope"
    scope.mkdir()
    target = tmp_path / "target.py"
    target.write_text("fixture")
    (scope / "alias.py").symlink_to(target)
    assert paths_overlap(
        normalize_path(str(scope / "*.py")), normalize_path(str(target))
    )
    for i in range(1001):
        (scope / f"file-{i}.py").touch()
    with pytest.raises(ReservationError, match="GLOB_SCOPE_UNKNOWN"):
        paths_overlap(
            normalize_path(str(scope / "*.py")),
            normalize_path(str(tmp_path / "unrelated.py")),
        )


@pytest.mark.parametrize(
    "raw,diagnostic",
    [
        ("relative.py", "ABSOLUTE"),
        ("C:\\src\\a.py", "WSL"),
        ("~/a.py", "ABSOLUTE"),
        ("/tmp/*.py/../x", "GLOB_PARENT"),
        ("/tmp/a\\b", "SEPARATOR"),
        ("", "INVALID"),
        ("tool://", "INVALID"),
    ],
)
def test_ambiguous_paths_rejected(raw, diagnostic):
    with pytest.raises(ReservationError, match=diagnostic):
        normalize_path(raw)


def test_unicode_preserved_when_filesystem_distinguishes_it(tmp_path):
    composed, decomposed = tmp_path / "é.py", tmp_path / "e\u0301.py"
    composed.write_text("one")
    decomposed.write_text("two")
    a, b = normalize_path(str(composed)), normalize_path(str(decomposed))
    assert a.value != b.value
    assert paths_overlap(a, b) is composed.samefile(decomposed)


def test_case_alias_follows_filesystem(tmp_path):
    upper, lower = tmp_path / "FILE.py", tmp_path / "file.py"
    upper.write_text("one")
    lower.write_text("two")
    assert paths_overlap(
        normalize_path(str(upper)), normalize_path(str(lower))
    ) is upper.samefile(lower)


def test_future_file_under_case_alias_and_case_variant(server, tmp_path):
    directory = tmp_path / "Dir"
    directory.mkdir()
    a = normalize_path(str(directory / "new.py"))
    b = normalize_path(str(tmp_path / "dir/new.py"))
    aliases = (tmp_path / "dir").exists()
    assert paths_overlap(a, b) is aliases
    if a.case_insensitive:
        assert paths_overlap(a, normalize_path(str(directory / "NEW.py")))
        service, _ = server
        client(service, tmp_path).call("reserve_files", paths=["Dir/new.py"])
        with pytest.raises(ReservationError, match="CONFLICT"):
            client(service, tmp_path, "beta").call(
                "reserve_files", paths=["dir/NEW.py"]
            )


def test_explicit_wsl_mapping_and_virtual_type(tmp_path):
    drive_root = tmp_path / "mnt/c"
    assert normalize_path(
        "C:\\space dir\\new.py", wsl_drives={"c": str(drive_root)}
    ) == normalize_path(str(drive_root / "space dir/new.py"))
    virtual = normalize_path("tool://editor/write")
    assert virtual.kind == "virtual" and paths_overlap(virtual, virtual)
    assert not paths_overlap(
        virtual, normalize_path(str(tmp_path / "tool/editor/write"))
    )
    assert not paths_overlap(virtual, normalize_path("resource://editor/write"))


def test_shared_and_atomic_conflicts(server, tmp_path):
    service, _ = server
    a, b = client(service, tmp_path), client(service, tmp_path, "beta")
    a.call("reserve_files", paths=["shared"], exclusive=False)
    b.call("reserve_files", paths=["shared"], exclusive=False)
    with pytest.raises(ReservationError, match="CONFLICT"):
        b.call("reserve_files", paths=["new", "shared"], exclusive=True)
    assert not b.call("check_reservations", paths=["new"])


@pytest.mark.parametrize("operation", ["release_reservations", "renew_reservations"])
def test_owner_and_token_enforced(server, tmp_path, operation):
    service, _ = server
    lease = client(service, tmp_path).call("reserve_files", paths=["new"])[0]
    with pytest.raises(ReservationError, match="OWNER"):
        client(service, tmp_path, "beta").call(
            operation, file_reservation_ids=[lease.id]
        )
    with pytest.raises(ReservationError, match="AUTHENTICATION"):
        service.dispatch(
            operation,
            {
                "agent_name": "alpha",
                "registration_token": "bad",
                "file_reservation_ids": [lease.id],
            },
        )
    with pytest.raises(ReservationError, match="BOUND_IDENTITY"):
        client(service, tmp_path).call(operation, agent_name="beta")


@pytest.mark.parametrize("ttl", [True, 0, 59, 86401, "600"])
def test_ttl_validation(server, tmp_path, ttl):
    with pytest.raises(ReservationError, match="TTL"):
        client(server[0], tmp_path).call(
            "reserve_files", paths=["new"], ttl_seconds=ttl
        )


def test_unknown_no_early_release_but_ttl_and_owner_work(server, tmp_path):
    service, now = server
    service.probe = lambda _: Activity(False, reason="PROBE_UNKNOWN")
    lease = client(service, tmp_path).call(
        "reserve_files", paths=["new"], ttl_seconds=600
    )[0]
    now[0] += 200
    assert service.collect([lease.id]) == {lease.id: "activity_unknown"}
    assert service._leases[lease.id].expires == lease.expires
    now[0] += 500
    assert service.collect([lease.id]) == {lease.id: "ttl_expired"}
    assert not client(service, tmp_path).call(
        "renew_reservations", file_reservation_ids=[lease.id]
    )
    other = client(service, tmp_path).call("reserve_files", paths=["new"])[0]
    assert client(service, tmp_path).call(
        "release_reservations", file_reservation_ids=[other.id]
    )


def test_renew_extends_current_expiry_and_noop_is_not_activity(server, tmp_path):
    service, now = server
    bound = client(service, tmp_path)
    lease = bound.call("reserve_files", paths=["new.py"], ttl_seconds=600)[0]
    now[0] += 50
    renewed = bound.call("renew_reservations", paths=["new.py"], extend_seconds=60)[0]
    assert renewed.expires == lease.expires + 60
    now[0] += 200
    assert bound.call("renew_reservations", paths=["other.py"]) == []
    assert service.collect([lease.id]) == {lease.id: "stale"}


def test_expired_gc_does_not_need_filesystem_evidence(server, tmp_path):
    service, now = server
    lease = client(service, tmp_path).call(
        "reserve_files", paths=["new.py"], ttl_seconds=60
    )[0]
    now[0] += 61

    def forbidden(_):
        raise AssertionError("expired lease must not be probed")

    service.probe = forbidden
    assert service.collect([lease.id]) == {lease.id: "ttl_expired"}


def test_raw_candidate_tool_names_share_lifecycle(server, tmp_path):
    service, _ = server
    arguments = {
        "agent_name": "alpha",
        "registration_token": "test-alpha",
        "project_key": "ignored",
        "paths": [str(tmp_path / "new.py")],
        "format": "json",
    }
    lease = service.dispatch("file_reservation_paths", arguments)[0]
    renewed = service.dispatch("renew_file_reservations", arguments)[0]
    assert renewed.id == lease.id and renewed.expires > lease.expires
    assert service.dispatch("release_file_reservations", arguments)[0].id == lease.id


@pytest.mark.parametrize("pattern", ["new.py", "empty/*.py", "tool://editor/write"])
def test_unmatched_initial_grace_then_stale(server, tmp_path, pattern):
    service, now = server
    service.inactivity = 10
    lease = client(service, tmp_path).call("reserve_files", paths=[pattern])[0]
    now[0] += 20
    first = service.collect([lease.id])[lease.id]
    assert first == ("stale" if pattern.startswith("tool://") else "active")
    if first == "active":
        now[0] += 101
        assert service.collect([lease.id]) == {lease.id: "stale"}


def test_probe_permission_timeout_cap_and_unknown_anchor(tmp_path, monkeypatch):
    file = tmp_path / "a.py"
    file.write_text("x")
    pattern = normalize_path(str(tmp_path / "*.py"))
    assert not probe_activity(pattern, max_entries=1).complete
    assert not probe_activity(pattern, timeout=0).complete
    with monkeypatch.context() as patch:

        def denied(*args):
            raise PermissionError("denied")

        patch.setattr(os, "scandir", denied)
        assert not probe_activity(pattern).complete
    (tmp_path / "dangling").symlink_to(tmp_path / "absent")
    with pytest.raises(ReservationError, match="ANCHOR_UNKNOWN"):
        normalize_path(str(tmp_path / "dangling/new.py"))


def git(repo, *args, env=None):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def make_repo(path, stamp):
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.name", "Fixture")
    git(path, "config", "user.email", "fixture@example.test")
    file = path / "file.py"
    file.write_text("fixture")
    git(path, "add", ".")
    env = dict(
        os.environ,
        GIT_AUTHOR_DATE=f"@{int(stamp)} +0000",
        GIT_COMMITTER_DATE=f"@{int(stamp)} +0000",
    )
    git(path, "commit", "-qm", "fixture", env=env)
    os.utime(file, (stamp, stamp))
    return file


def test_each_lease_actual_repo_git_activity_and_symlink(server, tmp_path):
    service, now = server
    now[0] = time.time()
    old = now[0] - 1000
    a = make_repo(tmp_path / "a", now[0])
    b = make_repo(tmp_path / "b", old)
    # Git alone keeps a alive; its mtime is deliberately old.
    os.utime(a, (old, old))
    link = tmp_path / "link"
    link.symlink_to(a.parent, target_is_directory=True)
    assert probe_activity(normalize_path(str(link / a.name))).git >= now[0] - 1
    leases = client(service, tmp_path).call("reserve_files", paths=[str(a), str(b)])
    now[0] += 110
    service.grace = 200
    assert service.collect([lease.id for lease in leases]) == {
        leases[0].id: "active",
        leases[1].id: "stale",
    }


def test_broad_glob_multiple_repos_and_git_failure(tmp_path, monkeypatch):
    old = time.time() - 1000
    make_repo(tmp_path / "a", old)
    make_repo(tmp_path / "b", time.time())
    path = normalize_path(str(tmp_path / "*/file.py"))
    assert probe_activity(path).git > old
    original = subprocess.run

    def fail_one(command, **kwargs):
        if str(tmp_path / "b") in command:
            raise subprocess.TimeoutExpired(command, 0.01)
        return original(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", fail_one)
    assert not probe_activity(path).complete


@pytest.mark.parametrize("pattern", ["a/file.py", "*/file.py"])
def test_recent_deletion_commit_keeps_empty_scope_active(tmp_path, pattern):
    stamp = time.time()
    file = make_repo(tmp_path / "a", stamp - 1000)
    file.unlink()
    git(file.parent, "add", "-u")
    env = dict(
        os.environ,
        GIT_AUTHOR_DATE=f"@{int(stamp)} +0000",
        GIT_COMMITTER_DATE=f"@{int(stamp)} +0000",
    )
    git(file.parent, "commit", "-qm", "delete", env=env)
    activity = probe_activity(normalize_path(str(tmp_path / pattern)))
    assert activity.complete and not activity.matched
    assert activity.git >= stamp - 1


@pytest.mark.parametrize("change", ["mail", "renew", "filesystem"])
def test_gc_rechecks_activity_after_probe(server, tmp_path, change):
    service, now = server
    lease = client(service, tmp_path).call("reserve_files", paths=["file.py"])[0]
    now[0] += 200
    count = 0

    def probe(_):
        nonlocal count
        count += 1
        if count == 1:
            return Activity(True, matched=True, filesystem=0)
        if change == "mail":
            service.record_activity("alpha", "test-alpha", mail=True)
        elif change == "renew":
            client(service, tmp_path).call(
                "renew_reservations", file_reservation_ids=[lease.id]
            )
        return Activity(
            True, matched=True, filesystem=now[0] if change == "filesystem" else 0
        )

    service.probe = probe
    assert service.collect([lease.id])[lease.id] in {"active", "activity_changed"}
    assert not service._leases[lease.id].released
    assert count == 2


def test_candidate_hook_any_folder_unmanaged_and_bound_outage(server, tmp_path):
    service, _ = server
    cwd = tmp_path / "elsewhere"
    document = {"session_id": "fixture", "tool_input": {"file_path": "new.py"}}
    resolver = lambda _: Binding("alpha", "test-alpha")
    assert (
        hook_guard(
            document, cwd=cwd, resolve_session=lambda _: None, dispatch=service.dispatch
        )
        == 0
    )
    assert (
        hook_guard(
            document, cwd=cwd, resolve_session=resolver, dispatch=service.dispatch
        )
        == 2
    )
    client(service, cwd).call("reserve_files", paths=["new.py"])
    assert (
        hook_guard(
            document, cwd=cwd, resolve_session=resolver, dispatch=service.dispatch
        )
        == 0
    )

    def outage(*args):
        raise ConnectionError("unavailable")

    assert hook_guard(document, cwd=cwd, resolve_session=resolver, dispatch=outage) == 2
    assert (
        hook_guard(document, cwd=cwd, resolve_session=outage, dispatch=service.dispatch)
        == 2
    )
    assert (
        hook_release(
            document, cwd=cwd, resolve_session=resolver, dispatch=service.dispatch
        )
        == 0
    )
    assert (
        hook_guard(
            document, cwd=cwd, resolve_session=resolver, dispatch=service.dispatch
        )
        == 2
    )


def test_cli_and_bash32_hook_same_normalizer(tmp_path):
    env = dict(
        os.environ,
        PYTHONPATH=str(ROOT / "packages/agentstack_mail/src"),
        AGENTSTACK_PYTHON=sys.executable,
        AGENTSTACK_HOOKS_DIR=str(ROOT / "hooks"),
        AGENTSTACK_PROJECT_KEY=str(tmp_path / "wrong"),
    )
    expected = normalize_path("space dir/new.py", anchor=tmp_path).value
    cli = subprocess.run(
        [
            sys.executable,
            "-m",
            "agentstack_mail.reservation_clients",
            "--cwd",
            str(tmp_path),
            "space dir/new.py",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    hook = subprocess.run(
        [
            "/bin/bash",
            "-c",
            '. "$1"; reservation_absolute_paths "space dir/new.py"',
            "fixture",
            str(ROOT / "hooks/reservation-common.sh"),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert (
        json.loads(cli.stdout)
        == json.loads(hook.stdout)
        == [{"kind": "filesystem", "path": expected}]
    )


def test_candidate_not_published_or_activated():
    from agentstack_mail.namespace_contract import load_namespace_contract

    assert FIXTURE["activation"] is False
    assert load_namespace_contract()["activation_enabled"] is False
