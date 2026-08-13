#!/usr/bin/env python3
"""Tier 1: per-call latency of the ORRERY Mail HTTP surface.

The 2026-08-13 numbers (send 90ms / register 60ms) were hand-timed once. This
script is the same measurement as code, so the next regression is caught by
running it rather than by someone remembering what "fast" felt like.

What it does
------------
1. Starts a *scratch* server: an ephemeral port and a throwaway state root.
   It never touches port 8765/8770/7333 or ``~/.agentstack``.
2. Self-tests the success/failure detector before trusting a single timing
   (see "The detector" below).
3. Times ``register_agent`` / ``send_message`` / ``file_reservation_paths``
   over N iterations each, plus the ``tools/list`` payload size.
4. Asserts the calls actually produced state, then appends one row to
   ``bench/results.jsonl`` and gates on the thresholds in THRESHOLDS_MS.

The detector
------------
A successful ``tools/call`` body *contains the substring* ``"isError": false``.
Any check of the form ``'isError' not in body`` therefore reads every success
as an error -- that exact bug was shipped and hit on 2026-08-13. So the body is
parsed as JSON and a call counts as OK only when all three hold:

* the JSON-RPC envelope carries no top-level ``error``,
* ``result.isError`` is present and exactly ``False``,
* a structured payload is extractable.

Two deliberately invalid calls run at startup and must both be classified as
errors; if the detector cannot see a planted failure, the run aborts instead of
reporting timings measured with a blind instrument.

Usage
-----
    PYTHONPATH=<repo>/packages/agentstack_mail/src \
      <venv>/bin/python bench/tier1_latency.py

    bench/tier1_latency.py --iterations 25 --no-gate --no-append
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import mkdtemp
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
BENCH_DIR = REPO_ROOT / "bench"
RESULTS_PATH = BENCH_DIR / "results.jsonl"

# Ceilings, not targets. See bench/README.md for how these were chosen: they
# are set well above the scratch-server measurement so that only a structural
# regression (git back in the request path, a fresh fsync per call, an added
# network round trip) trips them.
THRESHOLDS_MS = {
    "register_agent": {"p50": 500.0, "p95": 900.0},
    "send_message": {"p50": 500.0, "p95": 900.0},
    "file_reservation_paths": {"p50": 500.0, "p95": 900.0},
}
# Softer band: exceeding it is not a build failure, but it is the signature of
# a structural change rather than a noisy laptop. Measured 2026-08-13: putting
# the git commit back in the request path lands every op in here even on a
# brand-new archive (register 217ms, reservation 259ms, send 310ms).
WATCH_MS = {
    "register_agent": {"p50": 120.0, "p95": 200.0},
    "send_message": {"p50": 200.0, "p95": 320.0},
    "file_reservation_paths": {"p50": 150.0, "p95": 250.0},
}
# Gating only means something for a production-shaped server. Quieting the tool
# log roughly halves every number, so a run configured for speed may report but
# may not pass a gate.
PRODUCTION_PROFILE = {
    "AGENTSTACK_MAIL_TOOLS_LOG_ENABLED": "true",
    "AGENTSTACK_MAIL_ARCHIVE_COMMIT_ASYNC": "true",
}
MIN_GATED_ITERATIONS = 20
MAX_GATED_WARMUP = 5
# tools/list is sent to every client on connect; it is context the agent pays
# for on every session. 2026-08-13 production value: 20.4 KB.
TOOLS_LIST_MAX_BYTES = 32 * 1024


class BenchError(RuntimeError):
    """Raised when the benchmark cannot produce a trustworthy number."""


# --------------------------------------------------------------------------
# process plumbing
# --------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(0.1)
        return connection.connect_ex(("127.0.0.1", port)) == 0


def wait_port(port: int, *, present: bool, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_open(port) is present:
            return
        time.sleep(0.05)
    raise BenchError(f"port {port} never reached present={present}")


PROTECTED_PORTS = {8765, 8770, 7333}


def write_env_file(
    path: Path,
    root: Path,
    port: int,
    commit_async: bool,
    tools_log: bool = True,
) -> dict[str, str]:
    """Write the scratch server's env file and return the settings recorded."""
    settings = {
        "AGENTSTACK_MAIL_TOOLS_LOG_ENABLED": "true" if tools_log else "false",
        "AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE": "passthrough",
        "AGENTSTACK_MAIL_HTTP_HOST": "127.0.0.1",
        "AGENTSTACK_MAIL_HTTP_PORT": str(port),
        "AGENTSTACK_MAIL_HTTP_PATH": "/api/",
        "AGENTSTACK_MAIL_DATABASE_URL": f"sqlite+aiosqlite:///{root / 'storage.sqlite3'}",
        "AGENTSTACK_MAIL_STORAGE_ROOT": str(root / "archive"),
        "AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR": str(root / "signals"),
        "AGENTSTACK_MAIL_ARCHIVE_COMMIT_ASYNC": "true" if commit_async else "false",
    }
    path.write_text("\n".join(f"{k}={v}" for k, v in settings.items()) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return settings


def runtime_environment(env_file: Path) -> dict[str, str]:
    """A clean environment: the caller's AGENTSTACK_/MCP_ vars must not leak in."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("AGENTSTACK_MAIL_") and not key.startswith("MCP_AGENT_MAIL_")
    }
    environment["AGENTSTACK_MAIL_ENV_FILE"] = str(env_file)
    environment["PYTHONUNBUFFERED"] = "1"
    return environment


def stop_process(process: subprocess.Popen[str], port: int) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not port_open(port):
            break
        time.sleep(0.05)
    if port_open(port):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


# --------------------------------------------------------------------------
# the detector
# --------------------------------------------------------------------------


def _decode_body(raw: str) -> Any:
    # The endpoint answers either bare JSON or an SSE frame, depending on the
    # Accept negotiation; take the data: line when present.
    for line in raw.splitlines():
        if line.startswith("data:"):
            raw = line[5:].strip()
            break
    return json.loads(raw)


def classify(envelope: Any) -> tuple[bool, Any, str]:
    """Return (ok, payload, reason). Never decide on substrings."""
    if not isinstance(envelope, dict):
        return False, None, "response was not a JSON object"
    if "error" in envelope:
        return False, None, f"jsonrpc error: {envelope['error']}"
    result = envelope.get("result")
    if not isinstance(result, dict):
        return False, None, "envelope carried no result object"
    if "isError" not in result:
        return False, None, "result omitted isError"
    if result["isError"] is not False:
        return False, None, f"isError={result['isError']!r}"
    payload = result.get("structuredContent")
    if payload is None:
        for block in result.get("content", []):
            if block.get("type") == "text":
                try:
                    payload = json.loads(block["text"])
                except json.JSONDecodeError:
                    payload = block["text"]
                break
    if payload is None:
        return False, None, "success carried no payload"
    return True, payload, ""


def rpc(port: int, method: str, params: dict[str, Any]) -> tuple[float, Any, int]:
    """One JSON-RPC POST. Returns (elapsed_ms, decoded envelope, byte length)."""
    body = json.dumps({"jsonrpc": "2.0", "id": method, "method": method, "params": params})
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/",
        data=body.encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:  # 4xx/5xx still carry a body worth reading
        raw = exc.read().decode("utf-8")
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return elapsed_ms, _decode_body(raw), len(raw.encode("utf-8"))


def call_tool(port: int, name: str, arguments: dict[str, Any]) -> tuple[float, bool, Any, str]:
    elapsed_ms, envelope, _size = rpc(
        port, "tools/call", {"name": name, "arguments": arguments}
    )
    ok, payload, reason = classify(envelope)
    return elapsed_ms, ok, payload, reason


def call_ok(port: int, name: str, arguments: dict[str, Any]) -> tuple[float, Any]:
    elapsed_ms, ok, payload, reason = call_tool(port, name, arguments)
    if not ok:
        raise BenchError(f"{name} failed and would have polluted the timings: {reason}")
    return elapsed_ms, payload


def selftest_detector(port: int, project: str) -> list[dict[str, Any]]:
    """Plant failures and a success; the detector must separate them.

    Measuring the instrument in the same command as the measurement: if this
    ever passes everything, the timings below are meaningless.
    """
    checks: list[dict[str, Any]] = []

    _, ok, _payload, reason = call_tool(port, "no_such_tool_at_all", {})
    checks.append({"case": "unknown_tool", "expected": "error", "ok": ok, "reason": reason})

    _, ok2, _payload, reason2 = call_tool(port, "register_agent", {"project_key": project})
    checks.append({"case": "missing_arguments", "expected": "error", "ok": ok2, "reason": reason2})

    _, ok3, payload3, reason3 = call_tool(port, "health_check", {})
    checks.append({"case": "healthy_call", "expected": "ok", "ok": ok3, "reason": reason3})

    if ok or ok2:
        raise BenchError(
            "detector self-test failed: an invalid call was classified as success "
            f"({checks})"
        )
    if not ok3 or not isinstance(payload3, dict) or payload3.get("status") != "ok":
        raise BenchError(f"detector self-test failed: health_check was not OK ({reason3})")
    return checks


# --------------------------------------------------------------------------
# the measurement
# --------------------------------------------------------------------------


def summarize(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        "n": len(ordered),
        "p50": round(statistics.median(ordered), 2),
        "p95": round(ordered[max(0, min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1)))))], 2),
        "min": round(ordered[0], 2),
        "max": round(ordered[-1], 2),
        "mean": round(statistics.fmean(ordered), 2),
    }


def measure(port: int, project: str, iterations: int, warmup: int) -> dict[str, Any]:
    call_ok(port, "ensure_project", {"human_key": project})
    # A fixed pair so send_message always has a live sender and recipient.
    call_ok(
        port,
        "register_agent",
        {
            "project_key": project,
            "program": "bench",
            "model": "bench",
            "name": "BenchSender",
        },
    )
    call_ok(
        port,
        "register_agent",
        {
            "project_key": project,
            "program": "bench",
            "model": "bench",
            "name": "BenchReceiver",
        },
    )

    samples: dict[str, list[float]] = {
        "register_agent": [],
        "send_message": [],
        "file_reservation_paths": [],
    }
    total = warmup + iterations
    for index in range(total):
        recorded = index >= warmup
        tag = f"{'warm' if not recorded else 'run'}{index}"

        elapsed, _ = call_ok(
            port,
            "register_agent",
            {
                "project_key": project,
                "program": "bench",
                "model": "bench",
                "name": f"BenchAgent{index:04d}",
                "task_description": "tier1 latency probe",
            },
        )
        if recorded:
            samples["register_agent"].append(elapsed)

        elapsed, _ = call_ok(
            port,
            "send_message",
            {
                "project_key": project,
                "sender_name": "BenchSender",
                "to": ["BenchReceiver"],
                "subject": f"tier1 probe {tag}",
                "body_md": "path: bench/tier1_latency.py\nprobe body for latency measurement.",
            },
        )
        if recorded:
            samples["send_message"].append(elapsed)

        elapsed, _ = call_ok(
            port,
            "file_reservation_paths",
            {
                "project_key": project,
                "agent_name": "BenchSender",
                "paths": [f"bench/probe/{tag}.md"],
                "ttl_seconds": 600,
                "reason": "tier1 latency probe",
            },
        )
        if recorded:
            samples["file_reservation_paths"].append(elapsed)

    return {name: summarize(values) for name, values in samples.items()}


def measure_tools_list(port: int) -> dict[str, Any]:
    elapsed_ms, envelope, size = rpc(port, "tools/list", {})
    if "error" in envelope:
        raise BenchError(f"tools/list failed: {envelope['error']}")
    tools = envelope.get("result", {}).get("tools", [])
    return {
        "bytes": size,
        "kib": round(size / 1024, 1),
        "tool_count": len(tools),
        "elapsed_ms": round(elapsed_ms, 2),
    }


def assert_work_happened(port: int, project: str, iterations: int, warmup: int, root: Path) -> dict[str, Any]:
    """A fast call that did nothing is not a fast call.

    Without this, the thresholds are satisfiable by a server that accepts every
    request and drops it -- the cheapest way to "pass" a latency gate.
    """
    expected = iterations + warmup
    _, inbox = call_ok(
        port,
        "fetch_inbox",
        {"project_key": project, "agent_name": "BenchReceiver", "limit": expected + 10},
    )
    rows = inbox.get("result", inbox) if isinstance(inbox, dict) else inbox
    delivered = len(rows) if isinstance(rows, list) else 0

    archive = root / "archive"
    message_files = [p for p in archive.rglob("messages/*/*/*.md") if ".git" not in p.parts]
    # Each granted reservation writes <sha1(path)>.json plus an id-<n>.json
    # alias; count the content-addressed one so the number is per reservation.
    reservation_files = [
        p
        for p in archive.rglob("file_reservations/*.json")
        if ".git" not in p.parts and not p.name.startswith("id-")
    ]

    # commit_async only defers the commit; the audit trail still has to land.
    # A "fast" server that silently stopped committing would otherwise pass.
    commits = 0
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        commits = int(shell(["git", "rev-list", "--count", "HEAD"], cwd=archive) or 0)
        if commits > 1:
            break
        time.sleep(0.2)

    effects = {
        "expected_messages": expected,
        "inbox_messages": delivered,
        "archive_message_files": len(message_files),
        "archive_reservation_files": len(reservation_files),
        "archive_commits": commits,
    }
    if delivered < expected:
        raise BenchError(f"messages went missing -- calls were not doing the work: {effects}")
    if len(message_files) < expected:
        raise BenchError(f"archive is short of canonical message files: {effects}")
    if len(reservation_files) < expected:
        raise BenchError(f"archive is short of reservation artifacts: {effects}")
    if commits <= 1:
        raise BenchError(f"archive commits never landed: {effects}")
    return effects


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


def shell(command: list[str], cwd: Path | None = None) -> str:
    return subprocess.run(
        command, cwd=cwd, capture_output=True, text=True, check=False
    ).stdout.strip()


def _exceeded(ops: dict[str, Any], limits_by_op: dict[str, Any]) -> list[str]:
    exceeded: list[str] = []
    for name, limits in limits_by_op.items():
        stats = ops.get(name)
        if stats is None:
            exceeded.append(f"{name}: not measured")
            continue
        for percentile, limit in limits.items():
            if stats[percentile] > limit:
                exceeded.append(f"{name} {percentile}={stats[percentile]}ms > {limit}ms")
    return exceeded


def gate_eligibility(settings: dict[str, str], iterations: int, warmup: int) -> list[str]:
    """Reasons this run must not be allowed to claim a pass.

    Every entry here closes a way of satisfying the thresholds without the
    server actually being fast: run it in a quieter configuration than
    production, or shrink the sample until the tail cannot be seen.
    """
    reasons: list[str] = []
    for key, expected in PRODUCTION_PROFILE.items():
        actual = settings.get(key)
        if actual != expected:
            reasons.append(f"{key}={actual} (gate requires {expected})")
    if iterations < MIN_GATED_ITERATIONS:
        reasons.append(f"iterations={iterations} < {MIN_GATED_ITERATIONS}")
    if warmup > MAX_GATED_WARMUP:
        reasons.append(f"warmup={warmup} > {MAX_GATED_WARMUP} (hides cold-path cost)")
    return reasons


def gate(
    ops: dict[str, Any],
    tools_list: dict[str, Any],
    settings: dict[str, str],
    iterations: int,
    warmup: int,
) -> tuple[str, list[str], list[str]]:
    breaches = _exceeded(ops, THRESHOLDS_MS)
    if tools_list["bytes"] > TOOLS_LIST_MAX_BYTES:
        breaches.append(f"tools/list {tools_list['bytes']}B > {TOOLS_LIST_MAX_BYTES}B")
    warnings = _exceeded(ops, WATCH_MS)

    ineligible = gate_eligibility(settings, iterations, warmup)
    if ineligible:
        # Report the numbers, refuse to call them a pass.
        return "ungated", breaches, warnings + [f"not gate-eligible: {r}" for r in ineligible]
    return ("pass" if not breaches else "fail"), breaches, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=25, help="measured calls per tool")
    parser.add_argument("--warmup", type=int, default=3, help="unrecorded calls per tool")
    parser.add_argument(
        "--commit-async",
        default="true",
        choices=("true", "false"),
        help="AGENTSTACK_MAIL_ARCHIVE_COMMIT_ASYNC for the scratch server",
    )
    parser.add_argument(
        "--tools-log",
        default="true",
        choices=("true", "false"),
        help="AGENTSTACK_MAIL_TOOLS_LOG_ENABLED; true matches production",
    )
    parser.add_argument("--server-executable", default="", help="path to agentstack-mail")
    parser.add_argument("--no-append", action="store_true", help="do not write results.jsonl")
    parser.add_argument("--no-gate", action="store_true", help="report but always exit 0")
    parser.add_argument("--label", default="", help="free-form tag recorded in the row")
    args = parser.parse_args()

    if args.iterations < MIN_GATED_ITERATIONS:
        print(
            f"warning: {args.iterations} iterations is below the gate floor of "
            f"{MIN_GATED_ITERATIONS}; this run can report but cannot pass"
        )

    server = Path(args.server_executable) if args.server_executable else (
        Path(sys.executable).parent / "agentstack-mail"
    )
    if not server.exists():
        raise BenchError(
            f"server executable not found: {server}\n"
            "Run this with the repo venv's python, or pass --server-executable."
        )

    port = free_port()
    if port in PROTECTED_PORTS:  # belt and braces; the kernel will not hand these out
        raise BenchError(f"refusing to bind protected port {port}")

    root = Path(mkdtemp(prefix="agentstack-bench-"))
    env_file = root / "bench.env"
    settings = write_env_file(
        env_file,
        root,
        port,
        args.commit_async == "true",
        tools_log=args.tools_log == "true",
    )
    environment = runtime_environment(env_file)
    project = str(root / "bench-project")
    Path(project).mkdir(parents=True, exist_ok=True)

    # Which source tree is actually being measured? The venv's editable install
    # points at the main checkout, so a worktree only gets measured when
    # PYTHONPATH says so -- record what the interpreter resolves, do not assume.
    source = shell(
        [str(Path(sys.executable)), "-c", "import agentstack_mail;print(agentstack_mail.__file__)"]
    )

    # The server's tool log is chatty. Piping it and not draining the pipe
    # deadlocks the server once the 64KB buffer fills -- which reads exactly
    # like "the second call hung". Send it to a file in the scratch root.
    server_log = root / "server.log"
    log_handle = server_log.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [str(server)],
        env=environment,
        start_new_session=True,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        wait_port(port, present=True)
        checks = selftest_detector(port, project)
        tools_list = measure_tools_list(port)
        ops = measure(port, project, args.iterations, args.warmup)
        effects = assert_work_happened(port, project, args.iterations, args.warmup, root)
    finally:
        stop_process(process, port)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pass
        log_handle.close()

    verdict, breaches, warnings = gate(
        ops, tools_list, settings, args.iterations, args.warmup
    )
    row = {
        "timestamp": shell(["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"]),
        "timestamp_local": shell(["date", "+%Y-%m-%dT%H:%M %Z"]),
        "tier": 1,
        "label": args.label,
        "git_sha": shell(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT),
        "git_branch": shell(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=REPO_ROOT),
        "git_dirty": bool(shell(["git", "status", "--porcelain"], cwd=REPO_ROOT)),
        "host": shell(["hostname"]),
        "python": sys.version.split()[0],
        "source": source,
        "server": str(server),
        "iterations": args.iterations,
        "warmup": args.warmup,
        "settings": settings,
        "detector_selftest": checks,
        "ops_ms": ops,
        "tools_list": tools_list,
        "effects": effects,
        "thresholds_ms": THRESHOLDS_MS,
        "watch_ms": WATCH_MS,
        "tools_list_max_bytes": TOOLS_LIST_MAX_BYTES,
        "verdict": verdict,
        "breaches": breaches,
        "warnings": warnings,
    }

    if not args.no_append:
        BENCH_DIR.mkdir(parents=True, exist_ok=True)
        with RESULTS_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")

    print(f"scratch server: 127.0.0.1:{port}  state-root: {root}")
    print(f"source measured: {source}")
    print(f"detector self-test: {len(checks)} cases, planted failures detected")
    for name, stats in ops.items():
        print(
            f"{name:<24} p50={stats['p50']:>7.2f}ms  p95={stats['p95']:>7.2f}ms  "
            f"min={stats['min']:>7.2f}  max={stats['max']:>7.2f}  n={stats['n']}"
        )
    print(f"{'tools/list':<24} {tools_list['kib']}KiB over {tools_list['tool_count']} tools")
    print(f"effects: {effects}")
    for warning in warnings:
        print(f"watch: {warning}")
    print(f"verdict: {verdict}" + (f" -- {breaches}" if breaches else ""))

    if verdict == "fail" and not args.no_gate:
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BenchError as error:
        print(f"bench aborted: {error}", file=sys.stderr)
        sys.exit(2)
