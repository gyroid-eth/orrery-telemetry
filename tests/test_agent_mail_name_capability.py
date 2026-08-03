#!/usr/bin/env python3
"""Passive classification of agent-mail's requested-name behaviour.

The classifier must never register a probe identity.  It may only read the
checkout and its configuration, and an unreadable or unfamiliar source tree is
``unknown`` rather than a guessed success.

Runnable two ways:
    python3 tests/test_agent_mail_name_capability.py
    pytest tests/test_agent_mail_name_capability.py
"""
from __future__ import annotations

import importlib.util
import pathlib
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "lib" / "agent_mail_passthrough.py"

_spec = importlib.util.spec_from_file_location("agent_mail_passthrough", MODULE_PATH)
capability = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(capability)

FIXTURE = ROOT / "tests" / "fixtures" / "agent_mail_stock" / "src" / "mcp_agent_mail"
CONFIG_PY = (FIXTURE / "config.py").read_text(encoding="utf-8")
APP_PY = (FIXTURE / "app.py").read_text(encoding="utf-8")


def _checkout(directory: pathlib.Path, *, app: str = APP_PY, config: str = CONFIG_PY) -> pathlib.Path:
    package = directory / "src" / "mcp_agent_mail"
    package.mkdir(parents=True)
    (package / "app.py").write_text(app, encoding="utf-8")
    (package / "config.py").write_text(config, encoding="utf-8")
    return directory


def _legacy_app() -> str:
    return APP_PY.replace("validate_explicit_agent_id", "legacy_explicit_name_check")


def test_explicit_identity_support_honors_hyphenated_names_without_a_warning():
    """Null case first: a capable server must not be reported as degraded."""
    with tempfile.TemporaryDirectory() as directory:
        result = capability.inspect_name_capability(_checkout(pathlib.Path(directory)))
    assert result["status"] == "honored"
    assert result["evidence"] == "validate_explicit_agent_id"
    assert result["warning"] is None


def test_active_passthrough_honors_names_without_explicit_identity_support():
    with tempfile.TemporaryDirectory() as directory:
        root = _checkout(pathlib.Path(directory), app=_legacy_app())
        capability.apply(root)
        mail_env = root / ".env"
        mail_env.write_text("AGENT_NAME_ENFORCEMENT_MODE=passthrough\n", encoding="utf-8")
        result = capability.inspect_name_capability(root, mail_env=mail_env)
    assert result["status"] == "honored"
    assert result["evidence"] == "passthrough"
    assert result["warning"] is None


def test_known_legacy_source_reports_generated_name_replacement():
    with tempfile.TemporaryDirectory() as directory:
        root = _checkout(pathlib.Path(directory), app=_legacy_app())
        result = capability.inspect_name_capability(root)
    assert result["status"] == "replaced"
    assert result["evidence"] == "legacy-naming"
    assert "generated" in result["warning"]


def test_always_auto_overrides_explicit_identity_support():
    with tempfile.TemporaryDirectory() as directory:
        root = _checkout(pathlib.Path(directory))
        mail_env = root / ".env"
        mail_env.write_text("AGENT_NAME_ENFORCEMENT_MODE=always_auto\n", encoding="utf-8")
        result = capability.inspect_name_capability(root, mail_env=mail_env)
    assert result["status"] == "replaced"
    assert result["evidence"] == "always-auto"


def test_passthrough_setting_without_the_patch_is_not_positive_evidence():
    with tempfile.TemporaryDirectory() as directory:
        root = _checkout(pathlib.Path(directory))
        mail_env = root / ".env"
        mail_env.write_text("AGENT_NAME_ENFORCEMENT_MODE=passthrough\n", encoding="utf-8")
        result = capability.inspect_name_capability(root, mail_env=mail_env)
    assert result["status"] == "unknown"
    assert result["evidence"] == "passthrough-unavailable"


def test_unreadable_source_is_unknown_not_honored():
    with tempfile.TemporaryDirectory() as directory:
        result = capability.inspect_name_capability(pathlib.Path(directory) / "missing")
    assert result["status"] == "unknown"
    assert result["evidence"] == "source-unreadable"
    assert "unknown" in result["warning"]


def test_unrecognised_fork_is_unknown_not_legacy():
    unfamiliar = APP_PY.replace(
        "validate_explicit_agent_id", "fork_explicit_name_check"
    ).replace("validate_agent_name_format", "fork_legacy_name_check")
    with tempfile.TemporaryDirectory() as directory:
        root = _checkout(pathlib.Path(directory), app=unfamiliar)
        result = capability.inspect_name_capability(root)
    assert result["status"] == "unknown"
    assert result["evidence"] == "source-unrecognised"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    raise SystemExit(1 if failures else 0)
