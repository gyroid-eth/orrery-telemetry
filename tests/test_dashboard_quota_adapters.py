from __future__ import annotations

import io
import json
import os
from queue import Queue
import subprocess
import sys
import threading
import time

import pytest

from dashboard import claude_quota_observe
from dashboard.quotas import claude as claude_quota
from dashboard.quotas import codex as codex_quota
from dashboard.quotas.antigravity import AntigravityQuotaProvider
from dashboard.quotas.base import QuotaBucket, QuotaSnapshot
from dashboard.quotas.codex import CodexQuotaProvider, _read_response
from dashboard.quotas.service import QuotaService


def _ok(provider: str) -> QuotaSnapshot:
    return QuotaSnapshot(
        provider=provider,
        source=f"{provider}-fixture",
        observed_at=1000,
        status="ok",
        buckets=(
            QuotaBucket.from_remaining(
                id="five-hour",
                label="5h",
                scope="account",
                remaining_percent=75,
                window_seconds=18000,
                resets_at=1200,
            ),
        ),
    )


def test_antigravity_opt_in_gate_never_invokes_cli():
    calls: list[object] = []

    def runner(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("runner must not be called before explicit opt-in")

    provider = AntigravityQuotaProvider(command="/fake/agy", enabled=False, runner=runner)
    snapshot = provider.read()

    assert snapshot.status == "unavailable"
    assert snapshot.reason == "telemetry_opt_in_required"
    assert calls == []


def test_antigravity_marker_enables_read_only_usage_poll(tmp_path):
    calls: list[list[str]] = []
    marker = tmp_path / "antigravity-quota.enabled"

    def runner(argv, **kwargs):
        calls.append(list(argv))
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(argv, 0, "Antigravity CLI 1.1.24\n", "")
        payload = {
            "response": {
                "groups": [
                    {
                        "displayName": "Gemini Models",
                        "buckets": [
                            {
                                "bucketId": "five-hour",
                                "displayName": "5h",
                                "remainingFraction": 0.5,
                            }
                        ],
                    }
                ]
            }
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    provider = AntigravityQuotaProvider(
        command="/fake/agy",
        opt_in_file=marker,
        runner=runner,
    )
    assert provider.read().reason == "telemetry_opt_in_required"
    assert calls == []

    marker.touch()
    snapshot = provider.read()
    assert snapshot.status == "ok"
    assert snapshot.buckets[0].remaining_percent == 50.0
    assert calls == [
        [provider.command, "--version"],
        [provider.command, "-p", "/usage", "--output-format", "json"],
    ]


def test_codex_adapter_accepts_injected_transport_without_spawning_process():
    calls: list[tuple[str, float]] = []

    def reader(command: str, *, timeout: float):
        calls.append((command, timeout))
        return {
            "rateLimits": {
                "limitId": "codex",
                "primary": {
                    "usedPercent": 20,
                    "windowDurationMins": 300,
                    "resetsAt": 2000,
                },
            }
        }

    provider = CodexQuotaProvider(command="codex-fixture", timeout=4.25, reader=reader)
    snapshot = provider.read()

    assert calls == [("codex-fixture", 4.25)]
    assert snapshot.status == "ok"
    assert snapshot.buckets[0].label == "5h"
    assert snapshot.buckets[0].remaining_percent == 80.0


def test_codex_transport_matches_documented_headerless_jsonl(monkeypatch):
    class RecordingStdin:
        def __init__(self) -> None:
            self.chunks: list[str] = []
            self.closed = False

        def write(self, value: str) -> int:
            self.chunks.append(value)
            return len(value)

        def flush(self) -> None:
            pass

        def close(self) -> None:
            self.closed = True

    class FakeProcess:
        def __init__(self) -> None:
            self.stdin = RecordingStdin()
            self.stdout = io.StringIO(
                '{"id":1,"result":{"userAgent":"test"}}\n'
                '{"id":2,"result":{"rateLimits":{"limitId":"codex","primary":'
                '{"usedPercent":20,"windowDurationMins":300,"resetsAt":2000}}}}\n'
            )
            self.returncode: int | None = None

        def poll(self):
            return self.returncode

        def terminate(self) -> None:
            self.returncode = 0

        def wait(self, timeout=None) -> int:
            self.returncode = 0
            return 0

        def kill(self) -> None:
            self.returncode = -9

    process = FakeProcess()
    monkeypatch.setattr(codex_quota.subprocess, "Popen", lambda *args, **kwargs: process)

    result = codex_quota._read_app_server_rate_limits("codex-fixture", timeout=1)
    wire = [json.loads(line) for line in "".join(process.stdin.chunks).splitlines()]

    assert result["rateLimits"]["primary"]["windowDurationMins"] == 300
    assert [message["method"] for message in wire] == [
        "initialize",
        "initialized",
        "account/rateLimits/read",
    ]
    assert all("jsonrpc" not in message for message in wire)


def test_codex_response_reader_ignores_notifications():
    responses: Queue[str | None] = Queue()
    responses.put('{"method":"account/rateLimits/updated"}\n')
    responses.put('{"id":2,"result":{"rateLimits":{}}}\n')

    assert _read_response(responses, 2, time.monotonic() + 1) == {"rateLimits": {}}


def test_claude_observer_concurrent_writes_are_atomic(tmp_path, monkeypatch):
    target = tmp_path / "claude-quota.json"
    monkeypatch.setenv("AGENTSTACK_CLAUDE_QUOTA_SNAPSHOT", str(target))
    errors: list[BaseException] = []

    def writer(used: int) -> None:
        try:
            claude_quota_observe._write_snapshot(
                {"five_hour": {"used_percentage": used, "resets_at": 2000}}
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(used,)) for used in range(10, 60, 10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert errors == []
    assert all(not thread.is_alive() for thread in threads)
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["rate_limits"]["five_hour"]["used_percentage"] in {10, 20, 30, 40, 50}
    assert list(tmp_path.glob(".claude-quota.json.*.tmp")) == []
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o600


def test_claude_observer_uses_the_stable_model_id_when_it_is_available():
    snapshot = claude_quota.parse_claude_statusline(
        {"rate_limits": {"model_scoped": [{
            "model_id": "claude-fable-5-1",
            "display_name": "Fable",
            "utilization": 0.67,
        }]}},
        observed_at=1000,
    )

    assert snapshot.buckets[0].id == "model-claude-fable-5-1"
    assert snapshot.buckets[0].label == "Fable"


class _BlockingProvider:
    provider_name = "codex"
    source_name = "fixture"
    ttl_seconds = 1

    def __init__(self, entered: threading.Event, release: threading.Event):
        self.entered = entered
        self.release = release
        self.calls = 0

    def read(self) -> QuotaSnapshot:
        self.calls += 1
        self.entered.set()
        if not self.release.wait(timeout=3):
            raise RuntimeError("test release timeout")
        return _ok(self.provider_name)


def test_concurrent_poll_does_not_wait_or_launch_duplicate_provider():
    entered = threading.Event()
    release = threading.Event()
    provider = _BlockingProvider(entered, release)
    service = QuotaService([provider], clock=lambda: 1000.0)
    worker = threading.Thread(target=service.read_all, daemon=True)
    worker.start()
    try:
        assert entered.wait(timeout=1)
        result = service.read_all()["providers"][0]
        assert result["status"] == "unavailable"
        assert result["reason"] == "refresh_in_progress"
        assert provider.calls == 1
        assert worker.is_alive()
    finally:
        release.set()
        worker.join(timeout=4)
    assert not worker.is_alive()


@pytest.mark.parametrize("code,stdout,stderr", [
    (1, "1.2.1", "failed"),
    (0, "", "requires version 1.2.1"),
    (0, "Gemini CLI 2.0.0", ""),
    (0, "1.1.11-beta.1", ""),
    (0, "1.1.23", ""),
])
def test_antigravity_version_gate_rejects_unproven_safe_support(code, stdout, stderr):
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, code, stdout, stderr)

    result = AntigravityQuotaProvider(command="fixture", enabled=True, runner=runner).read()
    assert result.status == "unavailable"
    assert len(calls) == 1
    assert calls[0][0][-1] == "--version"
    assert calls[0][1]["stdin"] == subprocess.DEVNULL
    assert calls[0][1]["encoding"] == "utf-8"


def test_codex_silent_real_subprocess_times_out_and_is_reaped(monkeypatch):
    native_popen = subprocess.Popen
    children = []

    def popen(argv, **kwargs):
        child = native_popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            **kwargs,
        )
        children.append(child)
        return child

    monkeypatch.setattr(codex_quota.subprocess, "Popen", popen)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        codex_quota._read_app_server_rate_limits("fake-codex", timeout=0.2)
    assert time.monotonic() - started < 5
    assert children[0].poll() is not None
    assert children[0].stdin.closed
    assert children[0].stdout.closed


@pytest.mark.parametrize("value", [True, False, 1.5, "300", float("inf"), -1, 0])
def test_codex_invalid_duration_does_not_manufacture_window(value):
    snapshot = codex_quota.parse_codex_rate_limits(
        {"rateLimits": {"primary": {"windowDurationMins": value, "usedPercent": 20}}},
        observed_at=1000,
    )
    assert snapshot.status == "unavailable"
    assert snapshot.buckets == ()


def _observe(argv, payload, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTSTACK_CLAUDE_QUOTA_SNAPSHOT", str(tmp_path / "claude-quota.json"))
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    code = claude_quota_observe.main(argv)
    return code, capsys.readouterr()


def _status_line_script(tmp_path, record_to=None):
    """A stand-in for the operator's own status line, runnable on any platform."""
    script = tmp_path / "mine.py"
    body = "import sys\ndata = sys.stdin.read()\n"
    if record_to is not None:
        body += f"open({str(record_to)!r}, 'w', encoding='utf-8').write(data)\n"
    body += "sys.stdout.write('MY LINE')\n"
    script.write_text(body, encoding="utf-8")
    return [sys.executable, str(script)]


def test_observer_wrapping_a_status_line_passes_the_payload_through(tmp_path, monkeypatch, capsys):
    """--exec must observe only: the operator keeps their own status line."""
    seen = tmp_path / "seen.json"
    payload = json.dumps({"rate_limits": {"five_hour": {"used_percentage": 10, "resets_at": 5}}})

    code, captured = _observe(
        ["--exec", *_status_line_script(tmp_path, seen)], payload, tmp_path, monkeypatch, capsys)

    assert code == 0
    assert captured.out == "MY LINE"
    assert json.loads(seen.read_text(encoding="utf-8")) == json.loads(payload)
    snapshot = json.loads((tmp_path / "claude-quota.json").read_text(encoding="utf-8"))
    assert snapshot["rate_limits"]["five_hour"]["used_percentage"] == 10


def test_observer_keeps_the_status_line_when_the_payload_is_unreadable(tmp_path, monkeypatch, capsys):
    code, captured = _observe(
        ["--exec", *_status_line_script(tmp_path)], "not json", tmp_path, monkeypatch, capsys)

    assert code == 0
    assert captured.out == "MY LINE"
    assert not (tmp_path / "claude-quota.json").exists()


def test_observer_without_exec_still_prints_its_own_line(tmp_path, monkeypatch, capsys):
    payload = json.dumps({"model": {"display_name": "Opus"},
                          "rate_limits": {"five_hour": {"used_percentage": 40, "resets_at": 5}}})

    code, captured = _observe([], payload, tmp_path, monkeypatch, capsys)

    assert code == 0
    assert captured.out.strip() == "[Opus] | 5h 60% left"


def test_observer_carries_a_model_window_a_later_session_did_not_report(tmp_path, monkeypatch, capsys):
    """An Opus session reports no Fable window; the last one must survive."""
    snapshot = tmp_path / "claude-quota.json"
    fable = json.dumps({"rate_limits": {"five_hour": {"used_percentage": 10, "resets_at": 5},
                                        "model_scoped": [{"display_name": "Fable", "utilization": 0.67,
                                                          "resets_at": "2099-01-01T00:00:00Z"}]}})
    opus = json.dumps({"rate_limits": {"five_hour": {"used_percentage": 20, "resets_at": 5}}})

    _observe([], fable, tmp_path, monkeypatch, capsys)
    _observe([], opus, tmp_path, monkeypatch, capsys)

    stored = json.loads(snapshot.read_text(encoding="utf-8"))
    assert stored["rate_limits"]["five_hour"]["used_percentage"] == 20
    carried = stored["rate_limits"]["model_scoped"]
    assert [entry["display_name"] for entry in carried] == ["Fable"]
    assert carried[0]["utilization"] == 0.67
    assert carried[0]["observed_at"] <= stored["observed_at"]

    parsed = claude_quota.parse_claude_statusline(stored, observed_at=stored["observed_at"] + 60)
    window = next(b for b in parsed.buckets if b.id == "model-fable")
    assert round(window.remaining_percent) == 33
    assert window.observed_at == carried[0]["observed_at"]


def test_observer_drops_a_model_window_once_it_has_reset(tmp_path, monkeypatch, capsys):
    expired = json.dumps({"rate_limits": {"model_scoped": [{"display_name": "Fable", "utilization": 0.67,
                                                            "resets_at": "2000-01-01T00:00:00Z"}]}})
    _observe([], expired, tmp_path, monkeypatch, capsys)
    _observe([], json.dumps({"rate_limits": {"five_hour": {"used_percentage": 20, "resets_at": 5}}}),
             tmp_path, monkeypatch, capsys)

    stored = json.loads((tmp_path / "claude-quota.json").read_text(encoding="utf-8"))
    assert "model_scoped" not in stored["rate_limits"]


def test_a_fresh_model_window_is_not_marked_as_carried(tmp_path, monkeypatch, capsys):
    payload = json.dumps({"rate_limits": {"model_scoped": [{"display_name": "Fable", "utilization": 0.1,
                                                            "resets_at": "2099-01-01T00:00:00Z"}]}})
    _observe([], payload, tmp_path, monkeypatch, capsys)
    stored = json.loads((tmp_path / "claude-quota.json").read_text(encoding="utf-8"))

    parsed = claude_quota.parse_claude_statusline(stored, observed_at=stored["observed_at"])
    assert next(b for b in parsed.buckets if b.id == "model-fable").observed_at is None
