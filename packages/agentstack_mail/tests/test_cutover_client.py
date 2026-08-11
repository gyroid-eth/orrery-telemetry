from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from agentstack_mail import cutover_client


CANARY = "cutover-client-canary-value"
OTHER_SAME_LENGTH = "x" * len(CANARY)


@pytest.fixture(autouse=True)
def _matching_codex_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(cutover_client.CODEX_BEARER_ENV, CANARY)


def _assert_secret_absent(secret: str, value: str | bytes) -> None:
    secret_value = secret.encode() if isinstance(value, bytes) else secret
    if secret_value in value:
        pytest.fail("secret material was disclosed", pytrace=False)


def _write_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    claude = tmp_path / "claude.json"
    claude.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "mcp-agent-mail": {
                        "type": "http",
                        "url": cutover_client.EXPECTED_ENDPOINT,
                        "headers": {"Authorization": f"Bearer {CANARY}"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    codex = tmp_path / "config.toml"
    codex.write_text(
        "\n".join(
            (
                "[mcp_servers.agent-mail]",
                f'url = "{cutover_client.EXPECTED_ENDPOINT}"',
                'bearer_token_env_var = "MCP_AGENT_MAIL_TOKEN"',
                "",
            )
        ),
        encoding="utf-8",
    )
    legacy_env = tmp_path / "legacy.env"
    legacy_env.write_text(f"HTTP_BEARER_TOKEN={CANARY}\n", encoding="utf-8")
    for path in (claude, codex, legacy_env):
        path.chmod(0o600)
    return claude, codex, legacy_env


def _write_seal(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    claude, codex, legacy_env = _write_inputs(tmp_path)
    seal = tmp_path / "client-config-seal.json"
    pin = tmp_path / "client-config-seal.sha256"
    state = cutover_client.write_client_config_seal(
        seal_path=seal,
        pin_path=pin,
        claude_config=claude,
        codex_config=codex,
        legacy_env=legacy_env,
    )
    _assert_secret_absent(CANARY, json.dumps(state))
    _assert_secret_absent(CANARY, seal.read_bytes())
    _assert_secret_absent(CANARY, pin.read_bytes())
    for path in (seal, pin):
        info = path.lstat()
        assert stat.S_ISREG(info.st_mode)
        assert stat.S_IMODE(info.st_mode) == 0o400
        assert info.st_nlink == 1
    return seal, pin, claude, codex, legacy_env


def _read_header(
    seal: Path,
    pin: Path,
    claude: Path,
    codex: Path,
    legacy_env: Path,
) -> str:
    return cutover_client.read_pinned_client_authorization(
        seal_path=seal,
        pin_path=pin,
        claude_config=claude,
        codex_config=codex,
        legacy_env=legacy_env,
    )


def test_write_once_seal_verifies_exact_unchanged_clients(tmp_path: Path) -> None:
    seal, pin, claude, codex, legacy_env = _write_seal(tmp_path)

    sealed = json.loads(seal.read_text(encoding="utf-8"))
    assert sealed["bearer"]["codex_process_environment"] == {"status": "matched"}
    authorization = _read_header(seal, pin, claude, codex, legacy_env)
    assert authorization.startswith("Bearer ")
    assert hashlib.sha256(authorization[7:].encode()).digest() == hashlib.sha256(
        CANARY.encode()
    ).digest()
    with pytest.raises(
        cutover_client.ClientConfigSealError,
        match="already exists",
    ):
        cutover_client.write_client_config_seal(
            seal_path=seal,
            pin_path=pin,
            claude_config=claude,
            codex_config=codex,
            legacy_env=legacy_env,
        )
    assert seal.exists()
    assert pin.exists()


def test_same_shape_bearer_change_fails_without_secret_in_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seal, pin, claude, codex, legacy_env = _write_seal(tmp_path)
    claude_info = claude.stat()
    legacy_info = legacy_env.stat()
    payload = json.loads(claude.read_text(encoding="utf-8"))
    payload["mcpServers"]["mcp-agent-mail"]["headers"]["Authorization"] = (
        f"Bearer {OTHER_SAME_LENGTH}"
    )
    claude.write_text(json.dumps(payload), encoding="utf-8")
    legacy_env.write_text(
        f"HTTP_BEARER_TOKEN={OTHER_SAME_LENGTH}\n", encoding="utf-8"
    )
    monkeypatch.setenv(cutover_client.CODEX_BEARER_ENV, OTHER_SAME_LENGTH)
    os.utime(
        claude,
        ns=(claude_info.st_atime_ns, claude_info.st_mtime_ns),
    )
    os.utime(
        legacy_env,
        ns=(legacy_info.st_atime_ns, legacy_info.st_mtime_ns),
    )

    with pytest.raises(cutover_client.ClientConfigSealError) as raised:
        _read_header(seal, pin, claude, codex, legacy_env)

    message = str(raised.value)
    _assert_secret_absent(CANARY, message)
    _assert_secret_absent(OTHER_SAME_LENGTH, message)


def test_same_shape_codex_process_token_drift_fails_without_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seal, pin, claude, codex, legacy_env = _write_seal(tmp_path)
    monkeypatch.setenv(cutover_client.CODEX_BEARER_ENV, OTHER_SAME_LENGTH)

    with pytest.raises(cutover_client.ClientConfigSealError) as raised:
        _read_header(seal, pin, claude, codex, legacy_env)

    message = str(raised.value)
    _assert_secret_absent(CANARY, message)
    _assert_secret_absent(OTHER_SAME_LENGTH, message)


def test_absent_codex_process_token_is_explicitly_unverified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(cutover_client.CODEX_BEARER_ENV)
    seal, pin, claude, codex, legacy_env = _write_seal(tmp_path)

    sealed = json.loads(seal.read_text(encoding="utf-8"))
    assert sealed["bearer"]["codex_process_environment"] == {
        "status": "not_present_unverified"
    }
    assert _read_header(seal, pin, claude, codex, legacy_env).startswith("Bearer ")


def test_running_token_census_reports_counts_without_values() -> None:
    process_table = "\n".join(
        (
            f"101 codex MCP_AGENT_MAIL_TOKEN={CANARY}",
            f"102 codex MCP_AGENT_MAIL_TOKEN={CANARY}",
            f"103 codex MCP_AGENT_MAIL_TOKEN={OTHER_SAME_LENGTH}",
            "104 unrelated",
        )
    )

    census = cutover_client._summarize_running_codex_tokens(
        process_table,
        canonical_bearer=CANARY,
    )

    assert census == {
        "kind": "orrery-mail-running-codex-token-census",
        "status": "observed",
        "report_only": True,
        "environment_variable": "MCP_AGENT_MAIL_TOKEN",
        "token_bearing_process_count": 3,
        "distinct_digest_count": 2,
        "drift_process_count": 1,
        "raw_values_emitted": False,
    }
    serialized = json.dumps(census)
    _assert_secret_absent(CANARY, serialized)
    _assert_secret_absent(OTHER_SAME_LENGTH, serialized)


def test_running_token_census_uses_configured_canonical_without_disclosure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude, codex, legacy_env = _write_inputs(tmp_path)
    process_table = f"101 codex MCP_AGENT_MAIL_TOKEN={CANARY}\n"
    monkeypatch.setattr(
        cutover_client.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout=process_table.encode(), stderr=b""
        ),
    )

    census = cutover_client.capture_running_codex_token_census(
        claude_config=claude,
        codex_config=codex,
        legacy_env=legacy_env,
    )

    assert census["status"] == "observed"
    assert census["distinct_digest_count"] == 1
    assert census["drift_process_count"] == 0
    assert census["captured_at"].endswith("+00:00")
    _assert_secret_absent(CANARY, json.dumps(census))


def test_running_token_census_is_written_once_without_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude, codex, legacy_env = _write_inputs(tmp_path)
    process_table = f"101 codex MCP_AGENT_MAIL_TOKEN={CANARY}\n"
    monkeypatch.setattr(
        cutover_client.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout=process_table.encode(), stderr=b""
        ),
    )
    output = tmp_path / "running-token-census.json"

    census = cutover_client.write_running_codex_token_census(
        output_path=output,
        claude_config=claude,
        codex_config=codex,
        legacy_env=legacy_env,
    )

    assert json.loads(output.read_text(encoding="utf-8")) == census
    assert stat.S_IMODE(output.stat().st_mode) == 0o400
    assert output.stat().st_nlink == 1
    _assert_secret_absent(CANARY, output.read_bytes())
    with pytest.raises(
        cutover_client.ClientConfigSealError,
        match="already exists",
    ):
        cutover_client.write_running_codex_token_census(
            output_path=output,
            claude_config=claude,
            codex_config=codex,
            legacy_env=legacy_env,
        )


def test_running_token_census_failure_does_not_disclose_process_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude, codex, legacy_env = _write_inputs(tmp_path)
    monkeypatch.setattr(
        cutover_client.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=CANARY.encode(),
            stderr=OTHER_SAME_LENGTH.encode(),
        ),
    )

    with pytest.raises(cutover_client.ClientConfigSealError) as raised:
        cutover_client.capture_running_codex_token_census(
            claude_config=claude,
            codex_config=codex,
            legacy_env=legacy_env,
        )

    message = str(raised.value)
    _assert_secret_absent(CANARY, message)
    _assert_secret_absent(OTHER_SAME_LENGTH, message)


@pytest.mark.parametrize("missing", ("header", "codex", "legacy"))
def test_missing_client_inputs_fail_closed_without_secret(
    tmp_path: Path,
    missing: str,
) -> None:
    seal, pin, claude, codex, legacy_env = _write_seal(tmp_path)
    if missing == "header":
        payload = json.loads(claude.read_text(encoding="utf-8"))
        del payload["mcpServers"]["mcp-agent-mail"]["headers"]["Authorization"]
        claude.write_text(json.dumps(payload), encoding="utf-8")
    elif missing == "codex":
        codex.write_text("[mcp_servers.other]\nurl = 'http://127.0.0.1/'\n")
    else:
        legacy_env.write_text("HTTP_HOST=127.0.0.1\n", encoding="utf-8")

    with pytest.raises(cutover_client.ClientConfigSealError) as raised:
        _read_header(seal, pin, claude, codex, legacy_env)

    _assert_secret_absent(CANARY, str(raised.value))


@pytest.mark.parametrize("target_name", ("seal", "pin"))
def test_seal_and_pin_mode_are_required(
    tmp_path: Path,
    target_name: str,
) -> None:
    seal, pin, claude, codex, legacy_env = _write_seal(tmp_path)
    target = seal if target_name == "seal" else pin
    target.chmod(0o600)

    with pytest.raises(
        cutover_client.ClientConfigSealError,
        match="not write-once",
    ):
        _read_header(seal, pin, claude, codex, legacy_env)


@pytest.mark.parametrize("target_name", ("seal", "pin"))
def test_seal_and_pin_hardlinks_are_rejected(
    tmp_path: Path,
    target_name: str,
) -> None:
    seal, pin, claude, codex, legacy_env = _write_seal(tmp_path)
    target = seal if target_name == "seal" else pin
    (tmp_path / f"{target_name}.hardlink").hardlink_to(target)

    with pytest.raises(
        cutover_client.ClientConfigSealError,
        match="not write-once",
    ):
        _read_header(seal, pin, claude, codex, legacy_env)


@pytest.mark.parametrize("mutation", ("digest", "basename"))
def test_pin_content_must_match_seal_exactly(
    tmp_path: Path,
    mutation: str,
) -> None:
    seal, pin, claude, codex, legacy_env = _write_seal(tmp_path)
    digest, basename = pin.read_text(encoding="ascii").rstrip("\n").split("  ", 1)
    if mutation == "digest":
        digest = "0" * len(digest)
    else:
        basename = f"wrong-{basename}"
    pin.chmod(0o600)
    pin.write_text(f"{digest}  {basename}\n", encoding="ascii")
    pin.chmod(0o400)

    with pytest.raises(
        cutover_client.ClientConfigSealError,
        match="seal pin is invalid",
    ):
        _read_header(seal, pin, claude, codex, legacy_env)


def test_write_once_race_never_removes_foreign_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "seal.json"
    original_open = os.open

    def racing_open(path: os.PathLike[str] | str, flags: int, mode: int = 0o777) -> int:
        if Path(path) == target and flags & os.O_EXCL:
            target.write_bytes(b"foreign-winner")
            raise FileExistsError(path)
        return original_open(path, flags, mode)

    monkeypatch.setattr(os, "open", racing_open)
    with pytest.raises(FileExistsError):
        cutover_client._write_once(target, b"ours")

    assert target.read_bytes() == b"foreign-winner"
