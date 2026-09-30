"""Non-secret Codex CLI catalog discovery and the shared child model contract.

Discovery is not account authorization. Local catalog metadata supplies choices
and effort policy. Bounded, cached probes use the spawner's CLI resolver and child environment
to select the default; no network, credentials or model instruction text is read.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import stat
import sys
import threading
import time

DEFAULT_MODEL = "gpt-6.1-sol"  # Preferred default, independent of catalog ordering.
FALLBACK_MODEL = "gpt-6-sol"
DEFAULT_MODEL_NOTE = (
    "GPT-6.1 Sol is unavailable in the selected CLI or fresh local catalog; omitted model and sol "
    "use gpt-6-sol. Update Codex CLI to 0.159.0 or later "
    "(npm install -g @openai/codex@latest), then refresh its model catalog."
)
DEFAULT_MODELS = (
    DEFAULT_MODEL, FALLBACK_MODEL, "gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra",
    "gpt-5.6-luna", "gpt-6-luna",
)
# Listed models shown under NEW AGENT's "more models" fold. A fixed table (a product
# call), not a version comparison; the default is never folded, and models the
# catalog hides (`visibility` other than "list") never reach this list at all.
BUNDLED_OVERFLOW = ("gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.5")
ALIASES = {"sol": DEFAULT_MODEL, "luna": "gpt-6-luna",
           "astra": "gpt-6-astra", "terra": "gpt-5.6-terra"}
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
DEFAULT_EFFORT = "xhigh"
MODEL_RE = re.compile(r"gpt-[a-z0-9]+(?:[._-][a-z0-9]+)*")
MAX_BYTES = 2 * 1024 * 1024
MAX_MODELS = 256
CACHE_TTL_SECONDS = 300  # Codex CLI rust-v0.154.0 models-manager default.
MAX_CACHE_LINK_HOPS = 8  # Nesting depth of Codex children whose catalog is still read.

CLI_MIN_VERSION = (0, 159, 0)
CLI_VERSION_TIMEOUT_SECONDS = 2
CLI_POLICY_TIMEOUT_SECONDS = 18  # Same 15s selection budget as spawn, plus cleanup.
CLI_VERSION_CACHE_SECONDS = 60


@dataclass(frozen=True)
class LauncherPolicy:
    binary: str = ""
    version: tuple[int, int, int] | None = None
    shell: str = ""


_VERSION_CACHE: dict[tuple, tuple[float, LauncherPolicy, tuple]] = {}
_VERSION_LOCK = threading.Lock()


def _file_identity(path: str | Path) -> tuple:
    try:
        info = os.stat(path)
        return (os.path.realpath(path), info.st_dev, info.st_ino, info.st_size,
                info.st_mtime_ns, info.st_ctime_ns)
    except OSError:
        return ()


def _capture(command: list[str], timeout: float) -> bytes | None:
    process = None
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   start_new_session=os.name == "posix")
        output, _ = process.communicate(timeout=timeout)
        return output if process.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        if process is not None:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
            process.communicate()
        return None


def _parse_version(output: bytes | None) -> tuple[int, int, int] | None:
    if output is None:
        return None
    match = re.search(rb"^codex-cli ([0-9]+)\.([0-9]+)\.([0-9]+)(?:[-+][^\s]+)?\s*$", output, re.MULTILINE)
    try:
        return tuple(int(part) for part in match.groups()) if match else None
    except ValueError:
        return None


def _native_cli_version(binary: str) -> tuple[int, int, int] | None:
    return _parse_version(_capture([binary, "--version"], CLI_VERSION_TIMEOUT_SECONDS))


def _probe_launcher() -> LauncherPolicy:
    if os.name != "posix":
        # Native Windows is the experimental lane; its launcher supplies a path.
        binary = os.environ.get("AGENTSTACK_CODEX_BIN", "").strip()
        return LauncherPolicy(binary, _native_cli_version(binary)) if os.path.isabs(binary) else LauncherPolicy()
    hooks = Path(os.environ.get("AGENTSTACK_HOOKS_DIR", "") or str(Path(__file__).resolve().parents[1] / "hooks"))
    helper = hooks / "codex-bin.sh"
    output = _capture(["/bin/bash", str(helper), "policy"], CLI_POLICY_TIMEOUT_SECONDS)
    if output is None:
        return LauncherPolicy()
    if output.startswith(b"policy-v2\0"):
        # The helper streams candidate outputs over a pipe to avoid even
        # transient filesystem writes. Only the selected (last) probe counts.
        fields = output.rsplit(b"\0", 2)
        if len(fields) == 3:
            fields = [fields[1], fields[0].rsplit(b"\0", 1)[-1], fields[2]]
    else:
        fields = output.split(b"\0", 2)  # Compatibility with installed older helpers.
    if len(fields) != 3:
        return LauncherPolicy()
    try:
        binary, shell = os.fsdecode(fields[0]), os.fsdecode(fields[2])
        return LauncherPolicy(binary, _parse_version(fields[1]) if binary else None, shell)
    except ValueError:
        return LauncherPolicy()


def resolve_launcher() -> LauncherPolicy:
    """Use the spawner's environment -> saved env.sh -> usable PATH resolver.

    All probes run under the same child login shell, PATH setup and guards.
    Memoize successes/failures for 60s; context or executable changes invalidate.
    """
    names = ("HOME", "PATH", "NVM_DIR", "AGENTSTACK_HOME", "AGENTSTACK_HOOKS_DIR",
             "AGENTSTACK_CODEX_BIN", "AGENTSTACK_CHILD_SHELL")
    context = tuple(os.environ.get(name, "") for name in names)
    env_file = Path(os.environ.get("AGENTSTACK_HOME", "") or "~/.agentstack").expanduser() / "env.sh"
    key = (context, _file_identity(env_file), _file_identity(context[5]))
    with _VERSION_LOCK:
        now = time.monotonic()
        cached = _VERSION_CACHE.get(key)
        if (cached is not None and now - cached[0] < CLI_VERSION_CACHE_SECONDS
                and cached[2] == _file_identity(cached[1].binary)):
            return cached[1]
        policy = _probe_launcher()
        _VERSION_CACHE.clear()
        _VERSION_CACHE[key] = (time.monotonic(), policy, _file_identity(policy.binary))
        return policy


def cli_version() -> tuple[int, int, int] | None:
    return resolve_launcher().version


@dataclass(frozen=True)
class EffortPolicy:
    supported: tuple[str, ...]
    default: str


_STANDARD = ("low", "medium", "high", "xhigh", "max", "ultra")
BUNDLED_EFFORTS = {
    model: EffortPolicy(_STANDARD, DEFAULT_EFFORT)
    for model in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-6-astra", "gpt-6-sol", DEFAULT_MODEL)
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
    overflow: tuple[str, ...] = ()
    default_model: str = FALLBACK_MODEL
    note: str = ""


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


def normalize_model(raw: str = "", *, launcher: LauncherPolicy | None = None) -> str:
    """Only unprefixed friendly names are aliases; formal IDs remain exact."""
    if not isinstance(raw, str):
        raise ValueError("invalid Codex model ID")
    value = raw.strip()
    model = (resolve_catalog(launcher=launcher).default_model if not value or value.lower() == "sol"
             else ALIASES.get(value.lower(), value))
    if not is_model_id(model):
        raise ValueError("invalid Codex model ID; use sol / luna / astra / terra / gpt-<id>")
    allowed = _allow_list()
    if allowed is not None and model not in allowed:
        raise ValueError(f"model not allowed for provider codex: {model}")
    return model


def _cache_open_path(path: Path) -> Path | None:
    """Return a cache path safe to open without arbitrary symlink following.

    The launcher links each child home's entries to its parent's home, so a
    Codex grandchild sees a chain (grandchild -> child -> original). Every hop
    but the last must be a launcher-owned child home entry; the chain is
    bounded and a revisited link is rejected.
    """
    if not path.is_symlink():
        return path
    runtime_raw = os.environ.get("AGENTSTACK_RUNTIME_DIR", "").strip()
    if not runtime_raw:
        return None
    runtime = Path(runtime_raw).expanduser()
    if not runtime.is_absolute():
        return None
    child_root = runtime / "child-agents"
    real_child_root = Path(os.path.realpath(child_root))
    current, seen = path, set()
    for _ in range(MAX_CACHE_LINK_HOPS):
        home = current.parent
        if (current.name != "models_cache.json" or home.parent != child_root
                or re.fullmatch(r"[A-Za-z0-9_.-]+\.codex-home", home.name) is None
                or current in seen):
            return None
        # The name check is textual; a home that is itself a symlink (or whose
        # real location is elsewhere) would lead the chain outside child-agents.
        if home.is_symlink() or Path(os.path.realpath(home)) != real_child_root / home.name:
            return None
        seen.add(current)
        try:
            target = Path(os.readlink(current))
        except OSError:
            return None
        if not target.is_absolute() or target.name != "models_cache.json":
            return None
        if not target.is_symlink():
            return target
        current = target
    return None


def _read_cache(path: Path, now: float,
                hidden_out: set[str] | None = None) -> dict[str, EffortPolicy]:
    """Listed models' effort policies; `hidden_out` receives the models a valid
    snapshot explicitly hides (a row with a visibility other than "list")."""
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
        hidden: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                return {}
            if row.get("visibility") != "list":
                if "visibility" in row and is_model_id(row.get("slug")):
                    hidden.add(row["slug"])
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
        if hidden_out is not None:
            hidden_out.update(hidden - set(policies))
        return policies
    except (OSError, ValueError, UnicodeError, RecursionError, OverflowError):
        return {}


def discover_models(now: float | None = None,
                    hidden_out: set[str] | None = None) -> dict[str, EffortPolicy]:
    """Read one observed-schema cache; do not identify the current account."""
    try:
        root = Path(os.environ.get("CODEX_HOME", "").strip() or "~/.codex").expanduser()
        if not root.is_absolute():
            return {}
        return _read_cache(root / "models_cache.json", time.time() if now is None else now, hidden_out)
    except (OSError, RuntimeError, ValueError):
        return {}


def resolve_catalog(now: float | None = None, *, launcher: LauncherPolicy | None = None) -> ModelCatalog:
    try:
        allowed = _allow_list()
    except ValueError as exc:
        return ModelCatalog((), "override", {}, str(exc))
    hidden: set[str] = set()
    discovered = discover_models(now, hidden)
    policies = {**BUNDLED_EFFORTS, **discovered}
    # Old, still-running CLI sessions overwrite the shared catalog periodically.
    # Trust the binary selected for NEW AGENT over that snapshot's client_version.
    # If its version cannot be read, use positive fresh catalog evidence instead.
    version = launcher.version if launcher is not None else cli_version()
    supported = version >= CLI_MIN_VERSION if version is not None else DEFAULT_MODEL in discovered
    default = DEFAULT_MODEL if supported else FALLBACK_MODEL
    bundled = tuple(model for model in DEFAULT_MODELS
                    if (model != DEFAULT_MODEL or default == DEFAULT_MODEL)
                    and (model == default or model not in hidden))
    listed = tuple(model for model in discovered if model != DEFAULT_MODEL or supported)
    models = allowed if allowed is not None else tuple(dict.fromkeys((*bundled, *listed)))
    source = "override" if allowed is not None else ("local_cache" if discovered else "bundled")
    # An explicit allow-list is the operator's own menu and is not folded,
    # matching the Claude provider.
    overflow = () if allowed is not None else tuple(
        model for model in models if model in BUNDLED_OVERFLOW and model != default)
    return ModelCatalog(models, source, policies, overflow=overflow, default_model=default,
                        note="" if default == DEFAULT_MODEL else DEFAULT_MODEL_NOTE)


def candidate_models() -> tuple[str, ...]:
    """Known IDs for collision checks, independently of the current default.

    A bundled ID belongs to Codex even when a CLI/catalog hides its UI choice.
    Read only non-secret local metadata; do not probe a launch target.
    """
    try:
        allowed = _allow_list()
    except ValueError:
        return ()
    if allowed is not None:
        return allowed
    return tuple(dict.fromkeys((*DEFAULT_MODELS, *sorted(discover_models()))))


def resolve_effort(model: str, raw: str = "", catalog: ModelCatalog | None = None) -> str:
    """Unknown metadata leaves an omitted effort to Codex; never guesses a tier."""
    model = normalize_model(model)
    if not isinstance(raw, str):
        raise ValueError("invalid Codex reasoning effort")
    effort = raw.strip().lower()
    # Effort metadata does not need a launch-target/version probe.
    catalog = resolve_catalog(launcher=LauncherPolicy()) if catalog is None else catalog
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
        "models": list(catalog.models), "default_model": catalog.default_model,
        "model_source": catalog.source, "model_error": catalog.error,
        "overflow_models": list(catalog.overflow),
        "efforts": list(_STANDARD), "effort_default": DEFAULT_EFFORT,
        "model_efforts": {model: list(catalog.efforts[model].supported) if model in catalog.efforts else [] for model in catalog.models},
        "model_effort_defaults": {model: catalog.efforts[model].default if model in catalog.efforts else "" for model in catalog.models},
    }


def main() -> int:
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "normalize":
            print(normalize_model(sys.argv[2]))
        elif len(sys.argv) == 3 and sys.argv[1] == "resolve":
            policy = resolve_launcher()
            model = normalize_model(sys.argv[2], launcher=policy)
            print(json.dumps({"model": model, "codex_bin": policy.binary,
                              "cli_version": ".".join(map(str, policy.version)) if policy.version else "",
                              "default_source": "cli_version" if policy.version else "local_catalog"}))
        elif len(sys.argv) == 2 and sys.argv[1] == "note":
            note = resolve_catalog().note
            if note:
                print(f"note: {note}")
        elif len(sys.argv) == 4 and sys.argv[1] == "effort":
            print(resolve_effort(sys.argv[2], sys.argv[3]))
        else:
            raise ValueError("usage: codex_models.py normalize MODEL | resolve MODEL | effort MODEL EFFORT | note")
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
