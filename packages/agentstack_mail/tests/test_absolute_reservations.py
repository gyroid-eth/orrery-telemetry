from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from test_global_server_s2c import prepared as prepared
from fixtures.sql_reservations import SQLTransport
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
from agentstack_mail import reservation_paths

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
def server(prepared):
    now = [time.time()]
    return SQLTransport(prepared, lambda: now[0]), now


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
    if reservation_paths.case_insensitive_at(tmp_path):
        with pytest.raises(ReservationError, match="CASE_RULES_UNKNOWN"):
            normalize_path(str(composed))
        return
    a, b = normalize_path(str(composed)), normalize_path(str(decomposed))
    assert a.value != b.value
    assert paths_overlap(a, b) is composed.samefile(decomposed)


@pytest.mark.parametrize("casefold", [0, 1, 2, None])
def test_linux_native_directory_flags_fail_closed(tmp_path, monkeypatch, casefold):
    import ctypes
    import fcntl

    def statfs(path, buffer):
        ctypes.c_long.from_buffer(buffer).value = 0xEF53
        return 0

    class Native:
        pass

    native = Native()
    native.statfs = statfs
    closed = []

    def ioctl(descriptor, request, output, mutate):
        assert descriptor == 12345
        if casefold is None:
            raise OSError("unsupported directory flags")
        if len(output) == 1:
            output[0] = 0x40000000 if casefold == 1 else 0
        else:
            output[0] = 0x00040000 if casefold == 2 else 0

    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", "linux")
        patch.setattr(ctypes, "CDLL", lambda *args, **kwargs: native)
        patch.setattr(os, "open", lambda *args: 12345)
        patch.setattr(os, "close", closed.append)
        patch.setattr(fcntl, "ioctl", ioctl)
        if casefold in {1, 2}:
            with pytest.raises(ReservationError, match="FILESYSTEM_RULES_UNKNOWN"):
                reservation_paths.unicode_rule_at(tmp_path)
        else:
            assert reservation_paths.unicode_rule_at(tmp_path) == (
                "exact" if casefold == 0 else None
            )
    assert closed == [12345]


def test_missing_unicode_leaves_follow_actual_filesystem(server, tmp_path):
    # Only this fixture writes a witness. Runtime policy detection is read-only.
    witness = tmp_path / "é-witness.py"
    witness.touch()
    aliases = (tmp_path / "e\u0301-witness.py").exists()
    a, b = tmp_path / "é-new.py", tmp_path / "e\u0301-new.py"
    if reservation_paths.case_insensitive_at(tmp_path):
        for path in (a, b):
            with pytest.raises(ReservationError, match="CASE_RULES_UNKNOWN"):
                normalize_path(str(path))
        assert not server[0]._leases
        return
    left, right = normalize_path(str(a)), normalize_path(str(b))
    assert not a.exists() and not b.exists()
    assert left.value != right.value  # Preserve supplied spelling.
    assert paths_overlap(left, right) is aliases
    service, _ = server
    client(service, tmp_path).call("reserve_files", paths=[a.name])
    if aliases:
        with pytest.raises(ReservationError, match="CONFLICT"):
            client(service, tmp_path, "beta").call("reserve_files", paths=[b.name])
        a.touch()
        assert a.samefile(b)
    else:
        client(service, tmp_path, "beta").call("reserve_files", paths=[b.name])
        a.touch()
        b.touch()
        assert not a.samefile(b)


def test_unicode_glob_overlap_and_recent_activity(server, tmp_path):
    file = tmp_path / "é-existing.py"
    file.touch()
    aliases = (tmp_path / "e\u0301-existing.py").exists()
    if reservation_paths.case_insensitive_at(tmp_path):
        with pytest.raises(ReservationError, match="CASE_RULES_UNKNOWN"):
            normalize_path(str(tmp_path / "e\u0301-*.py"))
        return
    pattern = normalize_path(str(tmp_path / "e\u0301-*.py"))
    assert paths_overlap(pattern, normalize_path(str(file))) is aliases
    service, _ = server
    client(service, tmp_path).call("reserve_files", paths=[pattern.value])
    if aliases:
        with pytest.raises(ReservationError, match="CONFLICT"):
            client(service, tmp_path, "beta").call("reserve_files", paths=[str(file)])


def test_distinguishing_unicode_rules_preserve_missing_names(tmp_path, monkeypatch):
    # An explicit byte-distinguishing filesystem contract, also exercised by
    # the actual Linux filesystem in test_missing_unicode_leaves_follow_actual_filesystem.
    monkeypatch.setattr(reservation_paths, "unicode_rule_at", lambda _: "exact")
    monkeypatch.setattr(reservation_paths, "case_insensitive_at", lambda _: False)
    a = normalize_path(str(tmp_path / "é-new.py"))
    b = normalize_path(str(tmp_path / "e\u0301-new.py"))
    assert not paths_overlap(a, b)
    assert not reservation_paths.component_matches(
        "é-file.py", "e\u0301-*.py", unicode_rule="exact"
    )


def test_unknown_unicode_rules_reject_acquire_and_prevent_stale(
    server, tmp_path, monkeypatch
):
    service, now = server
    monkeypatch.setattr(reservation_paths, "case_insensitive_at", lambda _: False)
    path = normalize_path(str(tmp_path / "e\u0301-*.py"))
    lease = client(service, tmp_path).call("reserve_files", paths=[path.value])[0]
    now[0] += 200
    monkeypatch.setattr(reservation_paths, "unicode_rule_at", lambda _: None)
    assert not service._leases[lease.id].released
    with pytest.raises(ReservationError, match="UNICODE_RULES_UNKNOWN"):
        client(service, tmp_path, "beta").call("reserve_files", paths=["é-new.py"])
    assert len(service._leases) == 1


@pytest.mark.parametrize("rule", ["apfs", "hfs"])
def test_supported_unicode_rules_match_canonical_variants(rule):
    assert reservation_paths.component_matches(
        "é-file.py", "e\u0301-*.py", unicode_rule=rule
    )
    assert reservation_paths.component_matches(
        "e\u0301-file.py", "é-*.py", unicode_rule=rule
    )
    assert reservation_paths.component_matches("é", "?", unicode_rule=rule)


def test_hfs_exclusions_and_unknown_apfs_unicode_version():
    assert not reservation_paths.component_matches(
        "Å-file.py", "Å-*.py", unicode_rule="hfs"
    )
    assert reservation_paths.component_matches(
        "Å-file.py", "Å-*.py", unicode_rule="apfs"
    )
    # U+1AB0 is a combining mark introduced after the pinned safe repertoire.
    with pytest.raises(ReservationError, match="UNICODE_RULES_UNKNOWN"):
        reservation_paths.component_matches("a\u1ab0", "*", unicode_rule="apfs")


def test_case_alias_follows_filesystem(tmp_path):
    upper, lower = tmp_path / "FILE.py", tmp_path / "file.py"
    upper.write_text("one")
    lower.write_text("two")
    assert paths_overlap(
        normalize_path(str(upper)), normalize_path(str(lower))
    ) is upper.samefile(lower)


def test_missing_nonascii_case_alias_never_grants_twice(server, tmp_path):
    service, _ = server
    paths = [tmp_path / "É-case.py", tmp_path / "é-case.py"]
    if reservation_paths.case_insensitive_at(tmp_path):
        for owner, path in zip(("alpha", "beta"), paths):
            with pytest.raises(ReservationError, match="CASE_RULES_UNKNOWN"):
                client(service, tmp_path, owner).call(
                    "reserve_files", paths=[path.name]
                )
        assert not service._leases
        paths[0].touch()
        assert paths[0].samefile(paths[1])
    else:
        for owner, path in zip(("alpha", "beta"), paths):
            client(service, tmp_path, owner).call("reserve_files", paths=[path.name])
            path.touch()
        assert not paths[0].samefile(paths[1])


@pytest.mark.parametrize("name", ["É-case.py", "é-case.py", "Σ*.py", "日本語.py"])
def test_nonascii_case_table_unknown_rejects_initial_acquire(
    server, tmp_path, monkeypatch, name
):
    monkeypatch.setattr(reservation_paths, "unicode_rule_at", lambda _: "apfs")
    monkeypatch.setattr(reservation_paths, "case_insensitive_at", lambda _: True)
    service, _ = server
    with pytest.raises(ReservationError, match="CASE_RULES_UNKNOWN"):
        client(service, tmp_path).call("reserve_files", paths=[name])
    assert not service._leases


def test_case_sensitive_unicode_case_names_stay_distinct(server, tmp_path, monkeypatch):
    monkeypatch.setattr(reservation_paths, "unicode_rule_at", lambda _: "apfs")
    monkeypatch.setattr(reservation_paths, "case_insensitive_at", lambda _: False)
    service, _ = server
    a = client(service, tmp_path).call("reserve_files", paths=["É-case.py"])[0]
    b = client(service, tmp_path, "beta").call("reserve_files", paths=["é-case.py"])[0]
    assert not paths_overlap(a.path, b.path)
    assert reservation_paths.component_matches(
        "é.py", "e\u0301.py", unicode_rule="apfs"
    )


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
    lease = client(service, tmp_path).call(
        "reserve_files", paths=["*.py"], ttl_seconds=60
    )[0]
    before = service._leases[lease.id]
    assert not before.released
    now[0] += 61
    assert not client(service, tmp_path).call("check_reservations", paths=["new.py"])
    assert service._leases[lease.id] == before
    client(service, tmp_path).call(
        "release_reservations", file_reservation_ids=[lease.id]
    )
    assert service._leases[lease.id].released


def test_renew_extends_current_expiry_and_noop_is_not_activity(server, tmp_path):
    service, now = server
    lease = client(service, tmp_path).call("reserve_files", paths=["new.py"])[0]
    renewed = client(service, tmp_path).call(
        "renew_reservations", file_reservation_ids=[lease.id], extend_seconds=600
    )[0]
    assert renewed.expires == lease.expires + 600
    assert client(service, tmp_path).call("check_reservations", paths=["new.py"])
    assert service._leases[lease.id] == renewed


def test_expired_gc_does_not_need_filesystem_evidence(server, tmp_path):
    service, now = server
    lease = client(service, tmp_path).call(
        "reserve_files", paths=["new.py"], ttl_seconds=60
    )[0]
    now[0] += 60
    assert not client(service, tmp_path).call("check_reservations", paths=["new.py"])
    assert service._leases[lease.id] == lease


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


@pytest.mark.parametrize(
    "outcome",
    [
        {"tool_response": {"success": False, "error": "permission denied"}},
        {"tool_response": {"success": False}},
        {"tool_result": {"status": "failed"}},
        {"tool_result": {"status": "blocked"}},
        {"tool_output": "ERROR: rejected"},
        {"tool_output": "PreToolUse: blocked"},
        {"tool_output": ["ok", {"nested": {"error": "denied"}}]},
        {"error": "blocked"},
        {"success": False},
        {"status": "blocked"},
    ],
)
def test_failed_candidate_edit_keeps_lease_without_dispatch(server, tmp_path, outcome):
    service, _ = server
    lease = client(service, tmp_path).call("reserve_files", paths=["new.py"])[0]
    document = {
        "session_id": "fixture",
        "tool_name": "Edit",
        "tool_input": {"file_path": "new.py"},
        **outcome,
    }

    def forbidden(*args):
        raise AssertionError("failed Edit must not resolve or dispatch")

    assert (
        hook_release(
            document, cwd=tmp_path, resolve_session=forbidden, dispatch=forbidden
        )
        == 0
    )
    assert not service._leases[lease.id].released
    with pytest.raises(ReservationError, match="CONFLICT"):
        client(service, tmp_path, "beta").call("reserve_files", paths=["new.py"])


def test_successful_candidate_edit_releases_lease(server, tmp_path):
    service, _ = server
    lease = client(service, tmp_path).call("reserve_files", paths=["new.py"])[0]
    document = {
        "session_id": "fixture",
        "tool_name": "Edit",
        "tool_input": {"file_path": "new.py"},
        "tool_response": {"success": True},
    }
    assert (
        hook_release(
            document,
            cwd=tmp_path,
            resolve_session=lambda _: Binding("alpha", "test-alpha"),
            dispatch=service.dispatch,
        )
        == 0
    )
    assert service._leases[lease.id].released
    assert client(service, tmp_path, "beta").call("reserve_files", paths=["new.py"])


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
