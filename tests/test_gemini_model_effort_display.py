"""Regression coverage for separating Antigravity model display from effort."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import dashboard.provider_server  # noqa: F401 - loads the optional provider runtime


gemini_runtime = sys.modules["_orrery_provider_dashboard_gemini"]


def _run_helper(provider: dict, expression: str):
    script = (
        gemini_runtime._UI_HELPERS
        + "\nconst provider="
        + json.dumps(provider)
        + ";\nprocess.stdout.write(JSON.stringify("
        + expression
        + "));\n"
    )
    result = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _display_models(provider: dict) -> list[dict]:
    return _run_helper(provider, "spawnProviderDisplayModels(provider)")


def _display_model_label(provider: dict, model_id: str) -> str:
    return _run_helper(
        provider,
        f"spawnProviderDisplayModelLabel(provider,{json.dumps(model_id)})",
    )


def test_explicit_effort_provider_displays_model_family_once():
    models = _display_models({
        "capabilities": {"effort_required": True},
        "efforts": ["low", "medium", "high"],
        "models": [
            {"id": "gemini-3.8-flash-high", "label": "gemini-3.8-flash-high"},
            {"id": "gemini-3.8-flash-medium", "label": "gemini-3.8-flash-medium"},
            {"id": "gemini-3.7-flash-high", "label": "gemini-3.7-flash-high"},
        ],
    })
    assert models == [
        {"id": "gemini-3.8-flash-high", "label": "gemini-3.8-flash"},
        {"id": "gemini-3.7-flash-high", "label": "gemini-3.7-flash"},
    ]


def test_engine_note_model_label_does_not_repeat_effort_suffix():
    provider = {
        "capabilities": {"effort_required": True},
        "efforts": ["low", "medium", "high"],
        "models": [],
    }
    assert _display_model_label(provider, "gemini-3.8-flash-high") == "gemini-3.8-flash"
    assert _display_model_label(provider, "gemini-3.8-flash-medium") == "gemini-3.8-flash"


def test_engine_note_uses_effort_free_display_model():
    source = Path(gemini_runtime.__file__).with_name("index.html").read_text(encoding="utf-8")
    patched, error = gemini_runtime.apply_ui_patches(source)
    assert error == ""
    assert (
        "const displayModel=spawnProviderDisplayModelLabel(provider,spmSelectedModel);"
        in patched
    )
    assert "displayModel,spmSelectedEffort" in patched


def test_other_provider_model_display_is_unchanged():
    original = [
        {"id": "gpt-5.6-sol", "label": "GPT 5.6 Sol"},
        {"id": "gpt-6-astra", "label": "GPT 6 Astra"},
    ]
    provider = {
        "capabilities": {"effort_required": False},
        "efforts": ["low", "medium", "high"],
        "models": original,
    }
    assert _display_models(provider) == original
    assert _display_model_label(provider, "gpt-5.6-sol") == "gpt-5.6-sol"
