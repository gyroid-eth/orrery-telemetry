#!/usr/bin/env python3
"""An identity the server did not grant has to be visible, not inferred.

agent-mail sometimes registers an agent under a name other than the one asked
for. The agent then works — it sends, it receives, it appears in the deck — and
the only trace is that the dashboard cannot find a portrait for the name. A
missing face reads as a style choice, so nobody investigates, and meanwhile the
parent is addressing mail to a name that does not exist.

The fix is not to give it a face. That would remove the last signal. It is to
say which name was asked for.

Runnable two ways:
    python3 tests/test_name_substitution_visible.py
    pytest tests/test_name_substitution_visible.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER = ROOT / "dashboard" / "server.py"
REGISTER_LIB = ROOT / "bin" / "lib" / "agentstack-register.sh"
INDEX = ROOT / "dashboard" / "index.html"


def _load_server(runtime_dir: pathlib.Path):
    saved = {k: os.environ.get(k) for k in ("AGENTSTACK_RUNTIME_DIR",)}
    os.environ["AGENTSTACK_RUNTIME_DIR"] = str(runtime_dir)
    sys.path.insert(0, str(ROOT / "dashboard"))
    try:
        spec = importlib.util.spec_from_file_location(
            f"srv_subst_{runtime_dir.name}", SERVER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(ROOT / "dashboard"))
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_no_file_means_no_claims():
    """The null case: an ordinary install must not mark anybody."""
    with tempfile.TemporaryDirectory() as directory:
        server = _load_server(pathlib.Path(directory))
        assert server._name_substitutions() == {}


def test_a_recorded_substitution_is_reported():
    with tempfile.TemporaryDirectory() as directory:
        runtime = pathlib.Path(directory)
        server = _load_server(runtime)
        server._record_name_substitution("GreenLake", "Zesty-Einstein")
        assert server._name_substitutions() == {"GreenLake": "Zesty-Einstein"}


def test_a_name_that_was_granted_is_not_marked():
    """Recording a no-op substitution would mark healthy agents."""
    with tempfile.TemporaryDirectory() as directory:
        runtime = pathlib.Path(directory)
        server = _load_server(runtime)
        server._record_name_substitution("Zesty-Einstein", "Zesty-Einstein")
        assert server._name_substitutions() == {}
        assert not (runtime / "name-substitutions.json").exists()


def test_a_corrupt_store_marks_nobody_rather_than_crashing():
    with tempfile.TemporaryDirectory() as directory:
        runtime = pathlib.Path(directory)
        (runtime / "name-substitutions.json").write_text("{not json", encoding="utf-8")
        server = _load_server(runtime)
        assert server._name_substitutions() == {}


def test_the_record_survives_a_second_spawn():
    with tempfile.TemporaryDirectory() as directory:
        runtime = pathlib.Path(directory)
        server = _load_server(runtime)
        server._record_name_substitution("GreenLake", "Zesty-Einstein")
        server._record_name_substitution("BlueStone", "Airy-Fermi")
        assert server._name_substitutions() == {
            "GreenLake": "Zesty-Einstein",
            "BlueStone": "Airy-Fermi",
        }


def test_the_shell_helper_writes_what_the_server_reads():
    """Two writers, one file. A format drift here is silent on both sides."""
    with tempfile.TemporaryDirectory() as directory:
        runtime = pathlib.Path(directory)
        script = (
            f'source "{REGISTER_LIB}"\n'
            f'ags_record_name_substitution "GreenLake" "Zesty-Einstein"\n'
        )
        result = subprocess.run(
            ["bash", "-c", script],
            env={**os.environ, "AGENTSTACK_RUNTIME_DIR": str(runtime)},
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, result.stderr
        store = runtime / "name-substitutions.json"
        assert store.is_file(), result.stderr
        written = json.loads(store.read_text(encoding="utf-8"))
        assert written["GreenLake"]["requested"] == "Zesty-Einstein"

        server = _load_server(runtime)
        assert server._name_substitutions() == {"GreenLake": "Zesty-Einstein"}


def test_the_shell_helper_records_nothing_when_the_name_was_granted():
    with tempfile.TemporaryDirectory() as directory:
        runtime = pathlib.Path(directory)
        script = (
            f'source "{REGISTER_LIB}"\n'
            f'ags_record_name_substitution "Zesty-Einstein" "Zesty-Einstein"\n'
        )
        result = subprocess.run(
            ["bash", "-c", script],
            env={**os.environ, "AGENTSTACK_RUNTIME_DIR": str(runtime)},
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, result.stderr
        assert not (runtime / "name-substitutions.json").exists()


def test_the_ui_states_the_requested_name_in_both_views():
    """The deck shows initials already; the graph shows only a dot.

    Whichever view somebody is looking at has to carry the fact, so both are
    checked here rather than trusting that one of them will be seen.
    """
    markup = INDEX.read_text(encoding="utf-8")
    assert "a.requested_name" in markup, "the deck card never mentions it"
    assert "g.requested_name" in markup, "the graph node never mentions it"
    assert "nlab-subst" in markup and ".node .nlab-subst" in markup
    assert "subst-note" in markup and ".subst-note{" in markup


def test_no_portrait_is_invented_for_an_unknown_name():
    """Giving every name a face would delete the signal this is built on."""
    markup = INDEX.read_text(encoding="utf-8")
    # avatarKeyOf must stay able to return null: a hash-to-portrait fallback
    # here would make a substituted identity indistinguishable from a healthy one.
    assert "return CUSTOM_PORT[(name||'').toLowerCase()]||scientistOf(name);" in markup


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    raise SystemExit(1 if failures else 0)
