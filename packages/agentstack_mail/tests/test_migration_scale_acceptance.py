from __future__ import annotations

from pathlib import Path

import pytest

from agentstack_mail import scale_acceptance


def test_synthetic_payload_is_deterministic_and_indexed() -> None:
    module = scale_acceptance
    size = 256

    assert module._payload(0, size) == module._payload(0, size)
    assert module._payload(0, size) != module._payload(1, size)


def test_tree_scale_rejects_non_regular_fixture_entries(tmp_path: Path) -> None:
    module = scale_acceptance
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "unsafe").symlink_to(tmp_path)

    with pytest.raises(module.ScaleAcceptanceError, match="unsafe fixture entry"):
        module._tree_scale(archive)


def test_git_environment_overrides_hostile_automatic_maintenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = scale_acceptance
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "gc.auto")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "1")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'maintenance.auto=true'")

    environment = module._git_environment()

    assert "GIT_CONFIG_PARAMETERS" not in environment
    assert environment["GIT_CONFIG_COUNT"] == "3"
    assert environment["GIT_CONFIG_KEY_0"] == "gc.auto"
    assert environment["GIT_CONFIG_VALUE_0"] == "0"
    assert environment["GIT_CONFIG_KEY_1"] == "gc.autoDetach"
    assert environment["GIT_CONFIG_VALUE_1"] == "false"
    assert environment["GIT_CONFIG_KEY_2"] == "maintenance.auto"
    assert environment["GIT_CONFIG_VALUE_2"] == "false"
