"""HISTORY reads only the end of a transcript (2026-10-16).

A transcript can be hundreds of megabytes, and /api/history read the whole
file on every HISTORY open to show the last 220 events. It now reads backwards
from the end until it has more than `limit` events, so it returns the same
newest events plus an exact `has_more`, but no longer counts the total:
`total` equals `shown`, and `history_window` is "tail". A `limit` of 0 or less
keeps the old whole-file read and its meaning.

The comparison below is against the whole-file reader, on files built to cross
the read blocks in awkward places: long lines, a 1 MB image next to short
text, one line that expands to more events than the limit, multi-byte
characters split across blocks, and an unfinished last line.
"""
from __future__ import annotations

import base64
import json
import time

import pytest

from dashboard import server


def claude_lines() -> list[str]:
    out = []
    for i in range(60):
        out.append({"type": "user", "timestamp": f"t{i}u", "message": {"role": "user", "content": f"質問 {i} — ünïcødé ✓"}})
        out.append({"type": "assistant", "timestamp": f"t{i}a", "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": f"考え {i}"},
            {"type": "text", "text": ("長い返事 " * (40000 if i == 7 else 3)) + str(i)},
            {"type": "tool_use", "name": "Bash", "input": {"command": f"echo {i}"}},
        ]}})
        if i == 20:  # short text and a 1 MB screenshot in the same line
            out.append({"type": "user", "timestamp": "img", "message": {"role": "user", "content": [
                {"type": "tool_result", "content": [
                    {"type": "text", "text": "スクリーンショットの説明"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                 "data": base64.b64encode(b"\x89PNG" * 262144).decode()}}]},
                {"type": "text", "text": "画像の横の短い本文"}]}})
        if i == 40:  # one line, 300 events
            out.append({"type": "assistant", "timestamp": "many", "message": {"role": "assistant", "content": [
                {"type": "text", "text": f"block {k}"} for k in range(300)]}})
        out.append({"type": "system", "timestamp": f"s{i}", "content": "not a message"})
    lines = [json.dumps(o, ensure_ascii=False) for o in out]
    return lines


def codex_lines() -> list[str]:
    out = []
    for i in range(80):
        out.append({"type": "response_item", "timestamp": f"c{i}", "payload": {
            "type": "message", "role": "user" if i % 2 else "assistant",
            "content": [{"type": "input_text" if i % 2 else "output_text", "text": f"メッセージ {i} " + "x" * (i * 97)}]}})
        out.append({"type": "event_msg", "timestamp": f"e{i}", "payload": {"type": "item_completed",
                                                                         "item": {"aggregated_output": "o" * 5000}}})
        if i == 33:
            out.append({"type": "response_item", "timestamp": "img", "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "画像つき"}, {"type": "input_image", "image_url": "data:" + "A" * 1_000_000}]}})
        if i == 50:
            out.append({"type": "response_item", "timestamp": "dev", "payload": {
                "type": "message", "role": "developer", "content": [{"type": "input_text", "text": "hidden"}]}})
    return [json.dumps(o, ensure_ascii=False) for o in out]


def write(tmp_path, name, lines, tail=""):
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n" + tail, encoding="utf-8")
    return str(path)


def full_claude(path):
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            events.extend(server._claude_line_events(line))
    return events


@pytest.fixture(params=[4096, 65536, 1 << 20], ids=["4KiB", "64KiB", "1MiB"])
def block(request, monkeypatch):
    monkeypatch.setattr(server, "_HISTORY_TAIL_BLOCK", request.param)
    return request.param


@pytest.mark.parametrize("limit", [1, 5, 220, 299, 300, 301, 5000])
@pytest.mark.parametrize("kind", ["claude", "codex"])
@pytest.mark.parametrize("unfinished", ["", '{"type": "user", "message": {"role": "user", "content": "half'])
def test_tail_is_the_end_of_the_whole_file(tmp_path, block, limit, kind, unfinished):
    if kind == "claude":
        path = write(tmp_path, "c.jsonl", claude_lines(), unfinished)
        full, line_events = full_claude(path), server._claude_line_events
    else:
        path = write(tmp_path, "x.jsonl", codex_lines(), unfinished)
        full, line_events = server._events_from_codex_jsonl(path), server._codex_line_events
    events, has_more = server._tail_events(path, line_events, limit)
    assert events == full[-limit:]
    assert has_more == (len(full) > limit)


def test_short_text_beside_a_large_image_is_kept(tmp_path, block):
    path = write(tmp_path, "c.jsonl", claude_lines())
    events, _ = server._tail_events(path, server._claude_line_events, 5000)
    texts = [e["text"] for e in events]
    assert "画像の横の短い本文" in texts
    assert any("スクリーンショットの説明" in t for t in texts)


def test_the_line_parsers_match_the_old_whole_file_readers(tmp_path):
    """_events_from_codex_jsonl now uses _codex_line_events; the Claude branch of
    history_payload uses _claude_line_events. Same events as the old loops."""
    path = write(tmp_path, "x.jsonl", codex_lines())
    old = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            o = json.loads(line)
            if o.get("type") != "response_item" or o["payload"].get("type") != "message":
                continue
            role = o["payload"]["role"]
            if role not in ("user", "assistant"):
                continue
            for blk in o["payload"]["content"]:
                if blk["type"] in ("text", "input_text", "output_text") and blk.get("text", "").strip():
                    old.append({"role": role, "kind": "text", "text": blk["text"], "ts": o["timestamp"]})
                elif blk["type"] in ("image", "image_url", "input_image"):
                    old.append({"role": role, "kind": "image", "text": "", "ts": o["timestamp"]})
    assert server._events_from_codex_jsonl(path) == old


@pytest.fixture
def session(tmp_path, monkeypatch):
    path = write(tmp_path, "c.jsonl", claude_lines())
    monkeypatch.setattr(server, "_valid", lambda name: True)
    monkeypatch.setattr(server, "_agent_program", lambda name: "claude-code")
    monkeypatch.setattr(server, "_transcript_path", lambda name: path)
    return path


def test_history_payload_tail_answer(session):
    full = full_claude(session)
    result = server.history_payload("Somebody", 220)
    assert result["ok"] and result["history_window"] == "tail"
    assert result["events"] == full[-220:]
    assert result["shown"] == result["total"] == 220  # an older client never shows a wrong count
    assert result["has_more"] is True


def test_history_payload_without_more(session):
    full = full_claude(session)
    result = server.history_payload("Somebody", len(full) + 10)
    assert result["events"] == full and result["has_more"] is False


@pytest.mark.parametrize("limit", [0, -3])
def test_zero_or_negative_limit_keeps_the_whole_file_read(session, limit):
    full = full_claude(session)
    result = server.history_payload("Somebody", limit)
    expected = full[-limit:] if limit and len(full) > limit else full
    assert result["events"] == expected
    assert result["total"] == len(full)
    assert "history_window" not in result and "has_more" not in result


def test_unreadable_claude_transcript_is_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_valid", lambda name: True)
    monkeypatch.setattr(server, "_agent_program", lambda name: "claude-code")
    monkeypatch.setattr(server, "_transcript_path", lambda name: str(tmp_path / "missing.jsonl"))
    result = server.history_payload("Somebody", 220)
    assert result["ok"] is False and "read" in result["error"]


def test_a_line_longer_than_many_blocks_is_read_once(tmp_path, monkeypatch):
    """A newline-free line of L bytes read in blocks of B must cost about L,
    not L*L/B: the unfinished line is kept as pieces and joined once."""
    monkeypatch.setattr(server, "_HISTORY_TAIL_BLOCK", 4096)
    big = json.dumps({"type": "user", "timestamp": "big", "message": {"role": "user", "content": [
        {"type": "text", "text": "x" * 24_000_000}]}})
    small = json.dumps({"type": "assistant", "timestamp": "small", "message": {"role": "assistant",
                                                                             "content": "after the big line"}})
    path = write(tmp_path, "long.jsonl", [small, big, small])
    decoded = []
    real_events = server._claude_line_events

    def events(line):
        decoded.append(len(line))
        return real_events(line)

    started = time.perf_counter()
    got, has_more = server._tail_events(path, events, 2)
    elapsed = time.perf_counter() - started
    assert [e["ts"] for e in got] == ["big", "small"] and has_more is True
    assert sum(1 for n in decoded if n > 1_000_000) == 1  # the long line is decoded once
    # quadratic re-copying (24 MB in 4 KiB steps) takes minutes; linear takes well under a second
    assert elapsed < 5, elapsed
