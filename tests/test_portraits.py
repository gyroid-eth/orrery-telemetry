"""Portrait resolution: overlay names are matched without regard to case, and
the operator's custom map applies server-side as well as in the browser.

Before this, `ProOpus` registered by the launcher never matched the
`proopus.png` an operator had dropped into the overlay, and the page quietly
showed initials — the same rendering an unknown name gets.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER = ROOT / "dashboard" / "server.py"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _load_server(tmp_path: pathlib.Path, **env: str):
    saved = {k: os.environ.get(k) for k in ("AGENTSTACK_RUNTIME_DIR", *env)}
    os.environ["AGENTSTACK_RUNTIME_DIR"] = str(tmp_path / "runtime")
    os.environ.update(env)
    sys.path.insert(0, str(ROOT / "dashboard"))
    try:
        spec = importlib.util.spec_from_file_location(f"srv_portrait_{tmp_path.name}", SERVER)
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


def test_overlay_portrait_matches_registered_name_case_insensitively(tmp_path):
    overlay = tmp_path / "faces"
    overlay.mkdir()
    (overlay / "proopus.png").write_bytes(PNG)
    srv = _load_server(tmp_path, AGENTSTACK_PORTRAITS_DIR=str(overlay))
    assert srv._portrait_file("ProOpus", False) == str(overlay / "proopus.png")
    assert srv._portrait_file("PROOPUS", True) == str(overlay / "proopus.png")
    assert srv._portrait_file("SeminarBot", False) == ""


def test_bundled_scientists_still_resolve_and_overlay_wins(tmp_path):
    overlay = tmp_path / "faces"
    overlay.mkdir()
    (overlay / "Curie.png").write_bytes(PNG)
    srv = _load_server(tmp_path, AGENTSTACK_PORTRAITS_DIR=str(overlay))
    assert srv._portrait_file("Curie", False) == str(overlay / "Curie.png")
    assert srv._portrait_file("curie", True) == str(overlay / "Curie.png")
    bundled = srv._portrait_file("Bohr", False)
    assert bundled.endswith(os.path.join("portraits_64", "Bohr.png"))


def test_custom_map_applies_on_the_server(tmp_path):
    overlay = tmp_path / "faces"
    overlay.mkdir()
    (overlay / "lab-mascot.png").write_bytes(PNG)
    mapping = tmp_path / "custom.json"
    mapping.write_text(json.dumps({"biomatterbot": "lab-mascot", "windyfermi": "Fermi"}), encoding="utf-8")
    srv = _load_server(
        tmp_path,
        AGENTSTACK_PORTRAITS_DIR=str(overlay),
        AGENTSTACK_CUSTOM_PORTRAITS=str(mapping),
    )
    assert srv._portrait_file("BiomatterBot", False) == str(overlay / "lab-mascot.png")
    assert srv._portrait_file("WindyFermi", False).endswith("Fermi.png")


def test_unsafe_names_never_resolve(tmp_path):
    srv = _load_server(tmp_path)
    for bad in ("../Bohr", "a/b", "", "..\\x"):
        assert srv._portrait_file(bad, False) == ""


def test_embedded_dashboard_hands_jump_and_spawn_to_the_cockpit():
    markup = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
    assert "window.parent.postMessage({type:'orrery-jump',name:name},location.origin);" in markup
    assert "window.parent.postMessage({type:'orrery-spawn'},location.origin);" in markup
    assert "tmOpen.textContent='OPEN IN COCKPIT'" in markup


def test_directory_typeahead_descends_into_a_complete_path():
    """After a chip or an option is chosen the field holds a whole directory.

    The useful next suggestions are its children. Asking for its siblings
    instead fails outright when the path is a spawn root (its parent is
    outside the browsable roots), which left the operator typing every level
    by hand.
    """
    markup = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
    assert "async function resolveSpawnDirRoot()" in markup
    descend = markup.index("if(raw&&raw!=='~'&&raw!=='.'&&!raw.endsWith('/')){")
    siblings = markup.index("const prefix=query.prefix.toLowerCase();")
    assert descend < siblings, "children must be tried before the sibling prefix search"
    assert "if(rows.length){renderSpawnDirOptions(rows,'');return;}" in markup
