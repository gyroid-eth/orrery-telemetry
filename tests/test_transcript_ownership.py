#!/usr/bin/env python3
"""One transcript belongs to one agent.

A tester opened the parent card and the child card and got the same history in
both — same file, same `66/66` footer, and the text was the parent's ("started
watching for the child's reply"). The resolver scores each name against every
transcript independently, so a parent that spawns and monitors a child mentions
that child constantly, and its large transcript can outscore the child's small
one. Nothing compared the two answers, so nobody noticed they were identical.

Showing somebody else's history is worse than showing none: it reads as a real
record of what that agent did. So the weaker claim yields.

Runnable two ways:
    python3 tests/test_transcript_ownership.py
    pytest tests/test_transcript_ownership.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER = ROOT / "dashboard" / "server.py"

PARENT = "Zesty-Einstein"
CHILD = "CoralLantern"


def _load_server():
    sys.path.insert(0, str(ROOT / "dashboard"))
    try:
        spec = importlib.util.spec_from_file_location("srv_ownership", SERVER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(ROOT / "dashboard"))


def _write_parent_transcript(path: pathlib.Path) -> None:
    """A parent that delegated: full of its own actions AND the child's name."""
    # The child's name arrives quoted, which is how it really appears: the
    # parent registers it, addresses mail to it, and polls for its reply.
    lines = [json.dumps({"sender_name": PARENT, "name": CHILD, "tool": "register_agent"})]
    for i in range(60):
        lines.append(json.dumps({
            "sender_name": PARENT,
            "to": [CHILD],
            "text": f"waiting for a reply ({i})",
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_child_transcript(path: pathlib.Path) -> None:
    """A child that has barely started: one registration and one inbox read."""
    lines = [
        json.dumps({"agent_name": CHILD, "text": "registered"}),
        json.dumps({"agent_name": CHILD, "text": "read inbox"}),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_the_parent_transcript_outscores_the_child_for_the_child_name():
    """The premise: without arbitration the confusion is real, not imagined."""
    server = _load_server()
    with tempfile.TemporaryDirectory() as directory:
        base = pathlib.Path(directory)
        parent_file, child_file = base / "parent.jsonl", base / "child.jsonl"
        _write_parent_transcript(parent_file)
        _write_child_transcript(child_file)
        parent_text = parent_file.read_text(encoding="utf-8")
        child_text = child_file.read_text(encoding="utf-8")
        assert server._ownership_score(parent_text, CHILD) > server._ownership_score(
            child_text, CHILD
        ), "the fixture no longer reproduces the reported confusion"


def test_the_stronger_claim_keeps_the_transcript():
    server = _load_server()
    server._TPATH_OWNER.clear()
    server._TPATH_CACHE.clear()
    assert server._claim_transcript("/t/parent.jsonl", PARENT, 400, exact=False)
    # The child scores lower on the same file, so it does not get it.
    assert not server._claim_transcript("/t/parent.jsonl", CHILD, 61, exact=False)
    assert server._TPATH_OWNER["/t/parent.jsonl"][0] == PARENT


def test_a_stronger_later_claim_evicts_the_weaker_one():
    server = _load_server()
    server._TPATH_OWNER.clear()
    server._TPATH_CACHE.clear()
    server._TPATH_CACHE[CHILD] = (1.0, "/t/parent.jsonl")
    assert server._claim_transcript("/t/parent.jsonl", CHILD, 10, exact=False)
    assert server._claim_transcript("/t/parent.jsonl", PARENT, 400, exact=False)
    assert server._TPATH_OWNER["/t/parent.jsonl"][0] == PARENT
    assert CHILD not in server._TPATH_CACHE, "the loser kept a stale cached answer"


def test_an_exact_session_index_match_is_never_taken_away():
    """The registration-time map is authoritative; heuristics do not overrule it."""
    server = _load_server()
    server._TPATH_OWNER.clear()
    server._TPATH_CACHE.clear()
    assert server._claim_transcript("/t/child.jsonl", CHILD, 1 << 30, exact=True)
    assert not server._claim_transcript("/t/child.jsonl", PARENT, 1 << 30, exact=False)
    assert server._TPATH_OWNER["/t/child.jsonl"][0] == CHILD


def test_the_same_agent_may_reclaim_its_own_transcript():
    """Re-resolution after a cache expiry must not fight itself."""
    server = _load_server()
    server._TPATH_OWNER.clear()
    assert server._claim_transcript("/t/a.jsonl", PARENT, 100, exact=False)
    assert server._claim_transcript("/t/a.jsonl", PARENT, 5, exact=False)


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


def test_a_legacy_index_record_is_not_exact_authority(tmp_path, monkeypatch):
    """An upgraded machine still has records written before the schema existed.

    Those were produced by a writer that recorded a registration whoever made
    it, so one can name a parent's transcript under a child's id. The exact
    path has to decline them and let the heuristic decide, rather than point at
    the wrong session with certainty.
    """
    import dashboard.server as server

    index_dir = tmp_path / "session_index"
    index_dir.mkdir()
    transcript = tmp_path / "legacy-parent.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    (index_dir / "77.json").write_text(
        json.dumps(
            {
                "agent_id": 77,
                "agent_name": "LegacyChild",
                "session_id": "parent-session",
                "transcript_path": str(transcript),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "SESSION_INDEX_DIR", str(index_dir))
    monkeypatch.setattr(server, "_agent_id_for_name", lambda name: 77)
    assert server._indexed_transcript("LegacyChild") is None

    # The null case: a record that shows what it is still resolves.
    (index_dir / "77.json").write_text(
        json.dumps(
            {
                "agent_id": 77,
                "agent_name": "LegacyChild",
                "session_id": "own-session",
                "transcript_path": str(transcript),
                "registered_by": "",
                "schema_version": 2,
                "binding_kind": "self",
            }
        ),
        encoding="utf-8",
    )
    assert server._indexed_transcript("LegacyChild") == str(transcript)
