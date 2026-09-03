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


def test_overlay_files_are_offered_to_the_browser_as_custom_portraits(tmp_path):
    """The page only requests a face for names in /api/custom-portraits."""
    overlay = tmp_path / "faces"
    overlay.mkdir()
    (overlay / "SeminarBot.png").write_bytes(PNG)
    mapping = tmp_path / "custom.json"
    mapping.write_text(json.dumps({"SeminarBot": "Curie"}), encoding="utf-8")
    srv = _load_server(tmp_path, AGENTSTACK_PORTRAITS_DIR=str(overlay))
    assert srv._custom_portrait_map() == {"seminarbot": "SeminarBot"}
    srv = _load_server(
        tmp_path, AGENTSTACK_PORTRAITS_DIR=str(overlay), AGENTSTACK_CUSTOM_PORTRAITS=str(mapping)
    )
    assert srv._custom_portrait_map() == {"seminarbot": "Curie"}
    assert srv._portrait_file("SeminarBot", False).endswith("Curie.png")


def test_directory_suggestions_return_the_whole_listing_for_prefix_filtering(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    for i in range(30):
        (root / f"{i:02d}_folder").mkdir()
    srv = _load_server(tmp_path)
    # Roots are read from the environment at call time, not at import.
    saved = os.environ.get("AGENTSTACK_SPAWN_ROOTS")
    os.environ["AGENTSTACK_SPAWN_ROOTS"] = str(root)
    try:
        payload = srv.spawn_directory_suggestions(str(root))
    finally:
        if saved is None:
            os.environ.pop("AGENTSTACK_SPAWN_ROOTS", None)
        else:
            os.environ["AGENTSTACK_SPAWN_ROOTS"] = saved
    names = [row["name"] for row in payload["dirs"]]
    assert names[-1] == "29_folder", "a 20-row cap hid the tail of the listing from the prefix filter"
    assert payload["truncated"] is False
