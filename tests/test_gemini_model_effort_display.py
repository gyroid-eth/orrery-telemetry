"""Regression coverage for separating Antigravity model display from effort."""
from __future__ import annotations

import json
import subprocess
import sys

import dashboard.provider_server  # noqa: F401 - loads the optional provider runtime


gemini_runtime = sys.modules["_orrery_provider_dashboard_gemini"]


def _display_models(provider: dict) -> list[dict]:
    script = (
        gemini_runtime._UI_HELPERS
        + "\nconst provider="
        + json.dumps(provider)
        + ";\nprocess.stdout.write(JSON.stringify(spawnProviderDisplayModels(provider)));\n"
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


def test_other_provider_model_display_is_unchanged():
    original = [
        {"id": "gpt-5.6-sol", "label": "GPT 5.6 Sol"},
        {"id": "gpt-6-astra", "label": "GPT 6 Astra"},
    ]
    assert _display_models({
        "capabilities": {"effort_required": False},
        "efforts": ["low", "medium", "high"],
        "models": original,
    }) == original
