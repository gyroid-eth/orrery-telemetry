"""DECK history range: `?days=` on /api/agents and the 4-way selector.

The DECK used to expose a single `show all` switch that silently meant "the
last 30 days", so a search for an agent retired 6 weeks ago came back empty
and read as a search bug. The range is now explicit (live / 7d / 30d / all).
"""
from __future__ import annotations

import re
from pathlib import Path

from dashboard import server

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "dashboard" / "index.html"


def test_parse_history_days_accepts_all_and_positive_numbers():
    assert server._parse_history_days("all") is None
    assert server._parse_history_days("ALL ") is None
    assert server._parse_history_days("7") == 7.0
    assert server._parse_history_days("0.5") == 0.5


def test_parse_history_days_falls_back_to_default_on_garbage():
    default = server.HISTORY_DAYS_DEFAULT
    for raw in ("", None, "abc", "-3", "0", "inf", "nan"):
        assert server._parse_history_days(raw) == default, raw


def test_history_cutoff_binds_days_as_a_parameter():
    sql, params = server._history_cutoff(7.0)
    assert "datetime('now', ?)" in sql
    assert params == ("-7 days",)
    sql, params = server._history_cutoff(None)
    assert sql == "" and params == ()


def test_build_agents_default_stays_thirty_days():
    """Callers that never asked for a window (kill / jump lookups pass None
    explicitly) keep the historical 30-day default."""
    import inspect

    sig = inspect.signature(server.build_agents)
    assert sig.parameters["history_days"].default == 30.0


def test_by_name_lookups_search_the_whole_history():
    src = (ROOT / "dashboard" / "server.py").read_text(encoding="utf-8")
    # jump / kill / exit resolve an agent by name; an agent older than the
    # DECK window must still be found or `all` history could show a card that
    # its own buttons refuse to act on.
    assert src.count("for r in build_agents(None)") == 3
    assert "for r in build_agents():" not in src


def test_deck_markup_offers_the_four_ranges_and_no_show_all_switch():
    html = INDEX.read_text(encoding="utf-8")
    ranges = re.findall(r'<button type="button" data-history="([a-z0-9]+)"', html)
    assert ranges == ["live", "7d", "30d", "all"]
    assert 'id="showAll"' not in html
    # the fetch carries the selected range
    assert "fetch('/api/agents?days='+HISTORY_DAYS[historyRange])" in html
