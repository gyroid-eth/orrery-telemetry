"""Fail closed when a built distribution drops contract or license evidence."""

from __future__ import annotations

import argparse
import ast
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

REQUIRED_RUNTIME_MODULES = {
    "__init__.py",
    "app.py",
    "boundary.py",
    "config.py",
    "contract.py",
    "db.py",
    "guard.py",
    "llm.py",
    "model_normalize.py",
    "models.py",
    "rich_logger.py",
    "storage.py",
    "utils.py",
}

WHEEL_REQUIRED_SUFFIXES = {
    ".dist-info/licenses/AGENTSTACK_LICENSE",
    ".dist-info/licenses/UPSTREAM_LICENSE",
    "agentstack_mail/NOTICE.md",
    "agentstack_mail/fixtures/compatibility-tools-v1.json",
    "agentstack_mail/fixtures/live-tools-list.json",
} | {f"agentstack_mail/{module}" for module in REQUIRED_RUNTIME_MODULES}

SDIST_REQUIRED_SUFFIXES = {
    "/AGENTSTACK_LICENSE",
    "/UPSTREAM_LICENSE",
    "/NOTICE.md",
    "/README.md",
    "/fixtures/compatibility-tools-v1.json",
    "/fixtures/live-tools-list.json",
    "/pyproject.toml",
    "/tests/verify_installed_contract.py",
    "/tests/verify_artifact.py",
} | {f"/src/agentstack_mail/{module}" for module in REQUIRED_RUNTIME_MODULES}

REQUIRED_METADATA = {
    "Name: agentstack-mail",
    "License-Expression: LicenseRef-PolyForm-Perimeter-1.0.1 AND LicenseRef-MCP-Agent-Mail",
    "Requires-Dist: fastmcp==2.13.0.2",
    "Requires-Dist: pydantic==2.12.5",
}


def _missing_suffixes(names: set[str], required: set[str]) -> list[str]:
    return sorted(
        suffix for suffix in required if not any(name.endswith(suffix) for name in names)
    )


def _old_namespace_imports(files: dict[str, bytes]) -> list[str]:
    old_namespace_imports: list[str] = []
    for name, content in sorted(files.items()):
        tree = ast.parse(content, filename=name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            else:
                continue
            if any(
                module == "mcp_agent_mail" or module.startswith("mcp_agent_mail.")
                for module in modules
            ):
                old_namespace_imports.append(name)
    return old_namespace_imports


def _assert_safe_paths(names: list[str], *, artifact: str) -> None:
    if len(names) != len(set(names)):
        raise SystemExit(f"{artifact} contains duplicate member paths")
    unsafe = [
        name
        for name in names
        if PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts
    ]
    if unsafe:
        raise SystemExit(
            f"{artifact} contains unsafe member paths: {', '.join(sorted(unsafe))}"
        )


def _assert_exact_runtime_modules(
    names: set[str],
    *,
    marker: str,
    artifact: str,
) -> None:
    actual = {
        name.split(marker, 1)[1]
        for name in names
        if marker in name
        and name.endswith(".py")
    }
    if actual != REQUIRED_RUNTIME_MODULES:
        raise SystemExit(
            f"{artifact} runtime module mismatch: "
            f"missing={sorted(REQUIRED_RUNTIME_MODULES - actual)}, "
            f"extra={sorted(actual - REQUIRED_RUNTIME_MODULES)}"
        )


def _assert_metadata(content: bytes, *, artifact: str) -> None:
    text = content.decode("utf-8")
    missing = sorted(fragment for fragment in REQUIRED_METADATA if fragment not in text)
    if missing:
        raise SystemExit(
            f"{artifact} metadata is missing required fields: {', '.join(missing)}"
        )


def verify_wheel(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        member_names = archive.namelist()
        _assert_safe_paths(member_names, artifact="wheel")
        names = set(member_names)
        python_files = {
            name: archive.read(name)
            for name in names
            if name.startswith("agentstack_mail/") and name.endswith(".py")
        }
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise SystemExit("wheel must contain exactly one .dist-info/METADATA")
        metadata = archive.read(metadata_names[0])

    missing = _missing_suffixes(names, WHEEL_REQUIRED_SUFFIXES)
    if missing:
        raise SystemExit(f"wheel is missing required artifacts: {', '.join(missing)}")
    if any(name.startswith("agentstack_mail/provenance/") for name in names):
        raise SystemExit("wheel must not contain the repository-only provenance bundle")
    _assert_exact_runtime_modules(
        names,
        marker="agentstack_mail/",
        artifact="wheel",
    )
    _assert_metadata(metadata, artifact="wheel")
    old_namespace_imports = _old_namespace_imports(python_files)
    if old_namespace_imports:
        raise SystemExit(
            "wheel contains imports from the old namespace: "
            + ", ".join(sorted(set(old_namespace_imports)))
        )


def verify_sdist(path: Path) -> None:
    with tarfile.open(path, mode="r:gz") as archive:
        all_members = archive.getmembers()
        member_names = [member.name for member in all_members]
        _assert_safe_paths(member_names, artifact="sdist")
        unsafe_types = [
            member.name
            for member in all_members
            if member.issym() or member.islnk() or member.isdev()
        ]
        if unsafe_types:
            raise SystemExit(
                "sdist contains link or device members: "
                + ", ".join(sorted(unsafe_types))
            )
        top_levels = {PurePosixPath(name).parts[0] for name in member_names if name}
        if len(top_levels) != 1:
            raise SystemExit("sdist must contain exactly one top-level directory")
        members = [member for member in archive.getmembers() if member.isfile()]
        names = {member.name for member in members}
        python_files = {
            member.name: extracted.read()
            for member in members
            if "/src/agentstack_mail/" in member.name
            and member.name.endswith(".py")
            and (extracted := archive.extractfile(member)) is not None
        }
        metadata_names = [name for name in names if name.endswith("/PKG-INFO")]
        if len(metadata_names) != 1:
            raise SystemExit("sdist must contain exactly one PKG-INFO")
        metadata_member = archive.getmember(metadata_names[0])
        metadata_file = archive.extractfile(metadata_member)
        if metadata_file is None:
            raise SystemExit("sdist PKG-INFO is not readable")
        metadata = metadata_file.read()

    missing = _missing_suffixes(names, SDIST_REQUIRED_SUFFIXES)
    if missing:
        raise SystemExit(f"sdist is missing required artifacts: {', '.join(missing)}")
    if any("/provenance/" in name for name in names):
        raise SystemExit("sdist must not contain repository-only provenance artifacts")
    _assert_exact_runtime_modules(
        names,
        marker="/src/agentstack_mail/",
        artifact="sdist",
    )
    _assert_metadata(metadata, artifact="sdist")
    old_namespace_imports = _old_namespace_imports(python_files)
    if old_namespace_imports:
        raise SystemExit(
            "sdist contains imports from the old namespace: "
            + ", ".join(sorted(set(old_namespace_imports)))
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact", type=Path)
    args = parser.parse_args()
    if args.artifact.suffix == ".whl":
        verify_wheel(args.artifact)
    elif args.artifact.name.endswith(".tar.gz"):
        verify_sdist(args.artifact)
    else:
        raise SystemExit(f"unsupported distribution artifact: {args.artifact}")


if __name__ == "__main__":
    main()
