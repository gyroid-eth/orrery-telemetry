#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_INSTALL_DIR="$HOME/.agentstack/integrations/codex_app"
if [[ -f "$SCRIPT_DIR/../install-state.json" ]]; then
  DEFAULT_INSTALL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
fi
INSTALL_DIR="${AGENTSTACK_CODEX_APP_INSTALL_DIR:-$DEFAULT_INSTALL_DIR}"
DRY_RUN=false
PURGE_DATA=false

usage() {
  cat <<'EOF'
Usage: uninstall-codex-app-integration.sh [options]

Options:
  --dry-run          Print actions without modifying files
  --purge-data       Also remove the exact runtime directory from the manifest
  --install-dir PATH Default: ~/.agentstack/integrations/codex_app
  -h, --help         Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=true; shift ;;
    --purge-data) PURGE_DATA=true; shift ;;
    --install-dir) INSTALL_DIR="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

MANIFEST="$INSTALL_DIR/install-state.json"
[[ -f "$MANIFEST" ]] || {
  echo "No Codex App integration manifest found: $MANIFEST" >&2
  exit 1
}

python3 - "$MANIFEST" "$DRY_RUN" "$PURGE_DATA" <<'PY'
import json
import os
import pathlib
import shutil
import subprocess
import sys

manifest_path = pathlib.Path(sys.argv[1]).expanduser()
dry_run = sys.argv[2] == "true"
purge_data = sys.argv[3] == "true"
data = json.loads(manifest_path.read_text(encoding="utf-8"))
install_dir = pathlib.Path(data["install_dir"]).expanduser()
runtime_dir = pathlib.Path(data["runtime_dir"]).expanduser()
home = pathlib.Path.home()

def safe(path: pathlib.Path) -> pathlib.Path:
    resolved = path.resolve()
    if resolved in {pathlib.Path("/"), home.resolve()}:
        raise RuntimeError(f"refusing unsafe removal: {resolved}")
    return resolved

def run(argv: list[str]) -> None:
    if dry_run:
        print("DRY-RUN would run: " + " ".join(argv))
    else:
        subprocess.run(argv, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

plugin = data.get("plugin", {})
codex_binary = str(data.get("codex_binary") or "codex")
if plugin.get("enabled"):
    run([codex_binary, "plugin", "remove", str(plugin["id"]), "--json"])
    run([codex_binary, "plugin", "marketplace", "remove", str(plugin["marketplace_name"])])

launchd = data.get("launchd", {})
if launchd.get("enabled"):
    label = str(launchd["label"])
    run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"])
    plist = safe(pathlib.Path(launchd["path"]).expanduser())
    print(("DRY-RUN would remove" if dry_run else "remove") + f": {plist}")
    if not dry_run:
        try:
            plist.unlink()
        except FileNotFoundError:
            pass

install_dir = safe(install_dir)
print(("DRY-RUN would remove" if dry_run else "remove") + f": {install_dir}")
if not dry_run:
    shutil.rmtree(install_dir)

if purge_data:
    runtime_dir = safe(runtime_dir)
    print(("DRY-RUN would purge" if dry_run else "purge") + f": {runtime_dir}")
    if not dry_run:
        shutil.rmtree(runtime_dir, ignore_errors=True)
else:
    print(f"retained runtime: {runtime_dir}")
PY
