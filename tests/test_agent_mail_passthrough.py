#!/usr/bin/env python3
"""The passthrough patch must land exactly, or not at all.

This patches somebody else's source tree — sometimes a server the installer did
not create and does not own. Two properties carry that: it changes no behaviour
until the mode is selected, and it refuses any checkout whose text it does not
recognise instead of patching approximately.

The fixture is an excerpt of the pinned upstream (5e48183) at its real
indentation, because an anchor that only matches a hand-written approximation of
the source would pass here and miss in the field. It is shared with
tests/test_install_passthrough_scope.py so both test one shape, not two.

Runnable two ways:
    python3 tests/test_agent_mail_passthrough.py
    pytest tests/test_agent_mail_passthrough.py
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "lib" / "agent_mail_passthrough.py"

_spec = importlib.util.spec_from_file_location("agent_mail_passthrough", MODULE_PATH)
patcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(patcher)
PatchError = patcher.PatchError


FIXTURE = ROOT / "tests" / "fixtures" / "agent_mail_stock" / "src" / "mcp_agent_mail"
CONFIG_PY = (FIXTURE / "config.py").read_text(encoding="utf-8")
APP_PY = (FIXTURE / "app.py").read_text(encoding="utf-8")


def _checkout(directory: pathlib.Path, *, app: str = APP_PY, config: str = CONFIG_PY) -> pathlib.Path:
    pkg = directory / "src" / "mcp_agent_mail"
    pkg.mkdir(parents=True)
    (pkg / "app.py").write_text(app, encoding="utf-8")
    (pkg / "config.py").write_text(config, encoding="utf-8")
    return directory


def _texts(root: pathlib.Path) -> dict[str, str]:
    pkg = root / "src" / "mcp_agent_mail"
    return {p.name: p.read_text(encoding="utf-8") for p in pkg.glob("*.py")}


def test_a_pinned_checkout_is_applicable():
    with tempfile.TemporaryDirectory() as directory:
        root = _checkout(pathlib.Path(directory))
        assert patcher.inspect(root)["state"] == "applicable"


def test_inspect_writes_nothing():
    """--dry-run must be able to ask without changing the answer."""
    with tempfile.TemporaryDirectory() as directory:
        root = _checkout(pathlib.Path(directory))
        before = _texts(root)
        patcher.inspect(root)
        assert _texts(root) == before


def test_apply_changes_exactly_three_lines():
    with tempfile.TemporaryDirectory() as directory:
        root = _checkout(pathlib.Path(directory))
        before = _texts(root)
        patcher.apply(root)
        after = _texts(root)
        changed = [
            (b, a)
            for name in before
            for b, a in zip(before[name].splitlines(), after[name].splitlines())
            if b != a
        ]
        assert len(changed) == 3, changed
        assert len(before["app.py"].splitlines()) == len(after["app.py"].splitlines())


def test_both_app_edits_survive_each_other():
    """Two edits share app.py; writing them separately would drop the first."""
    with tempfile.TemporaryDirectory() as directory:
        root = _checkout(pathlib.Path(directory))
        patcher.apply(root)
        app = (root / "src" / "mcp_agent_mail" / "app.py").read_text(encoding="utf-8")
        assert app.count('mode == "passthrough" or') == 2


def test_apply_is_idempotent():
    with tempfile.TemporaryDirectory() as directory:
        root = _checkout(pathlib.Path(directory))
        patcher.apply(root)
        once = _texts(root)
        assert patcher.apply(root)["state"] == "already"
        assert _texts(root) == once


def test_the_patch_is_inert_until_the_mode_is_selected():
    """The null case: patched but unconfigured must take the original path.

    Every edit is either a widening of the allowed-mode set, or an ``or`` whose
    left side is false in any other mode — so with the default ``coerce`` the
    expression reduces to exactly what was there before.
    """
    for edit in patcher.EDITS:
        if "frozenset" in edit.before:
            assert edit.after == edit.before.replace(
                '"always_auto"}', '"always_auto", "passthrough"}'
            )
            continue
        first_before = edit.before.splitlines()[0]
        first_after = edit.after.splitlines()[0]
        assert 'mode == "passthrough" or ' in first_after
        assert first_after.replace('mode == "passthrough" or ', "") == first_before
        assert edit.after.splitlines()[1:] == edit.before.splitlines()[1:]


def test_an_unrecognised_checkout_is_refused_and_left_alone():
    older = APP_PY.replace(
        "                if validate_agent_name_format(sanitized):",
        "                if some_older_helper(sanitized):",
    )
    with tempfile.TemporaryDirectory() as directory:
        root = _checkout(pathlib.Path(directory), app=older)
        before = _texts(root)
        assert patcher.inspect(root)["state"] == "unsupported"
        try:
            patcher.apply(root)
        except PatchError as exc:
            assert "does not match" in str(exc)
        else:
            raise AssertionError("apply() patched a checkout it does not recognise")
        assert _texts(root) == before


def test_a_duplicated_anchor_is_refused():
    """Two matches means picking one would be a guess."""
    doubled = APP_PY + "\n\n" + APP_PY
    with tempfile.TemporaryDirectory() as directory:
        root = _checkout(pathlib.Path(directory), app=doubled)
        verdict = patcher.inspect(root)
        assert verdict["state"] == "unsupported"
        assert any("2 matches" in item for item in verdict["unmatched"])


def test_a_half_patched_checkout_is_refused():
    half = APP_PY.replace(
        "            elif validate_agent_name_format(sanitized):",
        '            elif mode == "passthrough" or validate_agent_name_format(sanitized):',
    )
    with tempfile.TemporaryDirectory() as directory:
        root = _checkout(pathlib.Path(directory), app=half)
        before = _texts(root)
        assert patcher.inspect(root)["state"] == "partial"
        try:
            patcher.apply(root)
        except PatchError:
            pass
        else:
            raise AssertionError("apply() completed a half-patched checkout")
        assert _texts(root) == before


def test_a_missing_directory_is_an_error_not_a_success():
    with tempfile.TemporaryDirectory() as directory:
        missing = pathlib.Path(directory) / "nope"
        try:
            patcher.inspect(missing)
        except PatchError:
            return
        raise AssertionError("inspect() accepted a directory that does not exist")


def test_a_directory_that_is_not_agent_mail_is_an_error():
    with tempfile.TemporaryDirectory() as directory:
        try:
            patcher.inspect(pathlib.Path(directory))
        except PatchError as exc:
            assert "not an agent-mail checkout" in str(exc)
            return
        raise AssertionError("inspect() accepted a directory with no agent-mail source")


def test_the_cli_reports_failure_with_a_nonzero_status():
    with tempfile.TemporaryDirectory() as directory:
        code = patcher.main(["--mail-dir", str(pathlib.Path(directory) / "nope"), "--apply"])
        assert code == 1


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
