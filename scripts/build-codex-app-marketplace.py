#!/usr/bin/env python3
"""Build a self-contained local Codex marketplace snapshot."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


PLUGIN_NAME = "agentstack-codex-app"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("integration_root", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--marketplace-name", default="agentstack-local")
    return parser.parse_args()


def copy_tree(source: Path, destination: Path) -> None:
    shutil.copytree(
        source,
        destination,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(
            "__pycache__",
            "*.pyc",
            ".pytest_cache",
            ".DS_Store",
        ),
    )


def build(
    integration_root: Path,
    destination: Path,
    marketplace_name: str,
) -> Path:
    integration_root = integration_root.resolve()
    destination = destination.resolve()
    plugin_source = integration_root / "plugin"
    source_package = integration_root / "src"
    schemas = integration_root / "schemas"
    for required in (
        plugin_source / ".codex-plugin" / "plugin.json",
        plugin_source / ".mcp.json",
        source_package / "agentstack_codex_app" / "mcp_server.py",
        schemas / "migrations" / "001_delivery_state.sql",
    ):
        if not required.is_file():
            raise SystemExit(f"missing marketplace input: {required}")
    if not marketplace_name or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789-"
        for character in marketplace_name
    ):
        raise SystemExit("marketplace name must use lowercase letters, digits, and hyphens")

    if destination.exists():
        shutil.rmtree(destination)
    plugin_destination = destination / "plugins" / PLUGIN_NAME
    copy_tree(plugin_source, plugin_destination)
    copy_tree(source_package, plugin_destination / "src")
    copy_tree(schemas, plugin_destination / "schemas")

    manifest = json.loads(
        (plugin_destination / ".codex-plugin" / "plugin.json").read_text(
            encoding="utf-8"
        )
    )
    if manifest.get("name") != PLUGIN_NAME:
        raise SystemExit("plugin manifest name does not match bundle directory")

    marketplace = destination / ".agents" / "plugins" / "marketplace.json"
    marketplace.parent.mkdir(parents=True, exist_ok=True)
    marketplace.write_text(
        json.dumps(
            {
                "name": marketplace_name,
                "interface": {"displayName": "AgentStack Local"},
                "plugins": [
                    {
                        "name": PLUGIN_NAME,
                        "source": {
                            "source": "local",
                            "path": f"./plugins/{PLUGIN_NAME}",
                        },
                        "policy": {
                            "installation": "AVAILABLE",
                            "authentication": "ON_INSTALL",
                        },
                        "category": "Productivity",
                    }
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return marketplace


def main() -> None:
    args = parse_args()
    marketplace = build(
        args.integration_root,
        args.destination,
        args.marketplace_name,
    )
    print(marketplace)


if __name__ == "__main__":
    main()
