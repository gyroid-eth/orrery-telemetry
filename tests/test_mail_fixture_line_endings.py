"""Fresh Windows-style checkouts must preserve the mail service's byte pins."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
# Existing pins from authorization.py and test_agentstack_mail_contract.py.
PINNED_FIXTURES = {
    "packages/agentstack_mail/fixtures/authorization-tools-v1.json": (
        "6609e7b2c6816c039ab55432de3bda15ad7c491bad5fb5764b9ae77a2aeda607"
    ),
    "packages/agentstack_mail/fixtures/live-tools-list.json": (
        "6ea7dabf41f71091161fa1fcb8a4073a383a65c7bba4785306217fd35f9e8332"
    ),
}


def test_autocrlf_checkout_preserves_frozen_mail_fixture_bytes(tmp_path: Path) -> None:
    # Do not inherit user Git configuration, attributes, hooks, or signing.
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith("GIT_")
    }
    environment.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_ATTR_NOSYSTEM": "1",
    })

    def git(*arguments: str, cwd: Path) -> bytes:
        return subprocess.run(
            ["git", *arguments], cwd=cwd, env=environment,
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout

    source = tmp_path / "source"
    source.mkdir()
    git("init", "--quiet", cwd=source)
    (source / ".gitattributes").write_bytes((ROOT / ".gitattributes").read_bytes())
    for relative, expected_digest in PINNED_FIXTURES.items():
        content = (ROOT / relative).read_bytes()
        assert hashlib.sha256(content).hexdigest() == expected_digest, relative
        destination = source / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    (source / "unrelated.txt").write_bytes(b"first\nsecond\n")
    git("-c", "core.autocrlf=false", "add", ".", cwd=source)
    git(
        "-c", "user.name=fixture-check", "-c", "user.email=fixture@localhost",
        "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "fixtures",
        cwd=source,
    )

    checkout = tmp_path / "checkout"
    git(
        "clone", "--quiet", "--no-local", "--config", "core.autocrlf=true",
        str(source), str(checkout), cwd=tmp_path,
    )
    assert git("config", "--get", "core.autocrlf", cwd=checkout).strip() == b"true"
    # This control proves Git actually applied CRLF checkout conversion.
    assert (checkout / "unrelated.txt").read_bytes() == b"first\r\nsecond\r\n"
    for relative, expected_digest in PINNED_FIXTURES.items():
        attributes = git("check-attr", "text", "eol", "--", relative, cwd=checkout)
        assert f"{relative}: text: set".encode() in attributes
        assert f"{relative}: eol: lf".encode() in attributes
        content = (checkout / relative).read_bytes()
        assert b"\r\n" not in content
        assert hashlib.sha256(content).hexdigest() == expected_digest, relative
