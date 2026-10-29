"""Bounded Antigravity discovery and the independent Gemini spawn policy."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest

import dashboard.provider_server as server


gemini = sys.modules["_orrery_provider_dashboard_gemini"]
BASE = SimpleNamespace(_SPAWN_MODELS=(), _codex_models=lambda: ())
FALLBACK = ["gemini-3.8-flash-high", "gemini-3.8-flash-medium", "gemini-3.8-flash-low"]


@pytest.fixture(autouse=True)
def _isolated_cache(monkeypatch):
    monkeypatch.setattr(gemini, "_CLI_CATALOG_CACHE", None)


def _fake_cli(monkeypatch, tmp_path: Path, body: str) -> Path:
    binary = tmp_path / "agy"
    binary.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("AGENTSTACK_GEMINI_BIN", str(binary))
    monkeypatch.delenv("AGENTSTACK_GEMINI_MODELS", raising=False)
    return binary


def test_cli_adds_argon_without_mixing_other_providers(monkeypatch, tmp_path):
    rows = ("gemini-4-argon-medium\tMedium\n"
            "gemini-4-argon-high\tHigh\n"
            "gemini-4-argon-low\tLow\n"
            "gemini-4-argon-high\tDuplicate\n"
            "claude-opus-5-5-high\tClaude\n"
            "gpt-oss-120b-medium\tGPT\n"
            "gemini-invalid model\tInvalid\n")
    _fake_cli(monkeypatch, tmp_path, f"import sys\nsys.stdout.write({rows!r})")
    models, error = gemini._gemini_models(BASE)
    assert error == ""
    assert models == FALLBACK + ["gemini-4-argon-medium", "gemini-4-argon-high",
                                 "gemini-4-argon-low"]
    assert gemini._catalog_item(models)["default_model"] == FALLBACK[0]
    assert gemini._catalog_item(models)["model_efforts"]["gemini-4-argon-medium"] == [
        "low", "medium", "high"]


def test_single_effort_is_the_only_offered_effort():
    models = FALLBACK + ["gemini-4-argon-high"]
    catalog = gemini._catalog_item(models)
    assert catalog["model_efforts"]["gemini-4-argon-high"] == ["high"]
    assert catalog["model_efforts"]["gemini-3.8-flash-high"] == ["low", "medium", "high"]
    assert gemini._catalog_item(["custom-model"])["model_efforts"]["custom-model"] == [
        "low", "medium", "high"]


def test_cache_coalesces_concurrent_queries_and_override_skips_cli(monkeypatch, tmp_path):
    counter = tmp_path / "calls.txt"
    monkeypatch.setenv("AGY_TEST_COUNTER", str(counter))
    _fake_cli(monkeypatch, tmp_path, "import os,time\n"
              "with open(os.environ['AGY_TEST_COUNTER'], 'a') as f: f.write('call\\n')\n"
              "time.sleep(0.2)\nprint('gemini-4-argon-high\\tArgon')")
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: gemini._gemini_models(BASE)[0], range(6)))
    assert all(models == FALLBACK + ["gemini-4-argon-high"] for models in results)
    assert counter.read_text().splitlines() == ["call"]

    monkeypatch.setenv("AGENTSTACK_GEMINI_MODELS", "custom-model")
    assert gemini._gemini_models(BASE) == (["custom-model"], "")
    assert counter.read_text().splitlines() == ["call"]


def test_failure_recovers_soon_and_keeps_last_success(monkeypatch, tmp_path):
    _fake_cli(monkeypatch, tmp_path, "import os,sys\n"
              "if os.environ.get('AGY_TEST_FAIL') == '1': sys.exit(1)\n"
              "print('gemini-4-argon-high\\tArgon')")
    monkeypatch.setenv("AGY_TEST_FAIL", "1")
    assert gemini._gemini_models(BASE) == (FALLBACK, "")
    assert gemini._CLI_CATALOG_CACHE[2] == ()

    monkeypatch.setenv("AGY_TEST_FAIL", "0")
    key, _, last = gemini._CLI_CATALOG_CACHE
    monkeypatch.setattr(gemini, "_CLI_CATALOG_CACHE", (key, 0, last))
    assert gemini._gemini_models(BASE)[0] == FALLBACK + ["gemini-4-argon-high"]

    monkeypatch.setenv("AGY_TEST_FAIL", "1")
    key, _, last = gemini._CLI_CATALOG_CACHE
    monkeypatch.setattr(gemini, "_CLI_CATALOG_CACHE", (key, 0, last))
    assert gemini._gemini_models(BASE)[0] == FALLBACK + ["gemini-4-argon-high"]


@pytest.mark.parametrize("body", [
    "import sys; sys.exit(1)",
    "print('claude-opus-5-5-high\\tClaude')",
    "print('gemini-4-argon-high invalid-no-tab')",
    "import sys; sys.stdout.buffer.write(b'gemini-4-argon-high\\tArgon\\n'*6000)",
    "import sys; sys.stdout.buffer.write(b'\\xff\\xfe')",
    "for i in range(129): print(f'gemini-test-{i}\\tTest')",
])
def test_cli_invalid_response_falls_back(monkeypatch, tmp_path, body):
    _fake_cli(monkeypatch, tmp_path, body)
    assert gemini._gemini_models(BASE) == (FALLBACK, "")


def test_cli_timeout_is_bounded(monkeypatch, tmp_path):
    _fake_cli(monkeypatch, tmp_path, "import time; time.sleep(3)")
    monkeypatch.setattr(gemini, "_CLI_CATALOG_TIMEOUT", 0.1)
    assert gemini._gemini_models(BASE) == (FALLBACK, "")


def test_spawn_dry_run_does_not_query_cli(monkeypatch, tmp_path):
    def unexpected():
        pytest.fail("Gemini explicit spawn/dry_run started model discovery")
    monkeypatch.setattr(gemini, "_discover_models", unexpected)
    monkeypatch.delenv("AGENTSTACK_GEMINI_MODELS", raising=False)
    result = server.do_spawn({"parent":"Parent", "task":"audit", "dir":str(tmp_path),
                              "provider":"gemini", "model":"gemini-4-argon-high",
                              "effort":"high", "resources":"../invalid", "dry_run":True})
    assert result == {"ok":False, "error":"resource must not contain parent traversal: ../invalid"}


@pytest.mark.parametrize("fresh_mode", [False, True])
@pytest.mark.parametrize("browser_navigation", [False, True])
def test_cross_origin_catalog_request_does_not_trigger_discovery(monkeypatch, fresh_mode, browser_navigation):
    # Non-navigation requests fail at the existing authority guard. A cross-
    # site navigation passes that guard but must fail before model discovery.
    # The latter is important for the fresh runtime's earlier dispatch.
    monkeypatch.setattr(server, "FRESH_MODE", fresh_mode)
    calls: list[str] = []
    monkeypatch.setattr(server, "spawn_names_payload", lambda: calls.append("read") or {})
    http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    worker = threading.Thread(target=http.handle_request, daemon=True)
    worker.start()
    headers = ({"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"}
               if browser_navigation else
               {"Origin": "https://example.invalid", "Sec-Fetch-Site": "cross-site"})
    request = urllib.request.Request(
        f"http://127.0.0.1:{http.server_port}/api/spawn-names", headers=headers,
    )
    try:
        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=5)
        assert raised.value.code == 403
        body = raised.value.read()
        if browser_navigation:
            assert json.loads(body) == {"error":"cross_origin_spawn_names_request"}
        else:
            assert b"Untrusted dashboard authority" in body
    finally:
        worker.join(timeout=6)
        http.server_close()
    assert not worker.is_alive()
    assert calls == []


def test_same_origin_catalog_request_still_works(monkeypatch):
    monkeypatch.setattr(server, "spawn_names_payload", lambda: {"providers":[]})
    http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    worker = threading.Thread(target=http.handle_request, daemon=True)
    worker.start()
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{http.server_port}/api/spawn-names", timeout=5,
        ) as response:
            assert json.loads(response.read()) == {"providers":[]}
    finally:
        worker.join(timeout=6)
        http.server_close()
    assert not worker.is_alive()


@pytest.mark.skipif(os.name != "posix", reason="Requires a separate POSIX process group")
@pytest.mark.parametrize("failure", ["timeout", "overflow"])
def test_aborted_cli_kills_inherited_stdout_child_after_parent_exit(monkeypatch, tmp_path, failure):
    marker = tmp_path / "owned-processes.txt"
    child_code = ("import time; time.sleep(3)" if failure == "timeout" else
                  "import os,time; time.sleep(0.15); os.write(1,b'x'*70000); time.sleep(3)")
    _fake_cli(monkeypatch, tmp_path, "import os,subprocess,sys\n"
              f"child = subprocess.Popen([sys.executable,'-c',{child_code!r}], "
              "stdin=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
              "with open(os.environ['AGY_TEST_OWNED'], 'w') as f:\n"
              "    f.write(f'{os.getpid()} {os.getpgrp()} {child.pid}')")
    monkeypatch.setenv("AGY_TEST_OWNED", str(marker))
    monkeypatch.setattr(gemini, "_CLI_CATALOG_TIMEOUT", 0.35 if failure == "timeout" else 1.5)

    def active(pid):
        result = subprocess.run(["ps", "-p", str(pid), "-o", "stat="],
                                capture_output=True, text=True, timeout=2)
        state = result.stdout.strip()
        return result.returncode == 0 and bool(state) and not state.startswith("Z")

    try:
        started = time.monotonic()
        assert gemini._gemini_models(BASE) == (FALLBACK, "")
        assert time.monotonic() - started < 2.5
        parent_pid, group_id, child_pid = map(int, marker.read_text().split())
        assert parent_pid == group_id != os.getpgrp()
        assert not active(parent_pid)
        for _ in range(15):
            if not active(child_pid):
                break
            time.sleep(0.03)
        assert not active(child_pid), "aborted CLI left an inherited-stdout child running"
    finally:
        # A failed regression must not leave a live fixture process behind.
        if marker.exists():
            _, group_id, child_pid = map(int, marker.read_text().split())
            try:
                if group_id != os.getpgrp() and os.getpgid(child_pid) == group_id:
                    os.killpg(group_id, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.skipif(os.name != "posix", reason="Requires POSIX process-group handling")
def test_successful_catalog_read_does_not_signal_process_group(monkeypatch, tmp_path):
    _fake_cli(monkeypatch, tmp_path, "print('gemini-4-argon-high\\tArgon')")
    def unexpected_group_signal(*_):
        pytest.fail("a completed catalog read must not signal a recycled process group")
    monkeypatch.setattr(gemini.os, "killpg", unexpected_group_signal)
    assert gemini._gemini_models(BASE) == (FALLBACK + ["gemini-4-argon-high"], "")
