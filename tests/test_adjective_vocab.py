"""The launcher, dashboard payload, and suggestion API share one adjective source."""
from __future__ import annotations

import pathlib
import re
import subprocess
import hashlib

import dashboard.server as server


ROOT = pathlib.Path(__file__).resolve().parent.parent
LIB = ROOT / "bin" / "lib" / "agentstack-scientists.sh"


def _launcher_adjectives() -> list[str]:
    return subprocess.run(
        ["bash", "-c", 'source "$1" && ags_adjective_list', "adjectives", str(LIB)],
        check=True, capture_output=True, text=True,
    ).stdout.splitlines()


def test_expanded_adjective_vocabulary_is_valid_and_single_sourced(monkeypatch):
    adjectives = _launcher_adjectives()
    assert len(adjectives) == 134
    assert len(set(adjectives)) == 134
    assert all(re.fullmatch(r"[A-Z][A-Za-z]{2,7}", adjective) for adjective in adjectives)
    # mcp-agent-mail utils.py SIMPLE_ADJECTIVES, Round 3 (2026-06-26).
    assert hashlib.sha256(("\n".join(adjectives) + "\n").encode()).hexdigest() == (
        "c7ae2d219d650889108b49047531011afccc93408f187f69415476b39a8c28dc"
    )

    monkeypatch.setattr(server, "_spawn_name_status", lambda _: "available")
    payload = server.spawn_names_payload()
    assert payload["adjectives"] == adjectives
    suggestion = server.suggest_spawn_name("Curie")
    assert suggestion and suggestion.endswith("Curie")
    suggested_adjective, separator, suggested_scientist = suggestion.partition("-")
    assert separator == "-"
    assert suggested_adjective in adjectives
    assert suggested_scientist == "Curie"

    picked = subprocess.run(
        ["bash", "-c", 'source "$1" && ags_pick_adjective', "adjectives", str(LIB)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert picked in adjectives


def test_generated_agent_names_use_the_stock_safe_hyphen_separator():
    scientists = subprocess.run(
        ["bash", "-c", 'source "$1" && ags_scientist_list', "scientists", str(LIB)],
        check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    generated = subprocess.run(
        ["bash", "-c", 'source "$1" && ags_pick_adjective_scientist_name', "names", str(LIB)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()

    adjective, separator, scientist = generated.partition("-")
    assert separator == "-"
    assert adjective in _launcher_adjectives()
    assert scientist in scientists


def test_scientist_json_matches_agent_mail_canonical_set():
    scientists = subprocess.run(
        ["bash", "-c", 'source "$1" && ags_scientist_list', "scientists", str(LIB)],
        check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    assert len(scientists) == 50
    assert hashlib.sha256(("\n".join(scientists) + "\n").encode()).hexdigest() == (
        "b9302d93d60596dd71fe0a6868aa2ff25617ea847021a7bd79fab628c9b4bafc"
    )
