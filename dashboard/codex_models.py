"""Non-secret Codex CLI catalog discovery and the shared child model contract.

Discovery is not account authorization. No CLI, network, credentials or model
instruction text is consulted; only bounded local catalog metadata is used.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

DEFAULT_MODEL = "gpt-6-sol"  # Deliberately independent of catalog ordering.
DEFAULT_MODELS = (
    DEFAULT_MODEL, "gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra",
    "gpt-5.6-luna", "gpt-6-luna",
)
ALIASES = {"sol": "gpt-6-sol", "luna": "gpt-6-luna",
           "astra": "gpt-6-astra", "terra": "gpt-5.6-terra"}
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
DEFAULT_EFFORT = "xhigh"
MODEL_RE = re.compile(r"gpt-[a-z0-9]+(?:[._-][a-z0-9]+)*")
MAX_BYTES = 2 * 1024 * 1024
MAX_MODELS = 256
CACHE_TTL_SECONDS = 300  # Codex CLI rust-v0.154.0 models-manager default.


@dataclass(frozen=True)
class EffortPolicy:
    supported: tuple[str, ...]
    default: str


_STANDARD = ("low", "medium", "high", "xhigh", "max", "ultra")
BUNDLED_EFFORTS = {
    model: EffortPolicy(_STANDARD, DEFAULT_EFFORT)
    for model in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-6-astra", "gpt-6-sol")
}
BUNDLED_EFFORTS.update({
    model: EffortPolicy(_STANDARD[:-1], DEFAULT_EFFORT)
    for model in ("gpt-5.6-luna", "gpt-6-luna")
})
BUNDLED_EFFORTS["gpt-5.5"] = EffortPolicy(_STANDARD[:4], DEFAULT_EFFORT)


@dataclass(frozen=True)
class ModelCatalog:
    models: tuple[str, ...]
    source: str
    efforts: dict[str, EffortPolicy]
    error: str = ""


def is_model_id(value: object) -> bool:
    return (isinstance(value, str) and len(value) <= 128
            and MODEL_RE.fullmatch(value) is not None)


def _allow_list() -> tuple[str, ...] | None:
    raw = os.environ.get("AGENTSTACK_CODEX_MODELS", "")
    if not raw.strip():
        return None
    values = [value.strip() for value in raw.split(",") if value.strip()]
    if (len(raw) > MAX_MODELS * 129 or not values or len(values) > MAX_MODELS
            or any(not is_model_id(value) for value in values)):
        raise ValueError("AGENTSTACK_CODEX_MODELS contains invalid model IDs")
    return tuple(dict.fromkeys(values))


def normalize_model(raw: str = "") -> str:
    """Only unprefixed friendly names are aliases; formal IDs remain exact."""
    if not isinstance(raw, str):
        raise ValueError("invalid Codex model ID")
    value = raw.strip()
    model = ALIASES.get(value.lower(), value) if value else DEFAULT_MODEL
    if not is_model_id(model):
        raise ValueError("invalid Codex model ID; use sol / luna / astra / terra / gpt-<id>")
    allowed = _allow_list()
    if allowed is not None and model not in allowed:
        raise ValueError(f"model not allowed for provider codex: {model}")
    return model


def _cache_open_path(path: Path) -> Path | None:
    """Return a cache path safe to open without arbitrary symlink following."""
    if not path.is_symlink():
        return path
    runtime_raw = os.environ.get("AGENTSTACK_RUNTIME_DIR", "").strip()
    if not runtime_raw:
        return None
    runtime = Path(runtime_raw).expanduser()
    if not runtime.is_absolute():
        return None
    child_root = runtime / "child-agents"
    home = path.parent
    if home.parent != child_root or re.fullmatch(r"[A-Za-z0-9_.-]+\.codex-home", home.name) is None:
        return None
    try:
        target = Path(os.readlink(path))
    except OSError:
        return None
    if not target.is_absolute() or target.name != "models_cache.json":
        return None
    return target


def _read_cache(path: Path, now: float) -> dict[str, EffortPolicy]:
    try:
        open_path = _cache_open_path(path)
        if open_path is None:
            return {}
        # Never block on a FIFO and never follow the final path component.
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(open_path, flags), "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                return {}
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return {}
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("fetched_at"), str):
            return {}
        # Chrono emits up to nanoseconds; Python 3.9 accepts only 3/6 digits.
        match = re.fullmatch(
            r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})",
            data["fetched_at"],
        )
        if match is None:
            return {}
        fraction = ((match[2] or "") + "000000")[:6]
        offset = "+00:00" if match[3] == "Z" else match[3]
        fetched = datetime.fromisoformat(f"{match[1]}.{fraction}{offset}")
        if fetched.tzinfo is None:
            return {}
        age = now - fetched.timestamp()
        if not 0 <= age <= CACHE_TTL_SECONDS:
            return {}
        rows = data.get("models")
        if not isinstance(rows, list) or not rows or len(rows) > MAX_MODELS:
            return {}
        policies: dict[str, EffortPolicy] = {}
        for row in rows:
            if not isinstance(row, dict):
                return {}
            if row.get("visibility") != "list":
                continue
            model = row.get("slug")
            levels = row.get("supported_reasoning_levels")
            if not is_model_id(model) or not isinstance(levels, list) or len(levels) > len(EFFORTS):
                continue
            if any(not isinstance(level, dict) or level.get("effort") not in EFFORTS for level in levels):
                continue
            supported = tuple(dict.fromkeys(level["effort"] for level in levels))
            default = row.get("default_reasoning_level")
            if supported and default not in supported:
                continue
            policy = EffortPolicy(supported, DEFAULT_EFFORT if DEFAULT_EFFORT in supported else (default if supported else ""))
            if model in policies and policies[model] != policy:
                return {}  # Conflicting duplicate metadata is not a trustworthy snapshot.
            policies[model] = policy
        return policies
    except (OSError, ValueError, UnicodeError, RecursionError, OverflowError):
        return {}


def discover_models(now: float | None = None) -> dict[str, EffortPolicy]:
    """Read one observed-schema cache; do not identify the current account."""
    try:
        root = Path(os.environ.get("CODEX_HOME", "").strip() or "~/.codex").expanduser()
        if not root.is_absolute():
            return {}
        return _read_cache(root / "models_cache.json", time.time() if now is None else now)
    except (OSError, RuntimeError, ValueError):
        return {}


def resolve_catalog(now: float | None = None) -> ModelCatalog:
    try:
        allowed = _allow_list()
    except ValueError as exc:
        return ModelCatalog((), "override", {}, str(exc))
    discovered = discover_models(now)
    policies = {**BUNDLED_EFFORTS, **discovered}
    models = allowed if allowed is not None else tuple(dict.fromkeys((*DEFAULT_MODELS, *discovered)))
    source = "override" if allowed is not None else ("local_cache" if discovered else "bundled")
    return ModelCatalog(models, source, policies)


def resolve_effort(model: str, raw: str = "", catalog: ModelCatalog | None = None) -> str:
    """Unknown metadata leaves an omitted effort to Codex; never guesses a tier."""
    model = normalize_model(model)
    if not isinstance(raw, str):
        raise ValueError("invalid Codex reasoning effort")
    effort = raw.strip().lower()
    catalog = resolve_catalog() if catalog is None else catalog
    if catalog.error:
        raise ValueError(catalog.error)
    policy = catalog.efforts.get(model)
    if not effort:
        return policy.default if policy else ""
    if effort not in EFFORTS:
        raise ValueError("unknown Codex reasoning effort")
    # Explicit effort remains the operator choice; Codex validates it at launch.
    return effort


def provider_catalog() -> dict:
    catalog = resolve_catalog()
    return {
        "id": "codex", "label": "Codex", "program": "codex-cli",
        "models": list(catalog.models), "default_model": DEFAULT_MODEL,
        "model_source": catalog.source, "model_error": catalog.error,
        "efforts": list(_STANDARD), "effort_default": DEFAULT_EFFORT,
        "model_efforts": {model: list(catalog.efforts[model].supported) if model in catalog.efforts else [] for model in catalog.models},
        "model_effort_defaults": {model: catalog.efforts[model].default if model in catalog.efforts else "" for model in catalog.models},
    }


def main() -> int:
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "normalize":
            print(normalize_model(sys.argv[2]))
        elif len(sys.argv) == 4 and sys.argv[1] == "effort":
            print(resolve_effort(sys.argv[2], sys.argv[3]))
        else:
            raise ValueError("usage: codex_models.py normalize MODEL | effort MODEL EFFORT")
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
