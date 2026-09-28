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


def _read_catalog(path: Path, now_ms: float) -> tuple[float, tuple[str, ...]] | None:
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
    if not (_timestamp(fetched) and _timestamp(stale) and fetched <= now_ms < stale):
        return None
    catalog = data.get("catalog")
    if not isinstance(catalog, dict) or catalog.get("surface") != "cc":
        return None
    config = catalog.get("config")
    rows = config.get("models") if isinstance(config, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return None
    models = _model_ids([row.get("id") for row in rows])
    return (fetched, models) if models else None


def discover_models(now_ms: float | None = None) -> tuple[str, ...]:
    """Read the freshest v2 CLI cache without merging sources or inferring access."""
    try:
        root = Path(os.environ.get("CLAUDE_CONFIG_DIR", "").strip() or "~/.claude").expanduser()
    except (OSError, RuntimeError):
        return ()
    if not root.is_absolute():
        return ()
    directory = root / "cache" / "model-catalog"
    paths: list[Path] = []
    try:
        with os.scandir(directory) as entries:
            for count, entry in enumerate(entries):
                if count >= MAX_FILES:
                    return ()  # Never guess the newest from an incomplete scan.
                if entry.name.endswith(".json") and entry.is_file(follow_symlinks=False):
                    paths.append(Path(entry.path))
    except OSError:
        return ()
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    newest: tuple[float, tuple[str, ...]] | None = None
    for path in sorted(paths):
        candidate = _read_catalog(path, now_ms)
        if candidate and (newest is None or candidate[0] > newest[0]):
            newest = candidate
    return newest[1] if newest else ()


def resolve_catalog(fallback: tuple[str, ...]) -> ModelCatalog:
    """An explicit allow-list, otherwise bundled candidates plus fresh discovery."""
    override = os.environ.get("AGENTSTACK_CLAUDE_MODELS", "")
    if override.strip():
        values = [value.strip() for value in override.split(",") if value.strip()]
        models = _model_ids(values) if len(override) <= MAX_MODELS * 129 else ()
        if not models:
            return ModelCatalog((), "override", "AGENTSTACK_CLAUDE_MODELS contains invalid model IDs")
        return ModelCatalog(models, "override")
    models = discover_models()
    if models:
        return ModelCatalog(tuple(dict.fromkeys((*fallback, *models))), "local_cache")
    return ModelCatalog(fallback, "bundled")
