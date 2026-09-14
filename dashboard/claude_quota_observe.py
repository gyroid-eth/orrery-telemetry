#!/usr/bin/env python3
"""Capture Claude Code status-line quota fields into ORRERY runtime state.

This helper is intentionally opt-in. ORRERY does not overwrite an existing
Claude statusLine command. Operators who choose this helper receive a compact
status line while the quota fields are copied into dashboard runtime state.

An operator who already has a status line keeps it by wrapping it::

    claude_quota_observe.py --exec bash ~/.claude/statusline-command.sh

In that form this helper only observes: the payload is forwarded unchanged on
the wrapped command's stdin and its output is passed through verbatim.
"""

from __future__ import annotations

import json
import math
import os
import pathlib
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone


def _snapshot_path() -> pathlib.Path:
    explicit = os.environ.get("AGENTSTACK_CLAUDE_QUOTA_SNAPSHOT", "").strip()
    if explicit:
        return pathlib.Path(explicit).expanduser()
    runtime = os.environ.get("AGENTSTACK_RUNTIME_DIR", "").strip()
    if runtime:
        return pathlib.Path(runtime).expanduser() / "claude-quota.json"
    return pathlib.Path("~/.agentstack/runtime/claude-quota.json").expanduser()


def _replace_snapshot(temporary: pathlib.Path, target: pathlib.Path) -> None:
    delay = 0.005
    for attempt in range(8):
        try:
            os.replace(temporary, target)
            return
        except PermissionError:
            if os.name != "nt" or attempt == 7:
                raise
            time.sleep(delay)
            delay *= 2


def _reset_epoch(value: object) -> int | None:
    """`resets_at` arrives as an epoch on the account windows, ISO per model."""
    if type(value) is int:
        return value if value >= 0 else None
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    return None


def _carry_model_windows(rate_limits: dict, now: int) -> None:
    """Keep the per-model windows a later session did not report.

    Claude Code projects `model_scoped` for the model the session runs, so an
    Opus session reports no Fable window at all and a snapshot that simply
    replaced the list would drop it. A weekly window's utilization cannot fall
    before it resets, so the last value stays a valid floor until `resets_at`;
    each entry carries the time it was seen so the dashboard can say how old
    the number is.
    """
    kept: dict[str, dict] = {}
    for entry in _previous_model_windows():
        name = entry.get("display_name")
        resets_at = _reset_epoch(entry.get("resets_at"))
        if isinstance(name, str) and name.strip() and resets_at is not None and resets_at > now:
            kept[name.strip()] = entry
    fresh = rate_limits.get("model_scoped")
    for entry in fresh if isinstance(fresh, list) else []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("display_name")
        if isinstance(name, str) and name.strip():
            kept[name.strip()] = {**entry, "observed_at": now}
    if kept:
        rate_limits["model_scoped"] = list(kept.values())


def _previous_model_windows() -> list[dict]:
    try:
        stored = json.loads(_snapshot_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    rate_limits = stored.get("rate_limits") if isinstance(stored, dict) else None
    windows = rate_limits.get("model_scoped") if isinstance(rate_limits, dict) else None
    return [entry for entry in windows if isinstance(entry, dict)] if isinstance(windows, list) else []


def _write_snapshot(rate_limits: object) -> None:
    if not isinstance(rate_limits, dict):
        return
    target = _snapshot_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    rate_limits = dict(rate_limits)
    _carry_model_windows(rate_limits, now)
    payload = {"observed_at": now, "rate_limits": rate_limits}
    temporary: pathlib.Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = pathlib.Path(handle.name)
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        _replace_snapshot(temporary, target)
        temporary = None
        try:
            target.chmod(0o600)
        except OSError:
            pass
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _remaining(window: object) -> float | None:
    if not isinstance(window, dict):
        return None
    if isinstance(window.get("used_percentage"), bool):
        return None
    try:
        used = float(window.get("used_percentage"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(used):
        return None
    return max(0.0, min(100.0, 100.0 - used))


def _delegate(command: list[str], raw: str) -> int:
    """Run the operator's own status line with the untouched payload."""
    try:
        result = subprocess.run(command, input=raw, text=True, capture_output=True)
    except OSError as error:
        print(f"[statusline unavailable: {error}]")
        return 0
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    delegate: list[str] = []
    if argv and argv[0] == "--exec":
        delegate = argv[1:]
        if not delegate:
            print("--exec needs a command", file=sys.stderr)
            return 2

    try:
        raw = sys.stdin.read()
    except OSError:
        raw = ""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = None
    if not isinstance(payload, dict):
        # An unreadable payload must not cost the operator their status line.
        return _delegate(delegate, raw) if delegate else 0

    rate_limits = payload.get("rate_limits")
    _write_snapshot(rate_limits)

    if delegate:
        return _delegate(delegate, raw)

    model = payload.get("model")
    if isinstance(model, dict):
        model_name = str(model.get("display_name") or model.get("id") or "Claude")
    else:
        model_name = "Claude"
    parts = [f"[{model_name}]"]
    if isinstance(rate_limits, dict):
        five = _remaining(rate_limits.get("five_hour"))
        week = _remaining(rate_limits.get("seven_day"))
        if five is not None:
            parts.append(f"5h {five:.0f}% left")
        if week is not None:
            parts.append(f"7d {week:.0f}% left")
    print(" | ".join(parts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
