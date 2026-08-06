"""Fail closed when a built wheel drops contract or license evidence."""

from __future__ import annotations

import argparse
import ast
import zipfile
from pathlib import Path

REQUIRED_SUFFIXES = {
    ".dist-info/licenses/AGENTSTACK_LICENSE",
    ".dist-info/licenses/UPSTREAM_LICENSE",
    "agentstack_mail/NOTICE.md",
    "agentstack_mail/fixtures/compatibility-tools-v1.json",
    "agentstack_mail/fixtures/live-tools-list.json",
    "agentstack_mail/app.py",
    "agentstack_mail/boundary.py",
    "agentstack_mail/config.py",
    "agentstack_mail/db.py",
    "agentstack_mail/models.py",
    "agentstack_mail/storage.py",
}


def verify_wheel(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())

        old_namespace_imports: list[str] = []
        for name in sorted(n for n in names if n.startswith("agentstack_mail/") and n.endswith(".py")):
            tree = ast.parse(archive.read(name), filename=name)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    modules = [node.module]
                else:
                    continue
                if any(module == "mcp_agent_mail" or module.startswith("mcp_agent_mail.") for module in modules):
                    old_namespace_imports.append(name)

    missing = sorted(
        suffix for suffix in REQUIRED_SUFFIXES if not any(name.endswith(suffix) for name in names)
    )
    if missing:
        raise SystemExit(f"wheel is missing required artifacts: {', '.join(missing)}")
    if any(name.startswith("agentstack_mail/provenance/") for name in names):
        raise SystemExit("wheel must not contain the repository-only provenance bundle")
    if old_namespace_imports:
        raise SystemExit(
            "wheel contains imports from the old namespace: "
            + ", ".join(sorted(set(old_namespace_imports)))
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    verify_wheel(args.wheel)


if __name__ == "__main__":
    main()
