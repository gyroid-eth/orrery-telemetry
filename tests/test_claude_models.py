"""Hermetic coverage of the opportunistic local Claude Code catalog."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from dashboard import claude_models as catalog

FALLBACK = ("claude-sonnet-5", "claude-opus-5-5")
NOW = 1000


@pytest.fixture(autouse=True)
def profile(monkeypatch, tmp_path):
    root = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    monkeypatch.delenv("AGENTSTACK_CLAUDE_MODELS", raising=False)
    monkeypatch.setattr(catalog.time, "time", lambda: NOW / 1000)
    return root


def document(*models, fetched=100, stale=2000):
    return {"version": 2, "fetchedAt": fetched, "staleAt": stale,
            "catalog": {"surface": "cc", "config": {"models": [{"id": m} for m in models]}}}


def write(root, data, name="cache.json"):
    path = root / "cache" / "model-catalog" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_override_precedes_cache_and_preserves_order(monkeypatch, profile):
    write(profile, document("claude-cache-9"))
    monkeypatch.setenv("AGENTSTACK_CLAUDE_MODELS", " claude-opus-9, ,claude-opus-9,claude-sonnet-9[1m] ")
    monkeypatch.setattr(catalog, "discover_catalog", lambda: pytest.fail("override read cache"))
    assert catalog.resolve_catalog(FALLBACK) == catalog.ModelCatalog(("claude-opus-9", "claude-sonnet-9[1m]"), "override")


@pytest.mark.parametrize("raw", ["gpt-9", "claude-opus-9,gpt-9", "claude-x;echo BAD", "../x", "claude-x\nBAD", ",,", "claude--9", "claude-" + "x" * 128])
def test_invalid_override_disables_without_echoing_value(monkeypatch, raw):
    monkeypatch.setenv("AGENTSTACK_CLAUDE_MODELS", raw)
    result = catalog.resolve_catalog(FALLBACK)
    assert result.models == ()
    assert result.source == "override"
    assert result.error == "AGENTSTACK_CLAUDE_MODELS contains invalid model IDs"


def test_freshest_cli_cache_is_selected_not_merged(profile):
    write(profile, document("claude-old-9"), "old.json")
    write(profile, document("claude-new-9", "claude-new-9", fetched=200), "new.json")
    write(profile, document("claude-stale-9", fetched=300, stale=500), "stale.json")
    desktop = document("claude-desktop-9", fetched=400)
    desktop["catalog"]["surface"] = "ccd"
    write(profile, desktop, "desktop.json")
    assert catalog.resolve_catalog(FALLBACK) == catalog.ModelCatalog((*FALLBACK, "claude-new-9"), "local_cache")


def test_cache_refresh_and_expiry_apply_without_restart(profile):
    path = write(profile, document("claude-new-9"))
    assert catalog.resolve_catalog(FALLBACK).models == (*FALLBACK, "claude-new-9")
    path.write_text(json.dumps(document("claude-new-10", fetched=200)))
    assert catalog.resolve_catalog(FALLBACK).models == (*FALLBACK, "claude-new-10")
    path.write_text(json.dumps(document("claude-new-10", stale=NOW)))
    assert catalog.resolve_catalog(FALLBACK) == catalog.ModelCatalog(FALLBACK, "bundled")


@pytest.mark.parametrize("key,value", [
    ("version", 1), ("version", True), ("version", "2"),
    ("fetchedAt", True), ("fetchedAt", "100"), ("fetchedAt", NOW + 1),
    ("fetchedAt", float("nan")), ("fetchedAt", float("inf")), ("fetchedAt", 10 ** 400),
    ("staleAt", NOW), ("staleAt", 99), ("staleAt", float("nan")),
    ("staleAt", float("inf")), ("staleAt", None), ("catalog", []),
])
def test_unknown_schema_or_bad_dates_fall_back(profile, key, value):
    data = document("claude-new-9")
    data[key] = value
    write(profile, data)
    assert catalog.resolve_catalog(FALLBACK).models == FALLBACK


@pytest.mark.parametrize("rows", [None, {}, [], [None], [{"id": "gpt-9"}], [{"id": "claude-good-9"}, {"id": "../bad"}], [{"id": "claude-9"}] * 129])
def test_malformed_model_rows_reject_the_entire_cache(profile, rows):
    data = document("claude-new-9")
    data["catalog"]["config"]["models"] = rows
    write(profile, data)
    assert catalog.resolve_catalog(FALLBACK).models == FALLBACK


@pytest.mark.parametrize("content", [b"{", b"\xff", b"[" * 1500, b" " * (catalog.MAX_BYTES + 1)])
def test_broken_or_oversized_cache_is_bounded(profile, content):
    path = write(profile, document("claude-new-9"))
    path.write_bytes(content)
    assert catalog.resolve_catalog(FALLBACK).models == FALLBACK


def test_newest_broken_cache_does_not_hide_older_valid_cache(profile):
    write(profile, document("claude-valid-9"))
    write(profile, [], "other.json")
    assert catalog.resolve_catalog(FALLBACK).models == (*FALLBACK, "claude-valid-9")


@pytest.mark.parametrize("setting", [None, "", "   "])
def test_default_profile_for_missing_or_empty_setting(monkeypatch, tmp_path, setting):
    monkeypatch.setenv("HOME", str(tmp_path))
    if setting is None:
        monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    else:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", setting)
    write(tmp_path / ".claude", document("claude-home-9"))
    assert catalog.resolve_catalog(FALLBACK).models == (*FALLBACK, "claude-home-9")


def test_relative_profile_never_searches_working_directory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "relative")
    write(tmp_path / "relative", document("claude-untrusted-9"))
    assert catalog.resolve_catalog(FALLBACK).models == FALLBACK


def test_symlink_and_nonregular_cache_are_not_read(profile, tmp_path):
    target = write(tmp_path, document("claude-external-9"))
    directory = profile / "cache" / "model-catalog"
    directory.mkdir(parents=True)
    (directory / "linked.json").symlink_to(target)
    (directory / "directory.json").mkdir()
    if hasattr(os, "mkfifo"):
        os.mkfifo(directory / "pipe.json")
    assert catalog.resolve_catalog(FALLBACK).models == FALLBACK


def test_file_budget_falls_back_instead_of_guessing_newest(profile):
    for n in range(catalog.MAX_FILES + 1):
        write(profile, document("claude-many-9"), f"{n}.json")
    assert catalog.resolve_catalog(FALLBACK).models == FALLBACK


def test_discovery_only_opens_catalog_files(monkeypatch, profile):
    path = write(profile, document("claude-new-9"))
    (profile / ".credentials.json").write_text("DO NOT READ")
    opened = []
    original = catalog.os.open
    def checked_open(target, flags, *args, **kwargs):
        opened.append(Path(target))
        assert Path(target) == path
        return original(target, flags, *args, **kwargs)
    monkeypatch.setattr(catalog.os, "open", checked_open)
    assert catalog.resolve_catalog(FALLBACK).models == (*FALLBACK, "claude-new-9")
    assert opened == [path]


def test_unresolvable_profile_home_falls_back(monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "~orrery-no-such-user-test/profile")
    assert catalog.resolve_catalog(FALLBACK).models == FALLBACK


def test_discovery_adds_candidates_without_reordering_or_removing_bundled(profile):
    write(profile, document("claude-new-9", FALLBACK[1], FALLBACK[0]))
    assert catalog.resolve_catalog(FALLBACK).models == (*FALLBACK, "claude-new-9")


@pytest.mark.parametrize("value,valid", [
    ("claude-future-9", True), ("claude-sonnet-9[1m]", True),
    ("claude-fable-5", True), ("gpt-9", False), ("claude-", False),
    ("claude-x;echo BAD", False), ("claude-x\n", False), (None, False),
])
def test_formal_id_validation_is_independent_of_catalog(value, valid):
    assert catalog.is_model_id(value) is valid


def _sectioned(entries, fetched=100, stale=2000):
    data = document(*[m for m, _ in entries], fetched=fetched, stale=stale)
    for row, (_, section) in zip(data["catalog"]["config"]["models"], entries):
        if section:
            row["section"] = section
    return data


def test_fresh_sections_decide_the_fold(profile):
    write(profile, _sectioned([("claude-opus-5-5", "main"), ("claude-opus-4-8", "overflow"),
                               ("claude-sonnet-4-6", "overflow")]))
    resolved = catalog.resolve_catalog(FALLBACK, bundled_overflow=())
    assert resolved.source == "local_cache"
    assert resolved.models == ("claude-sonnet-5", "claude-opus-5-5", "claude-opus-4-8", "claude-sonnet-4-6")
    assert resolved.overflow == ("claude-opus-4-8", "claude-sonnet-4-6")


def test_bundled_table_folds_without_any_catalog(profile):
    resolved = catalog.resolve_catalog((*FALLBACK, "claude-opus-5"), bundled_overflow=("claude-opus-5",))
    assert resolved.source == "bundled"
    assert resolved.overflow == ("claude-opus-5",)


def test_stale_catalog_only_folds_existing_candidates(profile):
    write(profile, _sectioned([("claude-sonnet-5", "overflow"), ("claude-new-9", "overflow")], stale=500))
    resolved = catalog.resolve_catalog(FALLBACK, bundled_overflow=())
    assert resolved.models == FALLBACK  # a stale cache never adds candidates
    assert resolved.overflow == ("claude-sonnet-5",)


def test_fresh_main_overrides_the_bundled_table_but_stale_main_does_not(profile):
    write(profile, _sectioned([("claude-opus-5", "main")]))
    fresh = catalog.resolve_catalog((*FALLBACK, "claude-opus-5"), bundled_overflow=("claude-opus-5",))
    assert fresh.overflow == ()
    write(profile, _sectioned([("claude-opus-5", "main")], stale=500))
    stale = catalog.resolve_catalog((*FALLBACK, "claude-opus-5"), bundled_overflow=("claude-opus-5",))
    assert stale.overflow == ("claude-opus-5",)


def test_future_or_inverted_catalog_windows_are_ignored(profile):
    write(profile, _sectioned([("claude-sonnet-5", "overflow")], fetched=5000, stale=9000))
    assert catalog.resolve_catalog(FALLBACK, bundled_overflow=()).overflow == ()
    write(profile, _sectioned([("claude-sonnet-5", "overflow")], fetched=900, stale=800))
    assert catalog.resolve_catalog(FALLBACK, bundled_overflow=()).overflow == ()
