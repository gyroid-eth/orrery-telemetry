"""Fail closed when a built wheel drops contract or license evidence."""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

REQUIRED_SUFFIXES = {
    ".dist-info/licenses/AGENTSTACK_LICENSE",
    ".dist-info/licenses/UPSTREAM_LICENSE",
    "agentstack_mail/NOTICE.md",
    "agentstack_mail/fixtures/compatibility-tools-v1.json",
    "agentstack_mail/fixtures/live-tools-list.json",
}


def verify_wheel(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())

    missing = sorted(
        suffix for suffix in REQUIRED_SUFFIXES if not any(name.endswith(suffix) for name in names)
    )
    if missing:
        raise SystemExit(f"wheel is missing required artifacts: {', '.join(missing)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    verify_wheel(args.wheel)


if __name__ == "__main__":
    main()
