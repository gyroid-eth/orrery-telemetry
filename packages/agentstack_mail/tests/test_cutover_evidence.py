from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from agentstack_mail import evidence


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
    evidence._write_terminal(receipt, {"z": 1, "a": "日本語"})

    assert receipt.read_bytes() == '{"a":"日本語","z":1}\n'.encode()
    assert receipt.stat().st_mode & 0o777 == 0o400
    with pytest.raises(FileExistsError):
        evidence._write_terminal(receipt, {"replacement": True})


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
