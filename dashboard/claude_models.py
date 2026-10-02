"""Read-only Claude Code candidate discovery; never an account authorization API."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import sys
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
BUNDLED_OVERFLOW = ("claude-opus-5", "claude-sonnet-5")

# Where the unversioned family aliases point when no fresh local catalog names
# the current model of that family. This is the only fixed copy: the launcher,
# its warm-pool match and the dashboard default all resolve through
# resolve_alias(). Versioned aliases (sonnet-5, opus-5, [1m]) pin a generation
# on purpose and stay in spawn_child.sh.
BUNDLED_ALIASES = {
    "opus": "claude-opus-5-5",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-4-5-20251001",
    "fable": "claude-fable-5-1",
}
DEFAULT_ALIAS = "opus"
_VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+){0,3}")


@dataclass(frozen=True)
class _Snapshot:
    fetched: float
    fresh: bool
    models: tuple[str, ...]
    main: tuple[str, ...]
    overflow: tuple[str, ...]
    # family -> (model, min_claude_code_version or ""), from "main" rows only.
    current: tuple[tuple[str, tuple[str, str]], ...] = ()


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
    current: dict[str, tuple[str, str]] = {}
    for row in rows:
        family = row.get("short_name")
        if row.get("section") != "main" or not isinstance(family, str):
            continue
        family = family.strip().lower()
        if family not in BUNDLED_ALIASES or family in current:
            continue
        minimum = row.get("min_claude_code_version")
        if minimum is not None and not (isinstance(minimum, str) and _VERSION_RE.fullmatch(minimum)):
            continue  # An unreadable requirement is not evidence the CLI can run it.
        current[family] = (row["id"], minimum or "")
    return _Snapshot(fetched, now_ms < stale, models, main, overflow, tuple(current.items()))


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


def _parse_cli_version(text: str) -> tuple[int, ...] | None:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+){1,3})", text.strip())
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def _probe_cli_version() -> tuple[int, ...] | None:
    """Installed Claude Code version from its files, or None when unknown.

    Never runs the CLI: the dashboard resolves the default inside requests
    (including dry runs) that must not start processes. The native installer
    links `claude` to `.../claude/versions/<version>`; an npm install keeps
    the version in the package's package.json.
    """
    binary = os.environ.get("AGENTSTACK_CLAUDE_BIN", "").strip() or "claude"
    path = shutil.which(binary)
    if not path:
        return None
    try:
        real = Path(path).resolve()
    except (OSError, RuntimeError):
        return None
    if real.parent.name == "versions":
        return _parse_cli_version(real.name)
    for directory in list(real.parents)[:4]:
        manifest = directory / "package.json"
        try:
            if manifest.is_file() and manifest.stat().st_size <= MAX_BYTES:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("name") == "@anthropic-ai/claude-code":
                    version = data.get("version")
                    return _parse_cli_version(version) if isinstance(version, str) else None
        except (OSError, ValueError, UnicodeError, RecursionError):
            return None
    return None


@dataclass(frozen=True)
class AliasTarget:
    model: str
    # "local_catalog" or "bundled"; `note` says why the bundled table was used
    # or that the catalog was stale.
    source: str
    note: str = ""


def resolve_alias(family: str, now_ms: float | None = None, cli_version=None) -> AliasTarget:
    """Formal ID for an unversioned family alias (opus / sonnet / haiku / fable).

    The newest local catalog's "main" row for the family wins; Claude Code
    itself resolves `--model sonnet` the same way. Unlike candidate discovery,
    a stale catalog still counts here: Claude Code marks it stale after an
    hour, and even then it is the vendor's word on the current model, which
    the bundled table only repeats from release time. The bundled table is
    used when no catalog names the family, or when the installed CLI is older
    than the row requires.
    """
    family = family.strip().lower()
    if family not in BUNDLED_ALIASES:
        raise ValueError(f"unknown Claude model alias: {family}")
    bundled = BUNDLED_ALIASES[family]
    fresh, newest = _newest_snapshots(now_ms)
    snapshot = fresh or newest
    if snapshot is None:
        return AliasTarget(bundled, "bundled", "no readable local Claude Code model catalog")
    current = dict(snapshot.current).get(family)
    if current is None:
        return AliasTarget(bundled, "bundled", f"local catalog names no current {family} model")
    model, minimum = current
    if minimum:
        version = (cli_version or _probe_cli_version)()
        required = tuple(int(part) for part in minimum.split("."))
        if version is not None and version < required:
            installed = ".".join(map(str, version))
            return AliasTarget(bundled, "bundled",
                               f"{model} needs Claude Code {minimum}; installed {installed}")
    if fresh is None:
        return AliasTarget(model, "local_catalog", "catalog is stale")
    return AliasTarget(model, "local_catalog")


def default_model() -> str:
    return resolve_alias(DEFAULT_ALIAS).model


def alias_note() -> str:
    """One doctor line: where opus / sonnet / haiku / fable point, and why."""
    targets = {family: resolve_alias(family) for family in BUNDLED_ALIASES}
    mapping = ", ".join(f"{family}={target.model}" for family, target in targets.items())
    reasons = sorted({target.note for target in targets.values() if target.source == "bundled"})
    if not reasons:
        stale = any(target.note for target in targets.values())
        return ("ok: Claude model aliases follow the local Claude Code catalog"
                f"{' (stale; Claude Code refreshes it when it starts)' if stale else ''}: {mapping}")
    return (f"note: Claude model aliases use the bundled table for some models ({'; '.join(reasons)}): {mapping}. "
            "Starting Claude Code refreshes its catalog; a versioned alias such as sonnet-5-5 pins a model.")


def main(argv: list[str]) -> int:
    """`aliases`: one `family<TAB>model<TAB>source<TAB>note` line per alias.
    `note`: the doctor line."""
    if argv == ["aliases"]:
        for family in BUNDLED_ALIASES:
            target = resolve_alias(family)
            print("\t".join((family, target.model, target.source, target.note)))
    elif argv == ["note"]:
        print(alias_note())
    else:
        print("usage: claude_models.py aliases | note", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
