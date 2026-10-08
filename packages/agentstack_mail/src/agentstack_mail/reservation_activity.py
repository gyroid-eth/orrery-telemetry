"""Bounded per-lease filesystem and Git activity, with explicit unknown results.

Reads metadata and Git timestamps only. No project workspace, Mail database,
file contents, implicit drive mappings or unbounded subprocesses are used.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .reservation_paths import (
    ReservationError,
    ReservationPath,
    normalize_path,
    reservation_scopes,
    case_insensitive_at,
    component_matches,
)


@dataclass(frozen=True)
class Activity:
    complete: bool
    matched: bool = False
    filesystem: float | None = None
    git: float | None = None
    reason: str | None = None


class ProbeUnknown(Exception):
    pass


_PROBE_SLOTS = threading.BoundedSemaphore(8)


def _repo_for(path: Path, tick: Callable[[], None]) -> Path | None:
    directory = path if path.is_dir() else path.parent
    for parent in (directory, *directory.parents):
        tick()
        try:
            (parent / ".git").lstat()
        except FileNotFoundError:
            continue
        return parent
    return None


def _probe_activity(
    reservation: ReservationPath,
    *,
    timeout: float = 3.0,
    max_entries: int = 1000,
    clock: Callable[[], float] = time.monotonic,
) -> Activity:
    if reservation.kind == "virtual":
        return Activity(True)
    deadline = clock() + timeout
    steps = 0

    def tick() -> None:
        nonlocal steps
        steps += 1
        if steps > max_entries or clock() >= deadline:
            raise ProbeUnknown("PROBE_LIMIT")

    def expand(directory: Path, segments: tuple[str, ...]):
        tick()
        if not segments:
            yield directory
            return
        segment, rest = segments[0], segments[1:]
        fold = case_insensitive_at(directory)
        if segment == "**":
            yield from expand(directory, rest)
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    tick()
                    if segment == "**":
                        if entry.is_dir(follow_symlinks=True):
                            yield from expand(Path(entry.path), segments)
                        elif not rest:
                            yield Path(entry.path)
                    elif component_matches(entry.name, segment, fold=fold):
                        if rest:
                            if entry.is_dir(follow_symlinks=True):
                                yield from expand(Path(entry.path), rest)
                        else:
                            yield Path(entry.path)
        except FileNotFoundError:
            # Check the anchor strictly before accepting an empty/new path.
            current = directory
            while True:
                tick()
                try:
                    current.lstat()
                    current.resolve(strict=True)
                    return
                except FileNotFoundError:
                    if current.is_symlink() or current.parent == current:
                        raise ProbeUnknown("ANCHOR_UNKNOWN")
                    current = current.parent

    try:
        tick()
        if normalize_path(reservation.value) != reservation:
            return Activity(False, reason="ANCHOR_CHANGED")
        prefix = Path(reservation.prefix or "/")
        if reservation.is_glob:
            targets = expand(prefix, Path(reservation.value).relative_to(prefix).parts)
        else:
            targets = expand(prefix, (Path(reservation.value).name,))
        matched = False
        fs = git = None
        git_paths: set[tuple[Path, str]] = set()
        for target in targets:
            tick()
            resolved = Path(normalize_path(str(target)).value)
            stat = resolved.stat()
            matched = True
            fs = max(fs or stat.st_mtime, stat.st_mtime)
            repo = _repo_for(resolved, tick)
            if repo is not None:
                pathspec = ":(literal)" + str(resolved.relative_to(repo))
                git_paths.add((repo, pathspec))
        for scope in reservation_scopes(reservation, step=tick):
            # Missing/new files and empty globs can still have recent deletion
            # commits. Resolve their own anchor's repo, not a project workspace.
            anchor = Path(scope.prefix or "/")
            while not anchor.exists():
                tick()
                if anchor.is_symlink() or anchor.parent == anchor:
                    raise ProbeUnknown("ANCHOR_UNKNOWN")
                anchor = anchor.parent
            repo = _repo_for(anchor, tick)
            if repo is not None:
                relative = str(Path(scope.value).relative_to(repo))
                git_paths.add(
                    (repo, (":(glob)" if scope.is_glob else ":(literal)") + relative)
                )
        # Every matched target's actual repo is checked, including symlinks.
        for repo, pathspec in sorted(git_paths):
            tick()
            try:
                result = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(repo),
                        "log",
                        "-1",
                        "--format=%ct",
                        "--",
                        pathspec,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=max(0.001, deadline - clock()),
                )
            except subprocess.TimeoutExpired as exc:
                raise ProbeUnknown("GIT_TIMEOUT") from exc
            if result.returncode:
                raise ProbeUnknown("GIT_UNKNOWN")
            if result.stdout.strip():
                timestamp = float(result.stdout.strip())
                git = max(git or timestamp, timestamp)
        tick()
        return Activity(True, matched, fs, git)
    except (
        OSError,
        ReservationError,
        ProbeUnknown,
        ValueError,
        subprocess.TimeoutExpired,
    ) as exc:
        reason = str(exc) if isinstance(exc, ProbeUnknown) else "PROBE_UNKNOWN"
        return Activity(False, reason=reason)


def probe_activity(
    reservation: ReservationPath,
    *,
    timeout: float = 3.0,
    max_entries: int = 1000,
    clock: Callable[[], float] = time.monotonic,
) -> Activity:
    if reservation.kind == "virtual":
        return Activity(True)
    if not _PROBE_SLOTS.acquire(blocking=False):
        return Activity(False, reason="PROBE_CONCURRENCY")
    try:
        return _probe_activity(
            reservation, timeout=timeout, max_entries=max_entries, clock=clock
        )
    finally:
        _PROBE_SLOTS.release()
