"""Portable, isolated tests of Codex local discovery and launch policy."""
from __future__ import annotations

from datetime import datetime, timezone
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
    assert provider["default_model"] == "gpt-5.6-sol"
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


def test_symlink_is_not_followed(tmp_path):
    target = tmp_path / "other.json"
    target.write_text("not a catalog")
    path = Path(os.environ["CODEX_HOME"]) / "models_cache.json"
    path.parent.mkdir()
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("symlink privilege unavailable")
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


@pytest.mark.parametrize("override", [",,,", "claude-opus-5", "gpt-ok,bad", "gpt-x;echo", "x"*(models.MAX_MODELS*129+1)])
def test_invalid_allowlist_is_visible_error_not_silent_fallback(monkeypatch, override):
    cache([row("gpt-cache")])
    monkeypatch.setenv("AGENTSTACK_CODEX_MODELS", override)
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
    assert models.normalize_model() == "gpt-5.6-sol"
    assert models.normalize_model("sol") == "gpt-6-sol"
    assert models.normalize_model(" LUNA ") == "gpt-6-luna"
    assert models.normalize_model("astra") == "gpt-6-astra"
    assert models.normalize_model("terra") == "gpt-5.6-terra"


@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-5.6-luna", "gpt-5.5"])
def test_bundled_effort_restrictions_survive_cache_failure(model):
    with pytest.raises(ValueError, match="does not support ultra"):
        models.resolve_effort(model, "ultra")
    assert models.resolve_effort(model, "xhigh") == "xhigh"
    if model == "gpt-5.5":
        with pytest.raises(ValueError, match="does not support max"):
            models.resolve_effort(model, "max")


def test_fresh_effort_metadata_overrides_bundled_and_uses_valid_default():
    cache([row("gpt-6-sol", ("medium", "high"))])
    assert models.resolve_effort("gpt-6-sol") == "medium"
    with pytest.raises(ValueError, match="does not support xhigh"):
        models.resolve_effort("gpt-6-sol", "xhigh")


def test_unknown_model_and_nonreasoning_model_omit_effort_by_default():
    assert models.resolve_effort("gpt-future") == ""
    assert models.resolve_effort("gpt-future", "high") == "high"
    cache([row("gpt-no-reasoning", ())])
    assert models.resolve_effort("gpt-no-reasoning") == ""
    with pytest.raises(ValueError, match="does not support"):
        models.resolve_effort("gpt-no-reasoning", "high")


def test_cli_entrypoint_matches_python_policy():
    helper = Path(models.__file__)
    result = subprocess.run([sys.executable, str(helper), "normalize", "luna"], capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout.strip() == "gpt-6-luna"
    rejected = subprocess.run([sys.executable, str(helper), "effort", "gpt-6-luna", "ultra"], capture_output=True, text=True)
    assert rejected.returncode == 1 and "does not support ultra" in rejected.stderr


@pytest.mark.parametrize("fraction", ["1", "12", "123", "123456", "123456789"])
def test_rfc3339_fractional_seconds_are_portable(fraction):
    whole = datetime.fromtimestamp(NOW-1, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    cache([row("gpt-nanosecond")], fetched_at=whole+"."+fraction+"Z")
    assert "gpt-nanosecond" in models.discover_models()
