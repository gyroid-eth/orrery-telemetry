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
    assert 30 <= reel.STORY_SECONDS <= 35
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
    assert min(event["at"] for event in cross) == 16.5
    assert all(
        reel.AGENTS[event["sender"]]["group"]
        == reel.AGENTS[event["recipient"]]["group"]
        for event in mail if event["at"] < 16.5
    )
    cross_times = sorted({event["at"] for event in cross})
    assert max(b - a for a, b in zip(cross_times, cross_times[1:])) <= 1.0

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
