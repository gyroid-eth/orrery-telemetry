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


def row(model="gpt-6-sol", efforts=("low", "medium", "high", "xhigh", "max", "ultra"), **extra):
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
    assert provider["default_model"] == "gpt-6-sol"
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


@pytest.mark.parametrize("model", ["gpt-6-sol", "gpt-6-luna", "gpt-6-pro", "gpt-6", "gpt-5.6", "gpt-5.6-sol", "gpt-unknown-new-generation"])
def test_formal_ids_remain_exact_even_when_cache_disappears(model):
    path = cache([row(model)])
    assert models.normalize_model(model) == model
    path.unlink()
    assert models.normalize_model(model) == model


def test_explicit_aliases_are_separate_from_omitted_default():
    assert models.normalize_model() == "gpt-6-sol"
    assert models.normalize_model("sol") == "gpt-6-sol"
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
    assert front == ["gpt-6-sol", "gpt-6-astra", "gpt-5.6-terra", "gpt-6-luna"]
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
