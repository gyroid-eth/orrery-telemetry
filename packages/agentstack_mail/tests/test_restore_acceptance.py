from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import signal
import shutil
import sqlite3
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentstack_mail import evidence, restore_acceptance
from agentstack_mail.contract import COMPATIBILITY_TOOLS


ACCEPTED_BACKUP_PATHS = {
    "main": Path(
        "/Users/operator/orrery/backups/agent-mail-backup-20260811-215721.sqlite3"
    ),
    "wal": Path(
        "/Users/operator/orrery/backups/agent-mail-backup-20260811-215721.sqlite3-wal"
    ),
    "shm": Path(
        "/Users/operator/orrery/backups/agent-mail-backup-20260811-215721.sqlite3-shm"
    ),
}
ACCEPTED_BACKUP_PINS = {
    "main": {
        "state": "PRESENT",
        "sha256": "c80bdf9ddb59ab712c0ef23a60be08fbe8ec78f4fa523f02918fb1bae35eea02",
        "size": 67_293_184,
        "mode": "0644",
    },
    "wal": {
        "state": "PRESENT",
        "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "size": 0,
        "mode": "0644",
    },
    "shm": {
        "state": "PRESENT",
        "sha256": "fd4c9fda9cd3f9ae7c962b0ddf37232294d55580e1aa165aa06129b8549389eb",
        "size": 32_768,
        "mode": "0644",
    },
}
ACCEPTED_LOGICAL_SHA256 = (
    "afb50ad0a331b233c865db8d0e9512248c9ef5d75aa129c859198d9002317818"
)
ACCEPTED_MESSAGE_MAX_ID = 8_829
ACCEPTED_MESSAGE_SHA256 = (
    "1cc1f6636c3755d1404c2df953b64cc00e0e8a168ae75b1ccd2dfeada1430713"
)


def _pin_for(path: Path, *, state: str = "PRESENT") -> dict[str, Any]:
    if state == "ABSENT":
        return restore_acceptance._raw_family_pin(
            path=path,
            state="ABSENT",
            sha256=restore_acceptance.ABSENT_PIN,
            size=-1,
            mode=restore_acceptance.ABSENT_PIN,
        )
    info = path.lstat()
    return restore_acceptance._raw_family_pin(
        path=path,
        state="PRESENT",
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        size=info.st_size,
        mode=f"0{info.st_mode & 0o777:03o}",
    )


def _message_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE messages (
          id INTEGER PRIMARY KEY,
          project_id INTEGER NOT NULL,
          sender_id INTEGER NOT NULL,
          thread_id TEXT,
          topic TEXT,
          subject TEXT NOT NULL,
          body_md TEXT NOT NULL,
          importance TEXT NOT NULL,
          ack_required INTEGER NOT NULL,
          created_ts TEXT NOT NULL,
          attachments BLOB
        )
        """
    )
    connection.execute(
        "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            1,
            7,
            9,
            None,
            "日本語",
            "subject",
            "body",
            "normal",
            0,
            "2026-08-11T00:00:00+00:00",
            b"\x00\xff",
        ),
    )
    connection.commit()
    return connection


def _family(root: Path, *, device: int, inode_start: int) -> dict[str, Any]:
    members = {
        role: {
            "state": "PRESENT",
            "canonical_path": str(root / name),
            "device": device,
            "inode": inode_start + offset,
            "mode": 0o600,
            "nlink": 1,
            "no_symlink": True,
        }
        for offset, (role, name) in enumerate(
            (
                ("main", "storage.sqlite3"),
                ("wal", "storage.sqlite3-wal"),
                ("shm", "storage.sqlite3-shm"),
            )
        )
    }
    return {
        "database": str(root / "storage.sqlite3"),
        "existence_set": ["main", "shm", "wal"],
        "members": members,
    }


def test_message_digest_uses_exact_typed_eleven_column_recipe(tmp_path: Path) -> None:
    database = tmp_path / "messages.sqlite3"
    connection = _message_database(database)
    connection.close()

    observation = restore_acceptance._capture_message_window(database)

    expected_typed = [
        [
            ["integer", 1],
            ["integer", 7],
            ["integer", 9],
            ["null", None],
            ["text", "日本語"],
            ["text", "subject"],
            ["text", "body"],
            ["text", "normal"],
            ["integer", 0],
            ["text", "2026-08-11T00:00:00+00:00"],
            ["blob", hashlib.sha256(b"\x00\xff").hexdigest(), 2],
        ]
    ]
    expected = hashlib.sha256(
        json.dumps(
            expected_typed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert observation["columns"] == list(restore_acceptance.MESSAGE_COLUMNS)
    assert observation["transaction"] == "one-read-transaction"
    assert observation["max_id"] == 1
    assert observation["count"] == 1
    assert observation["prefix_sha256"] == expected
    assert observation["new_ids"] == []


def test_message_window_open_mode_is_read_only_with_write_control(
    tmp_path: Path,
) -> None:
    database = tmp_path / "messages.sqlite3"
    connection = _message_database(database)
    connection.close()

    observation = restore_acceptance._capture_message_window(database)
    assert observation["open_mode"]["uri"].endswith("?mode=ro")
    assert observation["open_mode"]["query_only"] == 1

    statement = (
        "INSERT INTO messages VALUES "
        "(99, 7, 9, NULL, 'topic', 'subject', 'body', 'normal', 0, 'later', NULL)"
    )
    digest_before = hashlib.sha256(database.read_bytes()).hexdigest()

    read_only = sqlite3.connect(
        restore_acceptance._database_uri(database), uri=True, isolation_level=None
    )
    try:
        read_only.execute("PRAGMA query_only=ON")
        with pytest.raises(sqlite3.OperationalError, match="read.?only"):
            read_only.execute(statement)
    finally:
        read_only.close()

    assert hashlib.sha256(database.read_bytes()).hexdigest() == digest_before
    assert restore_acceptance._capture_message_window(database)["max_id"] == 1

    # Negative control: the identical statement is a real write when the same
    # file is opened without the read-only URI, so the rejection above is the
    # open mode doing the work and not a statement that never applies.
    writable = sqlite3.connect(database, isolation_level=None)
    try:
        writable.execute(statement)
    finally:
        writable.close()

    assert hashlib.sha256(database.read_bytes()).hexdigest() != digest_before
    assert restore_acceptance._capture_message_window(database)["max_id"] == 99


def test_production_gate_allows_contiguous_message_append(tmp_path: Path) -> None:
    database = tmp_path / "messages.sqlite3"
    connection = _message_database(database)
    before_family = restore_acceptance._family_identity(database, label="before")
    before_messages = restore_acceptance._capture_message_window(database)
    connection.execute(
        "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (2, 7, 9, "t", "topic", "s2", "b2", "high", 1, "later", "[]"),
    )
    connection.commit()
    connection.close()
    after_messages = restore_acceptance._capture_message_window(
        database, prefix_max_id=before_messages["max_id"]
    )
    after_family = restore_acceptance._family_identity(database, label="after")

    evaluation = restore_acceptance._evaluate_production_invariants(
        before_family, after_family, before_messages, after_messages
    )

    assert set(evaluation["invariants"]) == {
        "family_identity_unchanged",
        "maximum_did_not_decrease",
        "prefix_bound_is_before_max",
        "prefix_count_unchanged",
        "prefix_digest_unchanged",
        "count_delta_matches_new_ids",
    }
    assert all(evaluation["invariants"].values())
    assert evaluation["observations"] == {"new_ids_are_contiguous": True}
    assert after_messages["new_ids"] == [2]
    assert before_family == after_family


def test_production_gate_reports_id_gap_without_rejecting(tmp_path: Path) -> None:
    database = tmp_path / "messages.sqlite3"
    connection = _message_database(database)
    before_family = restore_acceptance._family_identity(database, label="before")
    before_messages = restore_acceptance._capture_message_window(database)
    connection.execute(
        "INSERT INTO messages VALUES (3,7,9,NULL,NULL,'s','b','normal',0,'later','[]')"
    )
    connection.commit()
    connection.close()
    after_messages = restore_acceptance._capture_message_window(
        database, prefix_max_id=before_messages["max_id"]
    )
    after_family = restore_acceptance._family_identity(database, label="after")

    evaluation = restore_acceptance._evaluate_production_invariants(
        before_family, after_family, before_messages, after_messages
    )

    assert all(evaluation["invariants"].values())
    assert evaluation["observations"] == {"new_ids_are_contiguous": False}
    assert after_messages["new_ids"] == [3]


def test_production_gate_reports_huge_id_gap_in_constant_auxiliary_space() -> None:
    family = {"members": {"main": {"device": 1, "inode": 2}}}
    before_messages = {
        "max_id": 1,
        "count": 1,
        "prefix_max_id": 1,
        "prefix_count": 1,
        "prefix_sha256": "a" * 64,
        "new_ids": [],
    }
    after_messages = {
        "max_id": 2**63 - 1,
        "count": 2,
        "prefix_max_id": 1,
        "prefix_count": 1,
        "prefix_sha256": "a" * 64,
        "new_ids": [2**63 - 1],
    }

    evaluation = restore_acceptance._evaluate_production_invariants(
        family, family, before_messages, after_messages
    )

    assert all(evaluation["invariants"].values())
    assert evaluation["observations"] == {"new_ids_are_contiguous": False}


@pytest.mark.parametrize(
    "mutation",
    ["prefix", "maximum", "prefix_bound", "prefix_count", "count", "family"],
)
def test_production_gate_rejects_each_non_append_change(
    tmp_path: Path, mutation: str
) -> None:
    database = tmp_path / "messages.sqlite3"
    connection = _message_database(database)
    before_family = restore_acceptance._family_identity(database, label="before")
    before_messages = restore_acceptance._capture_message_window(database)
    if mutation == "prefix":
        connection.execute("UPDATE messages SET body_md='changed' WHERE id=1")
    else:
        connection.execute(
            "INSERT INTO messages VALUES (2,7,9,NULL,NULL,'s','b','normal',0,'later','[]')"
        )
    connection.commit()
    connection.close()
    after_messages = restore_acceptance._capture_message_window(
        database, prefix_max_id=before_messages["max_id"]
    )
    after_family = restore_acceptance._family_identity(database, label="after")
    if mutation == "maximum":
        after_messages["max_id"] = before_messages["max_id"] - 1
    if mutation == "prefix_bound":
        after_messages["prefix_max_id"] += 1
    if mutation == "prefix_count":
        after_messages["prefix_count"] += 1
    if mutation == "count":
        after_messages["count"] += 1
    if mutation == "family":
        after_family["members"]["main"]["inode"] += 1

    with pytest.raises(
        restore_acceptance.RestoreAcceptanceError,
        match="production invariants failed",
    ):
        restore_acceptance._evaluate_production_invariants(
            before_family, after_family, before_messages, after_messages
        )


def test_family_identity_rejects_hardlinks_and_symlinks(tmp_path: Path) -> None:
    database = tmp_path / "storage.sqlite3"
    database.write_bytes(b"sqlite")
    hardlink = tmp_path / "alias.sqlite3"
    os.link(database, hardlink)
    with pytest.raises(
        restore_acceptance.RestoreAcceptanceError, match="nlink=1"
    ):
        restore_acceptance._family_identity(database, label="hardlinked")
    hardlink.unlink()
    symlink = tmp_path / "linked.sqlite3"
    symlink.symlink_to(database)
    with pytest.raises(
        restore_acceptance.RestoreAcceptanceError, match="symbolic path component"
    ):
        restore_acceptance._family_identity(symlink, label="symlinked")


@pytest.mark.skipif(
    not all(path.is_file() for path in ACCEPTED_BACKUP_PATHS.values()),
    reason="accepted production backup family is not present on this machine",
)
def test_accepted_raw_family_opens_once_read_only_and_matches_mandatory_pins() -> None:
    pins = {
        role: restore_acceptance._raw_family_pin(
            path=ACCEPTED_BACKUP_PATHS[role], **ACCEPTED_BACKUP_PINS[role]
        )
        for role in restore_acceptance.RAW_FAMILY_ROLES
    }
    identity_fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_nlink",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    before = {
        role: tuple(
            int(getattr(ACCEPTED_BACKUP_PATHS[role].lstat(), field))
            for field in identity_fields
        )
        for role in restore_acceptance.RAW_FAMILY_ROLES
    }

    descriptor, file_descriptors = restore_acceptance._open_pinned_raw_family(pins)
    try:
        assert descriptor["source_open_count"] == 3
        assert descriptor["source_reopen_allowed"] is False
        assert tuple(
            descriptor["roles"][role]["sha256"]
            for role in restore_acceptance.RAW_FAMILY_ROLES
        ) == tuple(
            ACCEPTED_BACKUP_PINS[role]["sha256"]
            for role in restore_acceptance.RAW_FAMILY_ROLES
        )
        assert all(
            fcntl.fcntl(file_descriptor, fcntl.F_GETFL) & os.O_ACCMODE
            == os.O_RDONLY
            for file_descriptor in file_descriptors
        )
    finally:
        restore_acceptance._close_descriptors(file_descriptors)

    after = {
        role: tuple(
            int(getattr(ACCEPTED_BACKUP_PATHS[role].lstat(), field))
            for field in identity_fields
        )
        for role in restore_acceptance.RAW_FAMILY_ROLES
    }
    assert after == before


@pytest.mark.skipif(
    not all(path.is_file() for path in ACCEPTED_BACKUP_PATHS.values()),
    reason="accepted production backup family is not present on this machine",
)
def test_accepted_raw_family_wrong_sha_fails_closed() -> None:
    pins = {
        role: restore_acceptance._raw_family_pin(
            path=ACCEPTED_BACKUP_PATHS[role], **ACCEPTED_BACKUP_PINS[role]
        )
        for role in restore_acceptance.RAW_FAMILY_ROLES
    }
    pins["main"] = {**pins["main"], "sha256": "0" * 64}

    with pytest.raises(
        restore_acceptance.RestoreAcceptanceError,
        match="main sha256 differs from its mandatory pin",
    ):
        restore_acceptance._open_pinned_raw_family(pins)


def test_inherited_raw_family_copy_never_reopens_source_paths(tmp_path: Path) -> None:
    source = tmp_path / "accepted"
    source.mkdir()
    paths = restore_acceptance._family_paths(source / "storage.sqlite3")
    paths["main"].write_bytes(b"main-bytes")
    paths["wal"].write_bytes(b"")
    paths["shm"].write_bytes(b"shm-bytes")
    os.chmod(paths["main"], 0o640)
    os.chmod(paths["wal"], 0o600)
    os.chmod(paths["shm"], 0o644)
    pins = {role: _pin_for(paths[role]) for role in restore_acceptance.RAW_FAMILY_ROLES}
    descriptor, file_descriptors = restore_acceptance._open_pinned_raw_family(pins)
    renamed_source = tmp_path / "accepted-renamed"
    source.rename(renamed_source)

    try:
        copied = restore_acceptance._copy_inherited_raw_family(
            descriptor, tmp_path / "copied"
        )
    finally:
        restore_acceptance._close_descriptors(file_descriptors)

    for role in restore_acceptance.RAW_FAMILY_ROLES:
        target = Path(copied["files"][role]["path"])
        renamed = renamed_source / paths[role].name
        assert target.read_bytes() == renamed.read_bytes()
        assert copied["files"][role]["sha256"] == pins[role]["sha256"]


def test_raw_family_rejects_unrelated_sidecar_path(tmp_path: Path) -> None:
    main = tmp_path / "storage.sqlite3"
    main.write_bytes(b"main")
    unrelated = tmp_path / "unrelated-wal"
    unrelated.write_bytes(b"wal")
    pins = {
        "main": _pin_for(main),
        "wal": _pin_for(unrelated),
        "shm": _pin_for(Path(f"{main}-shm"), state="ABSENT"),
    }

    with pytest.raises(
        restore_acceptance.RestoreAcceptanceError, match="not the sidecar"
    ):
        restore_acceptance._open_pinned_raw_family(pins)


def test_non_alias_rejects_same_device_inode(tmp_path: Path) -> None:
    production = _family(tmp_path / "production", device=1, inode_start=10)
    target = _family(tmp_path / "target", device=2, inode_start=20)
    target["members"]["main"]["device"] = 1
    target["members"]["main"]["inode"] = 10

    with pytest.raises(
        restore_acceptance.RestoreAcceptanceError, match="device/inode"
    ):
        restore_acceptance._assert_non_alias(
            {"production": production, "target": target}
        )


def test_open_file_gate_requires_target_and_rejects_production(tmp_path: Path) -> None:
    production = _family(tmp_path / "production", device=1, inode_start=10)
    target = _family(tmp_path / "target", device=2, inode_start=20)
    files = [
        {
            "pid": 55,
            "type": "REG",
            "name": record["canonical_path"],
            "device": record["device"],
            "inode": record["inode"],
        }
        for record in target["members"].values()
    ]
    files.append(
        {"pid": 55, "type": "IPv4", "name": "127.0.0.1:28770 (LISTEN)"}
    )
    observation = {"commands": [], "files": files}

    result = restore_acceptance._assert_tree_open_boundary(
        observation,
        production_family=production,
        target_family=target,
        isolated_port=28770,
        require_target=True,
    )

    assert result["status"] == "sampled-isolated-observation"
    assert set(result["target_family_matches"]) == {"main", "wal", "shm"}
    files.append(
        {
            "pid": 55,
            "type": "REG",
            "name": production["members"]["main"]["canonical_path"],
            "device": 1,
            "inode": 10,
        }
    )
    with pytest.raises(
        restore_acceptance.RestoreAcceptanceError, match="production database"
    ):
        restore_acceptance._assert_tree_open_boundary(
            observation,
            production_family=production,
            target_family=target,
            isolated_port=28770,
            require_target=True,
        )


def test_open_file_gate_rejects_production_listener_connection(tmp_path: Path) -> None:
    production = _family(tmp_path / "production", device=1, inode_start=10)
    observation = {
        "commands": [],
        "files": [
            {
                "pid": 55,
                "type": "IPv4",
                "name": "127.0.0.1:54000->127.0.0.1:8765",
            }
        ],
    }
    with pytest.raises(
        restore_acceptance.RestoreAcceptanceError, match="production database"
    ):
        restore_acceptance._assert_tree_open_boundary(
            observation,
            production_family=production,
            target_family=None,
            isolated_port=None,
            require_target=False,
        )


def test_lsof_field_parser_retains_path_device_inode_and_network() -> None:
    raw = (
        "p55\n"
        "cpython\n"
        "f12u\n"
        "tREG\n"
        "D0x1000004\n"
        "i42\n"
        "n/private/tmp/storage.sqlite3\n"
        "f14u\n"
        "tIPv4\n"
        "n127.0.0.1:28770 (LISTEN)\n"
    )

    files = restore_acceptance._parse_lsof_fields(raw)

    assert files == [
        {
            "pid": 55,
            "fd": "12u",
            "type": "REG",
            "device": int("0x1000004", 0),
            "inode": 42,
            "name": "/private/tmp/storage.sqlite3",
        },
        {
            "pid": 55,
            "fd": "14u",
            "type": "IPv4",
            "name": "127.0.0.1:28770 (LISTEN)",
        },
    ]


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (subprocess.TimeoutExpired(["probe"], 1), "deadline expired"),
        (OSError("unavailable"), "could not start"),
    ],
)
def test_run_capture_normalizes_subprocess_failures(
    monkeypatch: pytest.MonkeyPatch, failure: Exception, message: str
) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise failure

    monkeypatch.setattr(restore_acceptance.subprocess, "run", fail)
    with pytest.raises(restore_acceptance.RestoreAcceptanceError, match=message):
        restore_acceptance._run_capture(["probe"], timeout=1)


def test_read_only_probe_calls_no_state_changing_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    class FakeClient:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def list_tools(self) -> list[SimpleNamespace]:
            return [SimpleNamespace(name=name) for name in sorted(COMPATIBILITY_TOOLS)]

        async def call_tool(
            self, name: str, arguments: dict[str, Any]
        ) -> SimpleNamespace:
            calls.append((name, arguments))
            return SimpleNamespace(
                structured_content={
                    "status": "ok",
                    "http_host": "127.0.0.1",
                    "http_port": 28770,
                    "database_url": "sqlite+aiosqlite:////isolated/storage.sqlite3",
                },
                data=None,
            )

    monkeypatch.setattr(restore_acceptance, "Client", FakeClient)

    result = asyncio.run(
        restore_acceptance._read_only_mcp_probe("http://127.0.0.1:28770/api/")
    )

    assert calls == [("health_check", {})]
    assert result["calls"] == ["list_tools", "health_check"]
    assert result["write_calls"] == []
    assert result["tool_count"] == 24


def test_readiness_requires_exact_tools_database_and_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = SimpleNamespace(poll=lambda: None)
    expected_database = "sqlite+aiosqlite:////isolated/storage.sqlite3"

    async def exact_probe(_url: str) -> dict[str, Any]:
        return {
            "tool_names": sorted(COMPATIBILITY_TOOLS),
            "tool_count": 24,
            "health": {
                "status": "ok",
                "http_host": "127.0.0.1",
                "http_port": 28770,
                "database_url": expected_database,
            },
            "calls": ["list_tools", "health_check"],
            "write_calls": [],
        }

    monkeypatch.setattr(restore_acceptance, "_read_only_mcp_probe", exact_probe)
    result = restore_acceptance._wait_read_only_ready(
        process,
        url="http://127.0.0.1:28770/api/",
        expected_database_url=expected_database,
        port=28770,
        timeout_seconds=1,
    )
    assert result["health"]["database_url"] == expected_database

    async def wrong_database(_url: str) -> dict[str, Any]:
        value = await exact_probe(_url)
        value["health"]["database_url"] = "sqlite+aiosqlite:////production.sqlite3"
        return value

    monkeypatch.setattr(
        restore_acceptance, "_read_only_mcp_probe", wrong_database
    )
    with pytest.raises(
        restore_acceptance.RestoreAcceptanceError, match="wrong database"
    ):
        restore_acceptance._wait_read_only_ready(
            process,
            url="http://127.0.0.1:28770/api/",
            expected_database_url=expected_database,
            port=28770,
            timeout_seconds=1,
        )


def test_write_once_pair_publishes_pin_first_and_json_last(tmp_path: Path) -> None:
    receipt = tmp_path / "restore-acceptance.json"
    pin = tmp_path / "restore-acceptance.sha256"

    published = restore_acceptance._publish_write_once_pair(
        receipt, pin, {"z": 1, "a": "日本語"}
    )

    expected_bytes = '{"a":"日本語","z":1}\n'.encode()
    expected_sha256 = hashlib.sha256(expected_bytes).hexdigest()
    assert receipt.read_bytes() == expected_bytes
    assert pin.read_text(encoding="ascii") == f"{expected_sha256}  {receipt.name}\n"
    assert receipt.stat().st_mode & 0o777 == 0o400
    assert pin.stat().st_mode & 0o777 == 0o400
    assert receipt.stat().st_nlink == 1
    assert pin.stat().st_nlink == 1
    assert published["receipt_sha256"] == expected_sha256
    with pytest.raises(
        restore_acceptance.RestoreAcceptanceError, match="must be absent"
    ):
        restore_acceptance._publish_write_once_pair(receipt, pin, {"replacement": True})


def test_write_once_pair_never_leaves_json_when_final_link_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = tmp_path / "restore-acceptance.json"
    pin = tmp_path / "restore-acceptance.sha256"
    original_link = restore_acceptance.os.link
    calls = 0

    def fail_second_link(
        source: os.PathLike[str] | str,
        destination: os.PathLike[str] | str,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected final-link failure")
        original_link(source, destination, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(restore_acceptance.os, "link", fail_second_link)

    with pytest.raises(OSError, match="injected"):
        restore_acceptance._publish_write_once_pair(receipt, pin, {"ok": True})

    assert not receipt.exists()
    assert not pin.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("phase", restore_acceptance.PUBLISH_FAULT_PHASES)
@pytest.mark.parametrize("failure_type", (OSError, KeyboardInterrupt))
def test_write_once_pair_fault_boundary_has_one_irreversible_success_edge(
    tmp_path: Path, phase: str, failure_type: type[BaseException]
) -> None:
    receipt = tmp_path / "restore-acceptance.json"
    pin = tmp_path / "restore-acceptance.sha256"

    def fault_hook(observed: str) -> None:
        if observed == phase:
            raise failure_type(f"injected at {phase}")

    post_commit = phase in {
        "after_pin_temporary_unlink",
        "before_cleanup_directory_fsync",
        "after_cleanup_directory_fsync",
    }
    if post_commit:
        result = restore_acceptance._publish_write_once_pair(
            receipt, pin, {"status": "passed"}, fault_hook=fault_hook
        )
        assert result["post_commit_durability_warning"] == (
            f"{failure_type.__name__}: injected at {phase}"
        )
        assert receipt.stat().st_nlink == 1
        assert pin.stat().st_nlink == 1
        assert receipt.stat().st_mode & 0o777 == 0o400
        assert pin.stat().st_mode & 0o777 == 0o400
        receipt_sha256 = hashlib.sha256(receipt.read_bytes()).hexdigest()
        assert pin.read_text(encoding="ascii") == (
            f"{receipt_sha256}  {receipt.name}\n"
        )
    else:
        with pytest.raises(failure_type, match="injected at"):
            restore_acceptance._publish_write_once_pair(
                receipt, pin, {"status": "passed"}, fault_hook=fault_hook
            )
        assert not receipt.exists()
        assert not pin.exists()
    assert not any(path.name.endswith(".prepared") for path in tmp_path.iterdir())


def test_write_once_pair_actual_prepared_unlink_failure_retracts_canonical_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = tmp_path / "restore-acceptance.json"
    pin = tmp_path / "restore-acceptance.sha256"
    original_unlink = Path.unlink
    injected = False

    def fail_receipt_prepared_once(
        path: Path, missing_ok: bool = False
    ) -> None:
        nonlocal injected
        if (
            not injected
            and path.name.startswith(f".{receipt.name}.")
            and path.name.endswith(".prepared")
        ):
            injected = True
            raise OSError("injected prepared unlink failure")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_receipt_prepared_once)

    with pytest.raises(OSError, match="prepared unlink"):
        restore_acceptance._publish_write_once_pair(
            receipt, pin, {"status": "passed"}
        )

    assert injected
    assert not receipt.exists()
    assert not pin.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("failure_type", (OSError, KeyboardInterrupt))
def test_write_once_pair_reconciles_final_unlink_mutation_then_raise_as_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[BaseException],
) -> None:
    receipt = tmp_path / "restore-acceptance.json"
    pin = tmp_path / "restore-acceptance.sha256"
    original_unlink = Path.unlink
    injected = False

    def unlink_then_raise(
        path: Path, missing_ok: bool = False
    ) -> None:
        nonlocal injected
        if (
            not injected
            and path.name.startswith(f".{pin.name}.")
            and path.name.endswith(".prepared")
        ):
            injected = True
            original_unlink(path, missing_ok=missing_ok)
            raise failure_type("injected after final alias unlink mutation")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink_then_raise)

    result = restore_acceptance._publish_write_once_pair(
        receipt, pin, {"status": "passed"}
    )

    assert injected
    assert result["post_commit_durability_warning"] == (
        f"{failure_type.__name__}: injected after final alias unlink mutation; "
        "final alias unlink reconciled as committed"
    )
    assert receipt.stat().st_nlink == 1
    assert pin.stat().st_nlink == 1
    assert receipt.stat().st_mode & 0o777 == 0o400
    assert pin.stat().st_mode & 0o777 == 0o400
    receipt_sha256 = hashlib.sha256(receipt.read_bytes()).hexdigest()
    assert pin.read_text(encoding="ascii") == f"{receipt_sha256}  {receipt.name}\n"
    assert not any(path.name.endswith(".prepared") for path in tmp_path.iterdir())


def test_write_once_pair_final_unlink_unknown_state_retracts_reader_valid_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = tmp_path / "restore-acceptance.json"
    pin = tmp_path / "restore-acceptance.sha256"
    original_unlink = Path.unlink
    injected = False

    def unlink_mutate_canonical_then_raise(
        path: Path, missing_ok: bool = False
    ) -> None:
        nonlocal injected
        if (
            not injected
            and path.name.startswith(f".{pin.name}.")
            and path.name.endswith(".prepared")
        ):
            injected = True
            original_unlink(path, missing_ok=missing_ok)
            os.chmod(pin, 0o600)
            raise OSError("injected ambiguous final alias unlink outcome")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink_mutate_canonical_then_raise)

    with pytest.raises(
        restore_acceptance.RestoreAcceptanceError,
        match="publication state is unknown.*must be retracted",
    ):
        restore_acceptance._publish_write_once_pair(
            receipt, pin, {"status": "passed"}
        )

    assert injected
    assert not receipt.exists()
    assert not pin.exists()
    assert not any(path.name.endswith(".prepared") for path in tmp_path.iterdir())


def test_write_once_pair_reconcile_lstat_failure_retracts_reader_valid_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = tmp_path / "restore-acceptance.json"
    pin = tmp_path / "restore-acceptance.sha256"
    original_unlink = Path.unlink
    original_lstat = Path.lstat
    mutation_completed = False
    observation_failed = False

    def unlink_then_raise(
        path: Path, missing_ok: bool = False
    ) -> None:
        nonlocal mutation_completed
        if (
            not mutation_completed
            and path.name.startswith(f".{pin.name}.")
            and path.name.endswith(".prepared")
        ):
            original_unlink(path, missing_ok=missing_ok)
            mutation_completed = True
            raise OSError("injected after final alias unlink mutation")
        original_unlink(path, missing_ok=missing_ok)

    def fail_first_reconcile_lstat(path: Path) -> os.stat_result:
        nonlocal observation_failed
        if mutation_completed and not observation_failed and path == receipt:
            observation_failed = True
            raise OSError("injected reconcile lstat failure")
        return original_lstat(path)

    monkeypatch.setattr(Path, "unlink", unlink_then_raise)
    monkeypatch.setattr(Path, "lstat", fail_first_reconcile_lstat)

    with pytest.raises(
        restore_acceptance.RestoreAcceptanceError,
        match="publication state is unknown.*must be retracted",
    ):
        restore_acceptance._publish_write_once_pair(
            receipt, pin, {"status": "passed"}
        )

    assert mutation_completed
    assert observation_failed
    assert not receipt.exists()
    assert not pin.exists()
    assert not any(path.name.endswith(".prepared") for path in tmp_path.iterdir())


def test_write_once_pair_actual_precommit_directory_fsync_failure_is_not_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = tmp_path / "restore-acceptance.json"
    pin = tmp_path / "restore-acceptance.sha256"
    original_fsync = restore_acceptance._fsync_directory
    calls = 0

    def fail_once(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 4:
            raise OSError("injected directory fsync failure 4")
        original_fsync(path)

    monkeypatch.setattr(restore_acceptance, "_fsync_directory", fail_once)

    with pytest.raises(OSError, match="directory fsync"):
        restore_acceptance._publish_write_once_pair(
            receipt, pin, {"status": "passed"}
        )

    assert not receipt.exists()
    assert not pin.exists()
    assert list(tmp_path.iterdir()) == []


def test_write_once_pair_actual_postcommit_directory_fsync_failure_is_success_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = tmp_path / "restore-acceptance.json"
    pin = tmp_path / "restore-acceptance.sha256"
    original_fsync = restore_acceptance._fsync_directory
    calls = 0

    def fail_once(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 5:
            raise OSError("injected post-commit directory fsync failure")
        original_fsync(path)

    monkeypatch.setattr(restore_acceptance, "_fsync_directory", fail_once)

    result = restore_acceptance._publish_write_once_pair(
        receipt, pin, {"status": "passed"}
    )

    assert result["post_commit_durability_warning"] == (
        "OSError: injected post-commit directory fsync failure"
    )
    assert receipt.stat().st_nlink == 1
    assert pin.stat().st_nlink == 1
    assert receipt.stat().st_mode & 0o777 == 0o400
    assert pin.stat().st_mode & 0o777 == 0o400


def test_evidence_cli_exposes_restore_rehearsal_contract() -> None:
    parser = evidence._parser()
    args = parser.parse_args(
        [
            "restore-rehearsal",
            "--run-dir",
            "/tmp/run",
            "--receipt",
            "/tmp/result.json",
            "--receipt-sha256",
            "/tmp/result.sha256",
            "--wheel",
            "/tmp/candidate.whl",
            "--candidate-repo",
            "/tmp/repo",
            "--candidate-commit",
            "1" * 40,
            "--backup-main",
            "/tmp/backup.sqlite3",
            "--backup-main-state",
            "PRESENT",
            "--backup-main-sha256",
            "2" * 64,
            "--backup-main-size",
            "123",
            "--backup-main-mode",
            "0644",
            "--backup-wal",
            "/tmp/backup.sqlite3-wal",
            "--backup-wal-state",
            "ABSENT",
            "--backup-wal-sha256",
            "ABSENT",
            "--backup-wal-size",
            "-1",
            "--backup-wal-mode",
            "ABSENT",
            "--backup-shm",
            "/tmp/backup.sqlite3-shm",
            "--backup-shm-state",
            "PRESENT",
            "--backup-shm-sha256",
            "3" * 64,
            "--backup-shm-size",
            "32768",
            "--backup-shm-mode",
            "0644",
            "--production-db",
            "/tmp/production.sqlite3",
            "--expected-logical-sha256",
            "5" * 64,
            "--expected-message-max-id",
            "8829",
            "--expected-message-sha256",
            "6" * 64,
        ]
    )
    assert args.command == "restore-rehearsal"
    assert args.port == 28770
    assert args.timeout_seconds == 20
    assert args.worker_timeout_seconds == 120


def test_candidate_shutdown_uses_one_shared_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    observed_wait_timeouts: list[float] = []

    class FakeProcess:
        def send_signal(self, observed_signal: int) -> None:
            assert observed_signal == signal.SIGTERM

        def wait(self, *, timeout: float) -> int:
            observed_wait_timeouts.append(timeout)
            clock[0] = 15.0
            return 0

    def slow_close(_port: int, *, timeout_seconds: float) -> dict[str, Any]:
        assert timeout_seconds == pytest.approx(5.0)
        clock[0] = 21.0
        return {"status": "closed", "deadline_seconds": timeout_seconds}

    monkeypatch.setattr(restore_acceptance.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(restore_acceptance, "_wait_closed", slow_close)
    monkeypatch.setattr(
        restore_acceptance,
        "_remaining_processes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("descendant check must not start after the shared deadline")
        ),
    )

    with pytest.raises(
        restore_acceptance.RestoreAcceptanceError,
        match="shared deadline",
    ):
        restore_acceptance._shutdown_candidate_server(
            FakeProcess(),  # type: ignore[arg-type]
            port=28770,
            process_ids=[123],
            process_group=123,
            timeout_seconds=20,
        )

    assert observed_wait_timeouts == [20.0]


def test_run_from_evidence_args_maps_every_pinned_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"status": "completed"}

    monkeypatch.setattr(restore_acceptance, "run_restore_acceptance", fake_run)
    args = evidence._parser().parse_args(
        [
            "restore-rehearsal",
            "--run-dir",
            "/tmp/run",
            "--receipt",
            "/tmp/result.json",
            "--receipt-sha256",
            "/tmp/result.sha256",
            "--wheel",
            "/tmp/candidate.whl",
            "--candidate-repo",
            "/tmp/repo",
            "--candidate-commit",
            "1" * 40,
            "--backup-main",
            "/tmp/backup.sqlite3",
            "--backup-main-state",
            "PRESENT",
            "--backup-main-sha256",
            "2" * 64,
            "--backup-main-size",
            "123",
            "--backup-main-mode",
            "0644",
            "--backup-wal",
            "/tmp/backup.sqlite3-wal",
            "--backup-wal-state",
            "ABSENT",
            "--backup-wal-sha256",
            "ABSENT",
            "--backup-wal-size",
            "-1",
            "--backup-wal-mode",
            "ABSENT",
            "--backup-shm",
            "/tmp/backup.sqlite3-shm",
            "--backup-shm-state",
            "PRESENT",
            "--backup-shm-sha256",
            "3" * 64,
            "--backup-shm-size",
            "32768",
            "--backup-shm-mode",
            "0644",
            "--production-db",
            "/tmp/production.sqlite3",
            "--expected-logical-sha256",
            "5" * 64,
            "--expected-message-max-id",
            "8829",
            "--expected-message-sha256",
            "6" * 64,
        ]
    )

    assert restore_acceptance.run_from_evidence_args(args) == {"status": "completed"}
    assert captured["candidate_commit"] == "1" * 40
    assert captured["backup_main"] == Path("/tmp/backup.sqlite3")
    assert captured["backup_wal_state"] == "ABSENT"
    assert captured["backup_shm_size"] == 32768
    assert captured["expected_message_max_id"] == 8829
    assert captured["port"] == 28770
    assert captured["timeout_seconds"] == 20
    assert captured["worker_timeout_seconds"] == 120


@pytest.mark.skipif(
    not all(path.is_file() for path in ACCEPTED_BACKUP_PATHS.values())
    or not Path("/usr/sbin/lsof").is_file(),
    reason="accepted backup family or lsof is not present on this machine",
)
def test_joint_success_raw_restore_server_contract_shutdown_and_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    production_root = tmp_path / "production"
    production_root.mkdir()
    production_database = production_root / "storage.sqlite3"
    production_paths = restore_acceptance._family_paths(production_database)
    for role in restore_acceptance.RAW_FAMILY_ROLES:
        shutil.copy2(ACCEPTED_BACKUP_PATHS[role], production_paths[role])

    repository = tmp_path / "candidate"
    repository.mkdir()
    wheel = tmp_path / "candidate.whl"
    wheel.write_bytes(b"candidate-wheel-binding-is-covered-separately")
    output = tmp_path / "output"
    output.mkdir()
    receipt = output / "restore-acceptance.json"
    pin = output / "restore-acceptance.sha256"

    monkeypatch.setattr(
        evidence,
        "_candidate_identity",
        lambda *_args, **_kwargs: {
            "status": "exact",
            "commit": "1" * 40,
        },
    )
    monkeypatch.setattr(
        evidence,
        "_verify_running_from_wheel",
        lambda *_args, **_kwargs: {
            "status": "exact",
            "wheel": str(wheel),
        },
    )
    try:
        restore_acceptance._run_capture(
            ["/bin/ps", "-ww", "-axo", "pid=,ppid=,pgid=,command="],
            timeout=2,
        )
    except restore_acceptance.RestoreAcceptanceError:
        # The Codex macOS sandbox denies process-list enumeration.  Keep the
        # real lsof sampling, listener checks, server, shutdown and publication
        # in this joined test; process-tree expansion itself has focused tests
        # and runs unmodified outside that sandbox.
        def direct_root_tree(process_id: int) -> dict[str, Any]:
            return {
                "root_pid": process_id,
                "root_pgid": os.getpgid(process_id),
                "pids": [process_id],
                "records": [{"pid": process_id}],
                "raw_selected_ps": "sandbox-root-only-test-observation",
            }

        monkeypatch.setattr(
            restore_acceptance, "_capture_process_tree", direct_root_tree
        )
        monkeypatch.setattr(
            restore_acceptance,
            "_remaining_processes",
            lambda _pids, _pgid, **_kwargs: [],
        )
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as production_listener:
        production_listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        production_listener.bind(("127.0.0.1", 0))
        production_listener.listen()
        production_port = int(production_listener.getsockname()[1])
        monkeypatch.setattr(restore_acceptance, "PRODUCTION_PORT", production_port)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            isolated_port = int(reservation.getsockname()[1])

        result = restore_acceptance.run_restore_acceptance(
            run_directory=tmp_path / "run",
            receipt_path=receipt,
            pin_path=pin,
            wheel=wheel,
            candidate_repository=repository,
            candidate_commit="1" * 40,
            backup_main=ACCEPTED_BACKUP_PATHS["main"],
            backup_main_state="PRESENT",
            backup_main_sha256=ACCEPTED_BACKUP_PINS["main"]["sha256"],
            backup_main_size=ACCEPTED_BACKUP_PINS["main"]["size"],
            backup_main_mode=ACCEPTED_BACKUP_PINS["main"]["mode"],
            backup_wal=ACCEPTED_BACKUP_PATHS["wal"],
            backup_wal_state="PRESENT",
            backup_wal_sha256=ACCEPTED_BACKUP_PINS["wal"]["sha256"],
            backup_wal_size=ACCEPTED_BACKUP_PINS["wal"]["size"],
            backup_wal_mode=ACCEPTED_BACKUP_PINS["wal"]["mode"],
            backup_shm=ACCEPTED_BACKUP_PATHS["shm"],
            backup_shm_state="PRESENT",
            backup_shm_sha256=ACCEPTED_BACKUP_PINS["shm"]["sha256"],
            backup_shm_size=ACCEPTED_BACKUP_PINS["shm"]["size"],
            backup_shm_mode=ACCEPTED_BACKUP_PINS["shm"]["mode"],
            production_database=production_database,
            expected_logical_sha256=ACCEPTED_LOGICAL_SHA256,
            expected_message_max_id=ACCEPTED_MESSAGE_MAX_ID,
            expected_message_sha256=ACCEPTED_MESSAGE_SHA256,
            port=isolated_port,
            timeout_seconds=20,
            worker_timeout_seconds=120,
        )

    assert result["status"] == "completed"
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert payload["status"] == "passed"
    assert all(payload["gates"].values())
    assert set(payload["production"]["invariants"]) == {
        "family_identity_unchanged",
        "maximum_did_not_decrease",
        "prefix_bound_is_before_max",
        "prefix_count_unchanged",
        "prefix_digest_unchanged",
        "count_delta_matches_new_ids",
    }
    assert all(payload["production"]["invariants"].values())
    assert set(payload["production"]["observations"]) == {"new_ids_are_contiguous"}
    assert type(payload["production"]["observations"]["new_ids_are_contiguous"]) is bool
    observer_payload = payload["observer"]
    # The receipt may not carry a production counter that nothing counts: every
    # production claim is a declared scope bound to evidence recorded elsewhere.
    assert [
        name
        for name, value in observer_payload.items()
        if name.startswith("production_") and isinstance(value, (int, float))
    ] == []
    assert observer_payload["production_write_claim_scope"]
    assert observer_payload["production_network_claim_scope"]
    for phase in ("before", "after"):
        open_mode = payload["production"][phase]["messages"]["open_mode"]
        assert open_mode["query_only"] == 1
        assert open_mode["uri"].endswith("?mode=ro")
    assert payload["production"]["before"]["listener"]["method"] == (
        "lsof-listener-table-no-network-connection"
    )
    assert payload["candidate_server"]["readiness"]["tool_count"] == 24
    assert payload["candidate_server"]["shutdown"]["exit_code"] == 0
    assert payload["deadlines"] == {
        "restore_worker_seconds": 120,
        "candidate_runtime_seconds": 20,
    }
    assert payload["restore_worker"]["deadline_seconds"] == 120
    assert payload["candidate_server"]["readiness"]["deadline_seconds"] == 20
    assert payload["candidate_server"]["shutdown"]["deadline_seconds"] == 20
    assert payload["candidate_server"]["shutdown"]["bounded_shutdown_ms"] <= 20_000
    assert (
        payload["candidate_server"]["shutdown"]["endpoint"]["deadline_seconds"]
        <= 20
    )
    assert payload["publication_contract"]["commit_transition"] == (
        "second_prepared_alias_unlink_returns"
    )
    assert payload["publication_contract"]["reader_synchronization"] == (
        "evaluate canonical receipt and pin only after the publisher command completes"
    )
    assert payload["publication_contract"]["post_commit_directory_fsync"] == (
        "best_effort_durability_only"
    )
    assert payload["restore_worker"]["result"]["backup"]["receipt"]["tool"] == (
        "agentstack-mail-migrate"
    )
    assert payload["restore_worker"]["result"]["migration"]["verification"] == {
        "status": "verified",
        "state_sha256": payload["restore_worker"]["result"]["migration"][
            "result"
        ]["state_sha256"],
    }
    worker_command = payload["restore_worker"]["command"]
    assert "--raw-family-descriptor" in worker_command
    assert "--backup-dir" not in worker_command
    assert "--migration-manifest" not in worker_command
    assert pin.read_text(encoding="ascii").startswith(result["receipt_sha256"])


def test_observer_failure_never_publishes_terminal_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "candidate"
    repository.mkdir()
    wheel = tmp_path / "candidate.whl"
    wheel.write_bytes(b"wheel")
    backup = tmp_path / "accepted"
    backup.mkdir()
    backup_main = backup / "storage.sqlite3"
    backup_main.write_bytes(b"backup")
    backup_wal = backup / "storage.sqlite3-wal"
    backup_shm = backup / "storage.sqlite3-shm"
    production_root = tmp_path / "production"
    production_root.mkdir()
    production = production_root / "storage.sqlite3"
    production.write_bytes(b"production")
    output = tmp_path / "output"
    output.mkdir()
    receipt = output / "restore-acceptance.json"
    pin = output / "restore-acceptance.sha256"
    family = _family(production_root, device=1, inode_start=10)
    messages = {
        "max_id": 1,
        "count": 1,
        "prefix_max_id": 1,
        "prefix_count": 1,
        "prefix_sha256": "a" * 64,
        "new_ids": [],
    }
    monkeypatch.setattr(restore_acceptance, "_port_accepts", lambda _port: False)
    monkeypatch.setattr(evidence, "_candidate_identity", lambda *_a, **_k: {})
    monkeypatch.setattr(evidence, "_verify_running_from_wheel", lambda *_a, **_k: {})
    monkeypatch.setattr(
        restore_acceptance, "_family_identity", lambda *_a, **_k: family
    )
    monkeypatch.setattr(
        restore_acceptance, "_capture_message_window", lambda *_a, **_k: messages
    )
    monkeypatch.setattr(
        restore_acceptance,
        "_listener_observation",
        lambda port: {"port": port, "pids": [101]},
    )
    monkeypatch.setattr(
        restore_acceptance,
        "_run_restore_worker",
        lambda **_kwargs: (_ for _ in ()).throw(
            restore_acceptance.RestoreAcceptanceError("injected worker failure")
        ),
    )

    with pytest.raises(
        restore_acceptance.RestoreAcceptanceError, match="injected worker failure"
    ):
        restore_acceptance.run_restore_acceptance(
            run_directory=tmp_path / "run",
            receipt_path=receipt,
            pin_path=pin,
            wheel=wheel,
            candidate_repository=repository,
            candidate_commit="1" * 40,
            backup_main=backup_main,
            backup_main_state="PRESENT",
            backup_main_sha256=hashlib.sha256(b"backup").hexdigest(),
            backup_main_size=6,
            backup_main_mode="0644",
            backup_wal=backup_wal,
            backup_wal_state="ABSENT",
            backup_wal_sha256="ABSENT",
            backup_wal_size=-1,
            backup_wal_mode="ABSENT",
            backup_shm=backup_shm,
            backup_shm_state="ABSENT",
            backup_shm_sha256="ABSENT",
            backup_shm_size=-1,
            backup_shm_mode="ABSENT",
            production_database=production,
            expected_logical_sha256="b" * 64,
            expected_message_max_id=1,
            expected_message_sha256="a" * 64,
            port=28770,
            timeout_seconds=1,
        )

    assert not receipt.exists()
    assert not pin.exists()
