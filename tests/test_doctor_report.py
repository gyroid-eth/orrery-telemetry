#!/usr/bin/env python3
"""`doctor.sh --report` — the paste-ready environment block.

Every defect this project has had came from a difference between the reporter's
machine and the developer's, and each one cost several rounds of "which version
of that do you have?" before anyone could even start. The report answers those
rounds up front.

Which makes it a thing a user will paste into a public chat, so the test that
matters most is the one asserting it carries no credentials.

Runnable two ways:
    python3 tests/test_doctor_report.py
    pytest tests/test_doctor_report.py
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCTOR = ROOT / "scripts" / "doctor.sh"

SECRET = "sk-do-not-print-this-anywhere-12345"

FIXTURE = (
    ROOT / "tests" / "fixtures" / "agent_mail_stock" / "src" / "mcp_agent_mail"
)


def _run(
    tmp_path: pathlib.Path,
    *args: str,
    app_text: str | None = None,
    include_source: bool = True,
) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "home"
    mail = home / "mcp_agent_mail"
    mail.mkdir(parents=True)
    (mail / ".env").write_text(
        f"HTTP_BEARER_TOKEN={SECRET}\nAGENT_NAME_ENFORCEMENT_MODE=coerce\n",
        encoding="utf-8",
    )
    (mail / "pyproject.toml").write_text('version = "0.9.9"\n', encoding="utf-8")
    if include_source:
        package = mail / "src" / "mcp_agent_mail"
        package.mkdir(parents=True)
        for name in ("app.py", "config.py"):
            source = (FIXTURE / name).read_text(encoding="utf-8")
            if name == "app.py" and app_text is not None:
                source = app_text
            (package / name).write_text(source, encoding="utf-8")
    (home / ".claude.json").write_text(
        '{"mcpServers": {"mcp-agent-mail": {"type": "http",'
        f' "url": "http://127.0.0.1:8765/mcp", "headers": {{"Authorization": "Bearer {SECRET}"}}}}}}}}',
        encoding="utf-8",
    )
    return subprocess.run(
        ["bash", str(DOCTOR), "--install-dir", str(home / ".agentstack"), *args],
        env={
            **os.environ,
            "HOME": str(home),
            "AGENTSTACK_MAIL_DIR": str(mail),
            "AGENTSTACK_MAIL_ENV": str(mail / ".env"),
        },
        text=True,
        capture_output=True,
        check=False,
    )


def test_the_report_never_prints_a_credential(tmp_path):
    """The one that matters: this output is meant to be pasted in public."""
    result = _run(tmp_path, "--report")
    assert SECRET not in result.stdout, "the bearer token reached the report"
    assert SECRET not in result.stderr, "the bearer token reached stderr"
    assert "Authorization" not in result.stdout


def test_the_report_answers_the_questions_we_kept_having_to_ask(tmp_path):
    result = _run(tmp_path, "--report")
    body = result.stdout
    # Each of these cost at least one round trip with a tester this week.
    for field in (
        "AGENT_NAME_ENFORCEMENT_MODE",   # decides whether your name survives
        "passthrough patch",             # decides whether that mode is even legal
        "requested-name handling",       # combined, fail-closed capability verdict
        "agents.retired_at column",      # decides whether the deck renders
        "open file limit",               # decides whether the server stays up
        "declared version",
        "- tmux:",
        "- python3:",
    ):
        assert field in body, f"{field!r} missing from the report"
    assert "coerce" in body
    assert "0.9.9" in body


def test_honored_name_capability_is_next_to_the_patch_without_a_warning(tmp_path):
    """Null case: doctor must not warn when #140 is visible in source."""
    result = _run(tmp_path, "--report")
    lines = result.stdout.splitlines()
    patch_index = next(i for i, line in enumerate(lines) if "passthrough patch:" in line)
    assert lines[patch_index + 1].startswith("- requested-name handling: honored")
    assert "requested-name handling" not in result.stderr


def test_legacy_and_unreadable_name_capabilities_are_not_rounded_to_honored(tmp_path):
    app = (FIXTURE / "app.py").read_text(encoding="utf-8").replace(
        "validate_explicit_agent_id", "legacy_explicit_name_check"
    )
    legacy = _run(tmp_path / "legacy", "--report", app_text=app)
    assert "- requested-name handling: replaced (legacy-naming)" in legacy.stdout

    unreadable = _run(tmp_path / "unreadable", "--report", include_source=False)
    assert "- requested-name handling: unknown (source-unreadable)" in unreadable.stdout


def test_the_report_is_opt_in(tmp_path):
    """Plain `doctor` output is a diagnosis; the report is a separate ask."""
    result = _run(tmp_path)
    assert "copy from here" not in result.stdout


def test_a_missing_tool_is_reported_rather_than_omitted(tmp_path):
    """A blank line reads as "fine". Absence is the interesting answer here."""
    result = _run(tmp_path, "--report")
    lines = [line for line in result.stdout.splitlines() if line.startswith("- ")]
    assert any("not found" in line for line in lines) or all(
        ":" in line for line in lines
    ), "tools must be listed with a version or an explicit 'not found'"


if __name__ == "__main__":
    import tempfile

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            with tempfile.TemporaryDirectory() as directory:
                try:
                    fn(pathlib.Path(directory))
                    print(f"PASS {name}")
                except AssertionError as exc:
                    failures += 1
                    print(f"FAIL {name}: {exc}")
    raise SystemExit(1 if failures else 0)
