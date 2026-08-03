#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DRY_RUN=false
SKIP_TESTS=false
SKIP_INSTALL_CHECK=false
PYTEST_BIN="${AGENTSTACK_PYTEST:-$(command -v pytest 2>/dev/null || true)}"

usage() {
  cat <<'EOF'
Usage: export-component.sh codex-app DEST [options]

Options:
  --dry-run            Validate and list the export without writing DEST
  --skip-tests         Skip the exported unit/integration test set
  --skip-install-check Skip the clean-HOME installer dry-run
  -h, --help           Show this help
EOF
}

[[ $# -ge 1 ]] || { usage >&2; exit 2; }
COMPONENT="$1"
shift
[[ "$COMPONENT" == "codex-app" ]] || {
  echo "Unsupported component: $COMPONENT" >&2
  exit 2
}
[[ $# -ge 1 ]] || { usage >&2; exit 2; }
DEST="$1"
shift

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=true; shift ;;
    --skip-tests) SKIP_TESTS=true; shift ;;
    --skip-install-check) SKIP_INSTALL_CHECK=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

MANIFEST="$REPO_ROOT/integrations/codex_app/export-manifest.txt"
[[ -f "$MANIFEST" ]] || {
  echo "Missing export manifest: $MANIFEST" >&2
  exit 1
}
DEST="$(
  python3 - "$DEST" <<'PY'
import pathlib
import sys

print(pathlib.Path(sys.argv[1]).expanduser().resolve())
PY
)"

python3 - "$REPO_ROOT" "$MANIFEST" "$DEST" "$DRY_RUN" <<'PY'
import pathlib
import re
import shutil
import sys

repo = pathlib.Path(sys.argv[1]).resolve()
manifest = pathlib.Path(sys.argv[2]).resolve()
destination = pathlib.Path(sys.argv[3]).resolve()
dry_run = sys.argv[4] == "true"

entries = []
for raw in manifest.read_text(encoding="utf-8").splitlines():
    value = raw.strip()
    if not value or value.startswith("#"):
        continue
    relative = pathlib.PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise SystemExit(f"unsafe export manifest entry: {value}")
    source = repo / relative
    if not source.exists():
        raise SystemExit(f"missing export manifest entry: {value}")
    if source.is_dir():
        entries.extend(
            path
            for path in source.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
    elif source.is_file() and not source.is_symlink():
        entries.append(source)

excluded_parts = {"__pycache__", ".pytest_cache"}
forbidden_names = {".DS_Store"}
files = []
for path in sorted(set(entries)):
    relative = path.relative_to(repo)
    if any(part in excluded_parts for part in relative.parts):
        continue
    if (
        path.name in forbidden_names
        or "transcript" in path.name.lower()
        or path.suffix in {".pyc", ".sqlite", ".sqlite3", ".db", ".log"}
    ):
        raise SystemExit(f"forbidden export file: {relative}")
    files.append(path)

patterns = [
    (re.compile(r"/Users/[A-Za-z0-9._-]+"), "personal macOS path"),
    (
        re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
        "email address",
    ),
    (re.compile(r"(?i)Bearer\s+[A-Za-z0-9._-]{12,}"), "bearer token"),
    (re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{12,}"), "API token"),
    (
        re.compile(r"(?m)^(?:MCP_AGENT_MAIL_TOKEN|HTTP_BEARER_TOKEN)=[^$<{\s][^\s]*$"),
        "embedded agent-mail token",
    ),
]
for path in files:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise SystemExit(f"binary file is not allowlisted: {path.relative_to(repo)}")
    for pattern, label in patterns:
        if pattern.search(text):
            raise SystemExit(f"{label} in export source: {path.relative_to(repo)}")

print(f"export allowlist: {len(files)} files")
for path in files:
    print(path.relative_to(repo))
if dry_run:
    raise SystemExit(0)
if destination.exists():
    if any(destination.iterdir()):
        raise SystemExit(f"destination is not empty: {destination}")
else:
    destination.mkdir(parents=True)
for path in files:
    target = destination / path.relative_to(repo)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)

sample = destination / "integrations" / "codex_app" / "env.sh.sample"
generated = sample.with_name("env.sh")
shutil.copy2(sample, generated)
generated.chmod(0o600)
PY

if [[ "$DRY_RUN" == true ]]; then
  echo "Dry-run complete: destination was not written."
  exit 0
fi

chmod +x \
  "$DEST/scripts/install-codex-app-integration.sh" \
  "$DEST/scripts/uninstall-codex-app-integration.sh" \
  "$DEST/scripts/doctor-codex-app-integration.sh" \
  "$DEST/scripts/export-component.sh" \
  "$DEST/scripts/run-codex-app-bridge.sh" \
  "$DEST/scripts/build-codex-app-marketplace.py" \
  "$DEST/integrations/codex_app/plugin/scripts/run-hook.sh" \
  "$DEST/integrations/codex_app/plugin/scripts/run-mcp.sh"

if [[ "$SKIP_INSTALL_CHECK" != true ]]; then
  # /private/tmp is macOS-only. On Linux it does not exist, so mktemp failed
  # and `set -e` ended the export gate with status 1 and nothing to read.
  gate_home="$(mktemp -d "${TMPDIR:-/tmp}/agentstack-codex-export-home.XXXXXX")"
  mkdir -p "$gate_home/.codex"
  HOME="$gate_home" CODEX_HOME="$gate_home/.codex" \
    AGENTSTACK_CODEX_APP_INSTALL_DIR="$gate_home/.agentstack/integrations/codex_app" \
    AGENTSTACK_CODEX_APP_RUNTIME_DIR="$gate_home/.agentstack/runtime/codex-app" \
    "$DEST/scripts/install-codex-app-integration.sh" \
      --dry-run \
      --no-service \
      --no-plugin \
      --project-key "$gate_home/project" \
      --agent-mail-url "http://127.0.0.1:8765/api/" \
      --agent-mail-env "$gate_home/mcp_agent_mail/.env" \
      --signals-dir "$gate_home/.mcp_agent_mail/signals"
  find "$gate_home" -type f -delete
  find "$gate_home" -depth -type d -empty -delete
fi

if [[ "$SKIP_TESTS" != true ]]; then
  [[ -x "$PYTEST_BIN" ]] || {
    echo "pytest executable is required for the export gate" >&2
    exit 1
  }
  (
    cd "$DEST"
    export PYTHONPATH="$DEST:$DEST/integrations/codex_app/src${PYTHONPATH:+:$PYTHONPATH}"
    "$PYTEST_BIN" -q \
      integrations/codex_app/tests \
      tests/test_codex_app_plugin_hooks.py \
      tests/test_codex_app_runtime_provider.py \
      tests/test_codex_app_schemas.py
  )
fi

live="${AGENTSTACK_CODEX_APP_INSTALL_DIR:-$HOME/.agentstack/integrations/codex_app}"
if [[ -d "$live" ]]; then
  echo "Live/source diff (diagnostic only):"
  git diff --no-index --stat \
    "$live" "$DEST/integrations/codex_app" || true
else
  echo "Live/source diff skipped: no live integration at $live"
fi

echo "Export complete: $DEST"
