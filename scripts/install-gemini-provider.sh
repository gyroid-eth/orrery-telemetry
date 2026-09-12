#!/usr/bin/env bash
# Install the optional Google Antigravity / Gemini provider payload into an
# existing ORRERY core install.  This installer is deliberately additive: it
# never replaces core Dashboard files and it never requires `agy` to be
# installed merely to stage/test the provider payload.
set -euo pipefail

PROG="install-gemini-provider.sh"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
INSTALL_DIR="${AGENTSTACK_HOME:-$HOME/.agentstack}"
PYTHON_BIN="${AGENTSTACK_PYTHON:-python3}"
DRY_RUN=false
CONFIGURE_MCP=false
GEMINI_MCP_CONFIG="${AGENTSTACK_GEMINI_MCP_CONFIG:-$HOME/.gemini/config/mcp_config.json}"

usage() {
  cat >&2 <<'EOF'
Usage: scripts/install-gemini-provider.sh [options]

Options:
  --install-dir PATH   ORRERY install dir (default ~/.agentstack)
  --configure-mcp      add/update the Antigravity ORRERY Mail MCP entry
  --dry-run            validate and print planned copies without mutation
  -h, --help           show this help

This installer does not install Antigravity CLI, does not require `agy` for a
dry run, and never changes Antigravity permission settings.  MCP configuration
is changed only when --configure-mcp is explicit.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --install-dir)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      INSTALL_DIR="$2"
      shift 2
      ;;
    --configure-mcp)
      CONFIGURE_MCP=true
      shift
      ;;
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "$PROG: unknown option: $1" >&2
      usage
      exit 2
      ;;
  esac
done

provider_dst="$INSTALL_DIR/dashboard/provider_server.py"
provider_initially_active=false
if [[ -e "$provider_dst" || -L "$provider_dst" ]]; then
  provider_initially_active=true
fi
TRANSACTION_STARTED=false

report_preflight_failure() {
  exit_status=$?
  if [[ "$exit_status" -ne 0 && "$TRANSACTION_STARTED" != true ]]; then
    echo "$PROG: provider installation failed; no Dashboard restart was performed" >&2
    if [[ "$provider_initially_active" == true ]]; then
      echo "$PROG: activation state: the existing Gemini provider remains active; no new payload was activated" >&2
    else
      echo "$PROG: activation state: Gemini provider remains disabled" >&2
    fi
    echo "$PROG: recovery: rerun the installer; restart the Dashboard only after a successful install" >&2
  fi
  return "$exit_status"
}
trap report_preflight_failure EXIT

command -v "$PYTHON_BIN" >/dev/null 2>&1 || [[ -x "$PYTHON_BIN" ]] || {
  echo "$PROG: selected Python is unavailable: $PYTHON_BIN" >&2
  exit 1
}

MANIFEST="$INSTALL_DIR/install-state.json"
INSTALLED_SERVER="$INSTALL_DIR/dashboard/server.py"
INSTALLED_SERVICE_RUNNER="$INSTALL_DIR/dashboard/service_runner.py"
[[ -f "$MANIFEST" ]] || {
  echo "$PROG: install-state.json not found under $INSTALL_DIR; install ORRERY core first" >&2
  exit 1
}
[[ -f "$INSTALLED_SERVER" ]] || {
  echo "$PROG: dashboard/server.py not found under $INSTALL_DIR; install ORRERY core first" >&2
  exit 1
}
[[ -f "$INSTALLED_SERVICE_RUNNER" ]] || {
  echo "$PROG: dashboard/service_runner.py not found under $INSTALL_DIR; install ORRERY core first" >&2
  exit 1
}

# Provider payload depends on these core-owned helpers.  Validate them before
# any copy so a partial/old core cannot receive a half-working Gemini runtime.
CORE_REQUIRED_FILES=(
  "bin/lib/agentstack-register.sh"
  "hooks/project-context.sh"
)
CORE_REQUIRED_EXECUTABLES=(
  "bin/agentstack-preregister-child"
  "hooks/cleanup-child-agent.sh"
  "integrations/codex_app/plugin/scripts/run-mcp.sh"
)
for relative in "${CORE_REQUIRED_FILES[@]}"; do
  [[ -f "$INSTALL_DIR/$relative" ]] || {
    echo "$PROG: incomplete ORRERY core: missing $relative; update/reinstall ORRERY core" >&2
    exit 1
  }
done
for relative in "${CORE_REQUIRED_EXECUTABLES[@]}"; do
  [[ -x "$INSTALL_DIR/$relative" ]] || {
    echo "$PROG: incomplete ORRERY core: missing executable $relative; update/reinstall ORRERY core" >&2
    exit 1
  }
done

FILES=(
  "bin/agent-start-gemini"
  "bin/agentstack-gemini-bootstrap"
  "bin/agentstack-gemini-setup"
  "bin/agentstack-gemini-mcp"
  "bin/agentstack-gemini-child-mail"
  "bin/agentstack-gemini-stream"
  "hooks/spawn_gemini_child.sh"
  "hooks/spawn_gemini_preregistered.sh"
  "dashboard/provider_server.py"
  "dashboard/gemini_provider_runtime.py"
  "dashboard/assets/google.svg"
)
for relative in "${FILES[@]}"; do
  [[ -f "$REPO_ROOT/$relative" ]] || {
    echo "$PROG: source payload missing: $relative" >&2
    exit 1
  }
done

# The core owns process-tree classification.  The optional payload must only be
# installed over a core that already recognizes program=antigravity / process=agy
# through the shared _is_agent_process_name/_agent_process_alive path.
"$PYTHON_BIN" - "$INSTALLED_SERVER" "$INSTALLED_SERVICE_RUNNER" <<'PY'
from pathlib import Path
import ast
import sys

server_path = Path(sys.argv[1])
runner_path = Path(sys.argv[2])
try:
    source = server_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(server_path))
except (OSError, SyntaxError) as exc:
    raise SystemExit(f"incompatible ORRERY core dashboard {server_path}: {exc}")

top_level_names = {
    node.name
    for node in tree.body
    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
}
required_process_helpers = {"_is_agent_process_name", "_agent_process_alive"}
missing = sorted(required_process_helpers - top_level_names)
if missing:
    raise SystemExit(
        "incompatible ORRERY core dashboard: missing shared process-tree helper(s): "
        + ", ".join(missing)
    )
if "antigravity" not in source or "agy" not in source:
    raise SystemExit(
        "incompatible ORRERY core dashboard: Antigravity is not on the shared "
        "process-tree liveness path; update ORRERY core first"
    )
required_launch_contract = {
    "SpawnLaunchSpec",
    "spawn_with_launch_spec",
    "_spawn_request",
    "_spawn_unavailable_error",
}
missing = sorted(required_launch_contract - top_level_names)
if missing:
    raise SystemExit(
        "incompatible ORRERY core dashboard: missing runtime launch contract "
        "helper(s): " + ", ".join(missing) + "; update ORRERY core first"
    )
try:
    runner_source = runner_path.read_text(encoding="utf-8")
except OSError as exc:
    raise SystemExit(f"incompatible ORRERY dashboard service runner {runner_path}: {exc}")
if "provider_server.py" not in runner_source:
    raise SystemExit(
        "incompatible ORRERY core dashboard: service runner cannot load optional "
        "provider_server.py; update/reinstall ORRERY core first"
    )
PY

# Validate manifest shape before mutation.  Provider files are added to the
# same ownership lists so the normal core uninstaller can remove them later.
"$PYTHON_BIN" - "$MANIFEST" <<'PY'
from pathlib import Path
import json
import sys

path = Path(sys.argv[1])
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
    raise SystemExit(f"invalid core install manifest {path}: {exc}")
if not isinstance(data, dict):
    raise SystemExit(f"invalid core install manifest {path}: expected object")
for key in ("owned_files", "owned_dirs"):
    value = data.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SystemExit(f"invalid core install manifest {path}: {key} must be a string list")
PY

ACTIVATION_FILE="dashboard/provider_server.py"

STAGING_DIR=""
PROVIDER_TMP=""
MANIFEST_BACKUP=""
TRANSACTION_STARTED=false
PROVIDER_QUARANTINED=false
PAYLOAD_COMMITTED=false
MANIFEST_UPDATED=false
INITIAL_EXISTS=()
INITIAL_STATE_CAPTURED=false

cleanup_provider_transaction() {
  exit_status=$?
  rollback_ok=true

  if [[ "$TRANSACTION_STARTED" == true && "$exit_status" -ne 0 && "$PAYLOAD_COMMITTED" != true ]]; then
    # A fresh install may have published files before a later transaction
    # step failed. Remove only paths that did not exist before the attempt;
    # upgrade files already owned by the core are intentionally left alone.
    if [[ "$INITIAL_STATE_CAPTURED" == true ]]; then
      index=0
      for relative in "${FILES[@]}"; do
        if [[ "${INITIAL_EXISTS[$index]:-false}" != true ]]; then
          rm -f "$INSTALL_DIR/$relative" || rollback_ok=false
        fi
        index=$((index + 1))
      done
    fi

    # The manifest is part of the same transaction. Restore its pre-image
    # atomically if ownership was recorded before activation failed.
    if [[ "$MANIFEST_UPDATED" == true && -n "$MANIFEST_BACKUP" ]]; then
      if "$PYTHON_BIN" - "$MANIFEST_BACKUP" "$MANIFEST" <<'PY'
from pathlib import Path
import os
import tempfile
import sys

backup = Path(sys.argv[1])
manifest = Path(sys.argv[2])
fd, temporary_name = tempfile.mkstemp(
    prefix="." + manifest.name + ".rollback-",
    dir=str(manifest.parent),
)
temporary = Path(temporary_name)
try:
    with os.fdopen(fd, "wb") as handle:
        handle.write(backup.read_bytes())
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(str(temporary), str(manifest))
except BaseException:
    try:
        temporary.unlink()
    except OSError:
        pass
    raise
PY
      then
        MANIFEST_UPDATED=false
      else
        rollback_ok=false
      fi
    fi
  fi

  if [[ -n "$PROVIDER_TMP" ]]; then
    rm -f "$PROVIDER_TMP" || rollback_ok=false
  fi
  if [[ -n "$STAGING_DIR" ]]; then
    rm -rf "$STAGING_DIR" || rollback_ok=false
  fi

  if [[ "$exit_status" -ne 0 && "$TRANSACTION_STARTED" == true ]]; then
    echo "$PROG: provider installation failed; no Dashboard restart was performed" >&2
    # PAYLOAD_COMMITTED is deliberately set only after the final mv returns.
    # An interrupt can arrive in the tiny window after mv has published the
    # entrypoint but before that assignment. Report the on-disk activation
    # state after rollback instead of inferring it from the flag alone.
    if [[ -e "$provider_dst" || -L "$provider_dst" ]]; then
      echo "$PROG: activation state: Gemini provider is active, but the installer did not complete" >&2
    elif [[ "$PROVIDER_QUARANTINED" == true ]]; then
      echo "$PROG: activation state: Gemini provider is disabled; the entrypoint was withheld to prevent a mixed payload" >&2
    elif [[ "$provider_initially_active" == true ]]; then
      echo "$PROG: activation state: the existing Gemini provider remains active; no new payload was activated" >&2
    else
      echo "$PROG: activation state: Gemini provider remains disabled" >&2
    fi
    if [[ "$rollback_ok" != true ]]; then
      echo "$PROG: rollback warning: ownership or payload cleanup did not complete; inspect the install before retrying" >&2
    fi
    echo "$PROG: recovery: rerun the installer; restart the Dashboard only after a successful install" >&2
  fi

  return "$exit_status"
}

if [[ "$DRY_RUN" == true ]]; then
  # Keep the preview in the same order as the payload declaration.  The
  # provider entrypoint is only special during a real install: it is the
  # activation point and must not be published until everything else is ready.
  for relative in "${FILES[@]}"; do
    dst="$INSTALL_DIR/$relative"
    echo "$PROG: copy $relative -> $dst"
  done
else
  # Prepare the complete payload in a private directory first.  This keeps an
  # upgrade's currently active provider paired with its current runtime while
  # staging, and lets all source copies fail before the active install changes.
  TRANSACTION_STARTED=true
  trap cleanup_provider_transaction EXIT
  STAGING_DIR="$(mktemp -d "$INSTALL_DIR/.gemini-provider-staging.XXXXXX")"
  index=0
  for relative in "${FILES[@]}"; do
    dst="$INSTALL_DIR/$relative"
    if [[ -e "$dst" || -L "$dst" ]]; then
      INITIAL_EXISTS[$index]=true
    else
      INITIAL_EXISTS[$index]=false
    fi
    index=$((index + 1))
  done
  INITIAL_STATE_CAPTURED=true

  MANIFEST_BACKUP="$STAGING_DIR/.manifest-before"
  "$PYTHON_BIN" - "$MANIFEST" "$MANIFEST_BACKUP" <<'PY'
from pathlib import Path
import sys

manifest = Path(sys.argv[1])
backup = Path(sys.argv[2])
backup.write_bytes(manifest.read_bytes())
PY

  for relative in "${FILES[@]}"; do
    src="$REPO_ROOT/$relative"
    staged="$STAGING_DIR/$relative"
    mkdir -p "$(dirname "$staged")"
    cp "$src" "$staged"
    case "$relative" in
      bin/*|hooks/*) chmod 755 "$staged" ;;
      *) chmod 644 "$staged" ;;
    esac
  done

  # Validate the staged bytes before changing the active install.  In addition
  # to catching missing files, comparing with the source catches a copy helper
  # that reports success without faithfully writing the payload.
  "$PYTHON_BIN" - "$REPO_ROOT" "$STAGING_DIR" "${FILES[@]}" <<'PY'
from pathlib import Path, PurePosixPath
import sys

source_root = Path(sys.argv[1]).expanduser().resolve(strict=False)
staging_root = Path(sys.argv[2]).expanduser().resolve(strict=False)
for relative in sys.argv[3:]:
    rel = PurePosixPath(relative)
    source = (source_root / Path(*rel.parts)).resolve(strict=False)
    staged = (staging_root / Path(*rel.parts)).resolve(strict=False)
    try:
        source.relative_to(source_root)
        staged.relative_to(staging_root)
    except ValueError as exc:
        raise SystemExit(f"provider payload escapes staging root: {relative}") from exc
    if not staged.is_file():
        raise SystemExit(f"provider payload staging incomplete: missing {relative}")
    if staged.read_bytes() != source.read_bytes():
        raise SystemExit(f"provider payload staging mismatch: {relative}")
PY

  if [[ -e "$provider_dst" || -L "$provider_dst" ]]; then
    # Do not let an old provider observe newly published runtime files.  If a
    # later step fails, leaving this name absent makes service_runner.py fall
    # back to the core dashboard instead of exposing a mixed upgrade.
    old_provider="$STAGING_DIR/.previous-provider-server.py"
    mv "$provider_dst" "$old_provider"
    PROVIDER_QUARANTINED=true
  fi

  # Publish all non-entrypoint files while the activation name is absent.  A
  # failure here therefore leaves either the old core dashboard or no Gemini
  # provider active, never an old provider paired with new dependencies.
  for relative in "${FILES[@]}"; do
    [[ "$relative" == "$ACTIVATION_FILE" ]] && continue
    src="$STAGING_DIR/$relative"
    dst="$INSTALL_DIR/$relative"
    echo "$PROG: copy $relative -> $dst"
    mkdir -p "$(dirname "$dst")"
    cp "$src" "$dst"
    case "$relative" in
      bin/*|hooks/*) chmod 755 "$dst" ;;
      *) chmod 644 "$dst" ;;
    esac
  done

  # A successful cp is not enough to make the activation safe when a
  # filesystem or test double reports success without leaving the exact
  # staged payload in place.  Confirm every dependency before recording
  # ownership or publishing provider_server.py.
  "$PYTHON_BIN" - "$INSTALL_DIR" "$STAGING_DIR" "$ACTIVATION_FILE" "${FILES[@]}" <<'PY'
from pathlib import Path, PurePosixPath
import sys

root = Path(sys.argv[1]).expanduser().resolve(strict=False)
staging_root = Path(sys.argv[2]).expanduser().resolve(strict=False)
activation = sys.argv[3]
for relative in sys.argv[4:]:
    if relative == activation:
        continue
    rel = PurePosixPath(relative)
    installed = (root / Path(*rel.parts)).resolve(strict=False)
    staged = (staging_root / Path(*rel.parts)).resolve(strict=False)
    try:
        installed.relative_to(root)
        staged.relative_to(staging_root)
    except ValueError as exc:
        raise SystemExit(f"provider payload escapes install root: {relative}") from exc
    if not installed.is_file():
        raise SystemExit(f"provider payload copy incomplete: missing {relative}")
    if installed.read_bytes() != staged.read_bytes():
        raise SystemExit(f"provider payload copy mismatch: {relative}")
PY

  "$PYTHON_BIN" - "$MANIFEST" "$INSTALL_DIR" "${FILES[@]}" <<'PY'
from pathlib import Path, PurePosixPath
import json
import os
import sys

manifest = Path(sys.argv[1]).expanduser()
root = Path(sys.argv[2]).expanduser().resolve(strict=False)
relative_paths = sys.argv[3:]
data = json.loads(manifest.read_text(encoding="utf-8"))
files = set(data["owned_files"])
dirs = set(data["owned_dirs"])
for relative in relative_paths:
    rel = PurePosixPath(relative)
    if rel.is_absolute() or ".." in rel.parts:
        raise SystemExit(f"unsafe provider payload path: {relative}")
    path = (root / Path(*rel.parts)).resolve(strict=False)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise SystemExit(f"provider payload escapes install root: {relative}") from exc
    files.add(os.fspath(path))
    parent = path.parent
    while True:
        dirs.add(os.fspath(parent))
        if parent == root:
            break
        parent = parent.parent

data["owned_files"] = sorted(files)
data["owned_dirs"] = sorted(dirs)
import tempfile
fd, temporary_name = tempfile.mkstemp(
    prefix="." + manifest.name + ".update-",
    dir=str(manifest.parent),
)
temporary = Path(temporary_name)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(str(temporary), str(manifest))
except BaseException:
    try:
        temporary.unlink()
    except OSError:
        pass
    raise
PY
  MANIFEST_UPDATED=true

  # Create the activation temp exclusively in the destination directory, so a
  # pre-existing symlink cannot redirect the final cp.  Replace the old name
  # only after all payload files and ownership metadata are ready.
  provider_src="$STAGING_DIR/$ACTIVATION_FILE"
  mkdir -p "$(dirname "$provider_dst")"
  PROVIDER_TMP="$(mktemp "$provider_dst.XXXXXX")"
  echo "$PROG: copy $ACTIVATION_FILE -> $provider_dst"
  cp "$provider_src" "$PROVIDER_TMP"
  chmod 644 "$PROVIDER_TMP"
  mv -f "$PROVIDER_TMP" "$provider_dst"
  PROVIDER_TMP=""
  PAYLOAD_COMMITTED=true
fi

if [[ "$CONFIGURE_MCP" == true ]]; then
  if [[ "$DRY_RUN" == true ]]; then
    echo "$PROG: would run $INSTALL_DIR/bin/agentstack-gemini-setup"
  else
    AGENTSTACK_HOME="$INSTALL_DIR" \
    AGENTSTACK_GEMINI_MCP_CONFIG="$GEMINI_MCP_CONFIG" \
      "$INSTALL_DIR/bin/agentstack-gemini-setup"
  fi
fi

if command -v agy >/dev/null 2>&1; then
  echo "$PROG: Antigravity CLI detected: $(command -v agy)"
else
  echo "$PROG: note: agy is not installed/on PATH; payload installation is still valid"
fi

if [[ "$DRY_RUN" == true ]]; then
  echo "$PROG: Gemini provider payload dry-run complete"
else
  echo "$PROG: Gemini provider payload installed into $INSTALL_DIR"
fi
