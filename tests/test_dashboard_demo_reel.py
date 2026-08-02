from __future__ import annotations

import importlib.util
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
REEL_PATH = ROOT / "scripts" / "dashboard-demo-reel.py"


def _load_reel():
    spec = importlib.util.spec_from_file_location("dashboard_demo_reel", REEL_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reel_builds_three_two_generation_clusters_before_crossing_them():
    reel = _load_reel()
    assert 45 <= reel.STORY_SECONDS <= 55
    assert {event["type"] for event in reel.TIMELINE} == {
        "agent_add", "spawn", "mail", "state",
    }
    assert [event["at"] for event in reel.TIMELINE] == sorted(
        event["at"] for event in reel.TIMELINE
    )
    assert reel.TIMELINE[-1]["at"] <= reel.STORY_SECONDS

    groups: dict[str, list[str]] = defaultdict(list)
    for name, data in reel.AGENTS.items():
        groups[data["group"]].append(name)
    assert len(groups) == 3
    assert sorted(len(names) for names in groups.values()) == [4, 4, 4]
    for names in groups.values():
        assert sorted(reel.SPAWN_DEPTHS[name] for name in names) == [0, 1, 2, 2]
    assert reel.MAX_SPAWN_DEPTH == 2

    mail = [event for event in reel.TIMELINE if event["type"] == "mail"]
    cross = [
        event for event in mail
        if reel.AGENTS[event["sender"]]["group"]
        != reel.AGENTS[event["recipient"]]["group"]
    ]
    assert reel.INTRA_CLUSTER_MAILS == 15
    assert reel.CROSS_CLUSTER_MAILS == 34
    assert min(event["at"] for event in cross) == 35.0
    assert all(
        reel.AGENTS[event["sender"]]["group"]
        == reel.AGENTS[event["recipient"]]["group"]
        for event in mail if event["at"] < 35.0
    )
    cross_times = sorted({event["at"] for event in cross})
    assert max(b - a for a, b in zip(cross_times, cross_times[1:])) <= 0.5

    mail_times = [event["at"] for event in mail]
    assert max(b - a for a, b in zip(mail_times, mail_times[1:])) <= (
        reel.DEMO_MAIL_TRAVEL_MS / 1000
    )

    pair_counts = Counter(
        tuple(sorted((event["sender"], event["recipient"]))) for event in cross
    )
    assert max(pair_counts.values()) >= 4
    assert len(pair_counts) >= 12


def test_reel_url_forces_all_nodes_and_readable_network_tuning():
    reel = _load_reel()
    url = reel._network_url(8877)
    assert "view=net" in url
    assert "window=all" in url
    assert "lang=en" in url
    assert "tune=NSIZE:11,L:175,KR:6400,GR:.006,KS:.015" in url
    assert reel.DEMO_MAIL_TRAVEL_MS == 2500


def test_reel_server_invalidates_graph_cache_for_sqlite_wal(tmp_path):
    reel = _load_reel()
    (tmp_path / "payload").mkdir()
    wrapper = reel._create_reel_server(tmp_path).read_text(encoding="utf-8")
    assert 'for db_path in (DB, Path(str(DB) + "-wal"))' in wrapper


def test_human_loop_states_lead_caption_by_runtime_cache_budget():
    reel = _load_reel()
    intervention = [
        event for event in reel.TIMELINE
        if event["type"] == "state" and event["act_state"] in {"ask", "question"}
    ]

    assert {event["agent"] for event in intervention} == {
        "Bright-Curie", "Swift-Noether",
    }
    assert {event["at"] for event in intervention} == {reel.HUMAN_LOOP_STATE_AT}
    assert reel.HUMAN_LOOP_STATE_LEAD_SECONDS >= 5.5
    assert reel.HUMAN_LOOP_STATE_AT == (
        reel.HUMAN_LOOP_BEAT_AT - reel.HUMAN_LOOP_STATE_LEAD_SECONDS
    )
    assert reel.HUMAN_LOOP_RESOLVE_AT - reel.HUMAN_LOOP_STATE_AT >= 13.0
    assert reel.STORY_SECONDS - reel.HUMAN_LOOP_RESOLVE_AT >= 2.0


def test_reel_phase_beats_have_settle_time_without_retiming_captions():
    reel = _load_reel()
    phase_times = [at for _, at in reel.PHASE_TIMES]
    assert phase_times == sorted(phase_times)
    assert min(b - a for a, b in zip(phase_times, phase_times[1:])) >= 2.0
    assert list(reel.CAPTIONS) == [
        {"at": 0.0, "text": "ONE TERMINAL"},
        {"at": 2.5, "text": "THREE PROJECTS"},
        {"at": 5.0, "text": "AGENTS MULTIPLY"},
        {"at": 8.0, "text": "A SECOND GENERATION"},
        {"at": 11.0, "text": "THREE CLUSTERS THINK"},
        {"at": 16.5, "text": "SIGNALS CROSS BORDERS"},
        {"at": 22.0, "text": "ONE COORDINATION MESH"},
        {"at": 25.0, "text": "HUMAN IN THE LOOP"},
        {"at": 31.0, "text": "ONE CLICK · SYSTEM MOVES"},
    ]
