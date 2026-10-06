"""Portable, isolated tests of Codex local discovery and launch policy."""
from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from dashboard import codex_models as models

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def isolated_catalog(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.delenv("AGENTSTACK_CODEX_MODELS", raising=False)
    monkeypatch.delenv("AGENTSTACK_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(models.time, "time", lambda: NOW)
    monkeypatch.delenv("AGENTSTACK_CODEX_BIN", raising=False)
    monkeypatch.setattr(models, "cli_version", lambda: (0, 159, 1))


def row(model="gpt-6.1-sol", efforts=("low", "medium", "high", "xhigh", "max", "ultra"), **extra):
    return {"slug": model, "visibility": "list", "default_reasoning_level": efforts[0] if efforts else "none",
            "supported_reasoning_levels": [{"effort": effort, "description": "unused"} for effort in efforts], **extra}


def cache(rows=None, *, age=0, **extra):
    path = Path(os.environ["CODEX_HOME"]) / "models_cache.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"fetched_at": datetime.fromtimestamp(NOW-age, timezone.utc).isoformat(),
            "client_version": "0.155.0", "models": [row()] if rows is None else rows, **extra}
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_fresh_cache_adds_gpt6_and_unknown_future_models_without_changing_default():
    cache([row("gpt-9-future.1"), row("gpt-6-luna", ("low", "medium", "high", "xhigh", "max")), row()])
    catalog = models.resolve_catalog()
    assert catalog.source == "local_cache"
    assert "gpt-9-future.1" in catalog.models
    assert "gpt-6-sol" in catalog.models and "gpt-6-luna" in catalog.models
    provider = models.provider_catalog()
    assert provider["default_model"] == "gpt-6.1-sol"
    assert "ultra" not in provider["model_efforts"]["gpt-6-luna"]
    assert "ultra" in provider["model_efforts"]["gpt-6-sol"]


def test_discovery_uses_only_selected_catalog_file(monkeypatch):
    path = cache([row("gpt-future")], identity="opaque-not-an-authorization-proof")
    real_open = models.os.open
    opened = []
    def tracked_open(file, *args, **kwargs):
        opened.append(Path(file))
        assert Path(file) == path
        return real_open(file, *args, **kwargs)
    monkeypatch.setattr(models.os, "open", tracked_open)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("discovery must not execute a CLI"))
    assert "gpt-future" in models.resolve_catalog().models
    assert opened == [path]


def test_codex_home_unset_uses_home_without_reading_configuration(monkeypatch, tmp_path):
    monkeypatch.delenv("CODEX_HOME")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    # Path.expanduser follows HOME/USERPROFILE; set both for the portable lane.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    p = tmp_path / ".codex" / "models_cache.json"
    p.parent.mkdir()
    p.write_text(json.dumps({"fetched_at": datetime.fromtimestamp(NOW, timezone.utc).isoformat(), "models": [row("gpt-home-test")]}))
    assert "gpt-home-test" in models.discover_models()


def test_relative_home_does_not_probe_a_project_directory(monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "relative-profile")
    assert models.resolve_catalog().source == "bundled"


@pytest.mark.parametrize("failure", ["missing", "broken", "empty", "array", "schema", "timestamp", "naive", "future", "stale", "oversize", "too_many", "directory"])
def test_cache_failure_is_bundled_fallback(failure):
    path = Path(os.environ["CODEX_HOME"]) / "models_cache.json"
    if failure == "missing":
        pass
    elif failure == "broken":
        cache().write_text("{broken")
    elif failure == "array":
        cache().write_text("[]")
    elif failure == "empty":
        cache([])
    elif failure == "schema":
        cache(models={"gpt-future": {}})
    elif failure == "timestamp":
        cache(fetched_at="not a time")
    elif failure == "naive":
        cache(fetched_at="2027-01-15T08:00:00")
    elif failure == "future":
        cache(age=-1)
    elif failure == "stale":
        cache(age=models.CACHE_TTL_SECONDS+1)
    elif failure == "oversize":
        cache().write_bytes(b" "*(models.MAX_BYTES+1))
    elif failure == "too_many":
        cache([row() for _ in range(models.MAX_MODELS+1)])
    elif failure == "directory":
        path.mkdir(parents=True)
    result = models.resolve_catalog()
    assert result.models == models.DEFAULT_MODELS
    assert result.source == "bundled" and result.error == ""
    assert result.default_model == models.DEFAULT_MODEL
    assert models.normalize_model("gpt-future-outside-cache") == "gpt-future-outside-cache"


@pytest.mark.parametrize("age", [0, models.CACHE_TTL_SECONDS])
def test_cache_ttl_boundary(age):
    cache([row("gpt-fresh")], age=age)
    assert "gpt-fresh" in models.discover_models()


def test_untrusted_symlink_is_not_followed(tmp_path):
    target = tmp_path / "other.json"
    target.write_text("not a catalog")
    path = Path(os.environ["CODEX_HOME"]) / "models_cache.json"
    path.parent.mkdir()
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("symlink privilege unavailable")
    assert models.discover_models() == {}


def test_launcher_owned_child_cache_symlink_is_followed(monkeypatch, tmp_path):
    source = tmp_path / "parent-codex" / "models_cache.json"
    source.parent.mkdir()
    source.write_text(json.dumps({
        "fetched_at": datetime.fromtimestamp(NOW, timezone.utc).isoformat(),
        "models": [row("gpt-child-cache")],
    }), encoding="utf-8")
    runtime = tmp_path / "runtime"
    child_home = runtime / "child-agents" / "ChildCurie.codex-home"
    child_home.mkdir(parents=True)
    path = child_home / "models_cache.json"
    try:
        path.symlink_to(source)
    except OSError:
        pytest.skip("symlink privilege unavailable")
    monkeypatch.setenv("AGENTSTACK_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("CODEX_HOME", str(child_home))
    assert "gpt-child-cache" in models.discover_models()


def _load_child_resume():
    spec = importlib.util.spec_from_file_location(
        "child_resume_for_catalog_test", Path(__file__).resolve().parents[1] / "hooks" / "child_resume.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(importlib.util.find_spec("fcntl") is None, reason="child_resume.py needs POSIX fcntl")
def test_grandchild_home_built_by_child_resume_follows_the_link_chain(monkeypatch, tmp_path):
    """Codex child -> Codex grandchild: build_home links to the parent home's entry, two hops."""
    child_resume = _load_child_resume()
    source = tmp_path / "codex"
    source.mkdir()
    (source / "models_cache.json").write_text(json.dumps({
        "fetched_at": datetime.fromtimestamp(NOW, timezone.utc).isoformat(),
        "models": [row("gpt-grandchild-cache")],
    }), encoding="utf-8")
    runtime = tmp_path / "runtime"
    runner = tmp_path / "runner"
    runner.write_text("#!/bin/sh\n")
    runner.chmod(0o755)

    def build(parent_home, name):
        token = tmp_path / f"token-{name}"
        token.write_text("token")
        token.chmod(0o600)
        home = runtime / "child-agents" / f"{name}.codex-home"
        try:
            return child_resume.build_home(
                home=home, source=parent_home, runner=runner, child=name, project_key=str(tmp_path),
                token_file=token, mcp_url="http://127.0.0.1:9/mcp", mail_env="", runtime_dir=runtime,
                bearer_mode="off", python_bin="", mcp_profile="inherit")
        except OSError:
            pytest.skip("symlink privilege unavailable")

    child = build(source, "ChildCurie")
    grandchild = build(child, "GrandchildNoether")
    assert os.readlink(grandchild / "models_cache.json") == str(child / "models_cache.json")
    monkeypatch.setenv("AGENTSTACK_RUNTIME_DIR", str(runtime))
    for home in (child, grandchild):
        monkeypatch.setenv("CODEX_HOME", str(home))
        assert "gpt-grandchild-cache" in models.discover_models()


def _child_link(runtime, name, target):
    home = runtime / "child-agents" / f"{name}.codex-home"
    home.mkdir(parents=True, exist_ok=True)
    link = home / "models_cache.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink privilege unavailable")
    return link


def test_link_chain_cycle_is_not_followed(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    first = runtime / "child-agents" / "A.codex-home" / "models_cache.json"
    second = _child_link(runtime, "B", first)
    _child_link(runtime, "A", second)
    monkeypatch.setenv("AGENTSTACK_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("CODEX_HOME", str(first.parent))
    assert models.discover_models() == {}


def test_link_chain_through_an_untrusted_hop_is_not_followed(monkeypatch, tmp_path):
    cache()  # a valid catalog at the end of the chain
    real = Path(os.environ["CODEX_HOME"]) / "models_cache.json"
    stray = tmp_path / "elsewhere" / "models_cache.json"
    stray.parent.mkdir()
    try:
        stray.symlink_to(real)
    except OSError:
        pytest.skip("symlink privilege unavailable")
    runtime = tmp_path / "runtime"
    link = _child_link(runtime, "ChildCurie", stray)
    monkeypatch.setenv("AGENTSTACK_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("CODEX_HOME", str(link.parent))
    assert models.discover_models() == {}


def test_link_chain_through_a_symlinked_child_home_is_not_followed(monkeypatch, tmp_path):
    """B.codex-home passes the name check but is a directory symlink leading outside."""
    external = tmp_path / "external-codex"
    external.mkdir()
    (external / "models_cache.json").write_text(json.dumps({
        "fetched_at": datetime.fromtimestamp(NOW, timezone.utc).isoformat(),
        "models": [row("gpt-marker-external")],
    }), encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    runtime = tmp_path / "runtime"
    child_root = runtime / "child-agents"
    child_root.mkdir(parents=True)
    try:
        (outside / "models_cache.json").symlink_to(external / "models_cache.json")
        (child_root / "B.codex-home").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink privilege unavailable")
    link = _child_link(runtime, "A", child_root / "B.codex-home" / "models_cache.json")
    monkeypatch.setenv("AGENTSTACK_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("CODEX_HOME", str(link.parent))
    assert models.discover_models() == {}
    # The symlinked home itself is not a launcher-owned home either.
    monkeypatch.setenv("CODEX_HOME", str(child_root / "B.codex-home"))
    assert models.discover_models() == {}


def test_link_chain_longer_than_the_hop_limit_is_not_followed(monkeypatch, tmp_path):
    cache()
    runtime = tmp_path / "runtime"
    target = Path(os.environ["CODEX_HOME"]) / "models_cache.json"
    for depth in range(models.MAX_CACHE_LINK_HOPS + 1):
        target = _child_link(runtime, f"Depth{depth}", target)
    monkeypatch.setenv("AGENTSTACK_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("CODEX_HOME", str(target.parent))
    assert models.discover_models() == {}


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO")
def test_fifo_does_not_block():
    path = Path(os.environ["CODEX_HOME"]) / "models_cache.json"
    path.parent.mkdir()
    os.mkfifo(path)
    assert models.discover_models() == {}


def test_hidden_and_malformed_models_are_not_advertised():
    cache([row("gpt-valid"), row("gpt-hidden", visibility="hide"), row("claude-opus-5"),
           row("gpt-x;touch /tmp/not-executed"), row("gpt-space model"), row("gpt-"), row("gpt-非ASCII")])
    discovered = models.discover_models()
    assert list(discovered) == ["gpt-valid"]


@pytest.mark.parametrize("bad", [None, [], 1, True, "gpt-", "gpt-x\n--help", "gpt-x y", "gpt-../x", "gpt-x$(id)", "claude-x", "gpt-"+"a"*129])
def test_invalid_ids(bad):
    assert not models.is_model_id(bad)


def test_duplicates_are_deduplicated_but_conflicting_efforts_fall_back():
    cache([row("gpt-future"), row("gpt-future")])
    assert models.resolve_catalog().models.count("gpt-future") == 1
    cache([row("gpt-future"), row("gpt-future", ("high",))])
    assert models.resolve_catalog().source == "bundled"


@pytest.mark.parametrize("bad_levels", [None, "high", [{}], [{"effort": []}], [{"effort": "not-an-effort"}]])
def test_changed_effort_schema_never_advertises_unvalidated_tiers(bad_levels):
    cache([row("gpt-future", supported_reasoning_levels=bad_levels)])
    assert "gpt-future" not in models.resolve_catalog().models


def test_explicit_allowlist_wins_and_keeps_fixed_default(monkeypatch):
    cache([row("gpt-cache-only"), row("gpt-explicit", ("high",))])
    monkeypatch.setenv("AGENTSTACK_CODEX_MODELS", "gpt-explicit, gpt-second,gpt-explicit")
    provider = models.provider_catalog()
    assert provider["models"] == ["gpt-explicit", "gpt-second"]
    assert provider["default_model"] == models.DEFAULT_MODEL
    assert provider["model_source"] == "override"
    assert provider["model_efforts"]["gpt-explicit"] == ["high"]
    with pytest.raises(ValueError, match="not allowed"):
        models.normalize_model("")
    with pytest.raises(ValueError, match="not allowed"):
        models.normalize_model("gpt-cache-only")


@pytest.mark.parametrize("override", [",,,", "claude-opus-5", "gpt-ok,bad", "gpt-x;echo", "x"*(models.MAX_MODELS*129+1)],
                         ids=["empty", "foreign", "mixed", "unsafe", "oversized"])
def test_invalid_allowlist_is_visible_error_not_silent_fallback(monkeypatch, override):
    cache([row("gpt-cache")])
    # Test the parser limit without exceeding Windows' OS environment limit.
    monkeypatch.setattr(models.os, "environ", {**os.environ, "AGENTSTACK_CODEX_MODELS": override})
    provider = models.provider_catalog()
    assert provider["models"] == []
    assert "AGENTSTACK_CODEX_MODELS" in provider["model_error"]
    with pytest.raises(ValueError, match="AGENTSTACK_CODEX_MODELS"):
        models.normalize_model("gpt-6-sol")


@pytest.mark.parametrize("model", ["gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna", "gpt-6-pro", "gpt-6", "gpt-5.6", "gpt-5.6-sol", "gpt-unknown-new-generation"])
def test_formal_ids_remain_exact_even_when_cache_disappears(model):
    path = cache([row(model)])
    assert models.normalize_model(model) == model
    path.unlink()
    assert models.normalize_model(model) == model


def test_explicit_aliases_are_separate_from_omitted_default():
    assert models.normalize_model() == "gpt-6.1-sol"
    assert models.normalize_model("sol") == "gpt-6.1-sol"
    assert models.normalize_model(" LUNA ") == "gpt-6-luna"
    assert models.normalize_model("astra") == "gpt-6-astra"
    assert models.normalize_model("terra") == "gpt-5.6-terra"


@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-5.6-luna", "gpt-5.5"])
def test_explicit_effort_is_passed_through_when_metadata_is_narrower(model):
    assert models.resolve_effort(model, "ultra") == "ultra"
    assert models.resolve_effort(model, "xhigh") == "xhigh"
    assert models.resolve_effort(model, "max") == "max"


def test_fresh_effort_metadata_overrides_bundled_and_uses_valid_default():
    cache([row("gpt-6-sol", ("medium", "high"))])
    assert models.resolve_effort("gpt-6-sol") == "medium"
    assert models.resolve_effort("gpt-6-sol", "xhigh") == "xhigh"


def test_unknown_model_and_nonreasoning_model_omit_effort_by_default():
    assert models.resolve_effort("gpt-future") == ""
    assert models.resolve_effort("gpt-future", "high") == "high"
    cache([row("gpt-no-reasoning", ())])
    assert models.resolve_effort("gpt-no-reasoning") == ""
    assert models.resolve_effort("gpt-no-reasoning", "high") == "high"


def test_cli_entrypoint_matches_python_policy():
    helper = Path(models.__file__)
    result = subprocess.run([sys.executable, str(helper), "normalize", "luna"], capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout.strip() == "gpt-6-luna"
    explicit = subprocess.run([sys.executable, str(helper), "effort", "gpt-6-luna", "ultra"], capture_output=True, text=True)
    assert explicit.returncode == 0 and explicit.stdout.strip() == "ultra"


@pytest.mark.parametrize("fraction", ["1", "12", "123", "123456", "123456789"])
def test_rfc3339_fractional_seconds_are_portable(fraction):
    whole = datetime.fromtimestamp(NOW-1, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    cache([row("gpt-nanosecond")], fetched_at=whole+"."+fraction+"Z")
    assert "gpt-nanosecond" in models.discover_models()


def test_bundled_overflow_folds_only_the_fixed_table():
    catalog = models.resolve_catalog()
    assert catalog.overflow == ("gpt-5.6-sol", "gpt-5.6-luna")
    front = [model for model in catalog.models if model not in catalog.overflow]
    assert front == ["gpt-6.1-sol", "gpt-6-sol", "gpt-6-astra", "gpt-5.6-terra", "gpt-6-luna"]
    assert models.provider_catalog()["overflow_models"] == ["gpt-5.6-sol", "gpt-5.6-luna"]


def test_overflow_follows_the_table_not_version_numbers():
    # gpt-5.5 folds once the cache lists it; an unknown older-looking model does not.
    cache([row(), row("gpt-5.5", ("low", "medium", "high", "xhigh")), row("gpt-5.4-mini")])
    catalog = models.resolve_catalog()
    assert catalog.overflow == ("gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.5")
    assert "gpt-5.4-mini" in catalog.models and "gpt-5.4-mini" not in catalog.overflow
    assert models.DEFAULT_MODEL not in catalog.overflow


def test_hidden_models_stay_hidden_and_are_not_folded():
    cache([row(), row("gpt-5.5", visibility="hide")])
    catalog = models.resolve_catalog()
    assert "gpt-5.5" not in catalog.models and "gpt-5.5" not in catalog.overflow


def test_explicit_allowlist_is_not_folded(monkeypatch):
    monkeypatch.setenv("AGENTSTACK_CODEX_MODELS", "gpt-6-sol,gpt-5.6-sol")
    assert models.resolve_catalog().overflow == ()
    assert models.provider_catalog()["overflow_models"] == []


def test_fresh_cache_hiding_bundled_models_removes_them_from_menu_and_fold():
    cache([row(), row("gpt-5.6-sol", visibility="hide"), row("gpt-5.6-luna", visibility="hide"),
           row("gpt-5.5", visibility="hide")])
    catalog = models.resolve_catalog()
    for model in ("gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.5"):
        assert model not in catalog.models and model not in catalog.overflow
    assert catalog.overflow == ()
    assert models.provider_catalog()["overflow_models"] == []
    assert "gpt-6-astra" in catalog.models  # not mentioned by the cache: bundled candidate stays


def test_hidden_default_stays_as_the_fixed_contract():
    cache([row("gpt-6-astra"), row(models.DEFAULT_MODEL, visibility="hide")])
    assert models.DEFAULT_MODEL in models.resolve_catalog().models


def test_expired_cache_does_not_hide_bundled_models():
    cache([row(), row("gpt-5.6-sol", visibility="hide")], age=models.CACHE_TTL_SECONDS + 1)
    catalog = models.resolve_catalog()
    assert "gpt-5.6-sol" in catalog.models and "gpt-5.6-sol" in catalog.overflow


def test_explicit_allowlist_keeps_a_model_the_cache_hides(monkeypatch):
    cache([row(), row("gpt-5.6-sol", visibility="hide")])
    monkeypatch.setenv("AGENTSTACK_CODEX_MODELS", "gpt-6-sol,gpt-5.6-sol")
    assert models.resolve_catalog().models == ("gpt-6-sol", "gpt-5.6-sol")


@pytest.mark.parametrize("version", [(0, 158, 0), (0, 159, 0), (0, 159, 1), (1, 0, 0), None])
def test_cli_version_caps_default_and_cache_can_lower_it(monkeypatch, version):
    monkeypatch.setattr(models, "cli_version", lambda: version)
    for client, rows in [("0.159.1", [row()]), ("0.158.0", [row("gpt-6-sol")]),
                         ("0.159.0", [row()]), ("0.157.0", [row("gpt-6-sol")])]:
        cache(rows, client_version=client)
        preferred = version >= models.CLI_MIN_VERSION if version else client.startswith("0.159")
        expected = models.DEFAULT_MODEL if preferred and client.startswith("0.159") else models.FALLBACK_MODEL
        catalog = models.resolve_catalog()
        assert catalog.default_model == expected
        assert models.normalize_model() == models.normalize_model(" SoL ") == expected
        assert models.provider_catalog()["default_model"] == expected
        assert (models.DEFAULT_MODEL in catalog.models) is preferred
        assert models.FALLBACK_MODEL in catalog.models
        assert expected not in catalog.overflow
        assert bool(catalog.note) is (expected != models.DEFAULT_MODEL)
        if not preferred:
            assert "0.159.0 or later" in catalog.note
        # Formal IDs remain exact, including a deliberate request on an old CLI.
        assert models.normalize_model(models.DEFAULT_MODEL) == models.DEFAULT_MODEL


@pytest.mark.parametrize("age", [0, models.CACHE_TTL_SECONDS, models.CACHE_TTL_SECONDS + 1, 86_400])
def test_free_account_cache_lowers_implicit_default_and_notifies(age):
    path = cache([row("gpt-5.6-luna"), row("gpt-5.6-terra"), row("gpt-6-luna"),
                  row("gpt-reserve"), row("gpt-5.5"), {"slug": "codex-auto-review"}],
                 age=age, identity="opaque-free-account")
    catalog = models.resolve_catalog()
    assert models.normalize_model() == models.normalize_model(" SoL ") == "gpt-6-luna"
    assert catalog.default_model == models.provider_catalog()["default_model"] == "gpt-6-luna"
    assert catalog.default_model not in catalog.overflow
    assert "from gpt-6.1-sol to gpt-6-luna" in catalog.note
    assert "does not contain" in catalog.note
    assert models.provider_catalog()["model_note"] == catalog.note
    if age > models.CACHE_TTL_SECONDS:
        assert "stale" in catalog.note
        assert json.loads(path.read_text())["fetched_at"] in catalog.note
        assert "Start codex once" in catalog.note
        assert "gpt-reserve" not in catalog.models
        assert catalog.source == "bundled"
    else:
        assert "fresh" in catalog.note
        assert "gpt-reserve" in catalog.models


@pytest.mark.parametrize("age", [0, 86_400])
def test_paid_account_cache_keeps_preferred_default(age):
    cache([row("gpt-6-luna"), row("gpt-6-sol"), row(), row("gpt-6-astra")],
          age=age, identity="opaque-paid-account")
    assert models.normalize_model() == models.normalize_model("sol") == models.DEFAULT_MODEL
    assert models.provider_catalog()["model_note"] == ""


@pytest.mark.parametrize("available,expected", [
    (["gpt-6-sol", "gpt-6-luna"], "gpt-6-sol"),
    (["gpt-5.6-luna", "gpt-5.6-terra"], "gpt-5.6-terra"),
    (["gpt-5.6-luna"], "gpt-5.6-luna"),
    (["gpt-reserve"], models.DEFAULT_MODEL),
])
def test_default_preference_uses_slug_presence_not_cache_order_or_effort_schema(available, expected):
    cache([{"slug": model} for model in reversed(available)])
    assert models.normalize_model() == expected


@pytest.mark.parametrize("version", [(0, 158, 0), (0, 159, 1), None])
@pytest.mark.parametrize("age", [0, 86_400])
def test_free_cache_fallback_respects_cli_compatibility_ceiling(monkeypatch, version, age):
    monkeypatch.setattr(models, "cli_version", lambda: version)
    cache([row("gpt-6-luna"), row("gpt-5.6-terra")], age=age)
    assert models.normalize_model() == "gpt-6-luna"
    assert models.normalize_model("gpt-6.1-sol") == "gpt-6.1-sol"


@pytest.mark.parametrize("requested", ["gpt-6.1-sol", "gpt-6-sol", "astra", "terra", "luna"])
def test_free_cache_preserves_explicit_requests(requested):
    cache([row("gpt-6-luna")])
    assert models.normalize_model(requested) == models.ALIASES.get(requested, requested)


def test_stale_snapshot_cannot_add_models_hide_models_or_override_efforts():
    cache([row("gpt-6-luna", ("medium",)), row("gpt-6-sol", visibility="hide"),
           row("gpt-new-cache-only")], age=86_400)
    catalog = models.resolve_catalog()
    # Presence includes hidden IDs, so the next Sol is preferred over Luna.
    assert catalog.default_model == "gpt-6-sol"
    assert "gpt-new-cache-only" not in catalog.models
    assert catalog.efforts["gpt-6-luna"] == models.BUNDLED_EFFORTS["gpt-6-luna"]
    assert models.discover_models() == {}


def test_unreadable_cache_keeps_version_default(monkeypatch):
    cache([row("gpt-6-luna")])
    monkeypatch.setattr(models.os, "open", lambda *a, **kw: (_ for _ in ()).throw(PermissionError()))
    assert models.normalize_model() == models.DEFAULT_MODEL


def test_cache_fallback_does_not_bypass_explicit_allowlist(monkeypatch):
    cache([row("gpt-6-luna")])
    monkeypatch.setenv("AGENTSTACK_CODEX_MODELS", models.DEFAULT_MODEL)
    with pytest.raises(ValueError, match="gpt-6-luna"):
        models.normalize_model()


@pytest.mark.parametrize("failure", ["missing", "expired", "malformed", "hidden"])
def test_unknown_version_and_unusable_catalog_use_legacy_default(monkeypatch, failure):
    monkeypatch.setattr(models, "cli_version", lambda: None)
    if failure == "expired":
        cache(age=models.CACHE_TTL_SECONDS + 1)
    elif failure == "malformed":
        cache().write_text("{")
    elif failure == "hidden":
        cache([row(visibility="hide")])
    catalog = models.resolve_catalog()
    assert catalog.default_model == models.FALLBACK_MODEL
    assert models.DEFAULT_MODEL not in catalog.models
    assert models.normalize_model("sol") == models.FALLBACK_MODEL


def test_fallback_still_respects_explicit_allowlist(monkeypatch):
    monkeypatch.setattr(models, "cli_version", lambda: (0, 158, 0))
    monkeypatch.setenv("AGENTSTACK_CODEX_MODELS", models.DEFAULT_MODEL)
    cache()
    assert models.resolve_catalog().models == (models.DEFAULT_MODEL,)
    for value in ("", "sol"):
        with pytest.raises(ValueError, match=models.FALLBACK_MODEL):
            models.normalize_model(value)


# Preserve the real probe before the autouse fixture replaces it.
_real_cli_version = models.cli_version


def _version_binary(tmp_path, body):
    binary = tmp_path / "codex-cli"
    binary.write_text(f"#!{sys.executable}\n" + body)
    binary.chmod(0o755)
    return binary


def _configure_version_binary(monkeypatch, binary):
    monkeypatch.setenv("AGENTSTACK_CODEX_BIN", str(binary))
    monkeypatch.setattr(models, "_probe_launcher",
                        lambda: models.LauncherPolicy(str(binary), models._native_cli_version(str(binary))))
    if os.name == "nt":
        # Windows cannot execute a shebang fixture. Run it with the native
        # Python executable while keeping the real Popen/timeout/cache behavior.
        real_popen = models.subprocess.Popen
        monkeypatch.setattr(models.subprocess, "Popen",
                            lambda args, **kwargs: real_popen([sys.executable, *args], **kwargs))


def test_version_probe_is_cached_and_invalidated_on_binary_replacement(monkeypatch, tmp_path):
    binary = _version_binary(tmp_path, "print('codex-cli 0.159.1')\n")
    _configure_version_binary(monkeypatch, binary)
    models._VERSION_CACHE.clear()
    assert _real_cli_version() == (0, 159, 1)
    with monkeypatch.context() as m:
        m.setattr(models.subprocess, "Popen", lambda *a, **kw: pytest.fail("cached version must not execute CLI"))
        assert _real_cli_version() == (0, 159, 1)
    _version_binary(tmp_path, "print('codex-cli 0.158.0')\n")
    assert _real_cli_version() == (0, 158, 0)
    monkeypatch.setattr(models.time, "monotonic", lambda: 1e20)
    real_popen = models.subprocess.Popen
    calls = []
    def tracked_popen(*args, **kwargs):
        calls.append(args[0])
        return real_popen(*args, **kwargs)
    monkeypatch.setattr(models.subprocess, "Popen", tracked_popen)
    assert _real_cli_version() == (0, 158, 0)
    assert calls == [[str(binary), "--version"]]  # expired memo probes again


@pytest.mark.parametrize("body", ["print('codex-cli dev-build')", "raise SystemExit(2)",
                                  "import time; time.sleep(30)"])
def test_failed_version_probe_is_bounded_cached_and_uses_catalog(monkeypatch, tmp_path, body):
    binary = _version_binary(tmp_path, body + "\n")
    _configure_version_binary(monkeypatch, binary)
    monkeypatch.setattr(models, "CLI_VERSION_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(models, "cli_version", _real_cli_version)
    models._VERSION_CACHE.clear()
    assert _real_cli_version() is None
    with monkeypatch.context() as m:
        m.setattr(models.subprocess, "Popen", lambda *a, **kw: pytest.fail("failed probes must be cached"))
        cache()
        assert models.resolve_catalog().default_model == models.DEFAULT_MODEL
        cache([row("gpt-6-sol")])
        assert models.resolve_catalog().default_model == models.FALLBACK_MODEL


def test_discovery_does_not_probe_cli(monkeypatch):
    cache()
    monkeypatch.setattr(models.subprocess, "Popen", lambda *a, **kw: pytest.fail("catalog reading must not execute CLI"))
    assert models.DEFAULT_MODEL in models.discover_models()
