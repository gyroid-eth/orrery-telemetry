"""Read-only Claude Code candidate discovery; never an account authorization API."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import stat
import time

MODEL_RE = re.compile(r"claude-[a-z0-9]+(?:[._-][a-z0-9]+)*(?:\[1m\])?")
MAX_BYTES = 1024 * 1024
MAX_FILES = 64
MAX_MODELS = 128


@dataclass(frozen=True)
class ModelCatalog:
    models: tuple[str, ...]
    source: str
    error: str = ""
    # Models shown under "more models": the CLI picker's "overflow" section or
    # the bundled table. Display-only: they stay launchable like any other.
    overflow: tuple[str, ...] = ()


def is_model_id(value: object) -> bool:
    """Validate a formal Claude ID independently of discovery results."""
    return (isinstance(value, str) and len(value) <= 128
            and MODEL_RE.fullmatch(value) is not None)


def _model_ids(values: list) -> tuple[str, ...]:
    if len(values) > MAX_MODELS:
        return ()
    models: list[str] = []
    for value in values:
        if not is_model_id(value):
            return ()
        if value not in models:
            models.append(value)
    return tuple(models)


def _timestamp(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value) and value > 0
    except OverflowError:
        return False


# Bundled models that belong under "more models" when no fresh catalog says
# otherwise. Display-only: review this table in the same PR that changes the
# bundled candidates or the fixed default.
BUNDLED_OVERFLOW = ("claude-opus-5",)


@dataclass(frozen=True)
class _Snapshot:
    fetched: float
    fresh: bool
    models: tuple[str, ...]
    main: tuple[str, ...]
    overflow: tuple[str, ...]


def _read_catalog(path: Path, now_ms: float) -> _Snapshot | None:
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                return None
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return None
        data = json.loads(raw)
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 2:
        return None
    fetched, stale = data.get("fetchedAt"), data.get("staleAt")
    # A future or inverted window is malformed, not merely stale.
    if not (_timestamp(fetched) and _timestamp(stale) and fetched <= now_ms and fetched < stale):
        return None
    catalog = data.get("catalog")
    if not isinstance(catalog, dict) or catalog.get("surface") != "cc":
        return None
    config = catalog.get("config")
    rows = config.get("models") if isinstance(config, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return None
    models = _model_ids([row.get("id") for row in rows])
    if not models:
        return None
    main = tuple(row["id"] for row in rows if row.get("section") == "main")
    overflow = tuple(row["id"] for row in rows if row.get("section") == "overflow")
    return _Snapshot(fetched, now_ms < stale, models, main, overflow)


def _newest_snapshots(
    now_ms: float | None = None,
) -> tuple[_Snapshot | None, _Snapshot | None]:
    """(newest fresh, newest valid) v2 CLI caches from one scan; never merged."""
    try:
        root = Path(os.environ.get("CLAUDE_CONFIG_DIR", "").strip() or "~/.claude").expanduser()
    except (OSError, RuntimeError):
        return None, None
    if not root.is_absolute():
        return None, None
    directory = root / "cache" / "model-catalog"
    paths: list[Path] = []
    try:
        with os.scandir(directory) as entries:
            for count, entry in enumerate(entries):
                if count >= MAX_FILES:
                    return None, None  # Never guess the newest from an incomplete scan.
                if entry.name.endswith(".json") and entry.is_file(follow_symlinks=False):
                    paths.append(Path(entry.path))
    except OSError:
        return None, None
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    fresh: _Snapshot | None = None
    any_valid: _Snapshot | None = None
    for path in sorted(paths):
        snap = _read_catalog(path, now_ms)
        if snap is None:
            continue
        if any_valid is None or snap.fetched > any_valid.fetched:
            any_valid = snap
        if snap.fresh and (fresh is None or snap.fetched > fresh.fetched):
            fresh = snap
    return fresh, any_valid


def discover_catalog(
    now_ms: float | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """(fresh models, fresh main, fresh overflow, stale overflow) from one scan.

    Only a fresh cache may add candidates. A stale one is used for nothing but
    folding candidates that already exist, and only when no fresh cache exists.
    """
    fresh, newest = _newest_snapshots(now_ms)
    if fresh is not None:
        return fresh.models, fresh.main, fresh.overflow, ()
    return (), (), (), (newest.overflow if newest is not None else ())


def discover_models(now_ms: float | None = None) -> tuple[str, ...]:
    return discover_catalog(now_ms)[0]


def _display_overflow(candidates, fresh_main, fresh_overflow, stale_overflow, bundled):
    """Fold order: fresh section > bundled table > stale overflow > main."""
    folded = []
    for model in candidates:
        if model in fresh_overflow:
            folded.append(model)
        elif model in fresh_main:
            continue
        elif model in bundled or model in stale_overflow:
            folded.append(model)
    return tuple(folded)


def resolve_catalog(
    fallback: tuple[str, ...], bundled_overflow: tuple[str, ...] = BUNDLED_OVERFLOW,
) -> ModelCatalog:
    """An explicit allow-list, otherwise bundled candidates plus fresh discovery."""
    override = os.environ.get("AGENTSTACK_CLAUDE_MODELS", "")
    if override.strip():
        values = [value.strip() for value in override.split(",") if value.strip()]
        models = _model_ids(values) if len(override) <= MAX_MODELS * 129 else ()
        if not models:
            return ModelCatalog((), "override", "AGENTSTACK_CLAUDE_MODELS contains invalid model IDs")
        return ModelCatalog(models, "override")
    models, main, overflow, stale_overflow = discover_catalog()
    candidates = tuple(dict.fromkeys((*fallback, *models))) if models else fallback
    folded = _display_overflow(candidates, main, overflow, stale_overflow, bundled_overflow)
    return ModelCatalog(candidates, "local_cache" if models else "bundled", overflow=folded)
