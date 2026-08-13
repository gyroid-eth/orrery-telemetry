from __future__ import annotations

import base64
import hashlib
import json
import os
import plistlib
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from agentstack_mail import evidence


def _completed(
    arguments: list[str],
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(arguments, returncode, stdout, stderr)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_port(port: int, *, present: bool, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if evidence._port_accepts(port) is present:
            return
        time.sleep(0.05)
    raise AssertionError(f"port {port} did not reach present={present}")


def test_terminal_receipt_is_canonical_exclusive_and_read_only(tmp_path: Path) -> None:
    receipt = tmp_path / "receipt.json"
    digest = evidence._write_terminal(receipt, {"z": 1, "a": "日本語"})

    assert receipt.read_bytes() == '{"a":"日本語","z":1}\n'.encode()
    assert digest == hashlib.sha256(receipt.read_bytes()).hexdigest()
    assert receipt.stat().st_mode & 0o777 == 0o400
    with pytest.raises(FileExistsError):
        evidence._write_terminal(receipt, {"replacement": True})


@pytest.mark.parametrize("failure_call", [1, 2])
def test_terminal_receipt_fsync_failure_leaves_no_canonical_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_call: int,
) -> None:
    receipt = tmp_path / "receipt.json"
    real_fsync = evidence.os.fsync
    calls = 0

    def fail_selected(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == failure_call:
            raise OSError("injected fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(evidence.os, "fsync", fail_selected)

    with pytest.raises(OSError, match="injected fsync failure"):
        evidence._write_terminal(receipt, {"status": "passed"})

    assert not receipt.exists()
    assert len(list(tmp_path.glob(".receipt.json.*.unconfirmed"))) == 1


def test_terminal_receipt_digest_failure_precedes_canonical_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = tmp_path / "receipt.json"
    monkeypatch.setattr(
        evidence,
        "_sha256_bytes",
        lambda _payload: (_ for _ in ()).throw(OSError("digest failed")),
    )

    with pytest.raises(OSError, match="digest failed"):
        evidence._write_terminal(receipt, {"status": "passed"})

    assert not receipt.exists()


def test_terminal_receipt_interrupt_quarantines_canonical_success_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = tmp_path / "receipt.json"
    monkeypatch.setattr(
        evidence.os,
        "fsync",
        lambda _descriptor: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        evidence._write_terminal(receipt, {"status": "passed"})

    assert not receipt.exists()
    assert len(list(tmp_path.glob(".receipt.json.*.unconfirmed"))) == 1


@pytest.mark.parametrize("failure_type", (OSError, KeyboardInterrupt))
def test_terminal_receipt_open_mutation_then_raise_quarantines_canonical_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[BaseException],
) -> None:
    receipt = tmp_path / "receipt.json"
    real_open = evidence.os.open
    injected = False

    def open_then_raise(path: object, flags: int, mode: int = 0o777) -> int:
        nonlocal injected
        descriptor = real_open(path, flags, mode)
        if Path(path) == receipt and flags & evidence.os.O_EXCL and not injected:
            injected = True
            evidence.os.close(descriptor)
            raise failure_type("injected after terminal open mutation")
        return descriptor

    monkeypatch.setattr(evidence.os, "open", open_then_raise)
    with pytest.raises(failure_type, match="after terminal open mutation"):
        evidence._write_terminal(receipt, {"status": "passed"})

    assert injected
    assert not receipt.exists()
    assert len(list(tmp_path.glob(".receipt.json.*.unconfirmed"))) == 1


@pytest.mark.parametrize("failure_type", (OSError, KeyboardInterrupt))
def test_terminal_set_call_return_interrupt_quarantines_current_canonical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[BaseException],
) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    real_write = evidence._write_terminal
    injected = False

    def write_then_raise(path: Path, value: object) -> str:
        nonlocal injected
        digest = real_write(path, value)
        if path == first and not injected:
            injected = True
            raise failure_type("injected after terminal call return")
        return digest

    monkeypatch.setattr(evidence, "_write_terminal", write_then_raise)
    with pytest.raises(failure_type, match="after terminal call return"):
        evidence._publish_terminal_set(
            ((first, {"status": "passed"}), (second, {"status": "passed"}))
        )

    assert injected
    assert not first.exists()
    assert not second.exists()
    assert len(list(tmp_path.glob(".first.json.*.unconfirmed"))) == 1


def test_terminal_set_failure_quarantines_earlier_canonical_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    real_write = evidence._write_terminal
    calls = 0

    def fail_second(path: Path, value: object) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("second publication failed")
        return real_write(path, value)

    monkeypatch.setattr(evidence, "_write_terminal", fail_second)

    with pytest.raises(OSError, match="second publication failed"):
        evidence._publish_terminal_set(
            ((first, {"status": "passed"}), (second, {"status": "passed"}))
        )

    assert not first.exists()
    assert not second.exists()
    assert len(list(tmp_path.glob(".first.json.*.unconfirmed"))) == 1


def test_terminal_set_interrupt_quarantines_earlier_canonical_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    real_write = evidence._write_terminal
    calls = 0

    def interrupt_second(path: Path, value: object) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt()
        return real_write(path, value)

    monkeypatch.setattr(evidence, "_write_terminal", interrupt_second)

    with pytest.raises(KeyboardInterrupt):
        evidence._publish_terminal_set(
            ((first, {"status": "passed"}), (second, {"status": "passed"}))
        )

    assert not first.exists()
    assert not second.exists()
    assert len(list(tmp_path.glob(".first.json.*.unconfirmed"))) == 1


def test_listener_owner_query_finds_only_the_isolated_server(tmp_path: Path) -> None:
    port = _free_port()
    process = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        cwd=tmp_path,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_port(port, present=True)
        assert evidence._listener_process_ids(port) == [process.pid]
    finally:
        process.terminate()
        process.wait(timeout=10)
        _wait_port(port, present=False)


def test_candidate_identity_binds_clean_exact_commit_and_source(tmp_path: Path) -> None:
    repository = tmp_path / "candidate"
    source = repository / evidence.PACKAGE_EVIDENCE_PATH
    source.parent.mkdir(parents=True)
    source.write_bytes(Path(evidence.__file__).read_bytes())
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=Evidence Test",
            "-c",
            "user.email=evidence@example.invalid",
            "commit",
            "-qm",
            "candidate",
        ],
        check=True,
    )
    commit = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    identity = evidence._candidate_identity(repository, commit)

    assert identity["head"] == commit
    assert identity["tracked_and_untracked_worktree_clean"] is True
    assert identity["evidence_py_sha256"] == hashlib.sha256(
        Path(evidence.__file__).read_bytes()
    ).hexdigest()

    source.write_text("dirty\n", encoding="utf-8")
    with pytest.raises(evidence.EvidenceError, match="completely clean"):
        evidence._candidate_identity(repository, commit)


def test_failed_rehearsal_cleans_every_spawned_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spawned: list[subprocess.Popen[str]] = []

    def fail_after_spawn(**kwargs: Any) -> dict[str, Any]:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
            text=True,
        )
        spawned.append(process)
        kwargs["processes"].append(process)
        raise evidence.EvidenceError("injected failure")

    monkeypatch.setattr(evidence, "_run_runtime_rehearsal", fail_after_spawn)

    with pytest.raises(evidence.EvidenceError, match="injected failure"):
        evidence.run_runtime_rehearsal(
            output_directory=tmp_path / "output",
            wheel=tmp_path / "candidate.whl",
            candidate_repository=tmp_path,
            candidate_commit="1" * 40,
            port=_free_port(),
        )

    assert len(spawned) == 1
    assert spawned[0].poll() is not None


def test_rejected_legacy_port_never_invokes_listener_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def reject_before_spawn(**_kwargs: Any) -> dict[str, Any]:
        raise evidence.EvidenceError("legacy port refused")

    monkeypatch.setattr(evidence, "_run_runtime_rehearsal", reject_before_spawn)
    monkeypatch.setattr(
        evidence,
        "_cleanup_isolated_runtime",
        lambda *_args, **_kwargs: pytest.fail("cleanup must not inspect port 8765"),
    )

    with pytest.raises(evidence.EvidenceError, match="legacy port refused"):
        evidence.run_runtime_rehearsal(
            output_directory=tmp_path / "output",
            wheel=tmp_path / "candidate.whl",
            candidate_repository=tmp_path,
            candidate_commit="1" * 40,
            port=evidence.LEGACY_PORT,
        )


def test_terminal_receipt_payload_has_no_caller_authored_verdict(tmp_path: Path) -> None:
    receipt = tmp_path / "receipt.json"
    evidence._write_terminal(receipt, {"kind": "service-lifecycle", "sequence": []})

    payload = json.loads(receipt.read_bytes())
    assert not ({"status", "passed", "verdict"} & payload.keys())


def test_disabled_override_snapshot_retains_only_exact_label() -> None:
    raw = """disabled services = {
        \"unrelated.private.job\" => disabled
        \"org.orrery.mail.rehearsal.12345678.once\" => enabled
    }
"""
    calls: list[list[str]] = []

    def runner(arguments: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        return _completed(arguments, stdout=raw)

    result = evidence._disabled_override_snapshot(
        "org.orrery.mail.rehearsal.12345678.once",
        runner=runner,
    )

    assert calls[0][1:] == ["print-disabled", f"gui/{evidence.os.getuid()}"]
    assert result == {
        "method": "launchctl-print-disabled-exact-label-only",
        "label": "org.orrery.mail.rehearsal.12345678.once",
        "entry_present": True,
        "disabled": False,
        "raw_domain_output_retained": False,
    }
    assert "unrelated" not in json.dumps(result)


def test_disabled_override_snapshot_rejects_duplicate_exact_label() -> None:
    label = "org.orrery.mail.rehearsal.12345678.once"

    def runner(arguments: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return _completed(
            arguments,
            stdout=(
                "disabled services = {\n"
                f'    "{label}" => enabled\n'
                f'    "{label}" => disabled\n'
                "}\n"
            ),
        )

    with pytest.raises(evidence.EvidenceError, match="repeated"):
        evidence._disabled_override_snapshot(label, runner=runner)


def test_launchd_job_fingerprint_distinguishes_absent_and_stable_loaded() -> None:
    outputs = iter(
        (
            _completed(["launchctl"], returncode=113, stderr="not found"),
            _completed(
                ["launchctl"],
                stdout="""path = /private/tmp/rehearsal.plist
program = /private/tmp/bin/agentstack-mail-service
arguments = {
    /private/tmp/bin/agentstack-mail-service
    foreground
}
""",
            ),
        )
    )

    def runner(_arguments: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return next(outputs)

    absent = evidence._launchd_job_fingerprint("org.orrery.mail", runner=runner)
    loaded = evidence._launchd_job_fingerprint("org.orrery.mail", runner=runner)

    assert absent["state"] == "absent"
    assert loaded["state"] == "loaded"
    assert len(loaded["definition_sha256"]) == 64
    assert "private/tmp" not in json.dumps(loaded)


def test_legacy_launchd_definition_snapshot_binds_loaded_job_to_plist(
    tmp_path: Path,
) -> None:
    plist = tmp_path / f"{evidence.LEGACY_LAUNCHD_LABEL}.plist"
    definition = {
        "Label": evidence.LEGACY_LAUNCHD_LABEL,
        "ProgramArguments": ["/bin/bash", "/private/tmp/run-server.sh"],
        "KeepAlive": True,
        "RunAtLoad": True,
        "WorkingDirectory": "/private/tmp/legacy",
    }
    plist.write_bytes(plistlib.dumps(definition))

    def runner(arguments: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return _completed(
            arguments,
            stdout=f"""path = {plist}
program = /bin/bash
arguments = {{
    /bin/bash
    /private/tmp/run-server.sh
}}
""",
        )

    snapshot = evidence._launchd_definition_snapshot(
        evidence.LEGACY_LAUNCHD_LABEL,
        runner=runner,
    )

    assert snapshot["plist_path"] == str(plist)
    assert snapshot["state"] == "loaded"
    assert snapshot["program_arguments"] == definition["ProgramArguments"]
    assert snapshot["keep_alive"] is True
    assert snapshot["working_directory"] == "/private/tmp/legacy"
    assert snapshot["loaded_path_program_arguments_match_plist"] is True
    assert base64.b64decode(snapshot["plist_bytes_base64"]) == plist.read_bytes()


def test_legacy_launchd_definition_snapshot_allows_explicit_offline_absence() -> None:
    def runner(arguments: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return _completed(arguments, returncode=113, stderr="not found")

    snapshot = evidence._launchd_definition_snapshot(
        evidence.LEGACY_LAUNCHD_LABEL,
        runner=runner,
        allow_absent=True,
    )

    assert snapshot == {
        "identity": f"gui/{os.getuid()}/{evidence.LEGACY_LAUNCHD_LABEL}",
        "label": evidence.LEGACY_LAUNCHD_LABEL,
        "state": "absent",
    }


def test_legacy_launchd_observation_requires_exact_live_topology(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition = {
        "state": "loaded",
        "plist_path": "/private/tmp/legacy.plist",
        "program": "/bin/bash",
        "program_arguments": ["/bin/bash", "/private/tmp/run.sh"],
    }
    stable = {
        "path": definition["plist_path"],
        "program": definition["program"],
        "arguments": definition["program_arguments"],
    }
    monkeypatch.setattr(
        evidence,
        "_launchd_definition_snapshot",
        lambda *_args, **_kwargs: definition,
    )
    monkeypatch.setattr(
        evidence,
        "_listener_fingerprint",
        lambda _port: {"port": 8765, "listener_count": 1},
    )
    monkeypatch.setattr(
        evidence,
        "_launchd_job_runtime",
        lambda _label: {
            "identity": f"gui/501/{evidence.LEGACY_LAUNCHD_LABEL}",
            "definition_sha256": evidence._sha256_bytes(
                evidence._canonical_json(stable)
            ),
            "wrapper_pid": 101,
        },
    )
    monkeypatch.setattr(evidence, "_listener_process_ids", lambda _port: [202])
    monkeypatch.setattr(
        evidence,
        "_process_record",
        lambda pid: {"pid": pid, "ppid": 101 if pid == 202 else 1, "arguments": []},
    )

    observation = evidence._legacy_launchd_observation(require_loaded=True)

    assert observation["cutover_eligible"] is True
    assert observation["runtime"]["wrapper_pid"] == 101
    assert observation["runtime"]["listener_pid"] == 202
    assert observation["runtime"]["listener_is_wrapper_child"] is True


def test_offline_legacy_observation_rejects_a_foreign_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        evidence,
        "_launchd_definition_snapshot",
        lambda *_args, **_kwargs: {"state": "absent"},
    )
    monkeypatch.setattr(
        evidence,
        "_listener_fingerprint",
        lambda _port: {"port": 8765, "listener_count": 1},
    )

    with pytest.raises(evidence.EvidenceError, match="job and listener absence"):
        evidence._legacy_launchd_observation(require_loaded=False)


def test_legacy_launchd_receipt_is_candidate_bound_and_write_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = "1" * 40
    wheel = tmp_path / "candidate.whl"
    wheel.write_bytes(b"wheel")
    repository = tmp_path / "repo"
    repository.mkdir()
    output = tmp_path / "legacy.json"
    definition = {
        "identity": f"gui/{os.getuid()}/{evidence.LEGACY_LAUNCHD_LABEL}",
        "label": evidence.LEGACY_LAUNCHD_LABEL,
        "state": "loaded",
        "plist_path": "/Users/example/Library/LaunchAgents/legacy.plist",
        "plist_sha256": "2" * 64,
        "program": "/bin/bash",
        "program_arguments": ["/bin/bash", "/Users/example/run.sh"],
        "keep_alive": {"SuccessfulExit": False},
        "run_at_load": True,
        "working_directory": "/Users/example/service",
        "raw_launchctl_output_retained": False,
        "plist_bytes": 5,
        "plist_bytes_base64": "cGxpc3Q=",
        "loaded_path_program_arguments_match_plist": True,
    }
    runtime = {
        "identity": definition["identity"],
        "definition_sha256": "4" * 64,
        "wrapper_pid": 101,
        "listener_pid": 202,
        "listener_port": 8765,
        "listener_is_wrapper_child": True,
    }
    monkeypatch.setattr(
        evidence,
        "_candidate_identity",
        lambda *_args, **_kwargs: {"head": candidate},
    )
    monkeypatch.setattr(
        evidence,
        "_verify_running_from_wheel",
        lambda *_args, **_kwargs: {"sha256": "3" * 64},
    )
    monkeypatch.setattr(
        evidence,
        "_legacy_launchd_observation",
        lambda **_kwargs: {
            "definition": definition,
            "listener": {"listener_count": 1, "port": 8765},
            "runtime": runtime,
            "cutover_eligible": True,
            "network_requests_sent": 0,
        },
    )
    monkeypatch.setattr(
        evidence,
        "_launchd_job_fingerprint",
        lambda _label: {"identity": "gui/501/org.orrery.mail", "state": "absent"},
    )

    result = evidence.write_legacy_launchd_snapshot(
        output_path=output,
        wheel=wheel,
        candidate_repository=repository,
        candidate_commit=candidate,
    )
    receipt = json.loads(output.read_text(encoding="utf-8"))

    assert result["status"] == "completed"
    assert output.stat().st_mode & 0o777 == 0o400
    assert receipt["candidate_commit"] == candidate
    assert receipt["cutover_eligible"] is True
    assert receipt["definition"] == definition
    assert receipt["runtime"]["listener_is_wrapper_child"] is True
    assert receipt["runtime"]["network_requests_sent"] == 0
    assert receipt["new_candidate_label"]["state"] == "absent"
    monkeypatch.setattr(
        evidence,
        "_launchd_job_fingerprint",
        lambda _label: {"identity": "gui/501/org.orrery.mail", "state": "loaded"},
    )
    with pytest.raises(evidence.EvidenceError, match="new candidate"):
        evidence.write_legacy_launchd_snapshot(
            output_path=tmp_path / "loaded-new-label.json",
            wheel=wheel,
            candidate_repository=repository,
            candidate_commit=candidate,
        )
    with pytest.raises(evidence.EvidenceError, match="must be absent"):
        evidence.write_legacy_launchd_snapshot(
            output_path=output,
            wheel=wheel,
            candidate_repository=repository,
            candidate_commit=candidate,
        )


def test_rehearsal_cleanup_boots_out_only_exact_loaded_identity() -> None:
    label = "org.orrery.mail.rehearsal.12345678.once"
    calls: list[list[str]] = []

    def runner(arguments: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        operation = arguments[1]
        if operation == "bootout":
            return _completed(arguments)
        print_count = sum(call[1] == "print" for call in calls)
        if print_count == 1:
            return _completed(
                arguments,
                stdout="""path = /private/tmp/rehearsal.plist
program = /private/tmp/bin/service
arguments = {
    /private/tmp/bin/service
}
""",
            )
        return _completed(arguments, returncode=113, stderr="not found")

    definition = {
        "path": "/private/tmp/rehearsal.plist",
        "program": "/private/tmp/bin/service",
        "arguments": ["/private/tmp/bin/service"],
    }
    expected = evidence._sha256_bytes(evidence._canonical_json(definition))
    result = evidence._ensure_rehearsal_job_absent(
        label,
        expected_definition_sha256=expected,
        runner=runner,
    )

    identity = f"gui/{evidence.os.getuid()}/{label}"
    assert [call[1:] for call in calls] == [
        ["print", identity],
        ["bootout", identity],
        ["print", identity],
    ]
    assert result["before"]["state"] == "loaded"
    assert result["after"]["state"] == "absent"


def test_rehearsal_cleanup_rejects_production_before_launchctl() -> None:
    def runner(_arguments: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        pytest.fail("production label must be rejected before launchctl")

    with pytest.raises(Exception, match="must not equal"):
        evidence._ensure_rehearsal_job_absent(
            evidence.service_runtime.LAUNCHD_LABEL,
            expected_definition_sha256="0" * 64,
            runner=runner,
        )


def test_rehearsal_cleanup_waits_for_asynchronous_bootout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    label = "org.orrery.mail.rehearsal.12345678.async"
    calls: list[list[str]] = []
    definition = {
        "path": "/private/tmp/rehearsal.plist",
        "program": "/private/tmp/bin/service",
        "arguments": ["/private/tmp/bin/service"],
    }
    loaded = """path = /private/tmp/rehearsal.plist
program = /private/tmp/bin/service
arguments = {
    /private/tmp/bin/service
}
"""

    def runner(arguments: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        if arguments[1] == "bootout":
            return _completed(arguments)
        print_count = sum(call[1] == "print" for call in calls)
        if print_count <= 3:
            return _completed(arguments, stdout=loaded)
        return _completed(arguments, returncode=113, stderr="not found")

    monkeypatch.setattr(evidence.time, "sleep", lambda _seconds: None)
    result = evidence._ensure_rehearsal_job_absent(
        label,
        expected_definition_sha256=evidence._sha256_bytes(
            evidence._canonical_json(definition)
        ),
        runner=runner,
        timeout_seconds=1,
    )

    assert result["after"]["state"] == "absent"
    assert result["retire_wait"]["poll_count"] == 3
    assert [call[1] for call in calls] == [
        "print",
        "bootout",
        "print",
        "print",
        "print",
    ]


def test_rehearsal_cleanup_rejects_ownership_change_during_bootout_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    label = "org.orrery.mail.rehearsal.12345678.foreign"
    calls: list[list[str]] = []
    definition = {
        "path": "/private/tmp/rehearsal.plist",
        "program": "/private/tmp/bin/service",
        "arguments": ["/private/tmp/bin/service"],
    }
    loaded = """path = /private/tmp/rehearsal.plist
program = /private/tmp/bin/service
arguments = {
    /private/tmp/bin/service
}
"""
    foreign = """path = /private/tmp/foreign.plist
program = /private/tmp/bin/foreign
arguments = {
    /private/tmp/bin/foreign
}
"""

    def runner(arguments: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        if arguments[1] == "bootout":
            return _completed(arguments)
        print_count = sum(call[1] == "print" for call in calls)
        return _completed(arguments, stdout=loaded if print_count <= 2 else foreign)

    monkeypatch.setattr(evidence.time, "sleep", lambda _seconds: None)
    with pytest.raises(evidence.EvidenceError, match="changed ownership"):
        evidence._ensure_rehearsal_job_absent(
            label,
            expected_definition_sha256=evidence._sha256_bytes(
                evidence._canonical_json(definition)
            ),
            runner=runner,
            timeout_seconds=1,
        )

    assert [call[1] for call in calls] == [
        "print",
        "bootout",
        "print",
        "print",
    ]
    assert sum(call[1] == "bootout" for call in calls) == 1


def test_rehearsal_cleanup_rejects_absence_observed_after_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    label = "org.orrery.mail.rehearsal.12345678.deadline"

    def runner(arguments: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return _completed(arguments, returncode=113, stderr="not found")

    observed = iter((0.0, 0.0, 31.0))
    monkeypatch.setattr(evidence.time, "monotonic", lambda: next(observed))
    with pytest.raises(evidence.EvidenceError, match="cleanup deadline"):
        evidence._ensure_rehearsal_job_absent(
            label,
            expected_definition_sha256="0" * 64,
            runner=runner,
            timeout_seconds=30,
        )


def test_candidate_rehearsal_label_requires_exact_candidate8() -> None:
    candidate = "12345678" + "a" * 32
    label = "org.orrery.mail.rehearsal.12345678.once"

    assert evidence._candidate_rehearsal_label(label, candidate) == label
    with pytest.raises(evidence.EvidenceError, match="candidate8"):
        evidence._candidate_rehearsal_label(
            "org.orrery.mail.rehearsal.87654321.once",
            candidate,
        )


def test_foreground_receipt_identity_requires_same_candidate_and_wrapper_loss(
    tmp_path: Path,
) -> None:
    candidate = "1" * 40
    receipt = tmp_path / "service-lifecycle-v1.json"
    receipt.write_text(
        json.dumps(
            {
                "schema_version": evidence.RUNTIME_SCHEMA_VERSION,
                "kind": "service-lifecycle",
                "candidate_commit": candidate,
                "candidate_checkout": {
                    "head": candidate,
                    "tracked_and_untracked_worktree_clean": True,
                },
                "wheel": {
                    "sha256": "a" * 64,
                    "candidate_package_members_byte_identical": True,
                    "console_scripts_candidate_bound": True,
                },
                "maximum_observed_ready_services": 1,
                "legacy_listener": {
                    "required": True,
                    "before": {"listener_count": 1},
                    "after": {"listener_count": 1},
                    "network_requests_sent": 0,
                },
                "sequence": [
                    {"step": step, "status": status}
                    for step, status in (
                        ("start", "ready"),
                        ("stop", "stopped"),
                        ("status", "stopped"),
                        ("start", "ready"),
                        ("duplicate", "rejected"),
                        ("crash", "nonzero_exit"),
                        ("start", "recovered_ready"),
                        ("stop", "stopped"),
                        ("status", "stopped"),
                        ("wrapper-loss", "original-writer-retained-lock"),
                    )
                ],
            }
        ),
        encoding="utf-8",
    )
    receipt.chmod(0o400)

    identity = evidence._foreground_receipt_identity(
        receipt,
        candidate_commit=candidate,
        expected_sha256=hashlib.sha256(receipt.read_bytes()).hexdigest(),
        wheel_sha256="a" * 64,
        require_legacy_listener=True,
    )

    assert identity["candidate_commit"] == candidate
    assert identity["sha256"] == hashlib.sha256(receipt.read_bytes()).hexdigest()
    with pytest.raises(evidence.EvidenceError, match="wrong identity"):
        evidence._foreground_receipt_identity(
            receipt,
            candidate_commit="2" * 40,
            expected_sha256=hashlib.sha256(receipt.read_bytes()).hexdigest(),
            wheel_sha256="a" * 64,
            require_legacy_listener=True,
        )


def test_disabled_override_snapshot_rejects_malformed_exact_entry() -> None:
    label = "org.orrery.mail.rehearsal.12345678.once"

    def runner(arguments: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return _completed(
            arguments,
            stdout=f'disabled services = {{\n    "{label}" = enabled\n}}\n',
        )

    with pytest.raises(evidence.EvidenceError, match="malformed exact-label"):
        evidence._disabled_override_snapshot(label, runner=runner)


def test_disabled_override_transition_is_exact() -> None:
    evidence._require_disabled_override_before(
        {"entry_present": False, "disabled": None}
    )
    evidence._require_disabled_override_before(
        {"entry_present": True, "disabled": False}
    )
    with pytest.raises(evidence.EvidenceError, match="persistently disabled"):
        evidence._require_disabled_override_before(
            {"entry_present": True, "disabled": True}
        )

    evidence._require_disabled_override_after(
        {"entry_present": True, "disabled": False}
    )
    for invalid in (
        {"entry_present": False, "disabled": None},
        {"entry_present": True, "disabled": True},
    ):
        with pytest.raises(evidence.EvidenceError, match="explicit enabled"):
            evidence._require_disabled_override_after(invalid)


def test_rehearsal_cleanup_refuses_foreign_definition_without_bootout() -> None:
    label = "org.orrery.mail.rehearsal.12345678.once"
    calls: list[list[str]] = []

    def runner(arguments: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        return _completed(
            arguments,
            stdout="""path = /private/tmp/foreign.plist
program = /private/tmp/bin/foreign
arguments = {
    /private/tmp/bin/foreign
}
""",
        )

    with pytest.raises(evidence.EvidenceError, match="foreign ownership"):
        evidence._ensure_rehearsal_job_absent(
            label,
            expected_definition_sha256="0" * 64,
            runner=runner,
        )

    assert [call[1] for call in calls] == ["print"]


@pytest.mark.parametrize("interruption", [KeyboardInterrupt(), SystemExit(17)])
def test_python_level_interruption_always_runs_exact_cleanup(
    interruption: BaseException,
    tmp_path: Path,
) -> None:
    cleanup_calls: list[str] = []

    def action() -> None:
        raise interruption

    def cleanup() -> dict[str, str]:
        cleanup_calls.append("exact-label-bootout")
        return {"status": "absent"}

    with pytest.raises(type(interruption)):
        evidence._execute_with_launchd_cleanup(
            action=action,
            cleanup=cleanup,
            label="org.orrery.mail.rehearsal.12345678.once",
            port=28765,
            output_directory=tmp_path,
        )

    assert cleanup_calls == ["exact-label-bootout"]


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_terminating_signal_is_converted_to_cleanup_then_failure(
    signum: signal.Signals,
    tmp_path: Path,
) -> None:
    cleanup_calls: list[str] = []

    def action() -> None:
        os.kill(os.getpid(), signum)

    def cleanup() -> dict[str, str]:
        cleanup_calls.append("exact-label-bootout")
        return {"status": "absent"}

    with pytest.raises(evidence.EvidenceError, match=signal.Signals(signum).name):
        evidence._execute_with_launchd_cleanup(
            action=action,
            cleanup=cleanup,
            label="org.orrery.mail.rehearsal.12345678.once",
            port=28765,
            output_directory=tmp_path,
        )

    assert cleanup_calls == ["exact-label-bootout"]


def test_signal_during_cleanup_is_deferred_then_fails(tmp_path: Path) -> None:
    def cleanup() -> dict[str, str]:
        os.kill(os.getpid(), signal.SIGTERM)
        return {"status": "absent"}

    with pytest.raises(evidence.EvidenceError, match="during cleanup.*SIGTERM"):
        evidence._execute_with_launchd_cleanup(
            action=lambda: {"mutated": True},
            cleanup=cleanup,
            label="org.orrery.mail.rehearsal.12345678.once",
            port=28765,
            output_directory=tmp_path,
        )


def test_process_group_signal_cannot_kill_cleanup_child(tmp_path: Path) -> None:
    marker = tmp_path / "cleanup-child-finished"
    script = f"""
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from agentstack_mail import evidence

marker = Path({str(marker)!r})

def send_group_signal():
    time.sleep(0.15)
    os.killpg(os.getpgrp(), signal.SIGTERM)

def cleanup():
    threading.Thread(target=send_group_signal, daemon=True).start()
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import time; from pathlib import Path; "
            "time.sleep(0.4); Path(" + repr(str(marker)) + ").write_text('done')",
        ],
        check=True,
    )
    return {{"status": "absent"}}

try:
    evidence._execute_with_launchd_cleanup(
        action=lambda: {{"mutated": True}},
        cleanup=cleanup,
        label="org.orrery.mail.rehearsal.12345678.once",
        port=28765,
        output_directory=marker.parent,
    )
except evidence.EvidenceError as exc:
    assert "during cleanup" in str(exc)
    assert marker.read_text() == "done"
else:
    raise AssertionError("pending process-group signal must fail the rehearsal")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        start_new_session=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert marker.read_text() == "done"


def test_cleanup_failure_never_creates_terminal_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(evidence, "_listener_process_ids", lambda _port: [4321])
    terminal = tmp_path / evidence.LAUNCHD_RECEIPT_NAME

    with pytest.raises(evidence.EvidenceError, match=r"listener_pids=\[4321\]"):
        evidence._execute_with_launchd_cleanup(
            action=lambda: {"mutated": True},
            cleanup=lambda: (_ for _ in ()).throw(
                evidence.EvidenceError("bootout failed")
            ),
            label="org.orrery.mail.rehearsal.12345678.once",
            port=28765,
            output_directory=tmp_path,
        )

    assert not terminal.exists()


@pytest.mark.parametrize(
    ("result", "status", "action"),
    [
        ({"status": "job_loaded", "owned": True, "environment_drift": False,
          "action": "noop"}, "job_loaded", "started"),
        ({"status": "stopped", "owned": True, "environment_drift": False,
          "action": "noop"}, "stopped", "stopped"),
        ({"status": "job_loaded", "owned": False, "environment_drift": False},
         "job_loaded", None),
    ],
)
def test_controller_exact_state_rejects_noop_or_unowned(
    result: dict[str, Any],
    status: str,
    action: str | None,
) -> None:
    with pytest.raises(evidence.EvidenceError):
        evidence._require_controller_state(
            {"result": result},
            status=status,
            action=action,
        )


def test_fake_launchd_sequence_uses_exact_actions_and_owned_crash_pid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    states = {
        "launchd-first-start": {
            "status": "job_loaded", "owned": True, "environment_drift": False,
            "action": "started",
        },
        "launchd-first-stop": {
            "status": "stopped", "owned": True, "environment_drift": False,
            "action": "stopped",
        },
        "launchd-status-after-first-stop": {
            "status": "stopped", "owned": True, "environment_drift": False,
        },
        "launchd-second-start": {
            "status": "job_loaded", "owned": True, "environment_drift": False,
            "action": "started",
        },
        "launchd-status-after-keepalive": {
            "status": "job_loaded", "owned": True, "environment_drift": False,
        },
        "launchd-final-stop": {
            "status": "stopped", "owned": True, "environment_drift": False,
            "action": "stopped",
        },
        "launchd-status-after-final-stop": {
            "status": "stopped", "owned": True, "environment_drift": False,
        },
    }
    commands: list[str] = []

    def service_command(*_args: Any, name: str, **_kwargs: Any) -> dict[str, Any]:
        commands.append(name)
        return {"result": states[name], "process": {"exit_code": 0}}

    ready = iter((101, 202, 303))
    monkeypatch.setattr(evidence, "_service_command", service_command)
    monkeypatch.setattr(
        evidence,
        "_wait_launchd_ready",
        lambda **_kwargs: {
            "listener_pid": next(ready),
            "tool_count": 24,
            "ownership": {"listener_is_exact_wrapper_child": True},
        },
    )
    monkeypatch.setattr(
        evidence,
        "_owned_launchd_listener",
        lambda **_kwargs: {
            "listener_pid": 202,
            "listener_is_exact_wrapper_child": True,
        },
    )
    monkeypatch.setattr(
        evidence,
        "_wait_closed",
        lambda *_args, **_kwargs: {"status": "closed"},
    )
    killed: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(evidence.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    sequence = evidence._launchd_mutation_sequence(
        service_executable=tmp_path / "service",
        server_executable=tmp_path / "server",
        env_file=tmp_path / "runtime.env",
        state_root=tmp_path / "state",
        output_directory=tmp_path,
        controller=["--ownership-manifest", "/tmp/owned", "--label", "test"],
        label="org.orrery.mail.rehearsal.12345678.once",
        expected_definition_sha256="a" * 64,
        url="http://127.0.0.1:28765/mcp",
        project=tmp_path / "project",
        port=28765,
        timeout_seconds=20,
    )

    assert [item["step"] for item in sequence] == [
        "start", "stop", "start", "crash", "stop"
    ]
    assert killed == [(202, signal.SIGKILL)]
    assert commands == list(states)


def test_listener_ownership_rejects_foreign_parent_before_crash_signal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        evidence,
        "_launchd_job_runtime",
        lambda _label: {
            "definition_sha256": "a" * 64,
            "wrapper_pid": 100,
        },
    )
    monkeypatch.setattr(evidence, "_single_listener_process_id", lambda _port: 200)

    def process_record(process_id: int) -> dict[str, Any]:
        if process_id == 100:
            return {
                "pid": 100,
                "ppid": 1,
                "arguments": [
                    str(tmp_path / "service"),
                    "foreground",
                    str(tmp_path / "server"),
                    str(tmp_path / "runtime.env"),
                    str(tmp_path / "state"),
                ],
            }
        return {
            "pid": 200,
            "ppid": 999,
            "arguments": [str(tmp_path / "server")],
        }

    monkeypatch.setattr(evidence, "_process_record", process_record)

    with pytest.raises(evidence.EvidenceError, match="not a child"):
        evidence._owned_launchd_listener(
            label="org.orrery.mail.rehearsal.12345678.once",
            port=28765,
            expected_definition_sha256="a" * 64,
            service_executable=tmp_path / "service",
            server_executable=tmp_path / "server",
            env_file=tmp_path / "runtime.env",
            state_root=tmp_path / "state",
        )


def test_verified_service_shim_ignores_caller_pythonpath(tmp_path: Path) -> None:
    fake_root = tmp_path / "fake-import"
    fake_package = fake_root / "agentstack_mail"
    fake_package.mkdir(parents=True)
    (fake_package / "__init__.py").write_text("", encoding="utf-8")
    sentinel = tmp_path / "fake-imported"
    (fake_package / "service.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(sentinel)!r}).write_text('imported')\n"
        "def main(): pass\n",
        encoding="utf-8",
    )
    shims = evidence._write_verified_entrypoint_shims(tmp_path)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(fake_root)

    result = subprocess.run(
        [shims["service"]["path"], "--help"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert not sentinel.exists()
    assert "PYTHONPATH" not in evidence._clean_environment(tmp_path / "runtime.env")
