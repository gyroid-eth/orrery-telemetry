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
    monkeypatch.setattr(catalog, "_bound_child_path", None)
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
    assert catalog.resolve_catalog(FALLBACK, bundled_overflow=()) == catalog.ModelCatalog((*FALLBACK, "claude-new-9"), "local_cache")


def test_cache_refresh_and_expiry_apply_without_restart(profile):
    path = write(profile, document("claude-new-9"))
    assert catalog.resolve_catalog(FALLBACK).models == (*FALLBACK, "claude-new-9")
    path.write_text(json.dumps(document("claude-new-10", fetched=200)))
    assert catalog.resolve_catalog(FALLBACK).models == (*FALLBACK, "claude-new-10")
    path.write_text(json.dumps(document("claude-new-10", stale=NOW)))
    assert catalog.resolve_catalog(FALLBACK, bundled_overflow=()) == catalog.ModelCatalog(FALLBACK, "bundled")


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


def _aliased(rows, fetched=100, stale=2000):
    """rows: (id, section, short_name, min_claude_code_version)."""
    data = document(*[row[0] for row in rows], fetched=fetched, stale=stale)
    for target, (_, section, short_name, minimum) in zip(data["catalog"]["config"]["models"], rows):
        target.update({"section": section, "short_name": short_name})
        if minimum is not None:
            target["min_claude_code_version"] = minimum
    return data


CURRENT = [("claude-opus-6", "main", "Opus", "2.2.0"), ("claude-sonnet-6", "main", "Sonnet", None),
           ("claude-haiku-5", "main", "Haiku", None), ("claude-fable-6", "main", "Fable", None),
           ("claude-sonnet-5-5", "overflow", "Sonnet", None), ("claude-opus-5-5", "overflow", "Opus", None)]


def _new_cli():
    return (2, 2, 1)


def test_aliases_use_bundled_table_without_a_catalog():
    for family, model in catalog.BUNDLED_ALIASES.items():
        target = catalog.resolve_alias(family, cli_version=_new_cli)
        assert (target.model, target.source) == (model, "bundled")
        assert target.note == "no readable local Claude Code model catalog"
    assert catalog.BUNDLED_ALIASES["sonnet"] == "claude-sonnet-5-5"
    assert catalog.default_model() == catalog.BUNDLED_ALIASES["opus"]


def test_aliases_follow_the_catalog_main_rows_not_overflow_or_order(profile):
    write(profile, _aliased(list(reversed(CURRENT))))
    resolved = {family: catalog.resolve_alias(family, cli_version=_new_cli) for family in catalog.BUNDLED_ALIASES}
    assert {family: target.model for family, target in resolved.items()} == {
        "opus": "claude-opus-6", "sonnet": "claude-sonnet-6", "haiku": "claude-haiku-5", "fable": "claude-fable-6"}
    assert {target.source for target in resolved.values()} == {"local_catalog"}
    assert {target.note for target in resolved.values()} == {""}


def test_stale_catalog_still_names_the_current_model_and_says_so(monkeypatch, profile):
    monkeypatch.setattr(catalog, "_probe_cli_version", _new_cli)
    write(profile, _aliased(CURRENT, fetched=100, stale=500))
    target = catalog.resolve_alias("sonnet", cli_version=_new_cli)
    assert (target.model, target.source, target.note) == ("claude-sonnet-6", "local_catalog", "catalog is stale")
    assert catalog.alias_note().startswith("ok: Claude model aliases follow the local Claude Code catalog (stale;")


def test_a_row_the_installed_cli_cannot_run_falls_back(monkeypatch, profile):
    write(profile, _aliased(CURRENT))
    target = catalog.resolve_alias("opus", cli_version=lambda: (2, 1, 290))
    assert (target.model, target.source) == (catalog.BUNDLED_ALIASES["opus"], "bundled")
    assert target.note == "claude-opus-6 needs Claude Code 2.2.0; installed 2.1.290"
    # Unknown CLI version is not evidence against the row.
    assert catalog.resolve_alias("opus", cli_version=lambda: None).model == "claude-opus-6"
    monkeypatch.setattr(catalog, "_probe_cli_version", lambda: (2, 1, 290))
    assert catalog.alias_note().startswith("note: Claude model aliases use the bundled table")


@pytest.mark.parametrize("minimum", ["two", 2.2, "2.2.0-beta"])
def test_an_unreadable_version_requirement_is_not_trusted(profile, minimum):
    write(profile, _aliased([("claude-opus-6", "main", "Opus", minimum)]))
    target = catalog.resolve_alias("opus", cli_version=_new_cli)
    assert (target.model, target.source) == (catalog.BUNDLED_ALIASES["opus"], "bundled")


def test_catalog_without_the_family_falls_back_per_family(profile):
    write(profile, _aliased([("claude-opus-6", "main", "Opus", None), ("claude-unknown-1", "main", "Mystery", None)]))
    assert catalog.resolve_alias("opus", cli_version=_new_cli).model == "claude-opus-6"
    fable = catalog.resolve_alias("fable", cli_version=_new_cli)
    assert (fable.model, fable.source, fable.note) == (
        catalog.BUNDLED_ALIASES["fable"], "bundled", "local catalog names no current fable model")


def test_unknown_alias_is_rejected():
    with pytest.raises(ValueError):
        catalog.resolve_alias("sonnet-5")


def test_cli_version_is_read_from_the_install_without_running_it(tmp_path):
    native = tmp_path / "share/claude/versions/2.1.287"
    native.parent.mkdir(parents=True)
    native.write_text("#!/bin/sh\nexit 99\n")
    link = tmp_path / "bin/claude"
    link.parent.mkdir()
    link.symlink_to(native)
    assert catalog._probe_cli_version(str(link)) == (2, 1, 287)

    package = tmp_path / "node_modules/@anthropic-ai/claude-code"
    package.mkdir(parents=True)
    (package / "package.json").write_text(json.dumps({"name": "@anthropic-ai/claude-code", "version": "2.1.250"}))
    (package / "cli.js").write_text("")
    assert catalog._probe_cli_version(str(package / "cli.js")) == (2, 1, 250)

    other = tmp_path / "other/claude"
    other.parent.mkdir()
    other.write_text("")
    assert catalog._probe_cli_version(str(other)) is None
    assert catalog._probe_cli_version("") is None  # The launcher could not find it.


def test_dashboard_checks_the_claude_a_child_runs_not_the_first_on_path(monkeypatch, tmp_path):
    """Children put ~/.local/bin first on PATH; AGENTSTACK_CLAUDE_BIN is not theirs."""
    home = tmp_path / "home"
    old = home / ".local/share/claude/versions/2.0.0"
    old.parent.mkdir(parents=True)
    old.write_text("")
    old.chmod(0o755)
    (home / ".local/bin").mkdir(parents=True)
    (home / ".local/bin/claude").symlink_to(old)
    new = tmp_path / "elsewhere/versions/9.0.0"
    new.parent.mkdir(parents=True)
    new.write_text("")
    new.chmod(0o755)
    (tmp_path / "pathbin").mkdir()
    (tmp_path / "pathbin/claude").symlink_to(new)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(tmp_path / "pathbin"))
    monkeypatch.setenv("AGENTSTACK_CLAUDE_BIN", str(tmp_path / "pathbin/claude"))
    assert catalog.child_cli_path() == str(home / ".local/bin/claude")
    assert catalog._probe_cli_version() == (2, 0, 0)
    (home / ".local/bin/claude").unlink()
    assert catalog._probe_cli_version() == (9, 0, 0)


def test_a_family_with_two_current_models_is_ambiguous(profile):
    write(profile, _aliased([("claude-opus-6", "main", "Opus", None), ("claude-opus-7", "main", "Opus", None),
                             ("claude-sonnet-6", "main", "Sonnet", None)]))
    opus = catalog.resolve_alias("opus", cli_version=_new_cli)
    assert (opus.model, opus.source) == (catalog.BUNDLED_ALIASES["opus"], "bundled")
    assert catalog.resolve_alias("sonnet", cli_version=_new_cli).model == "claude-sonnet-6"


def test_a_row_whose_id_is_not_its_family_is_not_trusted(profile):
    write(profile, _aliased([("claude-haiku-9", "main", "Opus", None)]))
    assert catalog.resolve_alias("opus", cli_version=_new_cli).source == "bundled"


@pytest.mark.parametrize("minimum", ["9" * 5000, "1234567.0", "1.2.3.4.5"])
def test_an_oversized_version_requirement_falls_back_without_failing(profile, minimum):
    write(profile, _aliased([("claude-opus-6", "main", "Opus", minimum)]))
    target = catalog.resolve_alias("opus", cli_version=_new_cli)
    assert (target.model, target.source) == (catalog.BUNDLED_ALIASES["opus"], "bundled")


def test_a_stale_catalog_naming_an_older_generation_loses_to_the_bundled_table(profile):
    write(profile, _aliased([("claude-opus-5", "main", "Opus", None)], fetched=100, stale=500))
    target = catalog.resolve_alias("opus", cli_version=_new_cli)
    assert (target.model, target.source) == ("claude-opus-5-5", "bundled")
    assert target.note == "stale catalog names an older opus (claude-opus-5)"
    # A fresh catalog is the vendor's current word, even when older.
    write(profile, _aliased([("claude-opus-5", "main", "Opus", None)]))
    assert catalog.resolve_alias("opus", cli_version=_new_cli).model == "claude-opus-5"


def test_alias_models_lists_what_the_aliases_launch(monkeypatch, profile):
    monkeypatch.setattr(catalog, "_probe_cli_version", _new_cli)
    write(profile, _aliased(CURRENT, fetched=100, stale=500))
    assert catalog.alias_models() == ("claude-opus-6", "claude-sonnet-6", "claude-haiku-5", "claude-fable-6")


def test_helper_checks_the_binary_the_launcher_passes(profile, tmp_path, capsys):
    write(profile, _aliased(CURRENT))
    old = tmp_path / "versions/2.1.0"
    old.parent.mkdir()
    old.write_text("")
    assert catalog.main(["aliases", "--claude-bin", str(old)]) == 0
    lines = dict(line.split("\t", 1) for line in capsys.readouterr().out.splitlines())
    assert lines["opus"].startswith("claude-opus-5-5\tbundled\tclaude-opus-6 needs Claude Code 2.2.0")
    assert lines["sonnet"].startswith("claude-sonnet-6\tlocal_catalog")
    assert catalog.main(["aliases", "--claude-bin", ""]) == 0
    assert "claude-opus-6\tlocal_catalog" in capsys.readouterr().out
    assert catalog.main(["aliases", "--claude-bin"]) == 2


def test_doctor_reports_where_the_aliases_resolve():
    doctor = (Path(__file__).resolve().parent.parent / "scripts/doctor.sh").read_text(encoding="utf-8")
    assert 'report_claude_model_aliases "$INSTALL_DIR/dashboard/claude_models.py"' in doctor
    assert '"$PYTHON_BIN" "$helper" note' in doctor


def test_the_shared_script_answers_for_the_child_and_is_remembered(monkeypatch, tmp_path):
    hooks = Path(__file__).resolve().parent.parent / "hooks"
    found = tmp_path / "versions/2.0.0"
    found.parent.mkdir()
    found.write_text("")
    found.chmod(0o755)
    shell = tmp_path / "shell"
    shell.write_text(f"#!/bin/sh\necho noise from a profile\necho {found}\n")
    shell.chmod(0o755)
    monkeypatch.setenv("AGENTSTACK_CHILD_SHELL", str(shell))
    assert catalog.bind_child_cli_path(str(hooks)) == str(found)
    assert catalog.child_cli_path() == str(found)
    assert catalog._probe_cli_version() == (2, 0, 0)
    shell.write_text("#!/bin/sh\necho not-a-path\n")
    assert catalog.bind_child_cli_path(str(hooks)) == ""
    assert catalog._probe_cli_version() is None  # Found nothing: unknown, not a guess.
    assert catalog.bind_child_cli_path(str(tmp_path / "no-hooks")) is None
    assert catalog.child_cli_path() == ""
