"""The bundled /log skill shows its log with show_files and points to it once, only where it works."""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKILL = (ROOT / "skills/log/SKILL.md").read_text(encoding="utf-8")
SECTION = SKILL.split("## Finish: Show The Log", 1)[-1]


def test_the_section_is_the_last_step():
    assert "## Finish: Show The Log" in SKILL
    assert "\n## " not in SECTION


def test_the_line_has_three_conditions_and_is_otherwise_silent():
    assert "only when all of these hold; otherwise say nothing" in SECTION
    assert "`AGENT_NAME` is set" in SECTION
    assert "inside the session's working folder" in SECTION
    assert "stay silent" in SECTION


def test_the_line_appears_once_per_machine():
    marker = "${AGENTSTACK_RUNTIME_DIR:-$HOME/.agentstack/runtime}/hints/log-history-preview"
    assert marker in SECTION
    assert "once per machine" in SECTION
    assert "`touch`" in SECTION


def test_the_language_follows_the_user_not_the_locale():
    assert "in the language the user has been writing in this session, not the locale" in SECTION


def test_the_section_names_no_private_place():
    for word in ("/Users/", "/home/", "8791", "127.0.0.1"):
        assert word not in SECTION
