#!/usr/bin/env python3
"""Sleep through the edit grace period, then release if its token is current."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path


def main() -> None:
    state_file = Path(os.environ["QUERY_STATE_FILE"])
    state_token = os.environ["QUERY_STATE_TOKEN"]
    grace_seconds = int(os.environ.get("QUERY_GRACE_SECONDS", "90"))
    time.sleep(grace_seconds)

    try:
        current_token = state_file.read_text(encoding="utf-8").strip()
    except OSError:
        current_token = ""
    if current_token != state_token:
        return

    common_lib = os.environ["QUERY_COMMON_LIB"]
    command = (
        '. "$1"; reservation_release_request "$2" "$3" "$4" '
        ">/dev/null 2>&1"
    )
    subprocess.run(
        [
            "/bin/bash",
            "-c",
            command,
            "release-worker",
            common_lib,
            os.environ["QUERY_AGENT"],
            os.environ["QUERY_PROJECT_KEY"],
            os.environ["QUERY_PATHS_JSON"],
        ],
        check=False,
        env=os.environ.copy(),
    )

    try:
        current_token = state_file.read_text(encoding="utf-8").strip()
    except OSError:
        current_token = ""
    if current_token == state_token:
        try:
            state_file.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    main()
