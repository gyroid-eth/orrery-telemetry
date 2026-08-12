"""Bounded, read-only discovery for the consumer cutover inventory.

The collector deliberately does not inherit Git ignore semantics.  Operators
must opt in to both ``--hidden`` and ``--no-ignore`` so a command copied from
the runbook cannot silently regress to a partial search.  A collection only
publishes its typed consumer inventory after two independent controls pass:
the configured known-positive selector was read, and a configured path that
is normally ignored was included in the matched set.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import stat
import time
from collections import Counter
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from .consumer import Desired, SUPPORTED_KINDS


COLLECTION_SCHEMA_VERSION: Final[int] = 1
INVENTORY_NAME: Final[str] = "inventory.json"
SEAL_NAME: Final[str] = "seal.json"


class InventoryCollectionError(RuntimeError):
    """The requested snapshot was incomplete, ambiguous, or unstable."""


@dataclass(frozen=True, slots=True)
class Rule:
    root: Path
    pattern: str
    kind: str


@dataclass(frozen=True, slots=True)
class StableFile:
    path: Path
    kind: str
    payload: bytes
    sha256: str
    size: int
    mode: int
    device: int
    inode: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class CollectionSpec:
    desired: Desired
    roots: tuple[Path, ...]
    rules: tuple[Rule, ...]
    excluded_paths: frozenset[Path]
    positive_path: Path
    positive_selector: str
    ignored_path: Path


def _canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _absolute_path(value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise InventoryCollectionError(f"{field} must be a non-empty path")
    path = Path(value)
    if not path.is_absolute():
        raise InventoryCollectionError(f"{field} must be absolute: {path}")
    return path


def _path_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _load_spec(path: Path) -> CollectionSpec:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise InventoryCollectionError(f"cannot read collection spec: {exc}") from exc
    required = {
        "schema_version",
        "desired",
        "roots",
        "rules",
        "excluded_paths",
        "controls",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise InventoryCollectionError("collection spec has missing or extra fields")
    if value["schema_version"] != COLLECTION_SCHEMA_VERSION:
        raise InventoryCollectionError("unsupported collection spec schema")
    try:
        desired = Desired.from_payload(value["desired"])
    except Exception as exc:
        raise InventoryCollectionError(f"invalid desired cutover state: {exc}") from exc

    raw_roots = value["roots"]
    if not isinstance(raw_roots, list) or not raw_roots:
        raise InventoryCollectionError("roots must be a non-empty list")
    roots = tuple(_absolute_path(item, "root") for item in raw_roots)
    if len(set(roots)) != len(roots):
        raise InventoryCollectionError("duplicate collection root")
    for index, root in enumerate(roots):
        if not root.is_dir() or root.is_symlink():
            raise InventoryCollectionError(f"collection root is not a real directory: {root}")
        for other in roots[index + 1 :]:
            if _path_under(root, other) or _path_under(other, root):
                raise InventoryCollectionError("collection roots must not overlap")

    raw_rules = value["rules"]
    if not isinstance(raw_rules, list) or not raw_rules:
        raise InventoryCollectionError("rules must be a non-empty list")
    rules: list[Rule] = []
    for item in raw_rules:
        if not isinstance(item, dict) or set(item) != {"root", "glob", "kind"}:
            raise InventoryCollectionError("each rule must contain root, glob, and kind")
        root = _absolute_path(item["root"], "rule root")
        pattern = item["glob"]
        kind = item["kind"]
        if root not in roots:
            raise InventoryCollectionError("rule root is not a declared collection root")
        if (
            not isinstance(pattern, str)
            or not pattern
            or pattern.startswith("/")
            or ".." in Path(pattern).parts
        ):
            raise InventoryCollectionError("rule glob must be a safe relative pattern")
        if kind not in SUPPORTED_KINDS:
            raise InventoryCollectionError(f"unsupported consumer kind: {kind}")
        rules.append(Rule(root=root, pattern=pattern, kind=kind))

    raw_excluded = value["excluded_paths"]
    if not isinstance(raw_excluded, list):
        raise InventoryCollectionError("excluded_paths must be a list")
    excluded = frozenset(
        _absolute_path(item, "excluded path") for item in raw_excluded
    )
    if any(not any(_path_under(item, root) for root in roots) for item in excluded):
        raise InventoryCollectionError("every excluded path must be under a root")

    controls = value["controls"]
    if not isinstance(controls, dict) or set(controls) != {
        "known_positive_selector",
        "known_ignored_path",
    }:
        raise InventoryCollectionError("controls must contain both required controls")
    positive = controls["known_positive_selector"]
    if not isinstance(positive, dict) or set(positive) != {"path", "selector"}:
        raise InventoryCollectionError("known-positive control is malformed")
    positive_path = _absolute_path(positive["path"], "known-positive path")
    positive_selector = positive["selector"]
    if not isinstance(positive_selector, str) or not positive_selector:
        raise InventoryCollectionError("known-positive selector must be non-empty")
    ignored_path = _absolute_path(
        controls["known_ignored_path"], "known ignored path"
    )
    for control_path in (positive_path, ignored_path):
        if not any(_path_under(control_path, root) for root in roots):
            raise InventoryCollectionError("control path is outside collection roots")
        if control_path in excluded:
            raise InventoryCollectionError("control path cannot be excluded")
    return CollectionSpec(
        desired=desired,
        roots=roots,
        rules=tuple(rules),
        excluded_paths=excluded,
        positive_path=positive_path,
        positive_selector=positive_selector,
        ignored_path=ignored_path,
    )


def _snapshot(path: Path, kind: str) -> StableFile:
    before = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise InventoryCollectionError(f"matched consumer is not a regular file: {path}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise InventoryCollectionError(f"consumer changed while opening: {path}")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            payload = handle.read()
            after = os.fstat(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    final = path.stat(follow_symlinks=False)
    observed = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
        stat.S_IMODE(before.st_mode),
    )
    if observed != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
        stat.S_IMODE(after.st_mode),
    ) or observed != (
        final.st_dev,
        final.st_ino,
        final.st_size,
        final.st_mtime_ns,
        final.st_ctime_ns,
        stat.S_IMODE(final.st_mode),
    ):
        raise InventoryCollectionError(f"consumer changed while being read: {path}")
    return StableFile(
        path=path,
        kind=kind,
        payload=payload,
        sha256=hashlib.sha256(payload).hexdigest(),
        size=len(payload),
        mode=stat.S_IMODE(before.st_mode),
        device=before.st_dev,
        inode=before.st_ino,
        mtime_ns=before.st_mtime_ns,
    )


def _matching_kind(path: Path, spec: CollectionSpec) -> str | None:
    matches: set[str] = set()
    for rule in spec.rules:
        if not _path_under(path, rule.root):
            continue
        relative = path.relative_to(rule.root).as_posix()
        if fnmatch.fnmatchcase(relative, rule.pattern):
            matches.add(rule.kind)
    if len(matches) > 1:
        raise InventoryCollectionError(f"consumer rules disagree for {path}")
    return next(iter(matches), None)


def _walk(
    spec: CollectionSpec,
    *,
    max_files: int,
    deadline_seconds: float,
) -> tuple[list[StableFile], int]:
    if max_files < 1:
        raise InventoryCollectionError("max_files must be positive")
    if not (0 < deadline_seconds <= 300):
        raise InventoryCollectionError("deadline_seconds must be in (0, 300]")
    deadline = time.monotonic() + deadline_seconds
    scanned = 0
    matched: list[StableFile] = []
    for root in spec.roots:
        pending = [root]
        while pending:
            if time.monotonic() > deadline:
                raise InventoryCollectionError("collection deadline exceeded")
            directory = pending.pop()
            try:
                entries = sorted(os.scandir(directory), key=lambda item: item.name)
            except OSError as exc:
                raise InventoryCollectionError(f"cannot scan {directory}: {exc}") from exc
            for entry in entries:
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    if _matching_kind(path, spec) is not None:
                        raise InventoryCollectionError(
                            f"matched path is not a regular file: {path}"
                        )
                    continue
                scanned += 1
                if scanned > max_files:
                    raise InventoryCollectionError("collection file limit exceeded")
                if path in spec.excluded_paths:
                    continue
                kind = _matching_kind(path, spec)
                if kind is not None:
                    matched.append(_snapshot(path, kind))
    matched.sort(key=lambda item: str(item.path))
    if not matched:
        raise InventoryCollectionError("collection matched no consumers")
    if len({item.path for item in matched}) != len(matched):
        raise InventoryCollectionError("collection produced duplicate consumers")
    if len({(item.device, item.inode) for item in matched}) != len(matched):
        raise InventoryCollectionError("two consumer paths resolve to the same inode")
    return matched, scanned


def _write_sealed(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def collect(
    spec_path: Path,
    bundle: Path,
    *,
    hidden: bool,
    no_ignore: bool,
    max_files: int = 100_000,
    deadline_seconds: float = 60.0,
) -> dict[str, Any]:
    """Collect and seal one stable inventory without mutating a consumer."""

    if not hidden or not no_ignore:
        raise InventoryCollectionError(
            "collection requires both --hidden and --no-ignore semantics"
        )
    if bundle.exists() or bundle.is_symlink():
        raise InventoryCollectionError(f"collection bundle already exists: {bundle}")
    started = datetime.now(UTC)
    spec = _load_spec(spec_path)
    files, scanned = _walk(
        spec, max_files=max_files, deadline_seconds=deadline_seconds
    )
    by_path = {item.path: item for item in files}
    positive = by_path.get(spec.positive_path)
    if positive is None or spec.positive_selector.encode("utf-8") not in positive.payload:
        raise InventoryCollectionError("known-positive selector control failed")
    if spec.ignored_path not in by_path:
        raise InventoryCollectionError("known ignored-path completeness control failed")
    finished = datetime.now(UTC)

    inventory = {
        "schema_version": 1,
        "desired": asdict(spec.desired),
        "consumers": [
            {"kind": item.kind, "path": str(item.path)} for item in files
        ],
    }
    inventory_bytes = _canonical_json(inventory)
    inventory_sha256 = hashlib.sha256(inventory_bytes).hexdigest()
    counts = Counter(item.kind for item in files)
    seal = {
        "schema_version": COLLECTION_SCHEMA_VERSION,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "duration_ms": round((finished - started).total_seconds() * 1000, 3),
        "collection_semantics": {
            "hidden": True,
            "no_ignore": True,
            "required_cli_flags": ["--hidden", "--no-ignore"],
        },
        "roots": [str(root) for root in spec.roots],
        "rules": [
            {"root": str(rule.root), "glob": rule.pattern, "kind": rule.kind}
            for rule in spec.rules
        ],
        "excluded_paths": sorted(str(path) for path in spec.excluded_paths),
        "controls": {
            "search_liveness": {
                "status": "pass",
                "path": str(spec.positive_path),
                "selector_sha256": hashlib.sha256(
                    spec.positive_selector.encode("utf-8")
                ).hexdigest(),
            },
            "ignored_path_completeness": {
                "status": "pass",
                "path": str(spec.ignored_path),
            },
        },
        "counts": {
            "scanned_files": scanned,
            "matched_consumers": len(files),
            "by_kind": dict(sorted(counts.items())),
        },
        "files": [
            {
                "path": str(item.path),
                "kind": item.kind,
                "sha256": item.sha256,
                "size": item.size,
                "mode": f"{item.mode:04o}",
                "device": item.device,
                "inode": item.inode,
                "mtime_ns": item.mtime_ns,
            }
            for item in files
        ],
        "inventory_sha256": inventory_sha256,
    }
    seal_bytes = _canonical_json(seal)
    seal_sha256 = hashlib.sha256(seal_bytes).hexdigest()
    try:
        bundle.mkdir(mode=0o700)
        _write_sealed(bundle / INVENTORY_NAME, inventory_bytes)
        _write_sealed(bundle / SEAL_NAME, seal_bytes)
        descriptor = os.open(bundle, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except Exception:
        for child in (bundle / INVENTORY_NAME, bundle / SEAL_NAME):
            child.unlink(missing_ok=True)
        with suppress(OSError):
            bundle.rmdir()
        raise
    return {
        "status": "collected",
        "bundle": str(bundle),
        "inventory": str(bundle / INVENTORY_NAME),
        "inventory_sha256": inventory_sha256,
        "seal_sha256": seal_sha256,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "scanned_files": scanned,
        "consumer_count": len(files),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentstack-mail-consumer-inventory")
    parser.add_argument("--spec", required=True)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--hidden", action="store_true", required=True)
    parser.add_argument("--no-ignore", action="store_true", required=True)
    parser.add_argument("--max-files", type=int, default=100_000)
    parser.add_argument("--deadline-seconds", type=float, default=60.0)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        result = collect(
            Path(args.spec),
            Path(args.bundle),
            hidden=args.hidden,
            no_ignore=args.no_ignore,
            max_files=args.max_files,
            deadline_seconds=args.deadline_seconds,
        )
    except (InventoryCollectionError, OSError) as exc:
        print(f"agentstack-mail-consumer-inventory: {exc}", file=os.sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
