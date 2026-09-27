"""Execute the real NEW AGENT JavaScript against isolated catalog fixtures."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from dashboard import codex_models
from test_gemini_new_agent import _extract_ui_script, _UI_HARNESS

ROOT = Path(__file__).resolve().parents[1]


def run_ui(provider, scenario):
    source = (ROOT / "dashboard/index.html").read_text(encoding="utf-8")
    script = ("const CATALOG="+json.dumps({"providers": [provider]})+";\n"
              + _extract_ui_script(source)+"\n"
              + _UI_HARNESS.split("resetSpawnProviderCapabilities();", 1)[0]
              + "resetSpawnProviderCapabilities(); renderSpawnProviders(normalizeSpawnProviders(CATALOG));\n"
              + scenario+"\nprocess.stdout.write(JSON.stringify(out));")
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture
def provider(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.delenv("AGENTSTACK_CODEX_MODELS", raising=False)
    return codex_models.provider_catalog()


def test_model_switch_removes_unsupported_effort_and_preserves_id(provider):
    result = run_ui(provider, """
selectSpawnModel('gpt-6-sol');selectSpawnEffort('ultra');out.sol=state();
selectSpawnModel('gpt-6-luna');out.luna=state();
out.lunaEfforts=SPM('spm-efforts').children.map(x=>x.dataset.effort);
""")
    assert result["sol"]["payload"]["model"] == "gpt-6-sol"
    assert result["sol"]["payload"]["effort"] == "ultra"
    assert result["luna"]["payload"]["model"] == "gpt-6-luna"
    assert result["luna"]["payload"]["effort"] == "xhigh"
    assert "ultra" not in result["lunaEfforts"]


def test_default_is_not_the_first_cache_candidate(provider):
    provider["models"] = list(reversed(provider["models"]))
    assert provider["models"][0] != codex_models.DEFAULT_MODEL
    result = run_ui(provider, "out.initial=state();")
    assert result["initial"]["payload"]["model"] == codex_models.DEFAULT_MODEL


def test_excluded_default_requires_explicit_selection(provider):
    provider["models"] = ["gpt-6-sol", "gpt-6-luna"]
    result = run_ui(provider, "out.initial=state();selectSpawnModel('gpt-6-luna');out.chosen=state();")
    assert result["initial"]["payload"]["model"] == ""
    assert result["initial"]["launchDisabled"]
    assert result["initial"]["status"] == "select a model for this provider"
    assert result["chosen"]["payload"]["model"] == "gpt-6-luna"
    assert not result["chosen"]["launchDisabled"]


def test_invalid_allowlist_keeps_provider_visible_and_blocks_launch(provider):
    provider["models"] = []
    provider["model_error"] = "AGENTSTACK_CODEX_MODELS contains invalid model IDs"
    result = run_ui(provider, "out.state=state();out.providers=spmProviders.map(x=>x.id);out.error=SPM('spm-models').innerHTML;")
    assert result["providers"] == ["codex"]
    assert result["state"]["launchDisabled"]
    assert provider["model_error"] in result["error"]


def test_unknown_metadata_omits_effort_from_payload(provider):
    provider["models"].append("gpt-future")
    provider["model_efforts"]["gpt-future"] = []
    provider["model_effort_defaults"]["gpt-future"] = ""
    result = run_ui(provider, "selectSpawnModel('gpt-future');out.state=state();out.hidden=SPM('spm-effort-row').hidden;")
    assert result["state"]["payload"]["model"] == "gpt-future"
    assert "effort" not in result["state"]["payload"]
    assert result["hidden"] and not result["state"]["launchDisabled"]


def test_supported_effort_survives_model_change(provider):
    result = run_ui(provider, "selectSpawnModel('gpt-6-sol');selectSpawnEffort('high');selectSpawnModel('gpt-6-luna');out.state=state();out.note=SPM('spm-engine-note').textContent;")
    assert result["state"]["payload"]["effort"] == "high"
    assert "gpt-6-luna" in result["note"] and "high" in result["note"]
