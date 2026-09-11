"""Codex History must use the exact transcript recorded at registration.

`_transcript_path` (Claude) already asks `_indexed_transcript()` before any
heuristic. `_codex_transcript_path` skipped that step and chose the rollout
whose timestamp sat nearest ORRERY Mail's inception_ts, so with several
rollouts close together the History view showed another session's file (#27).
"""
import os
import tempfile

from dashboard import server


def test_indexed_transcript_wins_over_nearest_rollout(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        exact = os.path.join(tmp, "rollout-exact.jsonl")
        open(exact, "w", encoding="utf-8").close()
        server._TPATH_CACHE.pop(("codex", "IndexedCodex"), None)
        monkeypatch.setattr(server, "_indexed_transcript", lambda name: exact if name == "IndexedCodex" else None)
        # The heuristic path must not even be consulted: no sessions dir.
        monkeypatch.setattr(server, "_CODEX_SESSIONS_DIR", os.path.join(tmp, "absent"))
        assert server._codex_transcript_path("IndexedCodex") == exact
        assert server._TPATH_CACHE[("codex", "IndexedCodex")][1] == exact


def test_without_an_index_record_the_heuristic_still_runs(monkeypatch):
    server._TPATH_CACHE.pop(("codex", "PlainCodex"), None)
    monkeypatch.setattr(server, "_indexed_transcript", lambda _name: None)
    monkeypatch.setattr(server, "_CODEX_SESSIONS_DIR", "/nonexistent/codex/sessions")
    assert server._codex_transcript_path("PlainCodex") is None
