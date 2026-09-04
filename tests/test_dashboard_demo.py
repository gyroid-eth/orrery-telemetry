from __future__ import annotations

import os
import pytest

import json
import socket
import stat
import subprocess
import sys
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "dashboard-demo.py"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.skipif(
    os.environ.get("GITHUB_ACTIONS") == "true",
    reason="starts a real dashboard/service process; fails only on GitHub-hosted runners (no interactive user session), cause not isolated yet — run 33846626836",
)
def test_dashboard_demo_up_verify_down(tmp_path: Path):
    demo = tmp_path / "dashboard-demo"
    port = _free_port()
    up = subprocess.run(
        [sys.executable, str(SCRIPT), "up", "--install-dir", str(demo), "--port", str(port)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    try:
        result = json.loads(up.stdout)
        assert result["ok"] is True
        assert stat.S_IMODE((demo / ".agentstack-dashboard-demo.json").stat().st_mode) == 0o600
        assert len(result["agents"]) == 13
        assert result["state_counts"] == {
            "agent": 7,
            "finished": 2,
            "retired": 2,
            "gone": 2,
        }
        assert result["message_deliveries"] == 48
        assert result["communication_edges"] >= 20
        assert result["max_edge_messages"] >= 4
        assert result["child_exchange_messages"] == 4
        assert result["spawn_picker_scientists"] >= 4
        assert result["replay_events"] >= 48
        assert result["recent_comet_messages"] >= 2
        assert result["agents"]["Bright-Curie"]["category"] == "agent"
        assert result["agents"]["Bright-Curie"]["ctx_used"] == 24
        assert result["agents"]["Swift-Noether"]["ctx_used"] == 19
        assert result["agents"]["Bold-Hopper"]["ctx_used"] == 38
        assert result["agents"]["Vivid-Feynman"]["ctx_used"] == 72
        assert result["agents"]["Calm-Turing"]["category"] == "finished"
        assert result["agents"]["Gentle-Lamarr"]["category"] == "finished"
        assert result["agents"]["Quiet-Franklin"]["category"] == "retired"
        assert result["agents"]["Soft-Galileo"]["category"] == "retired"
        assert result["agents"]["Lively-Hubble"]["category"] == "gone"
        assert result["agents"]["Clear-Somerville"]["category"] == "gone"
        assert result["running_context_used"] == [9, 19, 24, 38, 47, 57, 72]
        assert len(result["groups"]) == 4
        assert {"anthropic", "openai"}.issubset(result["providers"])
        assert len(result["models"]) >= 5
        assert len(result["spawn"]) == 12
        assert result["max_spawn_depth"] == 4

        recreated = subprocess.run(
            [sys.executable, str(SCRIPT), "up", "--install-dir", str(demo), "--port", str(port)],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        recreated_result = json.loads(recreated.stdout)
        assert recreated_result["message_deliveries"] == result["message_deliveries"]
        assert recreated_result["spawn"] == result["spawn"]

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/graph?all=1", timeout=3) as response:
            graph = json.loads(response.read())
        assert {node["name"] for node in graph["nodes"]} == {
            "Bright-Curie", "Swift-Noether", "Calm-Turing", "Quiet-Franklin",
            "Bold-Hopper", "Warm-Lovelace", "Keen-Faraday", "Gentle-Lamarr",
            "Vivid-Feynman", "Lively-Hubble", "Steady-Bose", "Soft-Galileo",
            "Clear-Somerville",
        }
        assert all(node["annot"]["group"] for node in graph["nodes"])
        assert len({node["annot"]["group"] for node in graph["nodes"]}) == 4

        verify = subprocess.run(
            [sys.executable, str(SCRIPT), "verify", "--install-dir", str(demo), "--port", str(port)],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        assert json.loads(verify.stdout)["ok"] is True

        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/spawn",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
            assert b"read-only documentation demo" in exc.read()
        else:
            raise AssertionError("demo mutation endpoint was not blocked")
    finally:
        subprocess.run(
            [sys.executable, str(SCRIPT), "down", "--install-dir", str(demo)],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    assert not demo.exists()


def test_dashboard_demo_refuses_live_port_and_unowned_directory(tmp_path: Path):
    demo = tmp_path / "not-owned"
    demo.mkdir()
    sentinel = demo / "keep-me.txt"
    sentinel.write_text("user data", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "up", "--install-dir", str(demo), "--port", "8770"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "reserved for the live dashboard" in result.stderr
    assert sentinel.read_text(encoding="utf-8") == "user data"

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "up", "--install-dir", str(demo), "--port", str(_free_port())],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "refusing to overwrite non-demo directory" in result.stderr
    assert sentinel.read_text(encoding="utf-8") == "user data"
