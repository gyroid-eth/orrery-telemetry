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

functions = {
    node.name
    for node in tree.body
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
}
required = {"_is_agent_process_name", "_agent_process_alive"}
missing = sorted(required - functions)
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

for relative in "${FILES[@]}"; do
  src="$REPO_ROOT/$relative"
  dst="$INSTALL_DIR/$relative"
  echo "$PROG: copy $relative -> $dst"
  if [[ "$DRY_RUN" == true ]]; then
    continue
  fi
  mkdir -p "$(dirname "$dst")"
  cp "$src" "$dst"
  case "$relative" in
    bin/*|hooks/*) chmod 755 "$dst" ;;
    *) chmod 644 "$dst" ;;
  esac
done

if [[ "$DRY_RUN" != true ]]; then
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
tmp = manifest.with_name(manifest.name + ".tmp")
tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
os.replace(tmp, manifest)
PY
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
