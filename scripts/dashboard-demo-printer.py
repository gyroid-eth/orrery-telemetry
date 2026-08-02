#!/usr/bin/env python3
"""Render one deterministic demo-agent terminal inside a real tmux pane."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path


_STOP = False


def _stop(_signum: int, _frame: object) -> None:
    global _STOP
    _STOP = True


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-file", required=True, type=Path)
    parser.add_argument("--agent", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    previous = object()
    while not _STOP:
        try:
            raw = args.state_file.read_text(encoding="utf-8")
            payload = json.loads(raw)
            text = str(payload.get("capture") or "")
        except (OSError, ValueError, TypeError):
            text = f"AgentStack demo printer ready: {args.agent}\n"
        if text != previous:
            # 3J clears scrollback as well as the screen.  That matters because
            # dashboard/server.py intentionally parses the last 45 lines of a
            # pane: a prior work/question token must not leak into a new state.
            lines = text.rstrip().splitlines()
            try:
                pane_rows = os.get_terminal_size(sys.stdout.fileno()).lines
            except OSError:
                pane_rows = 24
            # AskUserQuestion is intentionally detected only in the last 12
            # pane lines.  Bottom-align the printer so a real 24-row pane has
            # the same parser semantics as a live Claude/Codex prompt.
            lead = "\n" * max(0, pane_rows - len(lines) - 1)
            sys.stdout.write("\033[3J\033[H\033[2J" + lead + "\n".join(lines) + "\n")
            sys.stdout.flush()
            previous = text
        time.sleep(0.10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
