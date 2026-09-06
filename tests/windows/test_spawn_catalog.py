"""Catalog parity, malformed inputs and launch boundary on native Windows."""
import hashlib
import json
import sqlite3

import pytest

import dashboard.server as server
from scripts.windows.spawn_catalog import UNAVAILABLE, load_vocabulary


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.delenv("AGENTSTACK_SCIENTISTS_JSON", raising=False)


def test_canonical_order_matches_existing_frozen_launcher_hashes():
    scientists, adjectives = load_vocabulary(server.SPAWN_SCIENTISTS_SCRIPT)
    assert len(scientists) == 50
    assert len(adjectives) == 134
    # Independently frozen by tests/test_adjective_vocab.py, not new expectations.
    for words, digest in [
        (adjectives, "c7ae2d219d650889108b49047531011afccc93408f187f69415476b39a8c28dc"),
        (scientists, "b9302d93d60596dd71fe0a6868aa2ff25617ea847021a7bd79fab628c9b4bafc"),
    ]:
        assert hashlib.sha256(("\n".join(words) + "\n").encode()).hexdigest() == digest


def source_tree(tmp_path, text="AGS_SIMPLE_ADJECTIVES=(\n Sunny Zesty\n)\n"):
    root = tmp_path / "space 日本語"
    script = root / "bin/lib/agentstack-scientists.sh"
    script.parent.mkdir(parents=True)
    script.write_bytes(text.replace("\n", "\r\n").encode())
    portraits = root / "dashboard/scientist_portraits.json"
    portraits.parent.mkdir()
    portraits.write_text(json.dumps({"Zulu": {}, "Curie": {}, "Émile": {}, "A-1": {}}), encoding="utf-8")
    return script, portraits


def test_crlf_unicode_paths_default_json_and_override(tmp_path, monkeypatch):
    script, _ = source_tree(tmp_path)
    assert load_vocabulary(str(script)) == (["Curie", "Zulu"], ["Sunny", "Zesty"])
    alternate = tmp_path / "alternate 日本語.json"
    alternate.write_text('{"Newton": {}, "Bohr": {}}', encoding="utf-8")
    monkeypatch.setenv("AGENTSTACK_SCIENTISTS_JSON", str(alternate))
    assert load_vocabulary(str(script))[0] == ["Bohr", "Newton"]


@pytest.mark.parametrize("body", [
    "", "AGS_SIMPLE_ADJECTIVES=(\n)\n",
    'AGS_SIMPLE_ADJECTIVES=(\n "Sunny"\n)\n',
    "AGS_SIMPLE_ADJECTIVES=(\n $(echo Sunny)\n)\n",
    "AGS_SIMPLE_ADJECTIVES=(\n Sunny Sunny\n)\n",
    "AGS_SIMPLE_ADJECTIVES=(\n Sunny\n)\nAGS_SIMPLE_ADJECTIVES+=(Other)\n",
])
def test_unsupported_array_fails_closed(tmp_path, body):
    script, _ = source_tree(tmp_path, body)
    with pytest.raises(ValueError, match="vocabulary unavailable"):
        load_vocabulary(str(script))


@pytest.mark.parametrize("contents", ["{broken", "[]", "{}", '{"A-1": {}}'])
def test_bad_json_fails_closed(tmp_path, contents):
    script, portraits = source_tree(tmp_path)
    portraits.write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError, match="vocabulary unavailable"):
        load_vocabulary(str(script))


def test_missing_override_does_not_fall_back(tmp_path, monkeypatch):
    script, _ = source_tree(tmp_path)
    monkeypatch.setenv("AGENTSTACK_SCIENTISTS_JSON", str(tmp_path / "absent.json"))
    with pytest.raises(ValueError):
        load_vocabulary(str(script))


def test_catalog_and_suggestions_use_same_vocabulary_without_launch(tmp_path, monkeypatch):
    script, _ = source_tree(tmp_path)
    monkeypatch.setattr(server, "SPAWN_SCIENTISTS_SCRIPT", str(script))
    database = tmp_path / "roster.sqlite3"
    with sqlite3.connect(database) as con:
        con.execute("CREATE TABLE agents (name TEXT)")
        con.executemany("INSERT INTO agents VALUES (?)", [("Sunny-Curie",), ("Zesty-Curie",)])
    monkeypatch.setattr(server, "DB_PATH", str(database))
    def forbidden(*args, **kwargs):
        raise AssertionError("must not execute subprocess or register agents")
    monkeypatch.setattr(server.subprocess, "run", forbidden)
    monkeypatch.setattr(server, "_mcp_jsonrpc", forbidden)
    catalog = server.spawn_names_payload()
    assert catalog["unavailable"] == UNAVAILABLE
    assert [(item["name"], item["status"]) for item in catalog["names"]] == [
        ("Curie", "occupied"), ("Zulu", "available")]
    assert server._spawn_name_vocabulary() == (["Curie", "Zulu"], catalog["adjectives"])
    assert server.suggest_spawn_name("Curie") is None
    assert server.suggest_spawn_name("Zulu") in ["Sunny-Zulu", "Zesty-Zulu"]
    assert server.do_spawn({"task": "do not run", "standalone": True}) == {"ok": False, "error": UNAVAILABLE}
    script.unlink()
    assert server._spawn_name_vocabulary() == ([], [])
    with pytest.raises(ValueError):
        server.spawn_names_payload()
