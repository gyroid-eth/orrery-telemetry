"""Hermetic source and environment helpers for differential tests.

The live side is reconstructed exclusively from the checked-in provenance
artifacts.  In particular, this module must never consult a developer's
mutable AgentMail checkout as a fallback.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

LIVE_BUNDLE_SHA256 = "2265572de9ae1161c0be5e2681137d10205400cc01c3efe93bbcb16c30e37a1e"
LIVE_PATCH_SHA256 = "8f592e415af1cb00c8daea9b190fadf8f9dcfbaa6d4b2b957c8a690da05f9eac"
LIVE_HEAD = "b8251c1336e5fdca80a91b8b608d843df91b64e8"
LIVE_COMMIT_COUNT = 1
FORBIDDEN_PROVENANCE_PATH = "signing-77c6e768.key"
FORBIDDEN_PROVENANCE_BLOB = "607de0ca5197430e8a3eae4c08c051d5799b84cc"

LIVE_NAMESPACE = "mcp_agent_mail"
CORE_NAMESPACE = "agentstack_mail"

_BUNDLE_RELATIVE_PATH = Path("provenance/live-head.bundle")
_PATCH_RELATIVE_PATH = Path("provenance/working-tree-tracked.patch")
_BASE_ENV_ALLOWLIST = frozenset(
    {
        # PATH is needed by GitPython.  The Windows entries let an absolute
        # Python executable start without inheriting the rest of the host env.
        "PATH",
        "COMSPEC",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
    }
)


class LiveReconstructionError(RuntimeError):
    """The frozen live source could not be authenticated or reconstructed."""


@dataclass(frozen=True)
class WorkerStateRoots:
    """Private filesystem roots for one differential worker process.

    Callers must pass :attr:`cwd` to ``subprocess.run(cwd=...)``.  That empty,
    worker-owned directory is what prevents the live decouple configuration
    from discovering a repository or user ``.env`` file.
    """

    home: Path
    database: Path
    storage: Path
    signals: Path
    temp: Path
    cwd: Path
    pythonpath: tuple[Path, ...] = ()

    @classmethod
    def under(
        cls,
        root: Path,
        *,
        pythonpath: Sequence[Path] = (),
    ) -> WorkerStateRoots:
        """Derive all worker-owned locations from one case-specific root."""

        root = Path(root).resolve()
        return cls(
            home=root / "home",
            database=root / "mail.sqlite3",
            storage=root / "archive",
            signals=root / "signals",
            temp=root / "tmp",
            cwd=root / "cwd",
            pythonpath=tuple(Path(item).resolve() for item in pythonpath),
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_artifact(path: Path, expected_sha256: str, label: str) -> None:
    if not path.is_file():
        raise LiveReconstructionError(f"{label} is missing: {path}")
    actual_sha256 = _sha256(path)
    if actual_sha256 != expected_sha256:
        raise LiveReconstructionError(
            f"{label} SHA-256 mismatch: expected {expected_sha256}, got {actual_sha256}"
        )


def _diagnostic_text(completed: subprocess.CalledProcessError) -> str:
    """Return a bounded Git diagnostic without dumping a worker environment."""

    stderr = completed.stderr.strip() if isinstance(completed.stderr, str) else ""
    stdout = completed.stdout.strip() if isinstance(completed.stdout, str) else ""
    detail = stderr or stdout or "git returned no diagnostic output"
    return detail[-4000:]


def _run_git(
    label: str,
    args: Sequence[str],
    *,
    cwd: Path,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    command = ["git", *args]
    git_env = {
        name: value for name in _BASE_ENV_ALLOWLIST if (value := os.environ.get(name))
    }
    git_env.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
            "TZ": "UTC",
        }
    )
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            env=git_env,
            input=input_text,
        )
    except FileNotFoundError as exc:
        raise LiveReconstructionError(
            f"{label} failed: required executable 'git' was not found"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise LiveReconstructionError(
            f"{label} failed with exit code {exc.returncode}: {_diagnostic_text(exc)}"
        ) from exc


def _single_line(result: subprocess.CompletedProcess[str], label: str) -> str:
    value = result.stdout.strip()
    if not value or "\n" in value:
        raise LiveReconstructionError(f"{label} returned an unexpected value")
    return value


def reconstruct_live(package_root: Path, tmp_path: Path) -> Path:
    """Reconstruct and authenticate the frozen tracked live checkout.

    ``package_root`` must be the checked-in ``packages/agentstack_mail``
    directory.  The returned path is a newly cloned, detached, depth-1 checkout
    below ``tmp_path`` with the frozen tracked working-tree patch applied.  There
    is deliberately no mutable-checkout or network fallback.
    """

    package_root = Path(package_root).resolve()
    tmp_path = Path(tmp_path).resolve()
    bundle = package_root / _BUNDLE_RELATIVE_PATH
    patch = package_root / _PATCH_RELATIVE_PATH

    _verify_artifact(bundle, LIVE_BUNDLE_SHA256, "live Git bundle")
    _verify_artifact(patch, LIVE_PATCH_SHA256, "live working-tree patch")
    if FORBIDDEN_PROVENANCE_PATH.encode() in patch.read_bytes():
        raise LiveReconstructionError(
            "live working-tree patch contains the forbidden signing-key path"
        )

    tmp_path.mkdir(parents=True, exist_ok=True)
    checkout = tmp_path / "live-agent-mail"
    if os.path.lexists(checkout):
        raise LiveReconstructionError(
            f"live reconstruction destination already exists: {checkout}"
        )

    _run_git(
        "live bundle verification",
        ["bundle", "verify", str(bundle)],
        cwd=package_root,
    )
    heads = _run_git(
        "live bundle HEAD inspection",
        ["bundle", "list-heads", str(bundle)],
        cwd=package_root,
    )
    expected_head_record = f"{LIVE_HEAD} HEAD"
    if expected_head_record not in heads.stdout.splitlines():
        raise LiveReconstructionError(
            f"live bundle does not advertise the expected HEAD {LIVE_HEAD}"
        )

    _run_git(
        "live bundle clone",
        ["clone", "--no-checkout", str(bundle), str(checkout)],
        cwd=tmp_path,
    )
    # A bundle does not transport the source repository's `.git/shallow` file.
    # Until this authenticated boundary is restored, rev-list/fsck will report
    # the intentionally absent parent as broken; that is not evidence that the
    # parent or another unreachable object is present in the bundle.
    shallow_file = checkout / ".git" / "shallow"
    shallow_file.write_text(f"{LIVE_HEAD}\n", encoding="ascii")
    _run_git(
        "live detached checkout", ["checkout", "--detach", LIVE_HEAD], cwd=checkout
    )

    actual_head = _single_line(
        _run_git("live HEAD verification", ["rev-parse", "HEAD"], cwd=checkout),
        "live HEAD verification",
    )
    if actual_head != LIVE_HEAD:
        raise LiveReconstructionError(
            f"live HEAD mismatch: expected {LIVE_HEAD}, got {actual_head}"
        )

    shallow = _single_line(
        _run_git(
            "live shallow-state verification",
            ["rev-parse", "--is-shallow-repository"],
            cwd=checkout,
        ),
        "live shallow-state verification",
    )
    if shallow != "true":
        raise LiveReconstructionError(
            f"live bundle clone did not retain its depth-1 boundary: {shallow}"
        )

    count_text = _single_line(
        _run_git(
            "live history-count verification",
            ["rev-list", "--count", "HEAD"],
            cwd=checkout,
        ),
        "live history-count verification",
    )
    try:
        commit_count = int(count_text)
    except ValueError as exc:
        raise LiveReconstructionError(
            "live history-count verification returned a non-integer value"
        ) from exc
    if commit_count != LIVE_COMMIT_COUNT:
        raise LiveReconstructionError(
            f"live history count mismatch: expected {LIVE_COMMIT_COUNT}, got {commit_count}"
        )

    # Before the shallow boundary is restored, path-limited history traversal
    # is invalid because the parent is intentionally absent.  Inspecting the
    # committed tree is the direct positive control for path presence.
    tree_paths = _run_git(
        "live HEAD path inspection",
        ["ls-tree", "-r", "--name-only", "HEAD"],
        cwd=checkout,
    ).stdout.splitlines()
    if "README.md" not in tree_paths:
        raise LiveReconstructionError(
            "live HEAD path inspection did not find the README.md positive control"
        )
    if FORBIDDEN_PROVENANCE_PATH in tree_paths:
        raise LiveReconstructionError(
            "live HEAD path inspection found the forbidden signing-key path"
        )
    forbidden_blob = _single_line(
        _run_git(
            "live forbidden signing-key blob inspection",
            ["cat-file", "--batch-check"],
            cwd=checkout,
            input_text=f"{FORBIDDEN_PROVENANCE_BLOB}\n",
        ),
        "live forbidden signing-key blob inspection",
    )
    if forbidden_blob != f"{FORBIDDEN_PROVENANCE_BLOB} missing":
        raise LiveReconstructionError(
            "live depth-1 bundle contains the forbidden signing-key blob"
        )

    fsck = _run_git(
        "live object integrity and reachability verification",
        ["fsck", "--full", "--no-reflogs", "--no-progress", "--unreachable"],
        cwd=checkout,
    )
    if fsck.stdout.strip() or fsck.stderr.strip():
        raise LiveReconstructionError(
            "live depth-1 bundle contains unreachable objects: "
            f"{(fsck.stderr or fsck.stdout).strip()[-4000:]}"
        )
    _run_git("live patch preflight", ["apply", "--check", str(patch)], cwd=checkout)
    _run_git("live patch application", ["apply", str(patch)], cwd=checkout)
    _run_git(
        "live applied-patch verification",
        ["apply", "--reverse", "--check", str(patch)],
        cwd=checkout,
    )
    _run_git(
        "live patched-tree whitespace verification", ["diff", "--check"], cwd=checkout
    )

    status = _run_git(
        "live patched-tree status inspection",
        ["status", "--porcelain=v1", "--untracked-files=all"],
        cwd=checkout,
    )
    status_lines = status.stdout.splitlines()
    if not status_lines:
        raise LiveReconstructionError(
            "live working-tree patch produced no tracked changes"
        )
    if any(line.startswith("?? ") for line in status_lines):
        raise LiveReconstructionError(
            "live reconstruction unexpectedly produced untracked files"
        )

    final_head = _single_line(
        _run_git("live final HEAD verification", ["rev-parse", "HEAD"], cwd=checkout),
        "live final HEAD verification",
    )
    if final_head != LIVE_HEAD:
        raise LiveReconstructionError(
            f"live HEAD changed while applying the tracked patch: {final_head}"
        )

    return checkout.resolve()


def _absolute(path: Path, label: str) -> Path:
    resolved = Path(path).resolve()
    if not resolved.is_absolute():  # pragma: no cover - Path.resolve is absolute
        raise ValueError(f"{label} must resolve to an absolute path")
    return resolved


def _sqlite_url(database: Path) -> str:
    # SQLAlchemy's absolute SQLite URL has three scheme slashes followed by
    # the absolute POSIX path's leading slash (four slashes on POSIX).
    return "sqlite+aiosqlite:///" + database.as_posix()


def isolated_worker_env(
    base: Mapping[str, str],
    namespace: str,
    state_roots: WorkerStateRoots,
) -> dict[str, str]:
    """Build a secret-free environment for one live or core worker.

    Only a small OS bootstrap allowlist is inherited from ``base``.  API keys,
    credentials, caller ``PYTHONPATH``, and both legacy and AgentStack Mail
    configuration names are therefore absent unless this function sets the
    appropriate variant explicitly.
    """

    if namespace not in {LIVE_NAMESPACE, CORE_NAMESPACE}:
        raise ValueError(
            f"unsupported mail namespace {namespace!r}; expected "
            f"{LIVE_NAMESPACE!r} or {CORE_NAMESPACE!r}"
        )

    home = _absolute(state_roots.home, "worker home")
    database = _absolute(state_roots.database, "worker database")
    storage = _absolute(state_roots.storage, "worker storage")
    signals = _absolute(state_roots.signals, "worker signals")
    temp = _absolute(state_roots.temp, "worker temp")
    cwd = _absolute(state_roots.cwd, "worker cwd")
    pythonpath = tuple(
        _absolute(item, "worker pythonpath") for item in state_roots.pythonpath
    )

    for directory in (
        home,
        database.parent,
        storage,
        signals,
        temp,
        cwd,
        home / ".cache",
        home / ".config",
        home / ".local/share",
    ):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)

    env = {name: value for name in _BASE_ENV_ALLOWLIST if (value := base.get(name))}
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local/share"),
            "TMPDIR": str(temp),
            "TMP": str(temp),
            "TEMP": str(temp),
            "PWD": str(cwd),
            "TZ": "UTC",
            "LANG": "C",
            "LC_ALL": "C",
            "PYTHONHASHSEED": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "PYTHONUNBUFFERED": "1",
            "NO_COLOR": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    if pythonpath:
        env["PYTHONPATH"] = os.pathsep.join(str(path) for path in pythonpath)

    database_url = _sqlite_url(database)
    if namespace == LIVE_NAMESPACE:
        env.update(
            {
                "DATABASE_URL": database_url,
                "STORAGE_ROOT": str(storage),
                "NOTIFICATIONS_ENABLED": "true",
                "NOTIFICATIONS_SIGNALS_DIR": str(signals),
                "NOTIFICATIONS_INCLUDE_METADATA": "true",
                "NOTIFICATIONS_DEBOUNCE_MS": "60000",
                "LLM_ENABLED": "false",
                "TOOLS_FILTER_ENABLED": "false",
                "TOOLS_LOG_ENABLED": "false",
                "LOG_RICH_ENABLED": "false",
                "AGENT_NAME_ENFORCEMENT_MODE": "passthrough",
                "HTTP_PORT": "28317",
                "GIT_AUTHOR_NAME": "differential-harness",
                "GIT_AUTHOR_EMAIL": "differential-harness@localhost",
            }
        )
    else:
        disabled_env_file = home / ".agentstack-mail-env-disabled"
        if disabled_env_file.exists():
            raise ValueError(
                "worker env-file sentinel unexpectedly exists; use a fresh worker home"
            )
        env.update(
            {
                "AGENTSTACK_MAIL_ENV_FILE": str(disabled_env_file),
                "AGENTSTACK_MAIL_DATABASE_URL": database_url,
                "AGENTSTACK_MAIL_STORAGE_ROOT": str(storage),
                "AGENTSTACK_MAIL_NOTIFICATIONS_ENABLED": "true",
                "AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR": str(signals),
                "AGENTSTACK_MAIL_NOTIFICATIONS_INCLUDE_METADATA": "true",
                "AGENTSTACK_MAIL_NOTIFICATIONS_DEBOUNCE_MS": "60000",
                "AGENTSTACK_MAIL_LLM_ENABLED": "false",
                "AGENTSTACK_MAIL_TOOLS_FILTER_ENABLED": "false",
                "AGENTSTACK_MAIL_TOOLS_LOG_ENABLED": "false",
                "AGENTSTACK_MAIL_LOG_RICH_ENABLED": "false",
                "AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE": "passthrough",
                "AGENTSTACK_MAIL_HTTP_PORT": "28317",
                "AGENTSTACK_MAIL_GIT_AUTHOR_NAME": "differential-harness",
                "AGENTSTACK_MAIL_GIT_AUTHOR_EMAIL": "differential-harness@localhost",
                # The frozen live side commits synchronously. Keep this parity
                # harness on the public kill switch so checkpoints observe the
                # same post-call durability boundary; default-on behavior has
                # dedicated config, runtime, and latency coverage.
                "AGENTSTACK_MAIL_ARCHIVE_COMMIT_ASYNC": "false",
            }
        )

    return env
