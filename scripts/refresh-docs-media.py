#!/usr/bin/env python3
"""Run the shared documentation capture from the Telemetry checkout."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument(
        "--cockpit-tools", type=Path, help="ORRERY checkout containing tools/docs_media"
    )
    parser.add_argument("--cockpit-source", type=Path)
    parser.add_argument(
        "--telemetry-source", type=Path, default=Path(__file__).resolve().parents[1]
    )
    options, remaining = parser.parse_known_args()
    if "--help" in remaining or "-h" in remaining:
        print(
            "Usage: refresh-docs-media.py --cockpit-source CHECKOUT [--cockpit-tools CHECKOUT]"
        )
        print(
            "       [--telemetry-source CHECKOUT] --output DIRECTORY [--cdp URL] [--group GROUP]"
        )
        return
    if options.cockpit_source is None:
        parser.error("--cockpit-source is required")
    tool_root = options.cockpit_tools or options.cockpit_source
    script = tool_root / "tools/docs_media/refresh.py"
    if not script.is_file():
        parser.error(
            f"capture tool not found at {script}; use --cockpit-tools with an updated ORRERY checkout"
        )
    return subprocess.call(
        [
            sys.executable,
            str(script),
            "--cockpit-source",
            str(options.cockpit_source),
            "--telemetry-source",
            str(options.telemetry_source),
            *remaining,
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
