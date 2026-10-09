#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MERGE_SETTINGS_SCRIPT="$SCRIPT_DIR/lib/merge_settings.py"
MERGE_CLAUDE_MCP_SCRIPT="$SCRIPT_DIR/lib/merge_claude_mcp.py"

DRY_RUN=false
RETIRE_LEGACY_MAIL=false
# What a re-run does to a running ORRERY Mail built from another commit:
#   keep   (default, --keep-mail) never switch; report the difference.
#   update (--update-mail) switch, or exit 1 saying why it did not happen.
#   auto   (AGENTSTACK_MAIL_UPDATE=auto, opt-in) switch when it can be done
#          safely -- managed deployment, verified offline, short expected
#          outage -- and otherwise keep the running build; never fails the run.
#          Not the default yet: an outage deadline covering a rollback, and a
#          check that the previous build still runs on the updated database,
#          come first (docs/agentstack-mail-update-design.md).
# An option wins over AGENTSTACK_MAIL_UPDATE. It is a per-run choice: env.sh
# never records it, so one choice does not stick to every later update.
MAIL_UPDATE_MODE="${AGENTSTACK_MAIL_UPDATE:-keep}"
MAIL_UPDATE_MODE_EXPLICIT="${AGENTSTACK_MAIL_UPDATE:+x}"
MAIL_UPDATE_MODE_FLAG=""
UPDATE_MAIL=false
PRINT_MAIL_PLAN=false
PRINT_MAIL_ADVICE=false
MAIL_UPDATE_PLANNED=false
MAIL_UPDATE_RESULT=""
MAIL_UPDATE_REASON=""
MAIL_UPDATE_BACKUP=""
# The machine-readable outcome (the final `mail-result:` line): what happened
# and a fixed reason code. setup.sh and update.sh read it instead of prose.
MAIL_RESULT=""
MAIL_RESULT_CODE=""
MAIL_UPDATE_START_SECONDS=""
MAIL_UPDATE_OUTAGE_SECONDS=""
LEGACY_MAIL_SCAN_COMPLETE=false
LEGACY_MAIL_DETECTED_LABELS=""
LEGACY_MAIL_RETIRE_PLANNED=false
ASSUME_YES="${AGENTSTACK_ASSUME_YES:-0}"
TIER="tier1"
TIER_OPTION=""
INSTALL_DIR="${AGENTSTACK_HOME:-$HOME/.agentstack}"
MAIL_DB_EXPLICIT="${AGENTSTACK_MAIL_DB+x}"
MAIL_ENV_EXPLICIT="${AGENTSTACK_MAIL_ENV+x}"
MAIL_HTTP_BEARER_MODE="disabled"
# Explicit values only; the rest come from the previous env.sh or the defaults
# once the options are parsed (see "setting resolution" below). Options record
# the env.sh name they set in OPTION_GIVEN, so an empty option value counts too.
OPTION_GIVEN=""
RESET_SETTINGS="${AGENTSTACK_RESET_SETTINGS:-0}"
PORT="${AGENTSTACK_PORT:-}"
LABEL_PREFIX="${AGENTSTACK_LABEL_PREFIX:-}"
TERMINAL="${AGENTSTACK_TERMINAL:-}"
AUTO_OPEN_CHILD_SETTING="${AGENTSTACK_AUTO_OPEN_CHILD:-}"
PROJECT_KEY="${AGENTSTACK_PROJECT_KEY:-${PROJECT_KEY:-}}"
PROTECTED_ROOTS="${AGENTSTACK_PROTECTED_ROOTS:-}"
DELIVERABLE_ROOTS="${AGENTSTACK_DELIVERABLE_ROOTS:-}"
LANG_SETTING="${AGENTSTACK_LANG:-}"
MURMUR_SETTING="${AGENTSTACK_MURMUR:-}"
# The Obsidian vault the dashboard falls back to and links Output items into.
VAULT_SETTING="${AGENTSTACK_VAULT:-}"
# NEW AGENT launch-directory presets. Resolved below: explicit > installed env.sh > empty.
SPAWN_DIRS_SETTING="${AGENTSTACK_SPAWN_DIRS:-}"
SPAWN_ROOTS_SETTING="${AGENTSTACK_SPAWN_ROOTS:-}"
# New worktrees live below the install root by default. An explicit environment
# override is persisted for shell launches and the dashboard service alike.
WORKTREE_ROOT_SETTING="${AGENTSTACK_WORKTREE_ROOT:-}"
# Codex child launch policy. Same lifecycle as the presets above: explicit >
# installed env.sh > product default (approval `never`, network on, no extra
# writable roots). Children run unattended, so the defaults avoid prompts
# nobody is there to answer.
CODEX_CHILD_APPROVAL_SETTING="${AGENTSTACK_CODEX_CHILD_APPROVAL:-}"
CODEX_CHILD_CONFIG_OVERLAY_SETTING="${AGENTSTACK_CODEX_CHILD_CONFIG_OVERLAY:-}"
CODEX_NETWORK_SETTING="${AGENTSTACK_CODEX_NETWORK:-}"
CHILD_DEFAULT_TOOLS_SETTING="${AGENTSTACK_CHILD_DEFAULT_TOOLS:-}"
CODEX_ADD_DIRS_SETTING="${AGENTSTACK_CODEX_ADD_DIRS:-}"
CHILD_RESUME_RETENTION_DAYS_SETTING="${AGENTSTACK_CHILD_RESUME_RETENTION_DAYS:-}"
# Where the Codex CLI lives. The dashboard runs under launchd / systemd with
# the minimal AGENTSTACK_PATH, which does not contain the per-user Node
# prefixes (nvm, nodebrew, ~/.npm-global) that `npm install -g` uses, so a
# NEW AGENT Codex spawn failed with "Codex CLI not found" while the same
# command worked from a shell (2026-09-08). Resolve it here, in the operator's
# shell, and persist it: explicit > installed env.sh > `command -v codex`.
CODEX_BIN_SETTING="${AGENTSTACK_CODEX_BIN:-}"
# Dashboard-only settings with the same lifecycle: read at install, persisted
# into env.sh and the service definition, inherited on re-install.
PORTRAITS_DIR_SETTING="${AGENTSTACK_PORTRAITS_DIR:-}"
CUSTOM_PORTRAITS_SETTING="${AGENTSTACK_CUSTOM_PORTRAITS:-}"
CLAUDE_MODELS_SETTING="${AGENTSTACK_CLAUDE_MODELS:-}"
CODEX_MODELS_SETTING="${AGENTSTACK_CODEX_MODELS:-}"
PYTHON_BIN="${AGENTSTACK_PYTHON:-}"
PYTHON_CHOICE="${AGENTSTACK_PYTHON:-}"
PYTHON_REQUESTED=""
PYTHON_INHERITED=""
PATH_VALUE="${AGENTSTACK_PATH:-}"
MCP_URL="${AGENTSTACK_MCP_URL:-}"

# These match packages/agentstack_mail/pyproject.toml. A regression test keeps
# the shell gate and package metadata in lock-step.
PYTHON_MIN_MAJOR=3
PYTHON_MIN_MINOR=11

# CI may bypass one preflight category at a time when it deliberately supplies
# a fake platform boundary. Skipping a check never supplies the dependency the
# rest of the installer actually needs.
PREFLIGHT_SKIP_OS="${AGENTSTACK_PREFLIGHT_SKIP_OS:-0}"
PREFLIGHT_SKIP_PYTHON="${AGENTSTACK_PREFLIGHT_SKIP_PYTHON:-0}"
PREFLIGHT_SKIP_COMMANDS="${AGENTSTACK_PREFLIGHT_SKIP_COMMANDS:-0}"
PREFLIGHT_SKIP_PORT="${AGENTSTACK_PREFLIGHT_SKIP_PORT:-0}"
PREFLIGHT_SKIP_WRITABLE="${AGENTSTACK_PREFLIGHT_SKIP_WRITABLE:-0}"

usage() {
  cat <<'EOF'
Usage: install.sh [--dry-run] [--dashboard-only|--scoped] [options]

Core install only. This creates ~/.agentstack, installs hooks/skills/dashboard assets,
creates env.sh and service files, and writes install-state.json. Tier1 shows a
Claude Code user-settings and MCP dry-run diffs and only merges after explicit approval
(an interactive yes, or a user-selected --assume-yes).
It does not modify shell dotfiles. After Tier1 preview and explicit approval,
it registers the fixed orrery-mail entry in ~/.claude.json and may update
only the managed marker block in project/global CLAUDE.md.

Options:
  --dry-run              Print planned actions without writing files
  -y, --assume-yes       Pre-approve MCP/settings/managed-block prompts only
  --dashboard-only       Tier0 footprint; install dashboard assets only
  --scoped               Tier2 placeholder; no user-settings merge
  --install-dir PATH     Default: ~/.agentstack
  --project-key PATH     Required on first install; existing env.sh is reused
  --port PORT            Default: existing env.sh, else 8770
  --label-prefix PREFIX  Default: existing env.sh, else org.agentstack
  --retire-legacy-mail   Retire a previous mail service found loaded (default:
                         report it and leave it running)
  --mail auto|update|keep
                         What to do with a running ORRERY Mail built from another
                         commit (default keep; AGENTSTACK_MAIL_UPDATE sets it too,
                         and this option wins). --update-mail and --keep-mail
                         are the same as --mail update and --mail keep
  --update-mail          Switch a running ORRERY Mail to this checkout's build,
                         or exit 1 saying why not: build and verify the
                         candidate on a scratch port, back up the database,
                         stop, start the new build, and restore the running
                         build if the new one is not healthy
  --keep-mail            Keep the running ORRERY Mail build even when this
                         checkout's build is newer (the default).
                         AGENTSTACK_MAIL_UPDATE=auto (opt-in) switches as
                         --update-mail does when the deployment is managed by
                         agentstack-mailctl, the candidate passes its check and
                         the expected outage is within
                         AGENTSTACK_MAIL_UPDATE_OUTAGE_BUDGET seconds (8), and
                         otherwise keeps the running build without failing
  --print-mail-update-advice
                         When the running ORRERY Mail is shown to be older
                         than this checkout's build (its commit is an ancestor
                         of this checkout's), print what that costs, the risk of
                         updating and the recovery (no update command). Reads
                         only; prints nothing otherwise; exit 0
  --print-mail-plan      Print one `mail-plan:` line saying what this run would
                         do to ORRERY Mail, then exit 0. Reads only; changes
                         nothing
  --terminal MODE        auto, ghostty, iterm, terminal, or none (default:
                         existing env.sh, else auto)
  --reset-settings       Do not inherit settings from the existing env.sh:
                         anything not given explicitly goes back to its
                         default. Kept even then: the project key, protected
                         roots, the ORRERY Mail state/service roots and
                         socket, the label prefix, the Mail launchd label and
                         the MCP URL. An empty explicit value (for example
                         AGENTSTACK_VAULT= or --codex-add-dirs "") resets
                         just that setting.
                         AGENTSTACK_RESET_SETTINGS=1 does the same.
  --spawn-dirs PATHS     ':'-separated NEW AGENT launch-directory presets
                         (absolute or ~; default: existing env.sh, else the project key then ~)
  --spawn-roots PATHS    ':'-separated roots the directory typeahead may
                         browse (default: existing env.sh, else $HOME)
  --codex-approval MODE  Codex child --ask-for-approval: never, on-request,
                         on-failure, untrusted (default: existing env.sh,
                         else never)
  --codex-child-overlay PATH
                         Absolute path to a TOML fragment merged over every
                         macOS/Linux Codex child's config.toml (default:
                         existing env.sh, else disabled)
  --child-default-tools SPEC  default child tools (same grammar as --tools; empty by default)
  --codex-network MODE   on or off: sandbox network access for Codex children
                         (default: existing env.sh, else on)
  --codex-bin PATH       Codex CLI executable used by the dashboard and child
                         launchers (default: existing env.sh, else `codex` on
                         this shell's PATH; empty when not installed)
  --codex-add-dirs PATHS ':'-separated extra writable roots for Codex children
                         on top of project, spawn dirs/roots, install dir,
                         worktrees, ~/.claude and ~/.codex (default: none)
  --child-resume-retention-days DAYS
                         Keep normal-finished Claude/Codex child resume credentials
                         for this many days (default: existing env.sh, else 30;
                         0 restores full deletion)
  -h, --help             Show this help

--assume-yes is not --force: validation and safety errors remain fatal. It must
be selected explicitly by the user; an agent or automation must not add it on
the user's behalf. AGENTSTACK_ASSUME_YES=1 provides the same explicit opt-in.
The bundled ORRERY Mail service uses port 18765 by default.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --retire-legacy-mail)
      # Explicit opt-in. Without it a previous mail service is reported and left
      # running: stopping a service someone is using is the operator's call.
      RETIRE_LEGACY_MAIL=true
      shift
      ;;
    --mail|--mail=*|--update-mail|--keep-mail)
      # --mail auto|update|keep; --update-mail and --keep-mail are aliases.
      case "$1" in
        --mail) [[ $# -ge 2 ]] || { echo "error: --mail needs auto, update or keep" >&2; exit 2; }
                wanted="$2"; shift ;;
        --mail=*) wanted="${1#--mail=}" ;;
        *) wanted="${1#--}"; wanted="${wanted%-mail}" ;;
      esac
      case "$wanted" in
        auto|update|keep) ;;
        *) echo "error: --mail must be auto, update or keep (got: $wanted)" >&2; exit 2 ;;
      esac
      if [[ -n "$MAIL_UPDATE_MODE_FLAG" && "$MAIL_UPDATE_MODE_FLAG" != "$wanted" ]]; then
        echo "error: conflicting ORRERY Mail options: --mail $MAIL_UPDATE_MODE_FLAG and --mail $wanted" >&2
        exit 2
      fi
      MAIL_UPDATE_MODE_FLAG="$wanted"
      shift
      ;;
    --print-mail-plan)
      PRINT_MAIL_PLAN=true
      shift
      ;;
    --print-mail-update-advice)
      PRINT_MAIL_ADVICE=true
      shift
      ;;
    -y|--assume-yes)
      ASSUME_YES=1
      shift
      ;;
    --dashboard-only)
      if [[ -n "$TIER_OPTION" && "$TIER_OPTION" != "dashboard-only" ]]; then
        echo "error: --dashboard-only and --scoped are mutually exclusive" >&2
        exit 2
      fi
      TIER="tier0"
      TIER_OPTION="dashboard-only"
      shift
      ;;
    --scoped)
      if [[ -n "$TIER_OPTION" && "$TIER_OPTION" != "scoped" ]]; then
        echo "error: --dashboard-only and --scoped are mutually exclusive" >&2
        exit 2
      fi
      TIER="tier2"
      TIER_OPTION="scoped"
      shift
      ;;
    --install-dir)
      INSTALL_DIR="$2"
      shift 2
      ;;
    --project-key)
      PROJECT_KEY="$2"
      PROTECTED_ROOTS="${AGENTSTACK_PROTECTED_ROOTS:-$PROJECT_KEY}"
      shift 2
      ;;
    --port)
      PORT="$2"
      OPTION_GIVEN="$OPTION_GIVEN AGENTSTACK_PORT"
      shift 2
      ;;
    --label-prefix)
      LABEL_PREFIX="$2"
      OPTION_GIVEN="$OPTION_GIVEN AGENTSTACK_LABEL_PREFIX"
      shift 2
      ;;
    --terminal)
      TERMINAL="$2"
      OPTION_GIVEN="$OPTION_GIVEN AGENTSTACK_TERMINAL"
      shift 2
      ;;
    --reset-settings)
      RESET_SETTINGS=1
      shift
      ;;
    --spawn-dirs)
      SPAWN_DIRS_SETTING="$2"
      OPTION_GIVEN="$OPTION_GIVEN AGENTSTACK_SPAWN_DIRS"
      shift 2
      ;;
    --spawn-roots)
      SPAWN_ROOTS_SETTING="$2"
      OPTION_GIVEN="$OPTION_GIVEN AGENTSTACK_SPAWN_ROOTS"
      shift 2
      ;;
    --codex-approval)
      CODEX_CHILD_APPROVAL_SETTING="$2"
      OPTION_GIVEN="$OPTION_GIVEN AGENTSTACK_CODEX_CHILD_APPROVAL"
      shift 2
      ;;
    --codex-child-overlay)
      CODEX_CHILD_CONFIG_OVERLAY_SETTING="$2"
      OPTION_GIVEN="$OPTION_GIVEN AGENTSTACK_CODEX_CHILD_CONFIG_OVERLAY"
      shift 2
      ;;
    --child-default-tools)
      if [[ $# -lt 2 ]]; then
        echo "error: --child-default-tools requires a value (empty clears it)" >&2
        exit 2
      fi
      CHILD_DEFAULT_TOOLS_SETTING="$2"
      OPTION_GIVEN="$OPTION_GIVEN AGENTSTACK_CHILD_DEFAULT_TOOLS"
      shift 2
      ;;
    --codex-network)
      CODEX_NETWORK_SETTING="$2"
      OPTION_GIVEN="$OPTION_GIVEN AGENTSTACK_CODEX_NETWORK"
      shift 2
      ;;
    --codex-add-dirs)
      CODEX_ADD_DIRS_SETTING="$2"
      OPTION_GIVEN="$OPTION_GIVEN AGENTSTACK_CODEX_ADD_DIRS"
      shift 2
      ;;
    --child-resume-retention-days)
      CHILD_RESUME_RETENTION_DAYS_SETTING="$2"
      OPTION_GIVEN="$OPTION_GIVEN AGENTSTACK_CHILD_RESUME_RETENTION_DAYS"
      shift 2
      ;;
    --codex-bin)
      CODEX_BIN_SETTING="$2"
      OPTION_GIVEN="$OPTION_GIVEN AGENTSTACK_CODEX_BIN"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

[[ -z "$MAIL_UPDATE_MODE_FLAG" ]] || { MAIL_UPDATE_MODE="$MAIL_UPDATE_MODE_FLAG"; MAIL_UPDATE_MODE_EXPLICIT=x; }
MAIL_KEEP_CODE=keep_default
[[ -z "$MAIL_UPDATE_MODE_EXPLICIT" ]] || MAIL_KEEP_CODE=keep_requested
case "$MAIL_UPDATE_MODE" in
  auto|keep) ;;
  update) UPDATE_MAIL=true ;;
  *) echo "error: AGENTSTACK_MAIL_UPDATE must be auto, update or keep (got: $MAIL_UPDATE_MODE)" >&2; exit 2 ;;
esac

# --print-mail-plan: one line for setup.sh's plan, before anything else runs.
# It reads the pidfile, the render it names and git; it never probes Codex,
# never needs a project key and always exits 0. A plan, not a promise: the
# real run still verifies the candidate and may keep the running build.
#   mail-plan: <switch|keep|unchanged|refuse|install> mode=<m> running=<id> to=<id> reason=<code>
print_mail_plan() {
  local service_root="${AGENTSTACK_MAIL_SERVICE_ROOT:-}" mcp_url="${AGENTSTACK_MCP_URL:-}" new_id
  new_id="${AGENTSTACK_MAIL_CANDIDATE_ID:-$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || true)}"
  python3 - "$INSTALL_DIR" "$service_root" "$mcp_url" "$REPO_ROOT" "${new_id:-source}" \
    "$MAIL_UPDATE_MODE" "${AGENTSTACK_MAIL_PACKAGE_SOURCE:-$REPO_ROOT/packages/agentstack_mail}" "$MAIL_KEEP_CODE" <<'PY' 2>/dev/null \
    || echo "mail-plan: refuse mode=$MAIL_UPDATE_MODE running= to=${new_id:-source} reason=plan_unavailable"
import json, os, pathlib, re, shlex, socket, subprocess, sys
from urllib.parse import urlparse

install_dir, service_root, mcp_url, repo, new_id, mode, package, keep_code = sys.argv[1:9]


def saved(name):
    try:
        lines = pathlib.Path(install_dir, "env.sh").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return ""
    for line in lines:
        match = re.match(rf"^\s*export\s+{name}=(.*)$", line)
        if match:
            try:
                parts = shlex.split(match.group(1), comments=True, posix=True)
            except ValueError:
                return ""
            return parts[0] if len(parts) == 1 else ""
    return ""


def emit(action, running, reason):
    print(f"mail-plan: {action} mode={mode} running={running} to={new_id} reason={reason}")
    raise SystemExit(0)


root = pathlib.Path(service_root or saved("AGENTSTACK_MAIL_DIR") or pathlib.Path(install_dir, "mail-service"))
url = urlparse(mcp_url or saved("AGENTSTACK_MCP_URL") or "http://127.0.0.1:18765/mcp")
try:
    pid_text, runner = (pathlib.Path(root, "runtime", "agentstack-mail.pid").read_text().splitlines() + ["", ""])[:2]
    os.kill(int(pid_text.split()[0]), 0)
except (OSError, ValueError, IndexError):
    runner = ""
if not runner:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        listening = probe.connect_ex((url.hostname or "127.0.0.1", url.port or 80)) == 0
    if not listening:
        emit("install", "", "no_running_mail")
    emit("keep" if mode == "keep" else "refuse", "", "deployment_unidentified")
render = pathlib.Path(runner).parent
try:
    running = json.loads((render / "deployment.json").read_text(encoding="utf-8")).get("source_id") or ""
except (OSError, ValueError, AttributeError):
    running = ""
running = running or render.name.rsplit("-", 1)[0]
if running == new_id:
    emit("unchanged", running, "same_candidate")


def tree(commit):
    if pathlib.Path(package).resolve() != pathlib.Path(repo, "packages", "agentstack_mail").resolve():
        return None
    done = subprocess.run(["git", "-C", repo, "rev-parse", "-q", "--verify", f"{commit}^{{commit}}:packages/agentstack_mail"],
                          capture_output=True, text=True)
    return done.stdout.strip() or None if done.returncode == 0 else None


old_tree = tree(running)
if old_tree is not None and old_tree == tree(new_id):
    emit("unchanged", running, "same_package")
emit("keep" if mode == "keep" else "switch", running, keep_code if mode == "keep" else "newer_build")
PY
}
if [[ "$PRINT_MAIL_PLAN" == true ]]; then
  print_mail_plan
  exit 0
fi
# --print-mail-update-advice: the "Mail is out of date" notice (what does not
# work, the risk of updating, the /mcp recovery) without the update command,
# for setup.sh to show next to its own. Reads only; always exits 0; prints
# nothing unless the running Mail is shown to be older: its commit is an
# ancestor of this checkout's. (A missing feature alone does not show that
# this checkout's build has it; doctor reports that case.)
if [[ "$PRINT_MAIL_ADVICE" == true ]]; then
  mail_plan_line="$(MAIL_UPDATE_MODE=update print_mail_plan)"
  case "$mail_plan_line" in
    "mail-plan: switch "*)
      plan_running="$(sed -n 's/.* running=\([^ ]*\).*/\1/p' <<< "$mail_plan_line")"
      plan_to="$(sed -n 's/.* to=\([^ ]*\).*/\1/p' <<< "$mail_plan_line")"
      plan_url="${AGENTSTACK_MCP_URL:-$(sed -n 's/^export AGENTSTACK_MCP_URL=//p' "$INSTALL_DIR/env.sh" 2>/dev/null | tr -d "\"'")}"
      python3 "$SCRIPT_DIR/lib/mail_update_notice.py" stale --without-how --only-if-stale \
        --repo "$REPO_ROOT" --running "$plan_running" \
        --to "$plan_to" ${plan_url:+--mcp-url "$plan_url"} 2>/dev/null || true
      ;;
  esac
  exit 0
fi

PROJECT_CONTEXT_LIB="$REPO_ROOT/hooks/project-context.sh"
if [[ ! -f "$PROJECT_CONTEXT_LIB" ]]; then
  echo "error: missing project context resolver: $PROJECT_CONTEXT_LIB" >&2
  exit 2
fi
# shellcheck disable=SC1090
. "$PROJECT_CONTEXT_LIB"
PROJECT_KEY_INPUT="$PROJECT_KEY"
PROJECT_KEY="$(agentstack_resolve_project_key "" "$INSTALL_DIR/env.sh" 0)"
if [[ -z "$PROJECT_KEY" ]]; then
  echo "error: project key is required on first install; pass --project-key /absolute/path/to/project or set AGENTSTACK_PROJECT_KEY" >&2
  exit 2
fi
PROTECTED_ROOTS="$(agentstack_resolve_protected_roots "$PROJECT_KEY" "$PROJECT_KEY_INPUT" "$INSTALL_DIR/env.sh")"
case "$RESET_SETTINGS" in
  0|"") RESET_SETTINGS=0 ;;
  1) ;;
  *) echo "error: AGENTSTACK_RESET_SETTINGS must be 0 or 1 (got: $RESET_SETTINGS)" >&2; exit 2 ;;
esac
# --- setting resolution (tests extract from here to the end marker) ---
# Every setting a person chooses: explicit (an option or the environment) > the
# previous env.sh > the product default, in the order hooks/project-context.sh
# defines. The dashboard runs under launchd/systemd, so a shell `export` never
# reaches it and a re-install that forgot a setting silently undid it (#137).
#
# - Explicit includes an empty value: `AGENTSTACK_VAULT=` or `--codex-add-dirs ""`
#   puts that one setting back to its default.
# - env.sh writes every value out, defaults included, so it also records which
#   were chosen (AGENTSTACK_CHOSEN_SETTINGS). Only those are inherited; a default
#   written out follows the current default next time. An env.sh from before
#   that record counts a value as chosen when it differs from today's default,
#   except PATH and Python, which the installer always worked out itself.
# - A value in the environment equal to what env.sh recorded is an echo of
#   env.sh (cockpit's update.sh and some shell profiles source it), not a new
#   choice: it keeps the status it had. An option is always a request. Only
#   an env.sh with the record can be echoed; before it, the value is explicit.
# - --reset-settings drops the previous choices. What names the data and the
#   services running on it (project key, protected roots, the Mail state and
#   service roots and socket, the label prefix, the Mail launchd label, the MCP
#   URL) is kept: resetting those left the old Mail job running on the same
#   database next to a new one (review of #146).
SETTINGS_ENV_FILE="$INSTALL_DIR/env.sh"
CHOSEN_SETTINGS=""
SETTING_SOURCE=""
PREVIOUS_CHOSEN=""
PREVIOUS_CHOSEN_RECORDED=false
if [[ -f "$SETTINGS_ENV_FILE" ]] && grep -q '^export AGENTSTACK_CHOSEN_SETTINGS=' "$SETTINGS_ENV_FILE"; then
  PREVIOUS_CHOSEN_RECORDED=true
  PREVIOUS_CHOSEN="$(agentstack_installed_env_value AGENTSTACK_CHOSEN_SETTINGS "$SETTINGS_ENV_FILE")"
fi

setting_listed() {
  case " $2 " in *" $1 "*) return 0 ;; esac
  return 1
}

setting_previously_chosen() {
  local name="$1" saved="$2" default="$3"
  if [[ "$PREVIOUS_CHOSEN_RECORDED" == true ]]; then
    setting_listed "$name" "$PREVIOUS_CHOSEN"
    return
  fi
  case "$name" in
    AGENTSTACK_PATH|AGENTSTACK_PYTHON) return 1 ;;
  esac
  [[ "$saved" != "$default" ]]
}

# resolve_setting VAR NAME DEFAULT [MODE [ENV_NAME]]
#   VAR holds the explicit value on entry (from ENV_NAME, default NAME, or an
#   option) and the value in effect on return; SETTING_SOURCE says which of
#   explicit / saved / default it is. MODE `kept` inherits the saved value even
#   under --reset-settings and whether or not it was chosen; `found` inherits
#   it, chosen or not, unless reset (a location the installer found itself).
resolve_setting() {
  local var="$1" name="$2" default="${3:-}" mode="${4:-chosen}" env_name="${5:-$2}"
  local value="${!1:-}" saved given=""
  saved="$(agentstack_installed_env_value "$name" "$SETTINGS_ENV_FILE")"
  if setting_listed "$env_name" "$OPTION_GIVEN"; then
    given=option
  elif [[ -n "${!env_name+x}" ]]; then
    given=environment
    # Only against a record of choices: before it, a value someone passes on
    # every install looks the same as an echo, and taking it for one sent it
    # back to the default and flipped it on the next install (#146, N-1).
    if [[ "$PREVIOUS_CHOSEN_RECORDED" == true && -n "$saved" && "$value" == "$saved" ]]; then
      given=""
    fi
  fi
  if [[ -n "$given" ]]; then
    if [[ -n "$value" ]]; then
      SETTING_SOURCE=explicit
      CHOSEN_SETTINGS="${CHOSEN_SETTINGS:+$CHOSEN_SETTINGS }$name"
    else
      value="$default"
      SETTING_SOURCE=default
    fi
  elif [[ -n "$saved" ]] && {
    [[ "$mode" == kept ]] ||
      { [[ "$RESET_SETTINGS" != 1 ]] &&
        { [[ "$mode" == found ]] || setting_previously_chosen "$name" "$saved" "$default"; }; }
  }; then
    value="$saved"
    SETTING_SOURCE=saved
    # Kept or found without being chosen stays unchosen in the record.
    if [[ "$mode" == chosen ]] || setting_previously_chosen "$name" "$saved" "$default"; then
      CHOSEN_SETTINGS="${CHOSEN_SETTINGS:+$CHOSEN_SETTINGS }$name"
    fi
  else
    value="$default"
    SETTING_SOURCE=default
  fi
  printf -v "$var" '%s' "$value"
}
# --- end setting resolution ---

resolve_setting PORT AGENTSTACK_PORT 8770
resolve_setting LABEL_PREFIX AGENTSTACK_LABEL_PREFIX org.agentstack kept
LABEL_PREFIX_SOURCE="$SETTING_SOURCE"
resolve_setting TERMINAL AGENTSTACK_TERMINAL auto
resolve_setting MCP_URL AGENTSTACK_MCP_URL http://127.0.0.1:18765/mcp kept
resolve_setting PATH_VALUE AGENTSTACK_PATH /opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
# An explicit AGENTSTACK_PYTHON must work or the install stops; one recorded
# before is only tried first, and select_python searches again without it.
resolve_setting PYTHON_CHOICE AGENTSTACK_PYTHON ""
case "$SETTING_SOURCE" in
  explicit) PYTHON_REQUESTED="$PYTHON_CHOICE" ;;
  saved) PYTHON_INHERITED="$PYTHON_CHOICE" ;;
esac
resolve_setting DELIVERABLE_ROOTS AGENTSTACK_DELIVERABLE_ROOTS
resolve_setting LANG_SETTING AGENTSTACK_LANG
resolve_setting MURMUR_SETTING AGENTSTACK_MURMUR
resolve_setting VAULT_SETTING AGENTSTACK_VAULT
resolve_setting AUTO_OPEN_CHILD_SETTING AGENTSTACK_AUTO_OPEN_CHILD 1
resolve_setting SPAWN_DIRS_SETTING AGENTSTACK_SPAWN_DIRS
resolve_setting SPAWN_ROOTS_SETTING AGENTSTACK_SPAWN_ROOTS
resolve_setting WORKTREE_ROOT_SETTING AGENTSTACK_WORKTREE_ROOT "$INSTALL_DIR/worktrees"
resolve_setting CODEX_CHILD_APPROVAL_SETTING AGENTSTACK_CODEX_CHILD_APPROVAL never
resolve_setting CODEX_CHILD_CONFIG_OVERLAY_SETTING AGENTSTACK_CODEX_CHILD_CONFIG_OVERLAY
resolve_setting CODEX_NETWORK_SETTING AGENTSTACK_CODEX_NETWORK on
resolve_setting CHILD_DEFAULT_TOOLS_SETTING AGENTSTACK_CHILD_DEFAULT_TOOLS
resolve_setting CODEX_ADD_DIRS_SETTING AGENTSTACK_CODEX_ADD_DIRS
resolve_setting CHILD_RESUME_RETENTION_DAYS_SETTING AGENTSTACK_CHILD_RESUME_RETENTION_DAYS 30
# --- codex launcher resolution (tests extract from here to the end marker) ---
# Under WSL, PATH also carries the Windows PATH (/mnt/c/...). A `codex` found
# there is the Windows npm shim: run by the Linux node it dies at once with
# "Missing optional dependency @openai/codex-linux-x64", and every Codex child
# spawned from the dashboard ends before its prompt arrives. A candidate is
# therefore used only if it is not under a Windows drive mount (WSL only) and
# answers `--version` within a short time.
WSL_WINDOWS_MOUNT_ROOT=/mnt
CODEX_VERSION_TIMEOUT_SECONDS=10

running_under_wsl() {
  [[ -r /proc/version ]] && grep -qi microsoft /proc/version 2>/dev/null
}

# Succeeds when `$1 --version` exits 0 within the timeout (portable: no
# coreutils `timeout` on macOS).
codex_version_answers() {
  local bin="$1" out="${2:-/dev/null}" pid tick=0 limit=$((CODEX_VERSION_TIMEOUT_SECONDS * 10))
  local err status=0
  # Why it failed, for codex_version_failure: a probe that ran out of time and
  # one that exited at once (a wrong node on PATH dies in a few hundredths of a
  # second) need different fixes, and used to read the same.
  CODEX_VERSION_STATUS=""
  CODEX_VERSION_ERROR=""
  CODEX_VERSION_ELAPSED=""
  err="$(mktemp "${TMPDIR:-/tmp}/agentstack-codex-stderr.XXXXXX" 2>/dev/null || true)"
  "$bin" --version </dev/null >"$out" 2>"${err:-/dev/null}" &
  pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    if [[ "$tick" -ge "$limit" ]]; then
      codex_probe_stop "$pid"
      CODEX_VERSION_STATUS=timeout
      [[ -n "$err" ]] && rm -f "$err"
      return 1
    fi
    sleep 0.1
    tick=$((tick + 1))
  done
  wait "$pid" || status=$?
  CODEX_VERSION_STATUS="$status"
  CODEX_VERSION_ELAPSED="$((tick / 10)).$((tick % 10))"
  if [[ -n "$err" ]]; then
    # First non-blank line, bounded: it is shown inside a one-line warning.
    CODEX_VERSION_ERROR="$(sed -n '/[^[:space:]]/{p;q;}' "$err" 2>/dev/null | cut -c1-200 || true)"
    rm -f "$err"
  fi
  return "$status"
}

# One line saying why the last codex_version_answers failed.
codex_version_failure() {
  local bin="$1"
  if [[ "$CODEX_VERSION_STATUS" == timeout ]]; then
    echo "'$bin --version' did not finish within ${CODEX_VERSION_TIMEOUT_SECONDS}s and was stopped"
  else
    echo "'$bin --version' exited with status ${CODEX_VERSION_STATUS:-unknown} after ${CODEX_VERSION_ELAPSED:-0.0}s: ${CODEX_VERSION_ERROR:-(no error output)}"
  fi
}

# Stop a probe that overran. Every wait here is a kill -0 poll with a
# deadline (one second of grace, one second for the probe to go after KILL),
# so waiting on `codex --version` and on the cleanup after it is bounded. The
# system commands used on the way (pgrep, ps) are not bounded: if they
# themselves stop responding, so does this function.
#
# The probe itself ($!) is this shell's own child, so it gets TERM and then
# KILL without further checks. Its descendants (an npm wrapper's native codex)
# are recorded first, while they are still attached to it, because a wrapper
# that exits on TERM orphans them out of reach of `pgrep -P`. They get TERM
# too; a descendant seen gone during the grace period is dropped, and KILL goes
# only to one whose `ps -o lstart=` start time (one-second resolution) still
# matches what was recorded. This is a best-effort identity check: a PID reused
# within the same second is not told apart. A still-running descendant whose
# start time cannot be read or no longer matches is left without KILL, with a
# note on stderr; one that received KILL is not checked again. A probe still present after its second of reaping is left with a note.
codex_probe_stop() {
  local pid="$1" level="$1" descendants="" next p t start depth round
  # PID lists are space-separated; a caller may have narrowed IFS (the PATH
  # scan in find_usable_codex_bin splits on ":").
  local IFS=$' \t\n'
  if command -v pgrep >/dev/null 2>&1; then
    for depth in 1 2 3 4; do
      next=""
      for p in $level; do
        next="$next $(pgrep -P "$p" 2>/dev/null | tr '\n' ' ' || true)"
      done
      next="$(echo $next)"
      [[ -n "$next" ]] || break
      for p in $next; do
        descendants="$descendants $p/$(codex_process_start "$p")"
      done
      level="$next"
    done
  fi
  kill -TERM "$pid" 2>/dev/null || true
  for t in $descendants; do
    kill -TERM "${t%%/*}" 2>/dev/null || true
  done
  # One second of grace for everything to exit on TERM.
  for round in 1 2 3 4 5 6 7 8 9 10; do
    next=""
    for t in $descendants; do
      kill -0 "${t%%/*}" 2>/dev/null && next="$next $t"
    done
    descendants="$(echo $next)"
    if [[ -z "$descendants" ]] && ! kill -0 "$pid" 2>/dev/null; then
      break
    fi
    sleep 0.1
  done
  kill -KILL "$pid" 2>/dev/null || true
  for t in $descendants; do
    p="${t%%/*}"
    start="${t#*/}"
    if [[ -n "$start" && "$(codex_process_start "$p")" == "$start" ]]; then
      kill -KILL "$p" 2>/dev/null || true
    elif kill -0 "$p" 2>/dev/null; then
      echo "note: left process $p from the codex --version probe (its identity could not be confirmed)" >&2
    fi
  done
  # Reap the probe only once it is gone; never block on it.
  for round in 1 2 3 4 5 6 7 8 9 10; do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.1
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "note: the codex --version probe (pid $pid) did not exit after KILL; leaving it" >&2
  else
    wait "$pid" 2>/dev/null || true
  fi
}

# The process start time as one word (empty when ps cannot tell), used to
# recognise the same process again under the same PID.
codex_process_start() {
  ps -o lstart= -p "$1" 2>/dev/null | tr -d ' \n' || true
}

# Prints why a codex candidate cannot be used; prints nothing when it can.
codex_bin_problem() {
  local bin="$1"
  if running_under_wsl && [[ "$bin" == "$WSL_WINDOWS_MOUNT_ROOT"/?/* ]]; then
    echo "it is a Windows install under $WSL_WINDOWS_MOUNT_ROOT (install Codex inside WSL; the Windows npm shim cannot run here)"
    return 0
  fi
  if [[ ! -x "$bin" ]]; then
    echo "it is not executable"
    return 0
  fi
  if ! codex_version_answers "$bin"; then
    codex_version_failure "$bin"
  fi
}

# First usable `codex` on PATH, then the per-user install locations that
# hooks/spawn_child.sh also searches (a fresh WSL shell may not have
# ~/.npm-global/bin on PATH yet).
find_usable_codex_bin() {
  local search="$PATH:$HOME/.local/bin:$HOME/.npm-global/bin:$HOME/.nodebrew/current/bin:/opt/homebrew/bin:/usr/local/bin"
  local dir candidate seen=":"
  local IFS=:
  for dir in $search; do
    [[ -n "$dir" && "$seen" != *":$dir:"* ]] || continue
    seen="$seen$dir:"
    candidate="$dir/codex"
    [[ -f "$candidate" || -L "$candidate" ]] || continue
    if [[ -z "$(codex_bin_problem "$candidate")" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 0
}
# --- end codex launcher resolution ---

resolve_setting CODEX_BIN_SETTING AGENTSTACK_CODEX_BIN "" found
if [[ "$SETTING_SOURCE" != explicit ]]; then
  if [[ -n "$CODEX_BIN_SETTING" ]]; then
    # A stale path from an earlier install (Node upgraded, prefix moved, or a
    # Windows shim picked up under WSL) must not pin the dashboard to a binary
    # that cannot run. An explicit --codex-bin that cannot run is rejected
    # below instead.
    codex_stale_reason="$(codex_bin_problem "$CODEX_BIN_SETTING")"
    if [[ -n "$codex_stale_reason" ]]; then
      echo "note: installed AGENTSTACK_CODEX_BIN=$CODEX_BIN_SETTING is stale ($codex_stale_reason); resolving codex again" >&2
      CODEX_BIN_SETTING=""
    fi
  fi
  if [[ -z "$CODEX_BIN_SETTING" ]]; then
    CODEX_BIN_SETTING="$(find_usable_codex_bin)"
  fi
elif codex_explicit_reason="$(codex_bin_problem "$CODEX_BIN_SETTING")" && [[ -n "$codex_explicit_reason" ]]; then
  echo "error: --codex-bin / AGENTSTACK_CODEX_BIN cannot be used: $CODEX_BIN_SETTING ($codex_explicit_reason)" >&2
  exit 2
fi
# A Node-installed `codex` is a wrapper that loads its platform package through
# whichever `node` is first on PATH; under the service's own PATH that is a
# different Node than the one it was installed under, and the wrapper dies with
# "Missing optional dependency". Putting the binary's own directory first keeps
# it with its runtime — the service otherwise cannot read Codex usage at all.
if [[ -n "$CODEX_BIN_SETTING" ]]; then
  codex_bin_dir="$(cd "$(dirname "$CODEX_BIN_SETTING")" 2>/dev/null && pwd -P || true)"
  if [[ -n "$codex_bin_dir" && ":$PATH_VALUE:" != *":$codex_bin_dir:"* ]]; then
    PATH_VALUE="$codex_bin_dir:$PATH_VALUE"
  fi
fi
# Product defaults are written out explicitly so env.sh, the service definition
# and install-state.json all say what a child actually gets.
AUTO_OPEN_CHILD_SETTING="${AUTO_OPEN_CHILD_SETTING:-1}"
CODEX_CHILD_APPROVAL_SETTING="${CODEX_CHILD_APPROVAL_SETTING:-never}"
CODEX_NETWORK_SETTING="${CODEX_NETWORK_SETTING:-on}"
CHILD_RESUME_RETENTION_DAYS_SETTING="${CHILD_RESUME_RETENTION_DAYS_SETTING:-30}"
resolve_setting PORTRAITS_DIR_SETTING AGENTSTACK_PORTRAITS_DIR
resolve_setting CUSTOM_PORTRAITS_SETTING AGENTSTACK_CUSTOM_PORTRAITS
# An empty AGENTSTACK_CLAUDE_MODELS is itself a choice: the built-in list.
resolve_setting CLAUDE_MODELS_SETTING AGENTSTACK_CLAUDE_MODELS
resolve_setting CODEX_MODELS_SETTING AGENTSTACK_CODEX_MODELS

# Pass the new settings as data, never interpolate them into Python source.
export AGENTSTACK_CLAUDE_MODELS="$CLAUDE_MODELS_SETTING"

HOOKS_DIR="$INSTALL_DIR/hooks"
SKILLS_DIR="$INSTALL_DIR/skills"
DASHBOARD_DIR="$INSTALL_DIR/dashboard"
BIN_DIR="$INSTALL_DIR/bin"
RUNTIME_DIR="$INSTALL_DIR/runtime"
CONNECTIONS_DIR="$INSTALL_DIR/connections"
PERSISTENT_PROFILES_DIR="$INSTALL_DIR/profiles"
BACKUPS_DIR="$INSTALL_DIR/backups"
ENV_FILE="$INSTALL_DIR/env.sh"
MANIFEST="$INSTALL_DIR/install-state.json"
CLAUDE_SETTINGS="${AGENTSTACK_CLAUDE_SETTINGS:-$HOME/.claude/settings.json}"
# Not inherited: a sandbox override for tests and trials, not a setting. A later
# plain install writes the real ~/.claude.json (review of #146).
CLAUDE_JSON="${AGENTSTACK_CLAUDE_JSON:-$HOME/.claude.json}"
CLAUDE_SKILLS_DIR="$HOME/.claude/skills"
CODEX_SKILLS_DIR="${CODEX_HOME:-$HOME/.codex}/skills"
SAFE_MERGE_RESULT_FILE="$RUNTIME_DIR/settings-merge-result.json"
MCP_MERGE_RESULT_FILE="$RUNTIME_DIR/claude-mcp-merge-result.json"
MAIL_DB="${AGENTSTACK_MAIL_DB:-}"
MANAGED_AGENTS_FILE="${AGENTSTACK_MANAGED_AGENTS_FILE:-}"
resolve_setting MANAGED_AGENTS_FILE AGENTSTACK_MANAGED_AGENTS_FILE "$RUNTIME_DIR/managed_agents.txt"
DASHBOARD_LOG="${AGENTSTACK_DASHBOARD_LOG:-}"
resolve_setting DASHBOARD_LOG AGENTSTACK_DASHBOARD_LOG "$RUNTIME_DIR/dashboard.log"
DASHBOARD_LOG_MAX_BYTES="${AGENTSTACK_DASHBOARD_LOG_MAX_BYTES:-}"
resolve_setting DASHBOARD_LOG_MAX_BYTES AGENTSTACK_DASHBOARD_LOG_MAX_BYTES 5242880
DASHBOARD_LOG_BACKUPS="${AGENTSTACK_DASHBOARD_LOG_BACKUPS:-}"
resolve_setting DASHBOARD_LOG_BACKUPS AGENTSTACK_DASHBOARD_LOG_BACKUPS 3
DASHBOARD_RESTART_DELAY="${AGENTSTACK_DASHBOARD_RESTART_DELAY:-}"
resolve_setting DASHBOARD_RESTART_DELAY AGENTSTACK_DASHBOARD_RESTART_DELAY 5
LABEL="$LABEL_PREFIX.agentdashboard"
URL="http://127.0.0.1:$PORT/"
ACTIVE_SERVICE_KIND=""
SERVICE_PATH=""
SERVICE_HEALTHY=false
SERVICE_FALLBACK_USED=false
EXISTING_AGENT_MAIL_SERVER=false
AGENT_MAIL_RUNNER=""
AGENT_MAIL_PIDFILE=""
AGENT_MAIL_LOG=""
AGENT_MAIL_SERVICE_KIND=""
AGENT_MAIL_SERVICE_PATH=""
# Login-time autostart for ORRERY Mail. `agentstack-mailctl start` daemonizes
# with nohup, which does not survive a reboot: the dashboard has had a launchd /
# systemd unit since day one, mail never did. A machine that reboots therefore
# came back with a dashboard and no mail server.
MAIL_AUTOSTART_LABEL="$LABEL_PREFIX.mail"
# Only a deliberately scoped install pins its own service label; leaving it
# empty keeps the historical default for everybody else, whose running job was
# registered under that name long before this setting existed.
# The label the previous install registered is kept (even under reset: it
# names the running job), unless the prefix it came from was given explicitly.
# An operator who named the label keeps it. Deriving one from the prefix would
# point this install at a job nobody registered under that name.
MAIL_LAUNCHD_LABEL_SETTING="${AGENTSTACK_MAIL_LAUNCHD_LABEL:-}"
resolve_setting MAIL_LAUNCHD_LABEL_SETTING AGENTSTACK_MAIL_LAUNCHD_LABEL "" kept
if [[ "$SETTING_SOURCE" == saved && "$LABEL_PREFIX_SOURCE" == explicit ]]; then
  MAIL_LAUNCHD_LABEL_SETTING=""
fi
if [[ -n "$MAIL_LAUNCHD_LABEL_SETTING" ]]; then
  :
elif [[ "$LABEL_PREFIX" == "org.agentstack" ]]; then
  MAIL_LAUNCHD_LABEL_SETTING=""
else
  MAIL_LAUNCHD_LABEL_SETTING="$LABEL_PREFIX.mail-service"
fi
AGENT_MAIL_AUTOSTART_KIND=""
AGENT_MAIL_AUTOSTART_PATH=""
# systemd needs a second file (service + timer); launchd does it in one plist.
AGENT_MAIL_AUTOSTART_SERVICE_PATH=""
# The Mail watcher turns signal files into tmux prompt injections. Until
# 2026-09-07 only `agent-start` and the Codex bootstrap started it (a detached
# tmux session), so a host whose agents were all spawned from the dashboard had
# signals piling up and nothing delivering them (WSL2 after `wsl --shutdown`:
# watcher_running=false, six signals stuck, agents never notified).
MAIL_WATCHER_LABEL="$LABEL_PREFIX.mail-watcher"
AGENT_MAIL_WATCHER_KIND=""
AGENT_MAIL_WATCHER_PATH=""
NATIVE_MAIL_EXISTING=false
PROVISION_NATIVE_MAIL=false
NATIVE_MAIL_DEPLOYMENT_IDENTIFIED=true
NATIVE_MAIL_ENROLL_AVAILABLE=true
NATIVE_MAIL_AUTOSTART_MANAGED_BY_INSTALL=true
# Where ORRERY Mail keeps its database. Falling back to the default here would
# quietly start an empty one, so the previous install's roots are inherited even
# under --reset-settings. env.sh records the service root as AGENTSTACK_MAIL_DIR.
NATIVE_MAIL_STATE_ROOT="${AGENTSTACK_MAIL_STATE_ROOT:-}"
resolve_setting NATIVE_MAIL_STATE_ROOT AGENTSTACK_MAIL_STATE_ROOT "$HOME/.agentstack/mail" kept
MAIL_STATE_ROOT_SOURCE="$SETTING_SOURCE"
NATIVE_MAIL_SERVICE_ROOT="${AGENTSTACK_MAIL_SERVICE_ROOT:-}"
resolve_setting NATIVE_MAIL_SERVICE_ROOT AGENTSTACK_MAIL_DIR "$INSTALL_DIR/mail-service" kept \
  AGENTSTACK_MAIL_SERVICE_ROOT
NATIVE_MAIL_PACKAGE_SOURCE="${AGENTSTACK_MAIL_PACKAGE_SOURCE:-$REPO_ROOT/packages/agentstack_mail}"
NATIVE_MAIL_SOURCE_ID="${AGENTSTACK_MAIL_CANDIDATE_ID:-}"
if [[ -z "$NATIVE_MAIL_SOURCE_ID" ]]; then
  NATIVE_MAIL_SOURCE_ID="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || true)"
  NATIVE_MAIL_SOURCE_ID="${NATIVE_MAIL_SOURCE_ID:-source}"
fi
NATIVE_MAIL_VENV_EXPLICIT="${AGENTSTACK_MAIL_SERVICE_VENV+x}"
NATIVE_MAIL_VENV="${AGENTSTACK_MAIL_SERVICE_VENV:-$NATIVE_MAIL_SERVICE_ROOT/candidates/$NATIVE_MAIL_SOURCE_ID/venv}"
NATIVE_MAIL_ENROLL_BIN="$NATIVE_MAIL_VENV/bin/agentstack-enroll"
NATIVE_MAIL_ENV_EXPLICIT="${AGENTSTACK_MAIL_SERVICE_ENV+x}"
NATIVE_MAIL_ENV="${AGENTSTACK_MAIL_SERVICE_ENV:-$NATIVE_MAIL_SERVICE_ROOT/renders/pending/service.env}"
NATIVE_MAIL_RUNNER="$(dirname "$NATIVE_MAIL_ENV")/run-agentstack-mail.sh"
NATIVE_MAIL_DEPLOYMENT="$(dirname "$NATIVE_MAIL_ENV")/deployment.json"
NATIVE_MAIL_PIDFILE="$NATIVE_MAIL_SERVICE_ROOT/runtime/agentstack-mail.pid"
NATIVE_MAIL_LOG="$NATIVE_MAIL_SERVICE_ROOT/runtime/agentstack-mail.log"
# Empty means "derive it from the state root" (below). A socket the previous
# install recorded belongs with that install's state root, so it is inherited
# like the roots (even under --reset-settings), unless the state root itself was
# given explicitly: a socket derived for another root would be shared by two.
NATIVE_MAIL_MANAGEMENT_SOCKET="${AGENTSTACK_MAIL_MANAGEMENT_SOCKET:-}"
resolve_setting NATIVE_MAIL_MANAGEMENT_SOCKET AGENTSTACK_MAIL_MANAGEMENT_SOCKET "" kept
MAIL_SOCKET_SOURCE="$SETTING_SOURCE"
if [[ "$SETTING_SOURCE" == saved && "$MAIL_STATE_ROOT_SOURCE" == explicit ]]; then
  NATIVE_MAIL_MANAGEMENT_SOCKET=""
  MAIL_SOCKET_SOURCE=default
fi
AGENT_MAIL_NAME_CAPABILITY_JSON='{"status":"unknown","evidence":"not-inspected","enforcement_mode":"unknown","mail_dir":"","detail":"installer has not inspected ORRERY Mail naming source","warning":"requested-name handling is unknown"}'
PREFLIGHT_OS=""
PREFLIGHT_ERRORS=()
PYTHON_SELECTION_ERROR=""

MAIL_DIR="$NATIVE_MAIL_SERVICE_ROOT"
MAIL_HOME="$NATIVE_MAIL_STATE_ROOT"
MAIL_DB="$NATIVE_MAIL_STATE_ROOT/storage.sqlite3"
MAIL_ENV="$NATIVE_MAIL_ENV"
SIGNALS_DIR="$NATIVE_MAIL_STATE_ROOT/signals"
AGENT_MAIL_RUNNER="$NATIVE_MAIL_RUNNER"
AGENT_MAIL_PIDFILE="$NATIVE_MAIL_PIDFILE"
AGENT_MAIL_LOG="$NATIVE_MAIL_LOG"

say() { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# Each `:`-separated entry must be absolute or start with `~` (the dashboard
# expands `~` itself). A missing directory is only a warning: presets often
# name checkouts that are cloned after the install.
validate_spawn_paths() {
  local name="$1" raw="$2" entry expanded
  local parts=()
  [[ -n "$raw" ]] || return 0
  IFS=':' read -r -a parts <<< "$raw"
  for entry in "${parts[@]}"; do
    [[ -n "$entry" ]] || continue
    case "$entry" in
      /*|"~"|"~/"*) ;;
      *)
        echo "error: $name entries must be absolute paths or start with ~ (got: $entry)" >&2
        exit 2
        ;;
    esac
    expanded="$entry"
    if [[ "$entry" == "~" ]]; then
      expanded="$HOME"
    elif [[ "$entry" == "~/"* ]]; then
      expanded="$HOME/${entry#\~/}"
    fi
    [[ -d "$expanded" ]] || warn "$name: directory does not exist yet: $entry"
  done
}
validate_spawn_paths AGENTSTACK_SPAWN_DIRS "$SPAWN_DIRS_SETTING"
validate_spawn_paths AGENTSTACK_SPAWN_ROOTS "$SPAWN_ROOTS_SETTING"
validate_spawn_paths AGENTSTACK_WORKTREE_ROOT "$WORKTREE_ROOT_SETTING"
validate_spawn_paths AGENTSTACK_PORTRAITS_DIR "$PORTRAITS_DIR_SETTING"
validate_spawn_paths AGENTSTACK_CUSTOM_PORTRAITS "$CUSTOM_PORTRAITS_SETTING"
validate_spawn_paths AGENTSTACK_CODEX_ADD_DIRS "$CODEX_ADD_DIRS_SETTING"
case "$AUTO_OPEN_CHILD_SETTING" in
  0|1) ;;
  *)
    echo "error: AGENTSTACK_AUTO_OPEN_CHILD must be 0 or 1" >&2
    exit 2
    ;;
esac
case "$CODEX_CHILD_APPROVAL_SETTING" in
  never|on-request|on-failure|untrusted) ;;
  *)
    echo "error: --codex-approval must be never, on-request, on-failure or untrusted (got: $CODEX_CHILD_APPROVAL_SETTING)" >&2
    exit 2
    ;;
esac
if [[ -n "$CODEX_CHILD_CONFIG_OVERLAY_SETTING" ]]; then
  if [[ "$CODEX_CHILD_CONFIG_OVERLAY_SETTING" != /* ]]; then
    echo "error: --codex-child-overlay must be an absolute path (got: $CODEX_CHILD_CONFIG_OVERLAY_SETTING)" >&2
    exit 2
  fi
  if [[ ! -f "$CODEX_CHILD_CONFIG_OVERLAY_SETTING" ]]; then
    echo "error: --codex-child-overlay file does not exist: $CODEX_CHILD_CONFIG_OVERLAY_SETTING" >&2
    exit 2
  fi
fi
case "$CODEX_NETWORK_SETTING" in
  on|off) ;;
  *)
    echo "error: --codex-network must be on or off (got: $CODEX_NETWORK_SETTING)" >&2
    exit 2
    ;;
esac
case "$CHILD_RESUME_RETENTION_DAYS_SETTING" in
  ''|*[!0-9]*)
    echo "error: --child-resume-retention-days must be a non-negative integer (got: $CHILD_RESUME_RETENTION_DAYS_SETTING)" >&2
    exit 2
    ;;
esac
if [[ -n "$CODEX_BIN_SETTING" && ! -x "$CODEX_BIN_SETTING" ]]; then
  echo "error: --codex-bin must point at an executable (got: $CODEX_BIN_SETTING)" >&2
  exit 2
fi

# The two mail jobs are different things: one runs the service, the other runs
# `agentstack-mailctl start` on a timer. Sharing a label makes the controller
# inspect the wrapper, decide the job under its label is not the mail service,
# and refuse to start -- an install that looks healthy until the first sweep.
if [[ -n "$MAIL_LAUNCHD_LABEL_SETTING" ]]; then
  if [[ "$MAIL_LAUNCHD_LABEL_SETTING" == "$MAIL_AUTOSTART_LABEL" ]]; then
    die "AGENTSTACK_MAIL_LAUNCHD_LABEL ($MAIL_LAUNCHD_LABEL_SETTING) collides with the mail autostart job; choose another label"
  fi
  if [[ "$MAIL_LAUNCHD_LABEL_SETTING" == "$LABEL_PREFIX.agentdashboard" ]]; then
    die "AGENTSTACK_MAIL_LAUNCHD_LABEL ($MAIL_LAUNCHD_LABEL_SETTING) collides with the dashboard job; choose another label"
  fi
fi


preflight_error() {
  PREFLIGHT_ERRORS[${#PREFLIGHT_ERRORS[@]}]="$1"
}

validate_preflight_switches() {
  local name value
  for name in \
    AGENTSTACK_PREFLIGHT_SKIP_OS \
    AGENTSTACK_PREFLIGHT_SKIP_PYTHON \
    AGENTSTACK_PREFLIGHT_SKIP_COMMANDS \
    AGENTSTACK_PREFLIGHT_SKIP_PORT \
    AGENTSTACK_PREFLIGHT_SKIP_WRITABLE
  do
    value="${!name:-0}"
    if [[ "$value" != "0" && "$value" != "1" ]]; then
      preflight_error "$name must be 0 or 1; set it to 1 only when a test deliberately replaces that boundary."
    fi
  done
}

preflight_required_commands() {
  if [[ "$PREFLIGHT_SKIP_COMMANDS" == "1" ]]; then
    say "preflight: required-command check skipped by AGENTSTACK_PREFLIGHT_SKIP_COMMANDS=1"
    return
  fi

  if ! command -v git >/dev/null 2>&1; then
    preflight_error "git is required. Install it with your OS package manager (macOS: xcode-select --install; Debian/Ubuntu: sudo apt install git)."
  fi
  if ! command -v tmux >/dev/null 2>&1; then
    preflight_error "tmux is required at install time and runtime. Install it with Homebrew (brew install tmux) or your Linux package manager."
  fi
}

preflight_platform() {
  PREFLIGHT_OS="$(uname -s 2>/dev/null || true)"
  if [[ "$PREFLIGHT_SKIP_OS" == "1" ]]; then
    say "preflight: OS check skipped by AGENTSTACK_PREFLIGHT_SKIP_OS=1"
    return
  fi

  case "$PREFLIGHT_OS" in
    Darwin)
      if ! command -v launchctl >/dev/null 2>&1; then
        preflight_error "macOS was detected but launchctl is unavailable. Restore the macOS launchd tools, then re-run install.sh."
      else
        say "preflight: supported OS macOS (launchd)"
      fi
      ;;
    Linux)
      if command -v systemctl >/dev/null 2>&1 && \
         systemctl --user show-environment >/dev/null 2>&1
      then
        say "preflight: supported OS Linux (systemd --user available)"
      else
        say "preflight: supported OS Linux (systemd --user unavailable; supervised background mode will be used)"
      fi
      ;;
    *)
      preflight_error "operating system '${PREFLIGHT_OS:-unknown}' is unsupported. Use macOS or Linux; native Windows and other kernels are not supported."
      ;;
  esac
}

preflight_target_writable() {
  if [[ "$PREFLIGHT_SKIP_WRITABLE" == "1" ]]; then
    say "preflight: install-directory write check skipped by AGENTSTACK_PREFLIGHT_SKIP_WRITABLE=1"
    return
  fi

  local target="$INSTALL_DIR"
  local ancestor="$target"
  if [[ -e "$target" && ! -d "$target" ]]; then
    preflight_error "install target '$target' exists but is not a directory. Move it aside or choose --install-dir PATH."
    return
  fi
  while [[ ! -e "$ancestor" && "$ancestor" != "/" ]]; do
    ancestor="$(dirname "$ancestor")"
  done
  if [[ ! -d "$ancestor" || ! -w "$ancestor" || ! -x "$ancestor" ]]; then
    preflight_error "install target '$target' is not writable. Fix permissions on '$ancestor' or choose --install-dir PATH."
    return
  fi
  if [[ -d "$target" && ( ! -w "$target" || ! -x "$target" ) ]]; then
    preflight_error "existing install directory '$target' is not writable. Fix its ownership/permissions or choose --install-dir PATH."
  fi
}

preflight_finish() {
  local count="${#PREFLIGHT_ERRORS[@]}"
  local item
  if [[ "$count" -eq 0 ]]; then
    say "preflight: passed"
    return 0
  fi
  printf 'error: preflight failed with %s problem(s):\n' "$count" >&2
  for item in "${PREFLIGHT_ERRORS[@]}"; do
    printf '  - %s\n' "$item" >&2
  done
  return 1
}

validate_assume_yes() {
  [[ "$ASSUME_YES" == "0" || "$ASSUME_YES" == "1" ]] || \
    die "AGENTSTACK_ASSUME_YES must be 0 or 1"
  [[ "$NATIVE_MAIL_SOURCE_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || \
    die "AGENTSTACK_MAIL_CANDIDATE_ID contains unsafe characters"
  if [[ -n "${AGENTSTACK_MAIL_MIGRATION_SOURCE_DB:-}" || \
        -n "${AGENTSTACK_MAIL_MIGRATION_SOURCE_ARCHIVE:-}" || \
        -n "${AGENTSTACK_MAIL_MIGRATION_SOURCE_SIGNALS:-}" ]]; then
    die "automatic mail migration is not part of install.sh; run agentstack-mail-migrate copy and verify manually before installing"
  fi
}

plan() {
  if [[ "$DRY_RUN" == true ]]; then
    say "DRY-RUN would $*"
  else
    say "$*"
  fi
}

run() {
  if [[ "$DRY_RUN" == true ]]; then
    say "DRY-RUN would run: $*"
  else
    "$@"
  fi
}

resolve_python_candidate() {
  case "$1" in
    */*) printf '%s\n' "$1" ;;
    *) command -v "$1" 2>/dev/null || true ;;
  esac
}

python_version() {
  "$1" -c 'import sys; print(".".join(str(part) for part in sys.version_info[:3]))' \
    2>/dev/null || printf '%s\n' unknown
}

python_is_compatible() {
  [[ -n "$1" && -x "$1" ]] || return 1
  "$1" -c "import sys; raise SystemExit(0 if sys.version_info >= ($PYTHON_MIN_MAJOR, $PYTHON_MIN_MINOR) else 1)" \
    >/dev/null 2>&1
}

select_python() {
  local requested="$PYTHON_REQUESTED"
  local candidate version
  PYTHON_SELECTION_ERROR=""
  if [[ -n "$requested" ]]; then
    candidate="$(resolve_python_candidate "$requested")"
    if [[ -z "$candidate" || ! -x "$candidate" ]]; then
      PYTHON_SELECTION_ERROR="AGENTSTACK_PYTHON is not an executable Python interpreter: $requested. Install Python $PYTHON_MIN_MAJOR.$PYTHON_MIN_MINOR or newer and point AGENTSTACK_PYTHON at it."
      return 1
    fi
    version="$(python_version "$candidate")"
    if [[ "$PREFLIGHT_SKIP_PYTHON" != "1" ]] && ! python_is_compatible "$candidate"; then
      PYTHON_SELECTION_ERROR="AGENTSTACK_PYTHON must be Python $PYTHON_MIN_MAJOR.$PYTHON_MIN_MINOR or newer; found $version at $candidate. Install a current Python or choose another AGENTSTACK_PYTHON."
      return 1
    fi
    PYTHON_BIN="$candidate"
    say "python: $PYTHON_BIN ($version)"
    return
  fi
  if [[ -n "$PYTHON_INHERITED" ]]; then
    candidate="$(resolve_python_candidate "$PYTHON_INHERITED")"
    if [[ -n "$candidate" ]] && { [[ "$PREFLIGHT_SKIP_PYTHON" == "1" && -x "$candidate" ]] || python_is_compatible "$candidate"; }; then
      PYTHON_BIN="$candidate"
      say "python: $PYTHON_BIN ($(python_version "$candidate"); from the existing env.sh)"
      return
    fi
    say "note: the Python recorded in the existing env.sh ($PYTHON_INHERITED) is missing or too old; searching again"
  fi

  local checked=""
  local seen=""
  local raw
  for raw in \
    python3 python3.14 python3.13 python3.12 python3.11 \
    /opt/homebrew/bin/python3 /usr/local/bin/python3 /opt/local/bin/python3 \
    /opt/homebrew/bin/python3.14 /opt/homebrew/bin/python3.13 \
    /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 \
    /usr/local/bin/python3.14 /usr/local/bin/python3.13 \
    /usr/local/bin/python3.12 /usr/local/bin/python3.11
  do
    candidate="$(resolve_python_candidate "$raw")"
    [[ -n "$candidate" ]] || continue
    case " $seen " in
      *" $candidate "*) continue ;;
    esac
    seen="$seen $candidate"
    version="$(python_version "$candidate")"
    if [[ -n "$checked" ]]; then
      checked="$checked, "
    fi
    checked="$checked$candidate ($version)"
    if [[ "$PREFLIGHT_SKIP_PYTHON" == "1" ]] || python_is_compatible "$candidate"; then
      PYTHON_BIN="$candidate"
      say "python: $PYTHON_BIN ($version)"
      return
    fi
  done

  [[ -n "$checked" ]] || checked="no python3 candidates found"
  PYTHON_SELECTION_ERROR="Python $PYTHON_MIN_MAJOR.$PYTHON_MIN_MINOR or newer is required; checked: $checked. Install a current Python or set AGENTSTACK_PYTHON."
  return 1
}

preflight_python() {
  if [[ "$PREFLIGHT_SKIP_PYTHON" == "1" ]]; then
    say "preflight: Python version check skipped by AGENTSTACK_PREFLIGHT_SKIP_PYTHON=1"
  fi
  if ! select_python; then
    preflight_error "$PYTHON_SELECTION_ERROR"
    return
  fi
  if [[ -n "$CHILD_DEFAULT_TOOLS_SETTING" ]] && ! "$PYTHON_BIN" \
      "$REPO_ROOT/hooks/child_tools.py" parse --provider claude \
      --tools "$CHILD_DEFAULT_TOOLS_SETTING" >/dev/null; then
    preflight_error "--child-default-tools must use the --tools grammar"
  fi
}

normalize_path() {
  "$PYTHON_BIN" - "$1" <<'PY'
import pathlib
import sys

print(pathlib.Path(sys.argv[1]).expanduser().resolve(strict=False))
PY
}

# The literal `export AGENTSTACK_MAIL_ENV=` assignment in the installed env.sh,
# or nothing. The file is read, never sourced: sourcing runs whatever else is in
# it, and a file that fails halfway would still leave a value behind that we
# would then treat as evidence. Anything unreadable, unparsable or absent yields
# no candidate, so the caller keeps the mismatch error.
installed_env_mail_env() {
  local env_file="$INSTALL_DIR/env.sh" value
  [[ -f "$env_file" ]] || return 0
  value="$("$PYTHON_BIN" - "$env_file" <<'PY' 2>/dev/null || true
import pathlib
import re
import shlex
import sys

try:
    raw = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
except (OSError, UnicodeError):
    raise SystemExit(0)

pattern = re.compile(r"^\s*export\s+AGENTSTACK_MAIL_ENV=(.*)$")
for line in raw.splitlines():
    match = pattern.match(line)
    if match is None:
        continue
    # Only a plain literal is evidence: a value that names a path only when
    # the shell expands it (a variable, a command substitution in either form)
    # is not one, whatever shlex leaves behind (#187 review). The installer
    # never writes either. chr(96) is the backquote, kept out of this heredoc.
    if "$" in match.group(1) or chr(96) in match.group(1):
        raise SystemExit(0)
    try:
        parts = shlex.split(match.group(1), comments=True, posix=True)
    except ValueError:
        raise SystemExit(0)
    if len(parts) == 1:
        print(parts[0], end="")
    raise SystemExit(0)
PY
)"
  [[ -n "$value" ]] || return 0
  normalize_path "$value"
}

# True when the path sits where this installation keeps its managed renders:
# <native service root>/renders/<one directory>/service.env. The render id is
# configurable, so its shape is deliberately not constrained.
is_managed_render_env_path() {
  local candidate="$1" root="$2" renders middle
  [[ -n "$candidate" && -n "$root" ]] || return 1
  renders="$root/renders"
  case "$candidate" in
    "$renders"/*/service.env) ;;
    *) return 1 ;;
  esac
  middle="${candidate#"$renders"/}"
  middle="${middle%/service.env}"
  case "$middle" in
    ""|*/*|.|..) return 1 ;;
  esac
  # A render this installer writes is named "<source id>-<hash>": the same
  # characters a source id may have. Anything else -- "$(...)", spaces, quotes
  # left over from an env.sh that only yields a path when run -- is not one
  # (#187 review).
  [[ "$middle" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]
}

native_mail_render_id() {
  "$PYTHON_BIN" - "$1" "$2" "$3" "$4" <<'PY'
import hashlib
import sys

payload = "\0".join(sys.argv[1:]).encode()
print(hashlib.sha256(payload).hexdigest()[:20])
PY
}

# Resolve the immutable candidate associated with one service render. New
# renders carry an owner-private deployment.json. Renders created before that
# metadata existed remain recoverable because their directory name is a hash of
# the source id, candidate venv, endpoint and state root. We deliberately do
# not inspect or split the running process command: argv is not deployment
# metadata and can point through wrappers, interpreters or mutable symlinks.
resolve_native_mail_candidate_for_render() {
  "$PYTHON_BIN" - "$1" "$NATIVE_MAIL_SERVICE_ROOT" \
    "$NATIVE_MAIL_STATE_ROOT" "$MCP_URL" <<'PY'
import hashlib
import json
import os
import pathlib
import stat
import sys

raw_env, raw_service_root, raw_state_root, mcp_url = sys.argv[1:]
service_env = pathlib.Path(raw_env).expanduser().resolve(strict=False)
service_root = pathlib.Path(raw_service_root).expanduser().resolve(strict=False)
state_root = pathlib.Path(raw_state_root).expanduser().resolve(strict=False)
deployment = service_env.parent / "deployment.json"


def fail(reason: str) -> None:
    print(f"deployment metadata rejected: {reason}", file=sys.stderr)
    raise SystemExit(1)


def render_path(source_id: str, venv: pathlib.Path) -> pathlib.Path:
    payload = "\0".join(
        (source_id, str(venv), mcp_url, str(state_root))
    ).encode()
    render_id = hashlib.sha256(payload).hexdigest()[:20]
    return service_root / "renders" / f"{source_id}-{render_id}" / "service.env"


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            fail("deployment-duplicate-key")
        value[key] = item
    return value


if deployment.exists() or deployment.is_symlink():
    try:
        info = deployment.lstat()
    except OSError:
        fail("deployment-unreadable")
    if not stat.S_ISREG(info.st_mode) or deployment.is_symlink():
        fail("deployment-not-regular")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        fail("deployment-permissions")
    try:
        value = json.loads(
            deployment.read_text(encoding="utf-8"),
            object_pairs_hook=unique_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        fail("deployment-unreadable")
    if not isinstance(value, dict) or value.get("kind") != "orrery-mail-deployment-v1":
        fail("deployment-kind")
    expected = {
        "service_env": str(service_env),
        "state_root": str(state_root),
        "mcp_url": mcp_url,
    }
    if any(value.get(key) != item for key, item in expected.items()):
        fail("deployment-mismatch")
    raw_venv = value.get("candidate_venv")
    source_id = value.get("source_id")
    if not isinstance(raw_venv, str) or not pathlib.Path(raw_venv).is_absolute():
        fail("candidate-venv")
    if not isinstance(source_id, str) or not source_id:
        fail("source-id")
    candidate = pathlib.Path(raw_venv).resolve(strict=False)
    raw_enroll = value.get("enroll_bin")
    if raw_enroll != str(candidate / "bin" / "agentstack-enroll"):
        fail("enroll-bin")
    print(candidate)
    raise SystemExit(0)

# Backward-compatible association for immutable managed renders written before
# deployment.json was introduced. Exact recomputation is the evidence: a mere
# candidate directory name, manifest from a later install, or running argv is
# not enough.
matches: list[pathlib.Path] = []
candidates = service_root / "candidates"
if candidates.is_dir():
    for candidate in candidates.iterdir():
        venv = (candidate / "venv").resolve(strict=False)
        if render_path(candidate.name, venv).resolve(strict=False) == service_env:
            matches.append(venv)
if len(matches) != 1:
    fail("legacy-render-association")
print(matches[0])
PY
} # end resolve_native_mail_candidate_for_render

mcp_endpoint_parts() {
  "$PYTHON_BIN" - "$MCP_URL" <<'PY'
import sys
import urllib.parse

parsed = urllib.parse.urlparse(sys.argv[1])
if parsed.scheme not in {"http", "https"} or not parsed.hostname:
    raise SystemExit(1)
port = parsed.port or (443 if parsed.scheme == "https" else 80)
print(f"{parsed.hostname}|{port}")
PY
}

mcp_local_server_parts() {
  "$PYTHON_BIN" - "$MCP_URL" <<'PY'
import sys
import urllib.parse

parsed = urllib.parse.urlparse(sys.argv[1])
if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
    raise SystemExit(1)
host = "127.0.0.1" if parsed.hostname == "localhost" else parsed.hostname
port = parsed.port or 80
path = parsed.path or "/mcp"
print(f"{host}|{port}|{path}")
PY
}

# Three-state probe. "Nothing is listening" and "the probe could not run" are
# different answers, and only ECONNREFUSED proves the first one. Collapsing
# every OSError into "free" made the installer announce an available port while
# a service held it: over SSH on macOS 26 every loopback connect returns
# EADDRNOTAVAIL, including one to a socket the probing process just opened.
# Callers that only ask "can I reach it" keep working, because undetermined is
# still non-zero.
#   0 = listening   1 = refused (nothing there)   2 = undetermined
mcp_endpoint_probe() {
  local parts host port
  parts="$(mcp_endpoint_parts)" || return 2
  IFS='|' read -r host port <<< "$parts"
  "$PYTHON_BIN" - "$host" "$port" <<'PY'
import errno
import socket
import sys

try:
    with socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=0.5):
        pass
except ConnectionRefusedError:
    raise SystemExit(1)
except OSError as exc:
    if exc.errno == errno.ECONNREFUSED:
        raise SystemExit(1)
    sys.stderr.write(
        f"port probe inconclusive for {sys.argv[1]}:{sys.argv[2]}: "
        f"[errno {exc.errno}] {exc.strerror}\n"
    )
    raise SystemExit(2)
PY
}

mcp_endpoint_listening() {
  mcp_endpoint_probe >/dev/null 2>&1
}

preflight_agent_mail_port() {
  if [[ "$PREFLIGHT_SKIP_PORT" == "1" ]]; then
    say "preflight: ORRERY Mail port check skipped by AGENTSTACK_PREFLIGHT_SKIP_PORT=1"
    return
  fi
  # Port probing needs the selected interpreter. Its own actionable Python
  # error is already in the aggregate when selection failed.
  [[ -n "$PYTHON_BIN" ]] || return

  local parts host port
  parts="$(mcp_endpoint_parts 2>/dev/null)" || {
    preflight_error "AGENTSTACK_MCP_URL '$MCP_URL' is invalid. Set it to an http(s) endpoint (default: ORRERY Mail on http://127.0.0.1:18765/mcp)."
    return
  }
  IFS='|' read -r host port <<< "$parts"
  case "$host" in
    127.0.0.1|localhost|::1) ;;
    *)
      say "preflight: remote ORRERY Mail endpoint $host:$port (local port check not applicable)"
      return
      ;;
  esac

  local probe_err probe_state
  probe_err="$(mcp_endpoint_probe 2>&1 >/dev/null)"
  probe_state=$?
  if [[ "$probe_state" == "1" ]]; then
    say "preflight: ORRERY Mail port $port is available"
    return
  fi
  if [[ "$probe_state" != "0" ]]; then
    # Undetermined is not free. Everything downstream — reuse an existing
    # service or start our own — branches on this answer, so guessing "free"
    # here is how an installer ends up fighting a service it never saw.
    preflight_error "could not determine whether ORRERY Mail port $port is in use${probe_err:+ (${probe_err##*: })}. Loopback connections from this shell are failing, which commonly happens over SSH on macOS; run the installer from a local terminal on the target machine, or set AGENTSTACK_PREFLIGHT_SKIP_PORT=1 if you have verified the port yourself."
    return
  fi
  if [[ -f "$MANIFEST" ]]; then
    say "preflight: existing ORRERY Telemetry install detected; occupied ORRERY Mail port $port will be verified for reuse"
  else
    # A user may intentionally share an already-running ORRERY Mail across
    # projects. Do not guess ownership from a listening socket: the existing
    # resolver verifies its health response/database before the first write.
    say "preflight: ORRERY Mail port $port is occupied; installer will verify that it is a reusable ORRERY Mail service"
  fi
}

run_preflight() {
  PREFLIGHT_ERRORS=()
  validate_preflight_switches
  preflight_required_commands
  preflight_platform
  preflight_python
  preflight_target_writable
  preflight_agent_mail_port
  preflight_finish
}

validate_codex_child_overlay() {
  [[ -n "$CODEX_CHILD_CONFIG_OVERLAY_SETTING" ]] || return 0
  local error
  if ! error="$("$PYTHON_BIN" - "$CODEX_CHILD_CONFIG_OVERLAY_SETTING" 2>&1 <<'PY'
import pathlib
import sys
import tomllib

path = pathlib.Path(sys.argv[1])
try:
    with path.open("rb") as handle:
        tomllib.load(handle)
except Exception as exc:
    print(exc)
    raise SystemExit(1)
PY
)"; then
    echo "error: --codex-child-overlay is not valid TOML ($CODEX_CHILD_CONFIG_OVERLAY_SETTING): $error" >&2
    exit 2
  fi
}

probe_agent_mail_database_url() {
  "$PYTHON_BIN" - "$MCP_URL" "$MAIL_ENV" <<'PY'
import json
import pathlib
import sys
import urllib.error
import urllib.request

url = sys.argv[1]
tokens = [None]
for raw_path in sys.argv[2:]:
    if not raw_path:
        continue
    try:
        lines = pathlib.Path(raw_path).expanduser().read_text(encoding="utf-8").splitlines()
    except OSError:
        continue
    for line in lines:
        if line.startswith("HTTP_BEARER_TOKEN="):
            token = line.split("=", 1)[1].strip().strip("'\"")
            if token and token not in tokens:
                tokens.append(token)
            break

payload = json.dumps({
    "jsonrpc": "2.0",
    "id": "agentstack-installer-probe",
    "method": "tools/call",
    "params": {"name": "health_check", "arguments": {}},
}).encode()
for token in tokens:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except (OSError, urllib.error.URLError):
        continue
    for line in raw.splitlines():
        if line.startswith("data:"):
            raw = line[5:].strip()
            break
    try:
        data = json.loads(raw)
        result = data.get("result") or {}
        health = result.get("structuredContent") or {}
        if not health:
            for block in result.get("content") or []:
                if block.get("type") == "text":
                    health = json.loads(block.get("text") or "{}")
                    break
        database_url = health.get("database_url")
    except (TypeError, ValueError, AttributeError):
        continue
    if isinstance(database_url, str) and database_url:
        print(database_url)
        raise SystemExit(0)
raise SystemExit(1)
PY
}

database_url_to_path() {
  "$PYTHON_BIN" - "$1" "$2" <<'PY'
import pathlib
import sys
import urllib.parse

database_url, cwd = sys.argv[1:3]
prefixes = ("sqlite+aiosqlite:///", "sqlite:///")
for prefix in prefixes:
    if database_url.startswith(prefix):
        raw = urllib.parse.unquote(database_url[len(prefix):].split("?", 1)[0])
        path = pathlib.Path(raw).expanduser()
        if not path.is_absolute():
            if not cwd:
                raise SystemExit(1)
            path = pathlib.Path(cwd) / path
        print(path.resolve(strict=False))
        raise SystemExit(0)
raise SystemExit(1)
PY
}

resolve_native_mail_connection() {
  local expected_db resolved_db database_url explicit_db render_id explicit_mail_env
  expected_db="$(normalize_path "$NATIVE_MAIL_STATE_ROOT/storage.sqlite3")"
  NATIVE_MAIL_STATE_ROOT="$(normalize_path "$NATIVE_MAIL_STATE_ROOT")"
  NATIVE_MAIL_SERVICE_ROOT="$(normalize_path "$NATIVE_MAIL_SERVICE_ROOT")"
  NATIVE_MAIL_PACKAGE_SOURCE="$(normalize_path "$NATIVE_MAIL_PACKAGE_SOURCE")"
  NATIVE_MAIL_VENV="$(normalize_path "$NATIVE_MAIL_VENV")"
  NATIVE_MAIL_ENROLL_BIN="$NATIVE_MAIL_VENV/bin/agentstack-enroll"
  if [[ -n "$NATIVE_MAIL_MANAGEMENT_SOCKET" ]]; then
    # Only a value given on this run is normalized. One inherited from env.sh
    # is what the previous install rendered into its immutable service env;
    # rewriting it (on macOS /tmp becomes /private/tmp) makes the same inputs
    # render differently, and re-rendering an existing render -- the same
    # commit reinstalled, or --update-mail going back -- is refused.
    if [[ "${MAIL_SOCKET_SOURCE:-explicit}" == explicit ]]; then
      NATIVE_MAIL_MANAGEMENT_SOCKET="$(normalize_path "$NATIVE_MAIL_MANAGEMENT_SOCKET")"
    fi
  else
    NATIVE_MAIL_MANAGEMENT_SOCKET="$($PYTHON_BIN - "$NATIVE_MAIL_STATE_ROOT" <<'PY'
import hashlib
import os
import sys

suffix = hashlib.sha256(sys.argv[1].encode()).hexdigest()[:20]
print(f"/tmp/orrery-mail-{os.getuid()}/{suffix}.sock")
PY
)"
  fi
  if [[ -n "$NATIVE_MAIL_ENV_EXPLICIT" ]]; then
    NATIVE_MAIL_ENV="$(normalize_path "$NATIVE_MAIL_ENV")"
  else
    render_id="$(native_mail_render_id \
      "$NATIVE_MAIL_SOURCE_ID" "$NATIVE_MAIL_VENV" "$MCP_URL" \
      "$NATIVE_MAIL_STATE_ROOT")"
    NATIVE_MAIL_ENV="$NATIVE_MAIL_SERVICE_ROOT/renders/$NATIVE_MAIL_SOURCE_ID-$render_id/service.env"
  fi
  NATIVE_MAIL_RUNNER="$(dirname "$NATIVE_MAIL_ENV")/run-agentstack-mail.sh"
  NATIVE_MAIL_DEPLOYMENT="$(dirname "$NATIVE_MAIL_ENV")/deployment.json"
  NATIVE_MAIL_PIDFILE="$NATIVE_MAIL_SERVICE_ROOT/runtime/agentstack-mail.pid"
  NATIVE_MAIL_LOG="$NATIVE_MAIL_SERVICE_ROOT/runtime/agentstack-mail.log"
  MAIL_DIR="$NATIVE_MAIL_SERVICE_ROOT"
  MAIL_HOME="$NATIVE_MAIL_STATE_ROOT"
  MAIL_DB="$expected_db"
  MAIL_ENV="$NATIVE_MAIL_ENV"
  SIGNALS_DIR="$NATIVE_MAIL_STATE_ROOT/signals"
  AGENT_MAIL_RUNNER="$NATIVE_MAIL_RUNNER"
  AGENT_MAIL_PIDFILE="$NATIVE_MAIL_PIDFILE"
  AGENT_MAIL_LOG="$NATIVE_MAIL_LOG"

  if [[ -n "$MAIL_DB_EXPLICIT" ]]; then
    [[ -n "${AGENTSTACK_MAIL_DB:-}" ]] || die "AGENTSTACK_MAIL_DB was set but empty"
    explicit_db="$(normalize_path "$AGENTSTACK_MAIL_DB")"
    [[ "$explicit_db" == "$expected_db" ]] || \
      die "AGENTSTACK_MAIL_DB must equal the native state database '$expected_db'"
  fi
  if [[ -n "$MAIL_ENV_EXPLICIT" ]]; then
    [[ -n "${AGENTSTACK_MAIL_ENV:-}" ]] || die "AGENTSTACK_MAIL_ENV was set but empty"
    explicit_mail_env="$(normalize_path "$AGENTSTACK_MAIL_ENV")"
    if [[ "$explicit_mail_env" != "$NATIVE_MAIL_ENV" ]]; then
      # A render path is derived from the source id and a hash of venv, endpoint
      # and state, so the path written into env.sh by one install does not match
      # the next one. Shell startup exports that file, so the documented
      # `git pull && ./scripts/install.sh` failed on this installer's own
      # output. Forgive that shape and nothing else:
      #
      #   - the installed env.sh has to name a managed render as a plain literal
      #     (read, never run; a value that needs expanding is no evidence), and
      #   - the inherited value has to be a managed render of this installation
      #     too. It need not equal env.sh's: a shell opened before the last Mail
      #     switch (--mail update) still exports the render that was current
      #     then (2026-10-03, an update from such a terminal stopped here).
      #
      # Neither proves who set the variable, so an operator who wants a native
      # path that outlives an upgrade pins AGENTSTACK_MAIL_SERVICE_ENV; that
      # takes precedence and is never forgiven here. Any other path keeps the
      # mismatch error.
      local installed_mail_env
      installed_mail_env="$(installed_env_mail_env)"
      if [[ -z "$NATIVE_MAIL_ENV_EXPLICIT" ]] \
        && [[ -n "$installed_mail_env" ]] \
        && is_managed_render_env_path "$installed_mail_env" "$NATIVE_MAIL_SERVICE_ROOT" \
        && is_managed_render_env_path "$explicit_mail_env" "$NATIVE_MAIL_SERVICE_ROOT"; then
        say "ignoring a managed AGENTSTACK_MAIL_ENV inherited from $INSTALL_DIR/env.sh (now or before an earlier Mail switch); resolving the current render"
        MAIL_ENV_EXPLICIT=""
        unset AGENTSTACK_MAIL_ENV
      else
        die "AGENTSTACK_MAIL_ENV must equal the native service env '$NATIVE_MAIL_ENV'"
      fi
    fi
  fi

  if mcp_endpoint_listening; then
    if [[ -n "${LEGACY_MAIL_DETECTED_LABELS:-}" ]]; then
      if [[ "$DRY_RUN" == true && "${LEGACY_MAIL_RETIRE_PLANNED:-false}" == true ]]; then
        say "legacy mail listener at $MCP_URL is planned for retirement; skipping reuse probe"
      elif [[ "$RETIRE_LEGACY_MAIL" == true ]]; then
        die "legacy mail service '$LEGACY_MAIL_DETECTED_LABELS' was retired, but $MCP_URL is still occupied; stop the remaining listener and re-run"
      else
        die "legacy mail service '$LEGACY_MAIL_DETECTED_LABELS' is holding $MCP_URL; re-run with --retire-legacy-mail to retire it before the ORRERY Mail reuse check"
      fi
    else
      say "existing ORRERY Mail listener detected at $MCP_URL"
      database_url="$(probe_agent_mail_database_url || true)"
      [[ -n "$database_url" ]] || \
        die "$MCP_URL is listening but did not answer an ORRERY Mail health check"
      resolved_db="$(database_url_to_path "$database_url" "" || true)"
      [[ -n "$resolved_db" ]] || \
        die "ORRERY Mail health returned an unsupported database URL: $database_url"
      resolved_db="$(normalize_path "$resolved_db")"
      [[ "$resolved_db" == "$expected_db" ]] || \
        die "ORRERY Mail at $MCP_URL uses '$resolved_db', expected isolated database '$expected_db'"
      [[ -f "$resolved_db" ]] || die "ORRERY Mail database does not exist: $resolved_db"
      NATIVE_MAIL_EXISTING=true
      EXISTING_AGENT_MAIL_SERVER=true
      # What this checkout would deploy; --update-mail switches to it once the
      # running deployment has been identified.
      mail_update_save NEW
      adopt_running_native_mail_render
      [[ "$NATIVE_MAIL_DEPLOYMENT_IDENTIFIED" != true ]] || mail_update_decide
      say "existing ORRERY Mail database: $resolved_db"
      return
    fi
  fi

  PROVISION_NATIVE_MAIL=true
  if [[ -f "$expected_db" && -d "$NATIVE_MAIL_STATE_ROOT/archive" ]]; then
    say "no native listener found; installer will start existing ORRERY Mail state at $NATIVE_MAIL_STATE_ROOT"
  else
    say "no native listener or state found; installer will provision ORRERY Mail at $MCP_URL"
  fi
}

# When a healthy native listener is already running, this run returns before it
# renders anything — but the paths computed from the current checkout are still
# what write_env_file records. On an update (different repo HEAD, MCP URL or
# state root) those paths name a render that never existed, so env.sh points at a
# missing service.env and every later `agentstack-mailctl start` dies with
# "service env is missing" — including the login trigger, which would then fail
# at every boot. Adopt the render the live service is actually using.
# (Found by review on 2026-08-16 with an isolated full install against a running
# listener; the same defect already made the documented manual start command fail
# for updating users, before any autostart existed.)
adopt_running_native_mail_render() {
  local pid runner dir candidate="" requested_env requested_venv resolved_venv deployment_metadata
  requested_env="$NATIVE_MAIL_ENV"
  requested_venv="$NATIVE_MAIL_VENV"
  # The pidfile agentstack-mailctl writes is TWO lines: the pid, then the runner
  # it started (bin/agentstack-mailctl write_pid). Read it exactly the way the
  # controller reads it — a plain `cat` yields "PID\nRUNNER", which fails the
  # numeric test and silently skips this whole function. (That bug shipped in the
  # first version of this helper and was caught by review, because the test
  # fixture had invented a one-line pidfile.)
  pid="$(sed -n '1{s/[[:space:]].*$//;p;}' "$NATIVE_MAIL_PIDFILE" 2>/dev/null || true)"
  runner="$(sed -n '2p' "$NATIVE_MAIL_PIDFILE" 2>/dev/null || true)"
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    if [[ -z "$runner" ]]; then
      # Pidfile from an older controller: recover the runner from the process.
      runner="$(ps -ww -o command= -p "$pid" 2>/dev/null |
        awk '{for (i = 1; i <= NF; i++) if ($i ~ /run-agentstack-mail\.sh$/) { print $i; exit }}')"
    fi
    if [[ -n "$runner" ]]; then
      dir="$(dirname "$runner")"
      [[ -f "$dir/service.env" ]] && candidate="$dir/service.env"
      # The manifest this run writes replaces the one that recorded the runner.
      # Only start_native_mail set the service record, so a reinstall over a
      # running listener wrote a manifest with no ORRERY Mail entry, and the
      # uninstaller -- which stops services from the manifest -- left the
      # runner alive. Every installer test that installs twice leaked one.
      AGENT_MAIL_SERVICE_KIND="nohup"
      AGENT_MAIL_SERVICE_PATH="$NATIVE_MAIL_PIDFILE"
    fi
  fi
  if [[ -z "$candidate" && -f "$INSTALL_DIR/env.sh" ]]; then
    # Parse only the literal generated assignment. Sourcing a prior env file
    # executes arbitrary shell and can leave partial values behind on failure.
    candidate="$(installed_env_mail_env)"
    [[ -n "$candidate" && -f "$candidate" ]] || candidate=""
  fi
  if [[ -z "$candidate" ]]; then
    if [[ "${UPDATE_MAIL:-false}" == true ]]; then
      die "--update-mail cannot replace the running ORRERY Mail because its deployment is not identifiable; nothing was stopped"
    fi
    if [[ -n "$NATIVE_MAIL_VENV_EXPLICIT" ]]; then
      die "cannot verify explicit AGENTSTACK_MAIL_SERVICE_VENV against the running ORRERY Mail deployment because its service env is not identifiable; stop Mail before switching candidates"
    fi
    if [[ -n "$NATIVE_MAIL_ENV_EXPLICIT" ]]; then
      die "cannot verify explicit AGENTSTACK_MAIL_SERVICE_ENV against the running ORRERY Mail deployment because its service env is not identifiable; stop Mail before switching renders"
    fi
    NATIVE_MAIL_DEPLOYMENT_IDENTIFIED=false
    NATIVE_MAIL_ENROLL_AVAILABLE=false
    NATIVE_MAIL_AUTOSTART_MANAGED_BY_INSTALL=false
    NATIVE_MAIL_ENV=""
    NATIVE_MAIL_RUNNER=""
    NATIVE_MAIL_DEPLOYMENT=""
    NATIVE_MAIL_VENV=""
    NATIVE_MAIL_ENROLL_BIN=""
    NATIVE_MAIL_MANAGEMENT_SOCKET=""
    MAIL_ENV=""
    AGENT_MAIL_RUNNER=""
    AGENT_MAIL_SERVICE_KIND=""
    AGENT_MAIL_SERVICE_PATH=""
    if [[ "$MAIL_UPDATE_MODE" == keep ]]; then
      MAIL_RESULT=kept
    else
      MAIL_RESULT=refused
    fi
    MAIL_RESULT_CODE=deployment_unidentified
    warn "reusing the healthy ORRERY Mail listener without changing it, but its deployment is not identifiable"
    warn "enrollment is unavailable until Mail is stopped and install.sh provisions a managed deployment"
    warn "this install did not update or register ORRERY Mail autostart; any existing trigger was left untouched and restart after login is not guaranteed"
    return 0
  fi
  candidate="$(normalize_path "$candidate")"
  if [[ -n "$NATIVE_MAIL_ENV_EXPLICIT" ]]; then
    requested_env="$(normalize_path "$requested_env")"
    if [[ "$requested_env" != "$candidate" ]]; then
      die "AGENTSTACK_MAIL_SERVICE_ENV '$requested_env' does not match the running ORRERY Mail deployment '$candidate'; stop Mail before switching renders"
    fi
  fi
  deployment_metadata="$(dirname "$candidate")/deployment.json"
  resolved_venv="$(resolve_native_mail_candidate_for_render "$candidate")" || \
    die "the running ORRERY Mail service env has no trusted candidate association: $candidate"
  resolved_venv="$(normalize_path "$resolved_venv")"
  if [[ -n "$NATIVE_MAIL_VENV_EXPLICIT" && "$requested_venv" != "$resolved_venv" && "${UPDATE_MAIL:-false}" != true ]]; then
    die "AGENTSTACK_MAIL_SERVICE_VENV '$requested_venv' does not match the running ORRERY Mail deployment '$resolved_venv'; stop Mail before switching candidates"
  fi
  native_mail_service_binaries_ready "$resolved_venv" || \
    die "the running ORRERY Mail deployment candidate is incomplete: $resolved_venv; stop Mail and re-run the installer to provision a current candidate"
  NATIVE_MAIL_ENV="$candidate"
  NATIVE_MAIL_RUNNER="$(dirname "$candidate")/run-agentstack-mail.sh"
  NATIVE_MAIL_DEPLOYMENT="$(dirname "$candidate")/deployment.json"
  [[ -f "$NATIVE_MAIL_DEPLOYMENT" ]] || NATIVE_MAIL_DEPLOYMENT=""
  NATIVE_MAIL_VENV="$resolved_venv"
  MAIL_ENV="$NATIVE_MAIL_ENV"
  if [[ -x "$resolved_venv/bin/agentstack-enroll" ]]; then
    NATIVE_MAIL_ENROLL_BIN="$resolved_venv/bin/agentstack-enroll"
  elif [[ -e "$deployment_metadata" || -L "$deployment_metadata" ]]; then
    die "the running ORRERY Mail deployment has no enrollment CLI: $resolved_venv/bin/agentstack-enroll; stop Mail and re-run the installer to provision a current candidate"
  else
    NATIVE_MAIL_ENROLL_AVAILABLE=false
    NATIVE_MAIL_ENROLL_BIN=""
    NATIVE_MAIL_MANAGEMENT_SOCKET=""
    warn "the trusted legacy ORRERY Mail deployment has no enrollment CLI; existing Mail and autostart remain usable, but persistent-agent enrollment is unavailable until Mail is reprovisioned"
  fi
  say "adopted the running ORRERY Mail deployment: $NATIVE_MAIL_ENV ($NATIVE_MAIL_VENV)"
} # end adopt_running_native_mail_render

# --- replacing a running ORRERY Mail (--update-mail) --------------------------
#
# The variables that name one deployment. The adopt step overwrites them with
# the running deployment; an update needs both sets, so it keeps them under the
# NEW_ and OLD_ prefixes and switches between them. Whatever set is current when
# write_env_file and write_manifest run is what env.sh, the autostart unit and
# install-state.json record -- so a rolled-back update records the old one.
MAIL_UPDATE_VARS="NATIVE_MAIL_SOURCE_ID NATIVE_MAIL_VENV NATIVE_MAIL_ENV NATIVE_MAIL_RUNNER NATIVE_MAIL_DEPLOYMENT NATIVE_MAIL_ENROLL_BIN NATIVE_MAIL_ENROLL_AVAILABLE NATIVE_MAIL_MANAGEMENT_SOCKET MAIL_ENV"

mail_update_save() {
  local prefix="$1" name
  for name in $MAIL_UPDATE_VARS; do
    printf -v "${prefix}_${name}" '%s' "${!name}"
  done
}

mail_update_use() {
  local prefix="$1" name source
  for name in $MAIL_UPDATE_VARS; do
    source="${prefix}_${name}"
    printf -v "$name" '%s' "${!source}"
  done
}

# The source id a render was built from: deployment.json records it; a render
# older than that metadata is named <source id>-<hash>.
mail_render_source_id() {
  "$PYTHON_BIN" - "$1" <<'PY'
import json
import pathlib
import sys

render = pathlib.Path(sys.argv[1]).parent
try:
    value = json.loads((render / "deployment.json").read_text(encoding="utf-8"))
    source = value.get("source_id") if isinstance(value, dict) else None
except (OSError, ValueError):
    source = None
if not isinstance(source, str) or not source:
    source = render.name.rsplit("-", 1)[0]
print(source)
PY
}

# True when both source ids are commits of this checkout and the bundled
# package tree is identical in them: a new candidate would be the same build.
mail_package_unchanged() {
  local old="$1" new="$2" old_tree new_tree
  [[ "$NATIVE_MAIL_PACKAGE_SOURCE" == "$REPO_ROOT/packages/agentstack_mail" ]] || return 1
  old_tree="$(git -C "$REPO_ROOT" rev-parse -q --verify "$old^{commit}:packages/agentstack_mail" 2>/dev/null)" || return 1
  new_tree="$(git -C "$REPO_ROOT" rev-parse -q --verify "$new^{commit}:packages/agentstack_mail" 2>/dev/null)" || return 1
  [[ -n "$old_tree" && "$old_tree" == "$new_tree" ]]
}

mail_update_decide() {
  local old_source
  NATIVE_MAIL_SOURCE_ID="$(mail_render_source_id "$NATIVE_MAIL_ENV")"
  mail_update_save OLD
  if [[ "$NATIVE_MAIL_VENV" == "$NEW_NATIVE_MAIL_VENV" ]]; then
    [[ "$UPDATE_MAIL" != true ]] || \
      say "--update-mail: the running ORRERY Mail already uses candidate $NATIVE_MAIL_VENV; nothing to switch"
    MAIL_RESULT=unchanged MAIL_RESULT_CODE=same_candidate
    return 0
  fi
  old_source="$OLD_NATIVE_MAIL_SOURCE_ID"
  if [[ -z "$NATIVE_MAIL_VENV_EXPLICIT" ]] && mail_package_unchanged "$old_source" "$NEW_NATIVE_MAIL_SOURCE_ID"; then
    [[ "$UPDATE_MAIL" != true ]] || \
      say "--update-mail: packages/agentstack_mail is identical in $old_source and $NEW_NATIVE_MAIL_SOURCE_ID; keeping the running build"
    MAIL_RESULT=unchanged MAIL_RESULT_CODE=same_package
    return 0
  fi
  if [[ "$MAIL_UPDATE_MODE" == keep ]]; then
    say "notice: the running ORRERY Mail is built from $old_source; this checkout would build $NEW_NATIVE_MAIL_SOURCE_ID"
    say "notice: the running build was kept. Re-run with --update-mail to switch (docs/agentstack-mail-update.md)"
    MAIL_RESULT=kept MAIL_RESULT_CODE="$MAIL_KEEP_CODE"
    return 0
  fi
  if [[ "$AGENT_MAIL_SERVICE_KIND" != nohup ]]; then
    [[ "$UPDATE_MAIL" != true ]] || \
      die "--update-mail can only replace an ORRERY Mail started by agentstack-mailctl, and $NATIVE_MAIL_PIDFILE does not name a live runner; nothing was stopped"
    warn "the running ORRERY Mail is built from $old_source; this checkout would build $NEW_NATIVE_MAIL_SOURCE_ID"
    warn "it was not started by agentstack-mailctl ($NATIVE_MAIL_PIDFILE names no live runner), so it was kept; see docs/agentstack-mail-update.md"
    MAIL_RESULT=refused MAIL_RESULT_CODE=not_mailctl_managed
    return 0
  fi
  MAIL_UPDATE_PLANNED=true
  say "ORRERY Mail ($MAIL_UPDATE_MODE): will switch from $old_source ($OLD_NATIVE_MAIL_VENV) to $NEW_NATIVE_MAIL_SOURCE_ID ($NEW_NATIVE_MAIL_VENV)"
  [[ "$MAIL_UPDATE_MODE" != auto ]] || \
    say "ORRERY Mail (auto): only after the candidate passes its check and the expected outage is short; pass --keep-mail to keep the running build"
}

check_dependencies() {
  if ! command -v fswatch >/dev/null 2>&1; then
    warn "optional dependency 'fswatch' not found; mail watcher will use polling"
  fi
  if [[ "$PREFLIGHT_OS" == "Darwin" ]] && [[ "$TERMINAL" != "none" ]]; then
    if [[ ! -d /Applications/Ghostty.app && ! -d "$HOME/Applications/Ghostty.app" ]] && ! command -v ghostty >/dev/null 2>&1; then
      warn "Ghostty not found; AGENTSTACK_TERMINAL=auto will fall back when possible"
    fi
  fi
}

check_agent_mail_provisioning_dependencies() {
  if [[ "$PREFLIGHT_SKIP_COMMANDS" == "1" ]]; then
    return 0
  fi
  if [[ "$PROVISION_NATIVE_MAIL" != true && "$MAIL_UPDATE_PLANNED" != true ]] || \
     [[ -n "$NATIVE_MAIL_VENV_EXPLICIT" ]]; then
    return 0
  fi
  if ! command -v uv >/dev/null 2>&1; then
    PREFLIGHT_ERRORS=()
    preflight_error "uv is required to provision ORRERY Mail. Install it from https://docs.astral.sh/uv/getting-started/installation/ and re-run install.sh."
    preflight_finish
    return 1
  fi
}

validate_repo_assets() {
  [[ -f "$REPO_ROOT/dashboard/server.py" ]] || die "missing dashboard/server.py"
  [[ -f "$REPO_ROOT/dashboard/index.html" ]] || die "missing dashboard/index.html"
  [[ -d "$REPO_ROOT/dashboard/assets" ]] || die "missing dashboard/assets"
  [[ -d "$REPO_ROOT/dashboard/portraits_64" ]] || die "missing dashboard/portraits_64"
  [[ -f "$REPO_ROOT/dashboard/scientist_portraits.json" ]] || die "missing dashboard/scientist_portraits.json"
  if [[ "$TIER" != "tier0" ]]; then
    [[ -f "$REPO_ROOT/hooks/check-file-reservation.sh" ]] || die "missing hooks/check-file-reservation.sh"
    [[ -f "$REPO_ROOT/hooks/settings.template.json" ]] || die "missing hooks/settings.template.json"
    [[ -d "$REPO_ROOT/skills" ]] || die "missing skills directory"
    [[ -f "$REPO_ROOT/claude/CLAUDE.md" ]] || die "missing claude/CLAUDE.md"
  fi
  [[ -f "$MERGE_SETTINGS_SCRIPT" ]] || die "missing scripts/lib/merge_settings.py"
  [[ -f "$MERGE_CLAUDE_MCP_SCRIPT" ]] || die "missing scripts/lib/merge_claude_mcp.py"
  [[ -f "$SCRIPT_DIR/lib/mcp_endpoint.py" ]] || die "missing scripts/lib/mcp_endpoint.py"
  [[ -f "$SCRIPT_DIR/lib/mail_required_features.json" ]] || die "missing scripts/lib/mail_required_features.json"
  [[ -f "$SCRIPT_DIR/lib/mail_update_notice.py" ]] || die "missing scripts/lib/mail_update_notice.py"
  [[ -f "$SCRIPT_DIR/lib/agentstack-persistent-launcher.sh" ]] || \
    die "missing scripts/lib/agentstack-persistent-launcher.sh"
  [[ -f "$SCRIPT_DIR/selftest.py" ]] || die "missing scripts/selftest.py"
  [[ -f "$NATIVE_MAIL_PACKAGE_SOURCE/pyproject.toml" ]] || \
    die "missing ORRERY Mail package: $NATIVE_MAIL_PACKAGE_SOURCE"
  [[ -f "$REPO_ROOT/bin/agentstack-mailctl" ]] || \
    die "missing ORRERY Mail lifecycle controller: $REPO_ROOT/bin/agentstack-mailctl"
}

port_in_use() {
  "$PYTHON_BIN" - "$PORT" <<'PY'
import socket
import sys
port = int(sys.argv[1])
s = socket.socket()
s.settimeout(0.3)
try:
    sys.exit(0 if s.connect_ex(("127.0.0.1", port)) == 0 else 1)
finally:
    s.close()
PY
}

listener_pids() {
  local lsof_bin
  lsof_bin="$(command -v lsof 2>/dev/null || true)"
  if [[ -z "$lsof_bin" && -x /usr/sbin/lsof ]]; then
    lsof_bin=/usr/sbin/lsof
  fi
  [[ -n "$lsof_bin" ]] || return 1
  "$lsof_bin" -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null |
    sed -n '/^[0-9][0-9]*$/p' | sort -u
}

process_parent_pid() {
  local pid="$1" lsof_bin
  if [[ -r "/proc/$pid/stat" ]]; then
    "$PYTHON_BIN" - "$pid" <<'PY'
import pathlib
import sys

try:
    fields = pathlib.Path(f"/proc/{sys.argv[1]}/stat").read_text().rsplit(")", 1)[1].split()
    parent = int(fields[1])
except (IndexError, OSError, ValueError):
    raise SystemExit(1)
print(parent)
PY
    return
  fi

  lsof_bin="$(command -v lsof 2>/dev/null || true)"
  if [[ -z "$lsof_bin" && -x /usr/sbin/lsof ]]; then
    lsof_bin=/usr/sbin/lsof
  fi
  if [[ -n "$lsof_bin" ]]; then
    "$lsof_bin" -a -p "$pid" -FpR 2>/dev/null |
      sed -n 's/^R//p' | sed -n '1p'
    return
  fi

  ps -o ppid= -p "$pid" 2>/dev/null | tr -d '[:space:]'
}

pid_is_same_or_descendant() {
  local candidate="$1" root="$2" parent attempts=0
  [[ "$candidate" =~ ^[0-9]+$ && "$root" =~ ^[0-9]+$ ]] || return 1
  while [[ "$candidate" -gt 1 && "$attempts" -lt 64 ]]; do
    [[ "$candidate" == "$root" ]] && return 0
    parent="$(process_parent_pid "$candidate" || true)"
    [[ "$parent" =~ ^[0-9]+$ && "$parent" != "$candidate" ]] || return 1
    candidate="$parent"
    attempts=$((attempts + 1))
  done
  return 1
}

launchd_dashboard_pid() {
  [[ "$(uname -s)" == "Darwin" ]] || return 1
  command -v launchctl >/dev/null 2>&1 || return 1
  launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null |
    sed -n 's/^[[:space:]]*pid = \([0-9][0-9]*\).*$/\1/p' | sed -n '1p'
}

supervised_dashboard_pid() {
  local pidfile="$RUNTIME_DIR/dashboard.pid" pid
  pid="$(sed -n '1p' "$pidfile" 2>/dev/null || true)"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  printf '%s\n' "$pid"
}

MANAGED_SUPERVISED_PID=""

listener_is_managed_dashboard() {
  local listener_pid="$1" manager_pid
  manager_pid="$(launchd_dashboard_pid || true)"
  if [[ -n "$manager_pid" ]] && pid_is_same_or_descendant "$listener_pid" "$manager_pid"; then
    return 0
  fi

  manager_pid="$(supervised_dashboard_pid || true)"
  if [[ -n "$manager_pid" ]] && pid_is_same_or_descendant "$listener_pid" "$manager_pid"; then
    MANAGED_SUPERVISED_PID="$manager_pid"
    return 0
  fi
  return 1
}

check_port() {
  if port_in_use; then
    local listeners listener all_managed=true
    listeners="$(listener_pids || true)"
    if [[ -z "$listeners" ]]; then
      all_managed=false
    else
      while IFS= read -r listener; do
        if ! listener_is_managed_dashboard "$listener"; then
          all_managed=false
          break
        fi
      done <<< "$listeners"
    fi
    if [[ "$all_managed" == true ]]; then
      say "managed dashboard owns port $PORT; replacing it during this install"
      return
    fi
    if [[ "$DRY_RUN" == true ]]; then
      warn "port $PORT is already in use; live install would stop before service registration"
    else
      die "port $PORT is already in use; set AGENTSTACK_PORT or --port"
    fi
  fi
}

detect_service_kind() {
  if [[ "$PREFLIGHT_OS" == "Darwin" ]]; then
    echo "launchd"
    return
  fi
  if [[ -r /proc/version ]] && grep -qi microsoft /proc/version 2>/dev/null; then
    echo "nohup"
    return
  fi
  if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    echo "systemd-user"
    return
  fi
  echo "nohup"
}

create_layout() {
  plan "create install layout under $INSTALL_DIR"
  run mkdir -p "$HOOKS_DIR" "$SKILLS_DIR" "$DASHBOARD_DIR" "$BIN_DIR" "$RUNTIME_DIR" "$BACKUPS_DIR" "$CONNECTIONS_DIR" "$PERSISTENT_PROFILES_DIR"
  if [[ "$DRY_RUN" != true ]]; then
    chmod 700 "$PERSISTENT_PROFILES_DIR"
  fi
}

migrate_legacy_annotations() {
  local legacy_path="$DASHBOARD_DIR/annotations.json"
  local runtime_path="$RUNTIME_DIR/annotations.json"
  if [[ ! -f "$legacy_path" || -e "$runtime_path" ]]; then
    return
  fi
  plan "migrate dashboard annotations $legacy_path -> $runtime_path"
  if [[ "$DRY_RUN" == true ]]; then
    return
  fi
  "$PYTHON_BIN" - "$legacy_path" "$runtime_path" <<'PY'
import os
import pathlib
import shutil
import sys

source = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2])
target.parent.mkdir(parents=True, exist_ok=True)
temporary = target.with_name(target.name + ".tmp")
shutil.copy2(source, temporary)
os.replace(temporary, target)
try:
    source.unlink()
except OSError as exc:
    print(f"warning: migrated annotations but could not remove legacy copy {source}: {exc}", file=sys.stderr)
PY
}

migrate_legacy_dashboard_log() {
  local legacy_path="$DASHBOARD_DIR/dashboard.log"
  local target_path="$DASHBOARD_LOG"
  local suffix=1
  if [[ ! -f "$legacy_path" ]]; then
    return
  fi
  if [[ -e "$target_path" ]]; then
    target_path="$RUNTIME_DIR/dashboard.legacy.log"
  fi
  while [[ -e "$target_path" ]]; do
    target_path="$RUNTIME_DIR/dashboard.legacy.$suffix.log"
    suffix=$((suffix + 1))
  done
  plan "migrate dashboard log $legacy_path -> $target_path"
  if [[ "$DRY_RUN" == true ]]; then
    return
  fi
  "$PYTHON_BIN" - "$legacy_path" "$target_path" <<'PY'
import os
import pathlib
import shutil
import sys

source = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2])
target.parent.mkdir(parents=True, exist_ok=True)
try:
    os.replace(source, target)
except OSError:
    temporary = target.with_name(target.name + ".tmp")
    shutil.copy2(source, temporary)
    os.replace(temporary, target)
    source.unlink()
PY
}

copy_tree() {
  local src="$1"
  local dst="$2"
  plan "copy $src -> $dst"
  if [[ "$DRY_RUN" != true ]]; then
    mkdir -p "$dst"
    cp -R "$src/." "$dst/"
  fi
}

# Install-relative files owned by optional provider installers (for example
# scripts/install-gemini-provider.sh). service_runner.py starts
# provider_server.py whenever it exists, so shipping it from the core would
# activate an optional provider that was never installed. The provider's
# dashboard runtime and its launcher hooks are one contract installed
# together; a core reinstall overwriting only the hooks would pair an old
# runtime with a new adapter (or the reverse). The core neither copies nor
# removes these: an existing provider install keeps all of its files.
OPTIONAL_PROVIDER_PAYLOAD=(
  "dashboard/provider_server.py"
  "dashboard/gemini_provider_runtime.py"
  "hooks/spawn_gemini_child.sh"
  "hooks/spawn_gemini_preregistered.sh"
)

is_optional_provider_payload() {
  local relative="$1"
  local owned
  for owned in "${OPTIONAL_PROVIDER_PAYLOAD[@]}"; do
    [[ "$relative" == "$owned" ]] && return 0
  done
  return 1
}

# copy_tree for a core directory that may also hold provider-owned files.
# $3 is the directory's install-relative name (dashboard, hooks).
copy_core_tree() {
  local src="$1"
  local dst="$2"
  local relative_root="$3"
  local entry
  local name
  plan "copy $src -> $dst (core files; optional provider payload excluded)"
  if [[ "$DRY_RUN" == true ]]; then
    return 0
  fi
  mkdir -p "$dst"
  for entry in "$src"/* "$src"/.[!.]* "$src"/..?*; do
    [[ -e "$entry" || -L "$entry" ]] || continue
    name="${entry##*/}"
    if is_optional_provider_payload "$relative_root/$name"; then
      continue
    fi
    cp -R "$entry" "$dst/"
  done
}

# The child MCP proxy that spawn_child.sh points each child at. It lives under
# integrations/codex_app because the Codex App bridge introduced it, but a
# spawned child's authenticated ORRERY Mail connection is a CORE feature: without
# this, hooks/spawn_child.sh silently falls back to the shared endpoint and the
# child must read its own token instead of the proxy injecting it.
#
# Only the runtime subset ships here (the runner plus the package it imports).
# The full optional bridge — daemon, launchd, marketplace — still comes from
# scripts/install-codex-app-integration.sh into this same directory.
install_child_mcp_proxy() {
  local source_dir="$REPO_ROOT/integrations/codex_app"
  local dest_dir="$INSTALL_DIR/integrations/codex_app"
  if [[ ! -f "$source_dir/plugin/scripts/run-mcp.sh" ]]; then
    warn "child MCP proxy not found in the repo; spawned children will fall back to the shared ORRERY Mail endpoint"
    return 0
  fi
  plan "install child MCP proxy -> $dest_dir"
  if [[ "$DRY_RUN" == true ]]; then
    return 0
  fi
  "${PYTHON_BIN:-python3}" - "$source_dir" "$dest_dir" <<'PY'
import pathlib
import shutil
import sys

source, dest = (pathlib.Path(p) for p in sys.argv[1:3])
ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")
for name in ("plugin", "src"):
    src = source / name
    if not src.is_dir():
        continue
    dst = dest / name
    if dst.exists():
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, ignore=ignore)
PY
  chmod +x "$dest_dir/plugin/scripts/run-mcp.sh" 2>/dev/null || true
}

install_payload() {
  if [[ "$TIER" != "tier0" ]]; then
    copy_core_tree "$REPO_ROOT/hooks" "$HOOKS_DIR" "hooks"
    copy_tree "$REPO_ROOT/skills" "$SKILLS_DIR"
    copy_tree "$REPO_ROOT/codex" "$INSTALL_DIR/codex"
    copy_tree "$REPO_ROOT/claude" "$INSTALL_DIR/claude"
    install_child_mcp_proxy
  else
    plan "skip hooks copy for --dashboard-only"
    plan "skip skills copy for --dashboard-only"
  fi
  copy_core_tree "$REPO_ROOT/dashboard" "$DASHBOARD_DIR" "dashboard"
  plan "copy VERSION -> $INSTALL_DIR/VERSION"
  if [[ "$DRY_RUN" != true ]]; then
    cp "$REPO_ROOT/VERSION" "$INSTALL_DIR/VERSION"
  fi
  plan "install helper scripts into $BIN_DIR"
  if [[ "$DRY_RUN" != true ]]; then
    cp "$SCRIPT_DIR/uninstall.sh" "$BIN_DIR/agentstack-uninstall"
    cp "$SCRIPT_DIR/doctor.sh" "$BIN_DIR/agentstack-doctor"
    cp "$SCRIPT_DIR/selftest.py" "$BIN_DIR/agentstack-selftest"
    cp "$MERGE_SETTINGS_SCRIPT" "$BIN_DIR/agentstack-merge-settings"
    cp "$MERGE_CLAUDE_MCP_SCRIPT" "$BIN_DIR/agentstack-merge-claude-mcp"
    mkdir -p "$BIN_DIR/lib"
    cp "$SCRIPT_DIR/lib/mcp_endpoint.py" "$BIN_DIR/lib/mcp_endpoint.py"
    cp "$SCRIPT_DIR/lib/mail_required_features.json" "$BIN_DIR/lib/mail_required_features.json"
    cp "$SCRIPT_DIR/lib/mail_update_notice.py" "$BIN_DIR/lib/mail_update_notice.py"
    cp "$REPO_ROOT/bin/lib/agentstack-launch.sh" "$BIN_DIR/lib/agentstack-launch.sh"
    cp "$REPO_ROOT/bin/lib/agentstack-register.sh" "$BIN_DIR/lib/agentstack-register.sh"
    cp "$REPO_ROOT/bin/lib/runtime_client.py" "$BIN_DIR/lib/runtime_client.py"
    cp "$REPO_ROOT/bin/agentstack-runtime-client" "$BIN_DIR/agentstack-runtime-client"
    cp "$REPO_ROOT/bin/lib/agentstack-scientists.sh" "$BIN_DIR/lib/agentstack-scientists.sh"
    cp "$REPO_ROOT/bin/lib/agentstack-managed-block.sh" "$BIN_DIR/lib/agentstack-managed-block.sh"
    cp "$REPO_ROOT/bin/agent-start" "$BIN_DIR/agent-start"
    cp "$REPO_ROOT/bin/agent-start-codex" "$BIN_DIR/agent-start-codex"
    cp "$REPO_ROOT/bin/agentstack-reregister" "$BIN_DIR/agentstack-reregister"
    cp "$REPO_ROOT/bin/agentstack-enroll" "$BIN_DIR/agentstack-enroll"
    # Keep the Python implementation beside the public launcher so its
    # __file__-relative defaults (agentstack-enroll, install root and hooks)
    # retain the same layout. The bash entry point loads env.sh and execs the
    # interpreter selected and version-checked by this installer; ambient PATH
    # never selects an older python3.
    cp "$REPO_ROOT/bin/agentstack-persistent" "$BIN_DIR/agentstack-persistent.py"
    cp "$SCRIPT_DIR/lib/agentstack-persistent-launcher.sh" "$BIN_DIR/agentstack-persistent"
    cp "$REPO_ROOT/bin/agentstack-persistent-deliver" "$BIN_DIR/agentstack-persistent-deliver"
    cp "$REPO_ROOT/bin/agentstack-preregister-child" "$BIN_DIR/agentstack-preregister-child"
    cp "$REPO_ROOT/bin/agentstack-await-reply" "$BIN_DIR/agentstack-await-reply"
    cp "$REPO_ROOT/bin/agentstack-codex-bootstrap" "$BIN_DIR/agentstack-codex-bootstrap"
    cp "$REPO_ROOT/bin/agentstack-resume" "$BIN_DIR/agentstack-resume"
    cp "$REPO_ROOT/bin/agentstack-purge-child-resume" "$BIN_DIR/agentstack-purge-child-resume"
    cp "$REPO_ROOT/bin/agentstack-codex-setup" "$BIN_DIR/agentstack-codex-setup"
    cp "$REPO_ROOT/bin/agentstack-claude-setup" "$BIN_DIR/agentstack-claude-setup"
    cp "$REPO_ROOT/bin/agentstack-mailctl" "$BIN_DIR/agentstack-mailctl"
    chmod +x "$BIN_DIR/agentstack-uninstall" "$BIN_DIR/agentstack-doctor" \
      "$BIN_DIR/agentstack-selftest" "$BIN_DIR/agentstack-merge-settings" \
      "$BIN_DIR/agentstack-merge-claude-mcp" \
      "$BIN_DIR/agent-start" "$BIN_DIR/agent-start-codex" "$BIN_DIR/agentstack-reregister" "$BIN_DIR/agentstack-enroll" \
      "$BIN_DIR/agentstack-persistent" "$BIN_DIR/agentstack-persistent-deliver" \
      "$BIN_DIR/agentstack-preregister-child" "$BIN_DIR/agentstack-await-reply" \
      "$BIN_DIR/agentstack-codex-bootstrap" "$BIN_DIR/agentstack-resume" "$BIN_DIR/agentstack-purge-child-resume" "$BIN_DIR/agentstack-codex-setup" "$BIN_DIR/agentstack-claude-setup" \
      "$BIN_DIR/agentstack-mailctl"
  fi
}

symlink_points_to() {
  "$PYTHON_BIN" - "$1" "$2" <<'PY'
import os
import pathlib
import sys

link = pathlib.Path(sys.argv[1])
expected = pathlib.Path(sys.argv[2])
try:
    target = pathlib.Path(os.readlink(link))
except OSError:
    raise SystemExit(1)
if not target.is_absolute():
    target = link.parent / target
raise SystemExit(0 if target.resolve(strict=False) == expected.resolve(strict=False) else 1)
PY
}

install_skill_links_for() {
  local host="$1"
  local skills_dir="$2"
  if [[ "$TIER" == "tier0" ]]; then
    plan "skip $host skill links for --dashboard-only"
    return
  fi

  if [[ -e "$skills_dir" && ! -d "$skills_dir" ]]; then
    warn "$host skills path exists but is not a directory; leaving it untouched: $skills_dir"
    return
  fi
  plan "create $host standard skills directory $skills_dir"
  run mkdir -p "$skills_dir"

  local discovery_root="$SKILLS_DIR"
  if [[ "$DRY_RUN" == true ]]; then
    discovery_root="$REPO_ROOT/skills"
  fi
  local skill_file skill_name source_path link_path
  while IFS= read -r -d '' skill_file; do
    skill_name="$(basename "$(dirname "$skill_file")")"
    source_path="$SKILLS_DIR/$skill_name"
    link_path="$skills_dir/$skill_name"

    if [[ -e "$link_path" || -L "$link_path" ]]; then
      if [[ -L "$link_path" ]] && symlink_points_to "$link_path" "$source_path"; then
        plan "reuse $host skill link $link_path -> $source_path"
      else
        warn "$host skill '$skill_name' already exists; leaving it untouched: $link_path"
      fi
      continue
    fi

    plan "link $host skill $link_path -> $source_path"
    run ln -s "$source_path" "$link_path"
  done < <(find "$discovery_root" -mindepth 2 -maxdepth 2 -name SKILL.md -type f -print0)
}

install_claude_skill_links() {
  install_skill_links_for "Claude" "$CLAUDE_SKILLS_DIR"
}

install_codex_skill_links() {
  install_skill_links_for "Codex" "$CODEX_SKILLS_DIR"
}

render_installed_templates() {
  if [[ "$TIER" != "tier0" ]]; then
    plan "render hook settings template token -> $HOOKS_DIR"
    if [[ "$DRY_RUN" != true ]]; then
      "$PYTHON_BIN" - "$HOOKS_DIR/settings.template.json" "$HOOKS_DIR" <<'PY'
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
hooks_dir = sys.argv[2]
text = path.read_text(encoding="utf-8").replace("__AGENTSTACK_HOOKS_DIR__", hooks_dir)
tmp = path.with_suffix(path.suffix + ".tmp")
tmp.write_text(text, encoding="utf-8")
tmp.replace(path)
PY
    fi
  fi
}

confirm_safe_merge() {
  if [[ "$ASSUME_YES" == "1" ]]; then
    return 0
  fi
  if [[ ! -t 0 ]]; then
    warn "non-interactive shell; skipping Tier1 user-settings merge"
    return 1
  fi
  printf 'Apply this ORRERY Telemetry settings merge to %s? Type yes to continue: ' "$CLAUDE_SETTINGS" >&2
  local reply
  read -r reply
  [[ "$reply" == "yes" ]]
}

safe_merge_settings() {
  if [[ "$TIER" != "tier1" ]]; then
    return
  fi

  local template="$HOOKS_DIR/settings.template.json"
  if [[ "$DRY_RUN" == true ]]; then
    template="$REPO_ROOT/hooks/settings.template.json"
  fi
  local merge_args=(
    "$MERGE_SETTINGS_SCRIPT"
    --settings "$CLAUDE_SETTINGS"
    --template "$template"
    --hooks-dir "$HOOKS_DIR"
    --bin-dir "$BIN_DIR"
    --skills-dir "$SKILLS_DIR"
    --claude-skills-dir "$CLAUDE_SKILLS_DIR"
    --backup-dir "$BACKUPS_DIR"
    --installed-entries "$RUNTIME_DIR/settings-installed-entries.json"
  )

  say "Tier1 settings safe-merge dry-run: $CLAUDE_SETTINGS"
  if [[ "$DRY_RUN" == true ]]; then
    "$PYTHON_BIN" "${merge_args[@]}" --dry-run
    return
  fi

  "$PYTHON_BIN" "${merge_args[@]}" --dry-run
  if confirm_safe_merge; then
    "$PYTHON_BIN" "${merge_args[@]}" --result-json "$SAFE_MERGE_RESULT_FILE"
    if [[ "$ASSUME_YES" == "1" ]]; then
      say "assume-yes: applied Tier1 settings merge to $CLAUDE_SETTINGS"
    fi
  else
    say "Skipped Tier1 user-settings merge."
  fi
}

print_claude_mcp_registration_instructions() {
  local helper="$BIN_DIR/agentstack-merge-claude-mcp"
  warn "Claude Code cannot use /delegate until the fixed 'orrery-mail' MCP entry is registered."
  printf 'Preview and apply it manually:\n' >&2
  printf '  %q %q --dry-run --config %q --mcp-url %q --mail-env %q --backup-dir %q\n' \
    "$PYTHON_BIN" "$helper" "$CLAUDE_JSON" "$MCP_URL" "$MAIL_ENV" "$BACKUPS_DIR" >&2
  printf '  %q %q --config %q --mcp-url %q --mail-env %q --backup-dir %q --existing-result %q\n' \
    "$PYTHON_BIN" "$helper" "$CLAUDE_JSON" "$MCP_URL" "$MAIL_ENV" "$BACKUPS_DIR" \
    "$MCP_MERGE_RESULT_FILE" >&2
}

confirm_claude_mcp_merge() {
  if [[ "$ASSUME_YES" == "1" ]]; then
    return 0
  fi
  if [[ ! -t 0 ]]; then
    warn "non-interactive shell; skipping Claude MCP user-config merge"
    print_claude_mcp_registration_instructions
    return 1
  fi
  printf "Register the fixed 'orrery-mail' entry in %s? Type yes to continue: " \
    "$CLAUDE_JSON" >&2
  local reply
  read -r reply
  [[ "$reply" == "yes" ]]
}

safe_merge_claude_mcp() {
  if [[ "$TIER" != "tier1" ]]; then
    return
  fi
  local merge_tool="$BIN_DIR/agentstack-merge-claude-mcp"
  if [[ "$DRY_RUN" == true ]]; then
    merge_tool="$MERGE_CLAUDE_MCP_SCRIPT"
  fi
  local merge_args=(
    "$merge_tool"
    --config "$CLAUDE_JSON"
    --mcp-url "$MCP_URL"
    --mail-env "$MAIL_ENV"
    --backup-dir "$BACKUPS_DIR"
    --existing-result "$MCP_MERGE_RESULT_FILE"
  )

  local merge_status
  merge_status="$("$PYTHON_BIN" "${merge_args[@]}" --check)"
  if [[ "$merge_status" == "configured" ]]; then
    say "Claude MCP already registered as orrery-mail in $CLAUDE_JSON"
    return
  fi
  say "Claude MCP user-config safe-merge dry-run: $CLAUDE_JSON"
  "$PYTHON_BIN" "${merge_args[@]}" --dry-run
  if [[ "$DRY_RUN" == true ]]; then
    return
  fi

  if confirm_claude_mcp_merge; then
    "$PYTHON_BIN" "${merge_args[@]}" --result-json "$MCP_MERGE_RESULT_FILE"
    if [[ "$ASSUME_YES" == "1" ]]; then
      say "assume-yes: registered orrery-mail in $CLAUDE_JSON"
    fi
  else
    say "Skipped Claude MCP user-config merge."
  fi
}

confirm_managed_setup() {
  local label="$1"
  if [[ "$ASSUME_YES" == "1" ]]; then
    return 0
  fi
  if [[ ! -t 0 ]]; then
    warn "non-interactive shell; skipping $label managed setup"
    return 1
  fi
  printf 'Apply this ORRERY Telemetry %s managed setup? Type yes to continue: ' "$label" >&2
  local reply
  read -r reply
  [[ "$reply" == "yes" ]]
}

run_managed_setup() {
  local label="$1" script_name="$2"
  local script_path setup_home
  if [[ "$DRY_RUN" == true ]]; then
    script_path="$REPO_ROOT/bin/$script_name"
    setup_home="$INSTALL_DIR"
  else
    script_path="$BIN_DIR/$script_name"
    setup_home="$INSTALL_DIR"
  fi

  say "$label managed setup dry-run:"
  AGENTSTACK_HOME="$setup_home" AGENTSTACK_TEMPLATE_HOME="$REPO_ROOT" AGENTSTACK_PROJECT_KEY="$PROJECT_KEY" "$script_path" --print
  if [[ "$DRY_RUN" == true ]]; then
    return
  fi

  if confirm_managed_setup "$label"; then
    AGENTSTACK_HOME="$INSTALL_DIR" AGENTSTACK_PROJECT_KEY="$PROJECT_KEY" "$script_path"
    if [[ "$ASSUME_YES" == "1" ]]; then
      say "assume-yes: applied $label managed setup"
    fi
  else
    say "Skipped $label managed setup."
  fi
}

project_key_is_this_checkout() {
  local resolved
  resolved="$(cd "$PROJECT_KEY" 2>/dev/null && pwd -P)" || return 1
  [[ "$resolved" == "$(cd "$REPO_ROOT" && pwd -P)" ]]
}

safe_managed_doc_setups() {
  if [[ "$TIER" != "tier1" ]]; then
    return
  fi
  # An explicitly selected project can still be this checkout. Avoid placing
  # the managed block into the stack's own tracked docs: the block belongs in
  # the project where agents will work.
  if project_key_is_this_checkout; then
    say "skip managed CLAUDE.md / AGENTS.md blocks: --project-key is this checkout"
    say "  The block belongs in the project you will run agents in. To add it:"
    say "    $BIN_DIR/agentstack-claude-setup   (with AGENTSTACK_PROJECT_KEY set)"
    say "    $BIN_DIR/agentstack-codex-setup"
    return
  fi
  run_managed_setup "Codex AGENTS.md" "agentstack-codex-setup"
  run_managed_setup "Claude CLAUDE.md" "agentstack-claude-setup"
}

# The runtime is updated at this point, but the managed blocks in the user's
# AGENTS.md / CLAUDE.md change only when the managed setup was applied. Say
# which of them still differ from the installed templates, with the command
# that updates each, instead of a generic recommendation that reads as done.
# Same comparison as agentstack-doctor (the setup scripts' --check).
report_managed_instructions() {
  local label script out status all_current=true
  local -a lines=()
  for script in agentstack-codex-setup agentstack-claude-setup; do
    status=0
    out="$(AGENTSTACK_HOME="$INSTALL_DIR" AGENTSTACK_PROJECT_KEY="$PROJECT_KEY" \
      "$BIN_DIR/$script" --check 2>&1)" || status=$?
    [[ "$status" -eq 0 ]] || all_current=false
    while IFS= read -r label; do
      [[ -n "$label" ]] && lines+=("$label")
    done <<MANAGED_CHECK
$out
MANAGED_CHECK
  done
  if [[ "$all_current" == true ]]; then
    say "Managed instructions: current (Codex AGENTS.md and Claude CLAUDE.md match the installed templates)"
    return 0
  fi
  say "Runtime updated; managed instructions differ. Agents started now still read the old block:"
  for label in ${lines[@]+"${lines[@]}"}; do
    say "  $label"
  done
}

write_env_file() {
  plan "write $ENV_FILE (mode 600, token-free)"
  if [[ "$DRY_RUN" == true ]]; then
    return
  fi
  umask 077
  "$PYTHON_BIN" - "$ENV_FILE" <<PY
import os
import pathlib
import shlex
import sys

path = pathlib.Path(sys.argv[1])
values = {
    "AGENTSTACK_PORT": "$PORT",
    "AGENTSTACK_LABEL_PREFIX": "$LABEL_PREFIX",
    # An install that chose its own label prefix gets its own mail service
    # label too. Otherwise agentstack-mailctl falls back to the built-in
    # default, and a scoped install acts on whatever job already owns that name.
    "AGENTSTACK_MAIL_LAUNCHD_LABEL": "$MAIL_LAUNCHD_LABEL_SETTING",
    "AGENTSTACK_MAIL_DB": "$MAIL_DB",
    "AGENTSTACK_MAIL_ENV": "$MAIL_ENV",
    "AGENTSTACK_MAIL_HOME": "$MAIL_HOME",
    "AGENTSTACK_SIGNALS_DIR": "$SIGNALS_DIR",
    "AGENTSTACK_MCP_URL": "$MCP_URL",
    "AGENTSTACK_CLAUDE_JSON": "$CLAUDE_JSON",
    "AGENTSTACK_TERMINAL": "$TERMINAL",
    "AGENTSTACK_AUTO_OPEN_CHILD": "$AUTO_OPEN_CHILD_SETTING",
    "AGENTSTACK_PROJECT_KEY": "$PROJECT_KEY",
    "AGENTSTACK_PROTECTED_ROOTS": "$PROTECTED_ROOTS",
    "AGENTSTACK_DELIVERABLE_ROOTS": "$DELIVERABLE_ROOTS",
    "AGENTSTACK_LANG": "$LANG_SETTING",
    "AGENTSTACK_MURMUR": "$MURMUR_SETTING",
    "AGENTSTACK_SPAWN_DIRS": "$SPAWN_DIRS_SETTING",
    "AGENTSTACK_SPAWN_ROOTS": "$SPAWN_ROOTS_SETTING",
    "AGENTSTACK_WORKTREE_ROOT": "$WORKTREE_ROOT_SETTING",
    "AGENTSTACK_CODEX_CHILD_APPROVAL": "$CODEX_CHILD_APPROVAL_SETTING",
    "AGENTSTACK_CODEX_CHILD_CONFIG_OVERLAY": "$CODEX_CHILD_CONFIG_OVERLAY_SETTING",
    "AGENTSTACK_CODEX_NETWORK": "$CODEX_NETWORK_SETTING",
    "AGENTSTACK_CHILD_DEFAULT_TOOLS": "$CHILD_DEFAULT_TOOLS_SETTING",
    "AGENTSTACK_CODEX_ADD_DIRS": "$CODEX_ADD_DIRS_SETTING",
    "AGENTSTACK_CHILD_RESUME_RETENTION_DAYS": "$CHILD_RESUME_RETENTION_DAYS_SETTING",
    "AGENTSTACK_CODEX_BIN": "$CODEX_BIN_SETTING",
    "AGENTSTACK_PORTRAITS_DIR": "$PORTRAITS_DIR_SETTING",
    "AGENTSTACK_CUSTOM_PORTRAITS": "$CUSTOM_PORTRAITS_SETTING",
    "AGENTSTACK_CLAUDE_MODELS": os.environ.get("AGENTSTACK_CLAUDE_MODELS", ""),
    "AGENTSTACK_CODEX_MODELS": "$CODEX_MODELS_SETTING",
    "AGENTSTACK_HOOKS_DIR": "$HOOKS_DIR",
    "AGENTSTACK_SKILLS_DIR": "$SKILLS_DIR",
    "AGENTSTACK_RUNTIME_DIR": "$RUNTIME_DIR",
    "AGENTSTACK_MAIL_ENROLL_BIN": "$NATIVE_MAIL_ENROLL_BIN",
    "AGENTSTACK_MAIL_MANAGEMENT_SOCKET": "$NATIVE_MAIL_MANAGEMENT_SOCKET",
    "AGENTSTACK_PERSISTENT_PROFILES_DIR": "$PERSISTENT_PROFILES_DIR",
    "AGENTSTACK_MANAGED_AGENTS_FILE": "$MANAGED_AGENTS_FILE",
    "AGENTSTACK_DASHBOARD_LOG": "$DASHBOARD_LOG",
    "AGENTSTACK_DASHBOARD_LOG_MAX_BYTES": "$DASHBOARD_LOG_MAX_BYTES",
    "AGENTSTACK_DASHBOARD_LOG_BACKUPS": "$DASHBOARD_LOG_BACKUPS",
    "AGENTSTACK_DASHBOARD_RESTART_DELAY": "$DASHBOARD_RESTART_DELAY",
    "AGENTSTACK_VAULT": "$VAULT_SETTING",
    "AGENTSTACK_PYTHON": "$PYTHON_BIN",
    "AGENTSTACK_PATH": "$PATH_VALUE",
    # Which of the values above were chosen, not written-out defaults: only
    # these are inherited by the next install.
    "AGENTSTACK_CHOSEN_SETTINGS": "$CHOSEN_SETTINGS",
}
values.update({
    "AGENTSTACK_MAIL_DIR": "$NATIVE_MAIL_SERVICE_ROOT",
    "AGENTSTACK_MAIL_STATE_ROOT": "$NATIVE_MAIL_STATE_ROOT",
    "AGENTSTACK_MAIL_HTTP_BEARER_MODE": "$MAIL_HTTP_BEARER_MODE",
})
lines = ["# Generated by ORRERY Telemetry install.sh", "# Do not put secrets in this file.", ""]
for key, value in values.items():
    lines.append(f"export {key}={shlex.quote(value)}")
path.write_text("\\n".join(lines) + "\\n", encoding="utf-8")
path.chmod(0o600)
PY
}

stop_new_agent_mail() {
  local pid
  pid="$(sed -n '1p' "$AGENT_MAIL_PIDFILE" 2>/dev/null || true)"
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
  fi
  rm -f "$AGENT_MAIL_PIDFILE"
}

native_mail_service_binaries_ready() {
  local venv="${1:-$NATIVE_MAIL_VENV}"
  [[ -x "$venv/bin/agentstack-mail" && \
     -x "$venv/bin/agentstack-mail-service" && \
     -x "$venv/bin/agentstack-mail-migrate" ]]
}

native_mail_binaries_ready() {
  native_mail_service_binaries_ready "$NATIVE_MAIL_VENV" && \
    [[ -x "$NATIVE_MAIL_VENV/bin/agentstack-enroll" ]]
}

ensure_native_mail_candidate() {
  if native_mail_binaries_ready; then
    plan "reuse immutable ORRERY Mail candidate venv $NATIVE_MAIL_VENV"
    return
  fi
  if [[ -e "$NATIVE_MAIL_VENV" ]]; then
    die "ORRERY Mail candidate venv exists but is incomplete: $NATIVE_MAIL_VENV"
  fi
  if [[ -n "$NATIVE_MAIL_VENV_EXPLICIT" ]]; then
    die "AGENTSTACK_MAIL_SERVICE_VENV does not contain the required ORRERY Mail executables: $NATIVE_MAIL_VENV"
  fi
  plan "create immutable ORRERY Mail candidate venv $NATIVE_MAIL_VENV"
  plan "install bundled ORRERY Mail package from $NATIVE_MAIL_PACKAGE_SOURCE"
  if [[ "$DRY_RUN" == true ]]; then
    return
  fi
  if [[ "$NATIVE_MAIL_PACKAGE_SOURCE" == "$REPO_ROOT/packages/agentstack_mail" ]] && \
     git -C "$REPO_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    local dirty_package
    dirty_package="$(git -C "$REPO_ROOT" status --porcelain --untracked-files=all -- packages/agentstack_mail)"
    [[ -z "$dirty_package" ]] || \
      die "bundled ORRERY Mail package is dirty; build a candidate from an exact clean commit"
  fi
  local uv_bin
  uv_bin="$(command -v uv 2>/dev/null || true)"
  [[ -n "$uv_bin" ]] || die "uv is required to install ORRERY Mail"
  mkdir -p "$(dirname "$NATIVE_MAIL_VENV")"
  if ! "$uv_bin" venv --python "$PYTHON_BIN" "$NATIVE_MAIL_VENV"; then
    die "failed to create ORRERY Mail candidate venv: $NATIVE_MAIL_VENV"
  fi
  if ! "$uv_bin" pip install --python "$NATIVE_MAIL_VENV/bin/python" \
    "$NATIVE_MAIL_PACKAGE_SOURCE"; then
    die "failed to install bundled ORRERY Mail into $NATIVE_MAIL_VENV"
  fi
  native_mail_binaries_ready || \
    die "ORRERY Mail installation completed without the required executables"
}

write_native_mail_env() {
  local parts host port path
  parts="$(mcp_local_server_parts)" || \
    die "ORRERY Mail requires a local HTTP endpoint: $MCP_URL"
  IFS='|' read -r host port path <<< "$parts"
  plan "render namespaced ORRERY Mail service env $NATIVE_MAIL_ENV"
  if [[ "$DRY_RUN" == true ]]; then
    return
  fi
  mkdir -p "$(dirname "$NATIVE_MAIL_ENV")" "$NATIVE_MAIL_SERVICE_ROOT/runtime"
  umask 077
  emit_native_mail_env "$NATIVE_MAIL_ENV" "$host" "$port" "$path" \
    "$NATIVE_MAIL_STATE_ROOT" "$NATIVE_MAIL_MANAGEMENT_SOCKET"
}

# One service env. The production render and the scratch env --update-mail
# verifies a candidate with come from this single definition, so a setting
# added here cannot be missed by the scratch copy and leave it pointing at a
# live location.
emit_native_mail_env() {
  "$PYTHON_BIN" - "$@" <<'PY'
import pathlib
import sys

target = pathlib.Path(sys.argv[1])
host, port, path, raw_root, management_socket = sys.argv[2:]
root = pathlib.Path(raw_root)
values = {
    "AGENTSTACK_MAIL_HTTP_HOST": host,
    "AGENTSTACK_MAIL_HTTP_PORT": port,
    "AGENTSTACK_MAIL_HTTP_PATH": path,
    "AGENTSTACK_MAIL_HTTP_PATH_ALIASES": "/mcp,/api",
    "AGENTSTACK_MAIL_DATABASE_URL": f"sqlite+aiosqlite:///{root / 'storage.sqlite3'}",
    "AGENTSTACK_MAIL_STORAGE_ROOT": str(root / "archive"),
    "AGENTSTACK_MAIL_NOTIFICATIONS_ENABLED": "true",
    "AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR": str(root / "signals"),
    "AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE": "passthrough",
    "AGENTSTACK_MAIL_MANAGEMENT_SOCKET": management_socket,
}
payload = "\n".join(f"{key}={value}" for key, value in values.items()) + "\n"
if target.exists() and target.read_text(encoding="utf-8") != payload:
    raise SystemExit(f"refusing to rewrite immutable service env: {target}")
target.write_text(payload, encoding="utf-8")
target.chmod(0o600)
PY
}

write_enrollment_connection_profile() {
  local profile="$CONNECTIONS_DIR/local.json"
  if [[ "$NATIVE_MAIL_ENROLL_AVAILABLE" != true ]]; then
    plan "leave local operator enrollment connection profile unchanged (enrollment unavailable)"
    return
  fi
  plan "write local operator enrollment connection profile $profile"
  if [[ "$DRY_RUN" == true ]]; then
    return
  fi
  umask 077
  "$PYTHON_BIN" - "$profile" "$NATIVE_MAIL_MANAGEMENT_SOCKET" "$RUNTIME_DIR" \
    "$MCP_URL" "$MAIL_ENV" "$MAIL_HTTP_BEARER_MODE" <<'PY'
import json
import os
import pathlib
import sys
import uuid

target = pathlib.Path(sys.argv[1])
profile = {
    "kind": "orrery-mail-connection-v1",
    "management_socket": sys.argv[2],
    "runtime_dir": sys.argv[3],
    "mcp_url": sys.argv[4],
    "mail_env": sys.argv[5],
    "http_bearer_mode": sys.argv[6],
}
if target.exists():
    existing = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(existing, dict) or existing.get("kind") != profile["kind"]:
        raise SystemExit(f"refusing to replace invalid enrollment connection profile: {target}")
    pinned = existing.get("expected_server_instance_id")
    if pinned is not None:
        if not isinstance(pinned, str) or not pinned:
            raise SystemExit(f"refusing to replace invalid enrollment authority pin: {target}")
        profile["expected_server_instance_id"] = pinned
payload = (
    json.dumps(
        profile,
        sort_keys=True,
        separators=(",", ":"),
    )
    + "\n"
).encode()
target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
target.parent.chmod(0o700)
temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.pending")
descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "wb") as handle:
    handle.write(payload)
    handle.flush()
    os.fsync(handle.fileno())
os.replace(temporary, target)
target.chmod(0o600)
PY
}

initialize_native_mail_state() {
  if [[ -f "$MAIL_DB" && -d "$NATIVE_MAIL_STATE_ROOT/archive" ]]; then
    return
  fi
  [[ ! -e "$NATIVE_MAIL_STATE_ROOT" ]] || \
    die "ORRERY Mail state root is partial; refusing initialization: $NATIVE_MAIL_STATE_ROOT"
  plan "initialize empty ORRERY Mail state at $NATIVE_MAIL_STATE_ROOT"
  if [[ "$DRY_RUN" == true ]]; then
    return
  fi
  local bootstrap_pid attempts=0
  AGENTSTACK_MAIL_ENV_FILE="$NATIVE_MAIL_ENV" \
    "$NATIVE_MAIL_VENV/bin/agentstack-mail" >> "$NATIVE_MAIL_LOG" 2>&1 &
  bootstrap_pid=$!
  while ! mcp_endpoint_listening && [[ "$attempts" -lt 150 ]]; do
    sleep 0.2
    attempts=$((attempts + 1))
  done
  if ! mcp_endpoint_listening; then
    kill "$bootstrap_pid" 2>/dev/null || true
    wait "$bootstrap_pid" 2>/dev/null || true
    die "ORRERY Mail bootstrap did not become reachable; inspect $NATIVE_MAIL_LOG"
  fi
  probe_agent_mail_database_url >/dev/null || {
    kill "$bootstrap_pid" 2>/dev/null || true
    wait "$bootstrap_pid" 2>/dev/null || true
    die "ORRERY Mail bootstrap did not return health"
  }
  kill "$bootstrap_pid" 2>/dev/null || true
  wait "$bootstrap_pid" 2>/dev/null || true
  [[ -f "$MAIL_DB" && -d "$NATIVE_MAIL_STATE_ROOT/archive" ]] || \
    die "ORRERY Mail bootstrap did not create its canonical database/archive"
}

render_native_mail_runner() {
  "$PYTHON_BIN" - "$NATIVE_MAIL_RUNNER" \
    "$NATIVE_MAIL_VENV/bin/agentstack-mail-service" \
    "$NATIVE_MAIL_VENV/bin/agentstack-mail" "$NATIVE_MAIL_ENV" \
    "$NATIVE_MAIL_STATE_ROOT" "$NATIVE_MAIL_SOURCE_ID" "$NATIVE_MAIL_VENV" \
    "$MCP_URL" "$NATIVE_MAIL_ENROLL_BIN" "$NATIVE_MAIL_DEPLOYMENT" <<'PY'
import json
import pathlib
import shlex
import sys

(
    runner,
    service,
    server,
    env_file,
    state_root,
    source_id,
    candidate_venv,
    mcp_url,
    enroll_bin,
    deployment_file,
) = sys.argv[1:]
command = [
    service, "foreground",
    "--server-executable", server,
    "--env-file", env_file,
    "--state-root", state_root,
]
payload = "\n".join([
    "#!/usr/bin/env bash",
    "set -u",
    "child_pid=''",
    "stop_runner() {",
    "  trap - TERM INT",
    "  if [[ \"$child_pid\" =~ ^[0-9]+$ ]] && kill -0 \"$child_pid\" 2>/dev/null; then",
    "    kill \"$child_pid\" 2>/dev/null || true",
    "    wait \"$child_pid\" 2>/dev/null || true",
    "  fi",
    "  exit 0",
    "}",
    "trap stop_runner TERM INT",
    "while true; do",
    f"  {shlex.join(command)} &",
    "  child_pid=$!",
    "  wait \"$child_pid\" || true",
    "  child_pid=''",
    "  sleep 5",
    "done",
    "",
])
target = pathlib.Path(runner)
if target.exists() and target.read_text(encoding="utf-8") != payload:
    raise SystemExit(f"refusing to rewrite immutable service runner: {target}")
target.write_text(payload, encoding="utf-8")
target.chmod(0o700)

deployment = {
    "kind": "orrery-mail-deployment-v1",
    "source_id": source_id,
    "candidate_venv": candidate_venv,
    "enroll_bin": enroll_bin,
    "service_env": env_file,
    "state_root": state_root,
    "mcp_url": mcp_url,
}
deployment_payload = json.dumps(
    deployment, sort_keys=True, separators=(",", ":")
) + "\n"
deployment_target = pathlib.Path(deployment_file)
if (
    deployment_target.exists()
    and deployment_target.read_text(encoding="utf-8") != deployment_payload
):
    raise SystemExit(
        f"refusing to rewrite immutable deployment metadata: {deployment_target}"
    )
deployment_target.write_text(deployment_payload, encoding="utf-8")
deployment_target.chmod(0o600)
PY
}

start_native_mail() {
  local database_url resolved_db
  plan "start ORRERY Mail with agentstack-mailctl at $MCP_URL"
  if [[ "$DRY_RUN" == true ]]; then
    return
  fi
  render_native_mail_runner
  AGENTSTACK_MAILCTL_SKIP_ENV=1 \
  AGENTSTACK_HOME="$INSTALL_DIR" \
  AGENTSTACK_MAIL_DIR="$NATIVE_MAIL_SERVICE_ROOT" \
  AGENTSTACK_MAIL_ENV="$NATIVE_MAIL_ENV" \
  AGENTSTACK_MAIL_RUNNER="$NATIVE_MAIL_RUNNER" \
  AGENTSTACK_MAIL_PIDFILE="$NATIVE_MAIL_PIDFILE" \
  AGENTSTACK_MAIL_LOG="$NATIVE_MAIL_LOG" \
  AGENTSTACK_MAIL_DB="$MAIL_DB" \
  AGENTSTACK_MCP_URL="$MCP_URL" \
  AGENTSTACK_MAIL_LAUNCHD_LABEL="$MAIL_LAUNCHD_LABEL_SETTING" \
  AGENTSTACK_PYTHON="$PYTHON_BIN" \
    "$BIN_DIR/agentstack-mailctl" start
  AGENT_MAIL_SERVICE_KIND="nohup"
  AGENT_MAIL_SERVICE_PATH="$NATIVE_MAIL_PIDFILE"
  database_url="$(probe_agent_mail_database_url || true)"
  resolved_db="$(database_url_to_path "$database_url" "" || true)"
  [[ -n "$resolved_db" ]] || {
    stop_new_agent_mail
    die "ORRERY Mail health did not report a local SQLite database"
  }
  resolved_db="$(normalize_path "$resolved_db")"
  if [[ "$resolved_db" != "$MAIL_DB" ]]; then
    stop_new_agent_mail
    die "ORRERY Mail started on '$resolved_db', expected isolated database '$MAIL_DB'"
  fi
  EXISTING_AGENT_MAIL_SERVER=true
  say "ORRERY Mail ready at $MCP_URL (database: $MAIL_DB)"
}

# agentstack-mailctl for one explicit deployment. The installed env.sh names
# the running deployment until write_env_file replaces it, so an update drives
# the controller with each render's own values instead of letting it read env.sh.
native_mailctl() {  # native_mailctl <service env> <runner> <action>
  AGENTSTACK_MAILCTL_SKIP_ENV=1 \
  AGENTSTACK_HOME="$INSTALL_DIR" \
  AGENTSTACK_MAIL_DIR="$NATIVE_MAIL_SERVICE_ROOT" \
  AGENTSTACK_MAIL_ENV="$1" \
  AGENTSTACK_MAIL_RUNNER="$2" \
  AGENTSTACK_MAIL_PIDFILE="$NATIVE_MAIL_PIDFILE" \
  AGENTSTACK_MAIL_LOG="$NATIVE_MAIL_LOG" \
  AGENTSTACK_MAIL_DB="$MAIL_DB" \
  AGENTSTACK_MCP_URL="$MCP_URL" \
  AGENTSTACK_MAIL_LAUNCHD_LABEL="$MAIL_LAUNCHD_LABEL_SETTING" \
  AGENTSTACK_PYTHON="$PYTHON_BIN" \
    "$BIN_DIR/agentstack-mailctl" "$3"
}

# Health on the configured endpoint names the shared database, and the
# controller's pidfile names this render's runner: the build answering the
# port is the one we meant to start, not something that came up in the window.
native_mail_serving_render() {  # native_mail_serving_render <runner>
  local database_url resolved_db runner
  database_url="$(probe_agent_mail_database_url || true)"
  resolved_db="$(database_url_to_path "$database_url" "" || true)"
  [[ -n "$resolved_db" ]] || { MAIL_UPDATE_REASON="health did not report a local SQLite database"; return 1; }
  resolved_db="$(normalize_path "$resolved_db")"
  [[ "$resolved_db" == "$MAIL_DB" ]] || { MAIL_UPDATE_REASON="the endpoint serves '$resolved_db', not '$MAIL_DB'"; return 1; }
  runner="$(sed -n '2p' "$NATIVE_MAIL_PIDFILE" 2>/dev/null || true)"
  [[ "$runner" == "$1" ]] || { MAIL_UPDATE_REASON="the pidfile names runner '$runner', not '$1'"; return 1; }
}

mail_update_helper() {
  "$PYTHON_BIN" - "$@" <<'PY'
import json
import os
import pathlib
import socket
import sqlite3
import sys
import time
import urllib.request


def schema(path):
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )]
        columns = {
            table: sorted(row[1] for row in connection.execute(f'PRAGMA table_info("{table}")'))
            for table in tables
        }
        # Indexes (a UNIQUE one constrains what the previous build may write)
        # and triggers, by their defining SQL. Automatic indexes have no SQL;
        # they come with their table and are compared through it.
        objects = {
            f"{kind} {name}": sql
            for kind, name, sql in connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE type IN ('index', 'trigger') AND sql IS NOT NULL"
            )
        }
        return {"columns": columns, "objects": objects}
    finally:
        connection.close()


def backup(source, target):
    # The backup API, not a file copy: the live database is in WAL mode and its
    # main file alone can miss committed pages.
    target = pathlib.Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def health_database(url):
    body = json.dumps({
        "jsonrpc": "2.0", "id": "agentstack-mail-update", "method": "tools/call",
        "params": {"name": "health_check", "arguments": {}},
    }).encode()
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    })
    with urllib.request.urlopen(request, timeout=5) as response:
        raw = response.read().decode("utf-8", errors="replace")
    for line in raw.splitlines():
        if line.startswith("data:"):
            raw = line[5:].strip()
            break
    result = json.loads(raw)["result"]
    health = result.get("structuredContent") or {}
    if not health:
        for block in result.get("content") or []:
            if block.get("type") == "text":
                health = json.loads(block.get("text") or "{}")
                break
    return str(health.get("database_url") or "")


command, *args = sys.argv[1:]
if command == "free-port":
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        print(probe.getsockname()[1])
elif command == "port-free":
    # 0 only when a connect is refused: something answering, or a probe that
    # cannot tell, is never treated as free.
    try:
        with socket.create_connection(("127.0.0.1", int(args[0])), timeout=0.5):
            pass
    except ConnectionRefusedError:
        raise SystemExit(0)
    except OSError as exc:
        print(f"could not probe port {args[0]}: {exc}", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(1)
elif command == "snapshot":
    live, target, schema_file = args
    backup(live, target)
    pathlib.Path(schema_file).write_text(json.dumps(schema(target)), encoding="utf-8")
elif command == "wait-health":
    # wait-health <server pid> <timeout> <expected db> <url>...
    pid, timeout, expected, *urls = args
    expected_path = pathlib.Path(expected).resolve()
    deadline = time.monotonic() + float(timeout)
    pending = list(urls)
    last = "not started"
    started = time.monotonic()
    next_report = started + 10
    while pending and time.monotonic() < deadline:
        if time.monotonic() >= next_report:
            # A silent two-minute wait invites Ctrl-C; say that it is waiting.
            print(f"still waiting for the candidate to answer health "
                  f"({int(time.monotonic() - started)}s of {timeout}s)", flush=True)
            next_report += 10
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            print("the candidate server exited before it answered health", file=sys.stderr)
            raise SystemExit(1)
        try:
            database_url = health_database(pending[0])
        except Exception as exc:  # readiness retry boundary
            last = str(exc)
            time.sleep(0.2)
            continue
        served = database_url.split(":///", 1)[-1].split("?", 1)[0]
        if not served or pathlib.Path(served).resolve() != expected_path:
            print(f"{pending[0]} reports database {database_url!r}, not the scratch snapshot", file=sys.stderr)
            raise SystemExit(1)
        pending.pop(0)
    if pending:
        print(f"{pending[0]} did not answer health within {timeout}s ({last})", file=sys.stderr)
        raise SystemExit(1)
    if os.environ.get("MAIL_UPDATE_READY_FILE"):
        # How long the candidate took to answer on a copy of the real database:
        # the start part of the outage a switch would cause.
        pathlib.Path(os.environ["MAIL_UPDATE_READY_FILE"]).write_text(f"{time.monotonic() - started:.1f}\n")
elif command == "schema-kept":
    # The candidate ran its startup DDL on the snapshot. Additive changes are
    # what the running build tolerates on rollback; a table or column that
    # disappeared is not, and that needs its own migration plan.
    schema_file, database = args
    before_all = json.loads(pathlib.Path(schema_file).read_text(encoding="utf-8"))
    after_all = schema(database)
    before, after = before_all["columns"], after_all["columns"]
    missing = [
        f"{table}.{column}" if column else table
        for table, columns in sorted(before.items())
        for column in ([None] if table not in after else [c for c in columns if c not in after[table]])
    ]
    missing += sorted(
        name for name in before_all["objects"] if name not in after_all["objects"]
    )
    changed = sorted(
        name for name, sql in before_all["objects"].items()
        if name in after_all["objects"] and after_all["objects"][name] != sql
    )
    if missing:
        print("the candidate removed schema the running build uses: " + ", ".join(missing), file=sys.stderr)
        raise SystemExit(1)
    if changed:
        print("the candidate redefined schema the running build uses: " + ", ".join(changed), file=sys.stderr)
        raise SystemExit(1)
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        check = connection.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        connection.close()
    if check != "ok":
        print(f"the snapshot failed quick_check after the candidate ran: {check}", file=sys.stderr)
        raise SystemExit(1)
    added = sorted(set(after) - set(before))
    added += sorted(
        f"{table}.{column}" for table in before if table in after
        for column in set(after[table]) - set(before[table])
    )
    added += sorted(set(after_all["objects"]) - set(before_all["objects"]))
    print("schema additions: " + (", ".join(added) if added else "none"))
elif command == "backup":
    live, directory, stem, keep = args
    directory = pathlib.Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    target = directory / f"storage-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-before-{stem}.sqlite3"
    backup(live, target)
    kept = sorted(directory.glob("storage-*-before-*.sqlite3"))
    for old in kept[:-int(keep)]:
        old.unlink()
    print(target)
else:
    raise SystemExit(f"unknown helper command {command}")
PY
}

# Scratch state of the offline check, global so the signal handler can reach it.
MAIL_VERIFY_SCRATCH=""
MAIL_VERIFY_SERVER_PID=""
MAIL_VERIFY_HELPER_PID=""

stop_mail_verify_pid() {  # stop_mail_verify_pid <pid>: TERM, wait up to 10 s, then KILL
  local pid="$1" waited=0
  [[ -n "$pid" ]] || return 0
  kill "$pid" 2>/dev/null || return 0
  while kill -0 "$pid" 2>/dev/null && (( waited < 100 )); do
    sleep 0.1
    waited=$((waited + 1))
  done
  kill -KILL "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
}

# The snapshot holds every message and every agent's registration token, and
# the scratch server serves it on loopback without authentication. Neither may
# outlive the check -- not on success, not on failure, not on Ctrl-C.
cleanup_mail_verify() {
  stop_mail_verify_pid "$MAIL_VERIFY_HELPER_PID"
  stop_mail_verify_pid "$MAIL_VERIFY_SERVER_PID"
  MAIL_VERIFY_HELPER_PID=""
  MAIL_VERIFY_SERVER_PID=""
  if [[ -n "$MAIL_VERIFY_SCRATCH" ]]; then
    rm -rf "$MAIL_VERIFY_SCRATCH"
    MAIL_VERIFY_SCRATCH=""
  fi
}

mail_verify_interrupted() {
  trap - EXIT INT TERM HUP
  cleanup_mail_verify
  warn "interrupted while verifying the ORRERY Mail candidate; the scratch server and snapshot were removed and the running Mail was not stopped"
  exit 130
}

# Start the candidate on a free loopback port against a snapshot of the shared
# database, in an empty process environment (python-decouple prefers the
# process environment over the env file, so an inherited AGENTSTACK_MAIL_*
# would point the scratch server at live state). Nothing here touches the
# running service, its database file, its archive, signals or management socket.
verify_native_mail_candidate_offline() {
  local scratch port parts path live_port rc=1 timeout log_copy
  timeout="${AGENTSTACK_MAIL_UPDATE_VERIFY_TIMEOUT:-120}"
  parts="$(mcp_local_server_parts)" || { MAIL_UPDATE_REASON="ORRERY Mail requires a local HTTP endpoint"; return 1; }
  IFS='|' read -r _ live_port path <<< "$parts"
  log_copy="$NATIVE_MAIL_SERVICE_ROOT/runtime/mail-update-verify.log"
  # Armed before the scratch directory exists, so no window leaves it behind.
  trap mail_verify_interrupted INT TERM HUP
  trap cleanup_mail_verify EXIT
  # Short on purpose: the management socket lives inside it, and a Unix socket
  # path is limited to about 100 bytes.
  if ! scratch="$(mktemp -d /tmp/orrery-mail-verify.XXXXXX)"; then
    trap - EXIT INT TERM HUP
    MAIL_UPDATE_REASON="could not create a scratch directory"
    return 1
  fi
  MAIL_VERIFY_SCRATCH="$scratch"
  chmod 700 "$scratch"
  mkdir -p "$scratch/state/archive" "$scratch/state/signals" "$scratch/home"
  port="${AGENTSTACK_MAIL_UPDATE_VERIFY_PORT:-}"
  if [[ -n "$port" && ! "$port" =~ ^[1-9][0-9]{0,4}$ ]]; then
    MAIL_UPDATE_REASON="AGENTSTACK_MAIL_UPDATE_VERIFY_PORT is not a port number: $port"
  elif [[ -n "$port" && "$port" == "$live_port" ]]; then
    MAIL_UPDATE_REASON="the scratch port $port is the live ORRERY Mail port; choose another AGENTSTACK_MAIL_UPDATE_VERIFY_PORT"
  elif [[ -z "$port" ]] && ! port="$(mail_update_helper free-port)"; then
    MAIL_UPDATE_REASON="could not pick a scratch port"
  elif ! mail_update_helper port-free "$port" 2>/dev/null; then
    # Never start next to, or probe as ours, whatever holds the port: its
    # health answer would be read as the candidate's.
    MAIL_UPDATE_REASON="the scratch port $port is in use; nothing was started on it"
  elif ! mail_update_helper snapshot "$MAIL_DB" "$scratch/state/storage.sqlite3" "$scratch/schema.json" 2> "$scratch/snapshot.err"; then
    MAIL_UPDATE_REASON="could not snapshot $MAIL_DB: $(tail -n 1 "$scratch/snapshot.err")"
  elif ! emit_native_mail_env "$scratch/service.env" 127.0.0.1 "$port" "$path" "$scratch/state" "$scratch/mgmt.sock"; then
    MAIL_UPDATE_REASON="could not render the scratch service env"
  else
    say "verifying candidate $NATIVE_MAIL_VENV on scratch port $port in $scratch (waits up to ${timeout}s)"
    env -i HOME="$scratch/home" PATH=/usr/bin:/bin AGENTSTACK_MAIL_ENV_FILE="$scratch/service.env" \
      "$NATIVE_MAIL_VENV/bin/agentstack-mail" > "$scratch/server.log" 2>&1 &
    MAIL_VERIFY_SERVER_PID=$!
    # In the background and collected with `wait`: bash runs a trap only after
    # a foreground command returns, so a SIGTERM would otherwise wait out the
    # whole health timeout before anything was cleaned up.
    MAIL_UPDATE_READY_FILE="$scratch/ready.seconds" \
      mail_update_helper wait-health "$MAIL_VERIFY_SERVER_PID" "$timeout" "$scratch/state/storage.sqlite3" \
      "http://127.0.0.1:$port$path" "http://127.0.0.1:$port/api" 2> "$scratch/probe.err" &
    MAIL_VERIFY_HELPER_PID=$!
    if wait "$MAIL_VERIFY_HELPER_PID"; then
      rc=0
      MAIL_UPDATE_START_SECONDS="$(cat "$scratch/ready.seconds" 2>/dev/null || true)"
    else
      MAIL_UPDATE_REASON="candidate failed offline verification: $(tail -n 1 "$scratch/probe.err")"
    fi
    MAIL_VERIFY_HELPER_PID=""
    stop_mail_verify_pid "$MAIL_VERIFY_SERVER_PID"
    MAIL_VERIFY_SERVER_PID=""
    if [[ "$rc" -eq 0 ]]; then
      if mail_update_helper schema-kept "$scratch/schema.json" "$scratch/state/storage.sqlite3" > "$scratch/schema.out" 2> "$scratch/schema.err"; then
        say "candidate verified offline ($(cat "$scratch/schema.out"); answered in ${MAIL_UPDATE_START_SECONDS:-?}s)"
      else
        rc=1
        MAIL_UPDATE_REASON="$(tail -n 1 "$scratch/schema.err")"
      fi
    fi
    mkdir -p "$(dirname "$log_copy")"
    cp "$scratch/server.log" "$log_copy" 2>/dev/null || true
  fi
  cleanup_mail_verify
  trap - EXIT INT TERM HUP
  return "$rc"
}

# From the stop until env.sh names the build that is serving, an interrupt
# would leave a stopped Mail held down by the stop marker, or a new runner
# that env.sh does not name (the autostart sweep and `status` then refuse it).
# Finish the job either way before exiting: if env.sh already names the new
# render, that is consistent and is left alone; otherwise the previous build
# is put back, as after a failed switch.
MAIL_UPDATE_SWITCH_ARMED=false
MAIL_UPDATE_STOPPED_AT=""

mail_update_switch_interrupted() {
  trap - INT TERM HUP
  MAIL_UPDATE_SWITCH_ARMED=false
  if [[ "$(installed_env_mail_env)" == "$NATIVE_MAIL_ENV" ]] && \
     native_mail_serving_render "$NATIVE_MAIL_RUNNER"; then
    warn "installer interrupted after ORRERY Mail was switched; $NATIVE_MAIL_ENV is serving and env.sh names it. Re-run install.sh to finish the rest of the install"
    mail_update_interrupted_hint
    exit 130
  fi
  MAIL_UPDATE_REASON="the installer was interrupted during the switch"
  rollback_native_mail
  mail_update_use OLD
  write_enrollment_connection_profile
  warn "installer interrupted; ORRERY Mail was restored to $NATIVE_MAIL_ENV, which env.sh still names. Re-run install.sh --update-mail to try again"
  mail_update_interrupted_hint
  exit 130
}

mail_update_disarm_switch() {
  if [[ "$MAIL_UPDATE_SWITCH_ARMED" == true ]]; then
    trap - INT TERM HUP
    MAIL_UPDATE_SWITCH_ARMED=false
  fi
}

switch_native_mail() {
  say "stopping ORRERY Mail $OLD_NATIVE_MAIL_ENV"
  if ! native_mailctl "$OLD_NATIVE_MAIL_ENV" "$OLD_NATIVE_MAIL_RUNNER" stop; then
    MAIL_UPDATE_REASON="agentstack-mailctl could not stop the running build"
    return 1
  fi
  # AGENTSTACK_MAIL_UPDATE_START_GRACE bounds only the new build's start; the
  # rollback below starts the previous build with the controller's own grace.
  local grace="${AGENTSTACK_MAIL_UPDATE_START_GRACE:-${AGENTSTACK_MAIL_START_GRACE:-180}}"
  say "starting ORRERY Mail $NATIVE_MAIL_ENV (waits up to ${grace}s for it to answer)"
  if ! AGENTSTACK_MAIL_START_GRACE="$grace" \
      native_mailctl "$NATIVE_MAIL_ENV" "$NATIVE_MAIL_RUNNER" start; then
    MAIL_UPDATE_REASON="the new build did not start (see $NATIVE_MAIL_LOG)"
    return 1
  fi
  native_mail_serving_render "$NATIVE_MAIL_RUNNER"
}

# Monotonic, so a clock adjustment during the switch cannot bend the outage.
# CLOCK_MONOTONIC is one clock for the whole system, so two processes compare.
mail_update_clock() {
  "$PYTHON_BIN" -c 'import time; print(time.monotonic())'
}

mail_update_elapsed() {  # mail_update_elapsed <start from mail_update_clock>
  "$PYTHON_BIN" -c 'import sys, time; print(f"{time.monotonic() - float(sys.argv[1]):.1f}")' "$1"
}

# The text is scripts/lib/mail_update_notice.py, shared with doctor and the
# cockpit's setup.sh so that the advice is worded once.
# After any stop, whatever its length: this installer's health check and a
# selftest prove that Mail answers, not that a session already running got its
# connection back. Proxy-connected children and Codex reconnect on their next
# call; Claude Code's own HTTP connection (the orrery-mail entry in
# ~/.claude.json) came back after outages up to 15 s in testing and stayed
# disconnected after 18 s, so each running session is the one to check.
mail_update_reconnect_hint() {
  "$PYTHON_BIN" "$SCRIPT_DIR/lib/mail_update_notice.py" after-stop --outage "${MAIL_UPDATE_OUTAGE_SECONDS:-?}" || \
    say "ORRERY Mail was unavailable for ${MAIL_UPDATE_OUTAGE_SECONDS:-?}s; check each running Claude Code session with /mcp"
}

# An interrupted switch stopped Mail too: running sessions need the same
# check. The outage is measured when the stop time is known.
mail_update_interrupted_hint() {
  MAIL_UPDATE_OUTAGE_SECONDS="?"
  [[ -z "$MAIL_UPDATE_STOPPED_AT" ]] || \
    MAIL_UPDATE_OUTAGE_SECONDS="$(mail_update_elapsed "$MAIL_UPDATE_STOPPED_AT" 2>/dev/null || echo "?")"
  mail_update_reconnect_hint
}

# Put the build that was running back. Its render, runner and candidate were
# never modified (all three are immutable), so this is the start that was
# already working before the update. A failure here is reported with the exact
# state and ends the run: there is no further fallback to try.
rollback_native_mail() {
  local runner
  warn "ORRERY Mail update failed: $MAIL_UPDATE_REASON"
  warn "restoring the previous ORRERY Mail build $OLD_NATIVE_MAIL_ENV"
  runner="$(sed -n '2p' "$NATIVE_MAIL_PIDFILE" 2>/dev/null || true)"
  if [[ "$runner" == "$NATIVE_MAIL_RUNNER" ]]; then
    native_mailctl "$NATIVE_MAIL_ENV" "$NATIVE_MAIL_RUNNER" stop || \
      die "rollback failed: could not stop the new ORRERY Mail runner recorded in $NATIVE_MAIL_PIDFILE; env.sh still names $OLD_NATIVE_MAIL_ENV. Stop that runner, then run $BIN_DIR/agentstack-mailctl start"
  fi
  if ! native_mailctl "$OLD_NATIVE_MAIL_ENV" "$OLD_NATIVE_MAIL_RUNNER" start; then
    # A start that ran out of grace leaves its runner alive and still coming
    # up (a loaded machine is enough). That runner is the previous build: wait
    # for it rather than declare the rollback failed while it is starting.
    local waited=0 limit="${AGENTSTACK_MAIL_START_GRACE:-180}"
    while ! native_mail_serving_render "$OLD_NATIVE_MAIL_RUNNER" 2>/dev/null; do
      [[ "$(sed -n '2p' "$NATIVE_MAIL_PIDFILE" 2>/dev/null || true)" == "$OLD_NATIVE_MAIL_RUNNER" ]] && \
        (( waited < limit )) || \
        die "rollback failed: the previous ORRERY Mail build did not start; env.sh still names $OLD_NATIVE_MAIL_ENV. Inspect $NATIVE_MAIL_LOG, then run $BIN_DIR/agentstack-mailctl start"
      sleep 1
      waited=$((waited + 1))
    done
  fi
  native_mail_serving_render "$OLD_NATIVE_MAIL_RUNNER" || \
    die "rollback failed: $MAIL_UPDATE_REASON; env.sh still names $OLD_NATIVE_MAIL_ENV"
  say "restored ORRERY Mail $OLD_NATIVE_MAIL_ENV"
}

# --update-mail. Everything that can fail slowly or be refused happens while
# the running build keeps serving: building the candidate, verifying it on a
# scratch port against a snapshot, backing up the database. Only then is the
# running build stopped, and the new one must answer on the real endpoint with
# the shared database or the old one is started again. env.sh, the autostart
# unit and install-state.json are written afterwards from whichever deployment
# ended up serving.
update_native_mail() {
  local backup
  mail_update_use NEW
  ensure_native_mail_candidate
  write_native_mail_env
  plan "verify candidate $NATIVE_MAIL_VENV on a scratch port against a snapshot of $MAIL_DB"
  plan "back up $MAIL_DB under $NATIVE_MAIL_SERVICE_ROOT/backups"
  plan "stop ORRERY Mail $OLD_NATIVE_MAIL_ENV, start $NATIVE_MAIL_ENV, and restore the previous build if the new one is not serving"
  if [[ "$DRY_RUN" == true ]]; then
    return
  fi
  if ! verify_native_mail_candidate_offline; then
    MAIL_UPDATE_RESULT="not-switched"
    MAIL_RESULT_CODE=verify_failed
    warn "ORRERY Mail was not switched: $MAIL_UPDATE_REASON"
    warn "the running build $OLD_NATIVE_MAIL_ENV was not stopped (server log: $NATIVE_MAIL_SERVICE_ROOT/runtime/mail-update-verify.log)"
    mail_update_use OLD
    return
  fi
  # The outage is the stop (well under a second) plus the time the candidate
  # took to answer on a copy of this database, plus the health poll interval.
  # Claude Code's direct HTTP connection gave up on outages of 18 s or more in
  # testing (2026-10-02), so auto keeps well under that.
  local budget estimate
  budget="${AGENTSTACK_MAIL_UPDATE_OUTAGE_BUDGET:-8}"
  estimate="$("$PYTHON_BIN" -c 'import math, sys; print(math.ceil(float(sys.argv[1]) + 1.5))' "${MAIL_UPDATE_START_SECONDS:-}" 2>/dev/null || true)"
  # Without a measured start the outage is unknown, which no budget allows.
  if [[ "$MAIL_UPDATE_MODE" == auto && ! "$estimate" =~ ^[0-9]{1,4}$ ]]; then
    MAIL_UPDATE_RESULT="not-switched"
    MAIL_RESULT_CODE=outage_unknown
    MAIL_UPDATE_REASON="the candidate's start time was not measured, so the outage cannot be estimated"
    warn "ORRERY Mail was not switched: $MAIL_UPDATE_REASON; the running build was not stopped"
    mail_update_use OLD
    return
  fi
  # Decimal on purpose: bash arithmetic reads 08 as a bad octal number, and an
  # arithmetic error in a test is false -- which would switch over budget.
  if [[ "$MAIL_UPDATE_MODE" == auto && ! "$budget" =~ ^[0-9]{1,4}$ ]]; then
    MAIL_UPDATE_RESULT="not-switched"
    MAIL_RESULT_CODE=outage_over_budget
    MAIL_UPDATE_REASON="AGENTSTACK_MAIL_UPDATE_OUTAGE_BUDGET is not a number of seconds: $budget"
    warn "ORRERY Mail was not switched: $MAIL_UPDATE_REASON"
    mail_update_use OLD
    return
  fi
  if [[ "$MAIL_UPDATE_MODE" == auto ]] && ! (( 10#$estimate <= 10#$budget )); then
    MAIL_UPDATE_RESULT="not-switched"
    MAIL_RESULT_CODE=outage_over_budget
    MAIL_UPDATE_REASON="the switch would stop ORRERY Mail for about ${estimate}s, over the ${budget}s budget"
    warn "ORRERY Mail was not switched: $MAIL_UPDATE_REASON; the running build was not stopped"
    warn "to switch anyway at a time you choose: install.sh --update-mail (or raise AGENTSTACK_MAIL_UPDATE_OUTAGE_BUDGET)"
    mail_update_use OLD
    return
  fi
  if ! backup="$(mail_update_helper backup "$MAIL_DB" "$NATIVE_MAIL_SERVICE_ROOT/backups" "$NATIVE_MAIL_SOURCE_ID" "${AGENTSTACK_MAIL_UPDATE_BACKUPS:-3}")"; then
    MAIL_UPDATE_RESULT="not-switched"
    MAIL_RESULT_CODE=backup_failed
    MAIL_UPDATE_REASON="could not back up $MAIL_DB"
    warn "ORRERY Mail was not switched: $MAIL_UPDATE_REASON"
    mail_update_use OLD
    return
  fi
  MAIL_UPDATE_BACKUP="$backup"
  say "backed up ORRERY Mail database to $backup"
  say "switching ORRERY Mail from $OLD_NATIVE_MAIL_SOURCE_ID to $NATIVE_MAIL_SOURCE_ID: Mail is unavailable for about ${estimate:-?}s"
  say "  agents' Mail calls fail meanwhile and work again afterwards; the database, tokens and registrations are unchanged"
  render_native_mail_runner
  # Disarmed in main once env.sh names the serving build.
  MAIL_UPDATE_SWITCH_ARMED=true
  trap mail_update_switch_interrupted INT TERM HUP
  MAIL_UPDATE_STOPPED_AT="$(mail_update_clock)"
  if switch_native_mail; then
    MAIL_UPDATE_RESULT="switched"
    MAIL_UPDATE_OUTAGE_SECONDS="$(mail_update_elapsed "$MAIL_UPDATE_STOPPED_AT")"
    say "ORRERY Mail switched from $OLD_NATIVE_MAIL_SOURCE_ID to $NATIVE_MAIL_SOURCE_ID (unavailable for ${MAIL_UPDATE_OUTAGE_SECONDS}s)"
    mail_update_reconnect_hint
    return
  fi
  mail_update_disarm_switch
  rollback_native_mail
  MAIL_UPDATE_OUTAGE_SECONDS="$(mail_update_elapsed "$MAIL_UPDATE_STOPPED_AT")"
  MAIL_UPDATE_RESULT="rolled-back"
  MAIL_RESULT_CODE=start_failed_rolled_back
  mail_update_use OLD
  mail_update_reconnect_hint
}

ensure_native_agentstack_mail() {
  AGENT_MAIL_NAME_CAPABILITY_JSON='{"status":"honored","evidence":"agentstack-cutover-profile","enforcement_mode":"passthrough","mail_dir":"","detail":"ORRERY Mail service env requires passthrough","warning":""}'
  if [[ "$NATIVE_MAIL_EXISTING" == true ]]; then
    if [[ "$MAIL_UPDATE_PLANNED" == true ]]; then
      update_native_mail
      return
    fi
    plan "reuse existing ORRERY Mail service at $MCP_URL"
    return
  fi
  ensure_native_mail_candidate
  write_native_mail_env
  initialize_native_mail_state
  start_native_mail
}

# Deliberately minimal. `agentstack-mailctl` sources $AGENTSTACK_HOME/env.sh
# unless AGENTSTACK_MAILCTL_SKIP_ENV=1, so the unit runs the *same* command a
# user runs by hand and reads the same configuration. Baking the mail paths into
# the unit instead would drift the moment a re-install renders a new service env,
# and the drift would only show up after the next reboot.
mail_autostart_environment() {
  cat <<ENVLIST
HOME=$HOME
AGENTSTACK_HOME=$INSTALL_DIR
PATH=$PATH_VALUE
AGENTSTACK_MAILCTL_SWEEP=1
ENVLIST
} # end mail_autostart_environment

# One-shot on purpose: `agentstack-mailctl start` spawns the runner with nohup and
# exits, so a KeepAlive/Restart=always unit would respawn the controller in a loop
# instead of supervising the server. `start` is idempotent — it reports "already
# running" and exits 0 when the pidfile process is alive and healthy — so running
# it at every login is safe.
render_mail_autostart_unit() {
  local kind="$1"
  # Its own log: the trigger re-checks every few minutes and would otherwise
  # write "already running" into the mail server's own log forever. Derived here
  # because NATIVE_MAIL_SERVICE_ROOT is still being resolved at parse time.
  local autostart_log="$NATIVE_MAIL_SERVICE_ROOT/runtime/agentstack-mail-autostart.log"
  if [[ "$kind" == "launchd" ]]; then
    AGENT_MAIL_AUTOSTART_PATH="$HOME/Library/LaunchAgents/$MAIL_AUTOSTART_LABEL.plist"
    plan "render launchd plist $AGENT_MAIL_AUTOSTART_PATH (ORRERY Mail autostart)"
    [[ "$DRY_RUN" == true ]] && return 0
    mkdir -p "$HOME/Library/LaunchAgents"
    # The env goes through argv, not a pipe: the `<<'PY'` heredoc already owns
    # this command's stdin, so a pipe would be silently discarded and the plist
    # would ship with an empty EnvironmentVariables block.
    "$PYTHON_BIN" - \
      "$AGENT_MAIL_AUTOSTART_PATH" "$MAIL_AUTOSTART_LABEL" \
      "$BIN_DIR/agentstack-mailctl" "$autostart_log" \
      "$(mail_autostart_environment)" <<'PY'
import pathlib
import plistlib
import sys

dst, label, mailctl, logfile, env_blob = sys.argv[1:6]
env = {}
for line in env_blob.splitlines():
    if "=" in line:
        key, value = line.split("=", 1)
        env[key] = value

plist = {
    "Label": label,
    "ProgramArguments": [mailctl, "start"],
    "RunAtLoad": True,
    # Not KeepAlive: mailctl exits after handing the server to nohup, so launchd
    # would respawn the *controller* in a loop instead of supervising the server.
    "KeepAlive": False,
    # The server outlives the trigger, so launchd must not clean it up. When a
    # job exits, the processes still in its process group are subject to
    # launchd's cleanup, and `nohup` does not change the group: without this
    # key the runner and the server this trigger itself spawned were gone
    # right after the job exited -- by the next 2 s observation -- although
    # "ORRERY Mail started" had been logged (a rebooted Mac, 2026-09-17). A
    # server already running in another group is not affected. The systemd
    # unit's KillMode=process serves the same purpose.
    "AbandonProcessGroup": True,
    # Re-check periodically instead. `start` is idempotent — it reports "already
    # running" and exits 0 — so this is a cheap liveness sweep that also covers
    # the cases RunAtLoad alone cannot: the runner being killed mid-session, and
    # a port that was briefly contended at login.
    "StartInterval": 300,
    "StandardOutPath": logfile,
    "StandardErrorPath": logfile,
    "EnvironmentVariables": env,
}
path = pathlib.Path(dst)
tmp = path.with_suffix(path.suffix + ".tmp")
tmp.write_bytes(plistlib.dumps(plist))
tmp.replace(path)
PY
    return 0
  fi

  AGENT_MAIL_AUTOSTART_PATH="$HOME/.config/systemd/user/$MAIL_AUTOSTART_LABEL.timer"
  AGENT_MAIL_AUTOSTART_SERVICE_PATH="$HOME/.config/systemd/user/$MAIL_AUTOSTART_LABEL.service"
  plan "render systemd user units $AGENT_MAIL_AUTOSTART_SERVICE_PATH and $AGENT_MAIL_AUTOSTART_PATH (ORRERY Mail autostart)"
  [[ "$DRY_RUN" == true ]] && return 0
  mkdir -p "$HOME/.config/systemd/user"
  {
    # NOT After=default.target: a unit installed into default.target.wants/ is
    # already ordered by systemd's own Wants completion, and ordering against
    # default.target as well is a cycle, which systemd resolves by dropping a
    # job. The dashboard unit uses network.target for the same reason.
    printf '[Unit]\nDescription=ORRERY Mail autostart\nAfter=network.target\n\n'
    # No RemainAfterExit: the timer re-runs this unit, and a unit left "active"
    # after exiting would never be started again.
    # KillMode=process: mailctl hands the server to nohup and exits, and the
    # default control-group KillMode then kills the server the moment the
    # oneshot finishes (seen on WSL2 Ubuntu 26.04: health ok, then "Shutting
    # down" in the same second). Only the controller is the unit's process.
    printf '[Service]\nType=oneshot\nKillMode=process\n'
    # systemd splits unquoted values on whitespace, so a HOME or install dir
    # containing a space would silently truncate every Environment= value and
    # the ExecStart path. Quote them the way systemd expects.
    "$PYTHON_BIN" - "$BIN_DIR/agentstack-mailctl" "$(mail_autostart_environment)" <<'PY_UNIT'
import sys

mailctl, env_blob = sys.argv[1:3]


def quote(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


for line in env_blob.splitlines():
    if "=" in line:
        key, value = line.split("=", 1)
        print(f"Environment={key}={quote(value)}")
print(f"ExecStart={quote(mailctl)} start")
print()
PY_UNIT
    # systemd has no equivalent of launchd's StandardOutPath in the plist, so
    # say it here or the "output goes to a dedicated log" promise is false on
    # Linux (it was).
    printf 'StandardOutput=append:%s\nStandardError=append:%s\n' \
      "$autostart_log" "$autostart_log"
  } > "$AGENT_MAIL_AUTOSTART_SERVICE_PATH.tmp"
  mv "$AGENT_MAIL_AUTOSTART_SERVICE_PATH.tmp" "$AGENT_MAIL_AUTOSTART_SERVICE_PATH"
  # The timer is what gets enabled: it covers boot *and* keeps checking, which a
  # login-only trigger cannot (a runner killed mid-session stays dead until the
  # next login). launchd gets the same behaviour from StartInterval.
  {
    printf '[Unit]\nDescription=ORRERY Mail autostart timer\n\n'
    # No Persistent=: it only affects OnCalendar timers, and implying that a
    # missed monotonic firing is caught up after resume is simply wrong.
    printf '[Timer]\nOnBootSec=1min\nOnUnitActiveSec=5min\nAccuracySec=30s\n'
    printf 'Unit=%s.service\n\n' "$MAIL_AUTOSTART_LABEL"
    printf '[Install]\nWantedBy=timers.target\n'
  } > "$AGENT_MAIL_AUTOSTART_PATH.tmp"
  mv "$AGENT_MAIL_AUTOSTART_PATH.tmp" "$AGENT_MAIL_AUTOSTART_PATH"
} # end render_mail_autostart_unit

wait_for_launchd_unload() {
  local target="$1"
  local waited=0
  while launchctl print "$target" >/dev/null 2>&1; do
    if (( waited >= 50 )); then
      return 1
    fi
    sleep 0.1
    waited=$((waited + 1))
  done
  return 0
} # end wait_for_launchd_unload

retire_legacy_mail_services() {
  # After a cutover the predecessor keeps running unless something retires it,
  # and nothing did: the only code that boots out a previous job is the
  # same-port handoff, which never fires because the new server binds a
  # different port, and the legacy label it looks for is a single guessed
  # string (com.<user>.mcp-agent-mail) that does not match what older
  # installers actually registered (org.agentstack.mcp-agent-mail). Reported by
  # a tester on 2026-08-17 with both jobs loaded and both listening. Two mail
  # servers own two databases, so agent identities and file reservations split
  # across two stores depending on which endpoint a client reaches.
  #
  # Retiring is opt-in. Stopping a service someone is running is not a decision
  # an installer gets to make silently, so the default is to report the
  # collision and let the operator choose.
  if [[ "${LEGACY_MAIL_SCAN_COMPLETE:-false}" == true ]]; then
    return 0
  fi
  LEGACY_MAIL_SCAN_COMPLETE=true

  if [[ "$(uname -s)" != "Darwin" ]] || ! command -v launchctl >/dev/null 2>&1; then
    return 0
  fi

  local labels=()
  if [[ -n "${AGENTSTACK_MAIL_LEGACY_LAUNCHD_LABELS:-}" ]]; then
    IFS=',' read -r -a labels <<< "$AGENTSTACK_MAIL_LEGACY_LAUNCHD_LABELS"
  else
    labels=("com.$(id -un).mcp-agent-mail" "org.agentstack.mcp-agent-mail")
  fi

  # Labels this install owns are never candidates, whatever the environment
  # says. Without this, one stray variable retires the server being installed.
  # Every label this install registers, by value. Denying the whole
  # "$LABEL_PREFIX." prefix would be simpler and wrong: the legacy job we exist
  # to retire is "org.agentstack.mcp-agent-mail", which lives under that very
  # prefix.
  local protected=(
    "${MAIL_LAUNCHD_LABEL_SETTING:-org.orrery.mail}"
    "${LABEL:-}"
    "${MAIL_AUTOSTART_LABEL:-}"
  )

  local label protected_label
  for label in "${labels[@]}"; do
    label="${label// /}"
    [[ -n "$label" ]] || continue
    for protected_label in "${protected[@]}"; do
      if [[ -n "$protected_label" && "$label" == "$protected_label" ]]; then
        warn "refusing to treat '$label' as a legacy mail service: this install owns it"
        continue 2
      fi
    done
    launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1 || continue

    local plist="$HOME/Library/LaunchAgents/$label.plist"
    # Only retire a job that is demonstrably a predecessor of this service.
    # "A label is loaded" is not evidence: the label may have been supplied by
    # the environment and belong to something else entirely.
    if ! legacy_mail_plist_looks_like_mail "$plist"; then
      warn "leaving '$label' alone: its launchd definition does not look like a mail service"
      continue
    fi

    case ",${LEGACY_MAIL_DETECTED_LABELS:-}," in
      *",$label,"*) ;;
      *)
        if [[ -n "${LEGACY_MAIL_DETECTED_LABELS:-}" ]]; then
          LEGACY_MAIL_DETECTED_LABELS="$LEGACY_MAIL_DETECTED_LABELS,$label"
        else
          LEGACY_MAIL_DETECTED_LABELS="$label"
        fi
        ;;
    esac

    if [[ "$RETIRE_LEGACY_MAIL" != true ]]; then
      warn "a previous mail service is still loaded: $label. Two mail servers means two databases, and agents will split across them depending on which endpoint they reach. Re-run with --retire-legacy-mail to retire it, or stop it yourself: launchctl bootout gui/$(id -u)/$label"
      continue
    fi
    if [[ "$DRY_RUN" == true ]]; then
      LEGACY_MAIL_RETIRE_PLANNED=true
      say "DRY-RUN would retire legacy mail service: $label"
      continue
    fi

    # Boot out first and confirm it is gone. Moving the plist before the job is
    # actually unloaded would leave the machine with a running server and no
    # definition to restore -- worse than the collision being retired.
    if ! launchctl bootout "gui/$(id -u)/$label" >/dev/null 2>&1; then
      # bootout reports "not loaded" as failure too; only a still-loaded job is
      # a problem.
      if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
        die "could not retire legacy mail service '$label'; retire it by hand (launchctl bootout gui/$(id -u)/$label) and re-run"
      fi
    fi
    wait_for_launchd_unload "gui/$(id -u)/$label" || \
      die "legacy mail service '$label' is still loaded after bootout; retire it by hand and re-run"

    # Park the plist rather than delete it. Retiring someone's running service
    # is not the kind of thing an installer should make unrecoverable.
    if [[ -f "$plist" ]]; then
      mkdir -p "$INSTALL_DIR/parked-launchd"
      local parked="$INSTALL_DIR/parked-launchd/$label.plist"
      if [[ -e "$parked" ]]; then
        # An earlier retirement parked a copy here. Overwriting it would
        # destroy the only remaining definition of that service.
        local suffix=1
        while [[ -e "$parked.$suffix" ]]; do suffix=$((suffix + 1)); done
        parked="$parked.$suffix"
      fi
      mv "$plist" "$parked"
      say "retired legacy mail service $label (plist parked at $parked)"
    else
      say "retired legacy mail service $label"
    fi
  done
}

# Is this path part of a mail install?
#
# Canonicalise first, then judge only the canonical path. Judging the path as
# written let ".../mcp_agent_mail/editor -> /bin/echo" and
# ".../mcp_agent_mail/../editor" pass on the strength of a directory name that
# says nothing about what runs. Directory names are the durable signal once the
# path is real: the predecessor lived in ~/mcp_agent_mail whichever script
# inside it launchd ran. Basenames are matched exactly -- a wildcard accepted
# "not-mcp-agent-mail-backup".
path_belongs_to_a_mail_install() {
  local candidate="$1" mode="${2:-executable}" resolved=""
  [[ -n "$candidate" ]] || return 1
  [[ -e "$candidate" ]] || return 1
  resolved="$(/usr/bin/python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$candidate" 2>/dev/null)" || return 1
  [[ -n "$resolved" && -f "$resolved" ]] || return 1
  # A definition that names something the system cannot run is not evidence of
  # a running service.
  if [[ "$mode" == "executable" ]]; then
    [[ -x "$resolved" ]] || return 1
  else
    [[ -r "$resolved" ]] || return 1
  fi
  case "/${resolved%/*}/" in
    */mcp_agent_mail/*|*/mcp-agent-mail/*|*/agentstack_mail/*|*/agentstack-mail/*|*/mail-service/*) return 0 ;;
  esac
  case "${resolved##*/}" in
    mcp-agent-mail|mcp_agent_mail|agentstack-mail|agentstack_mail|\
    agentstack-mail-service|run-agentstack-mail.sh|run_server_with_token.sh) return 0 ;;
  esac
  return 1
}

legacy_mail_plist_looks_like_mail() {
  # Read what the job runs. Four narrower mistakes preceded this: searching the
  # whole plist was circular (the labels are written in the document), searching
  # every argument was forgeable (an editor with --note=...mcp-agent-mail... was
  # retired), looking only at argv[0] missed the predecessor itself, whose sealed
  # definition is ["/bin/bash", ".../mcp_agent_mail/scripts/run_server_with_token.sh"],
  # and trusting any interpreter *name* let a shell script called /tmp/python3
  # vouch for whatever came after it.
  #
  # So: the executable, and -- only when the executable is one of the system
  # shells, by absolute path -- the script it is handed. Anything unreadable,
  # unparseable or symlinked at the plist level fails closed.
  local plist="$1" executable="" script=""
  [[ -f "$plist" ]] || return 1
  [[ -L "$plist" ]] && return 1
  command -v /usr/bin/plutil >/dev/null 2>&1 || return 1
  executable="$(/usr/bin/plutil -extract Program raw -o - "$plist" 2>/dev/null)" ||
    executable="$(/usr/bin/plutil -extract ProgramArguments.0 raw -o - "$plist" 2>/dev/null)" || return 1
  [[ -n "$executable" ]] || return 1
  path_belongs_to_a_mail_install "$executable" executable && return 0
  case "$executable" in
    /bin/bash|/bin/sh|/bin/zsh)
      script="$(/usr/bin/plutil -extract ProgramArguments.1 raw -o - "$plist" 2>/dev/null)" || return 1
      path_belongs_to_a_mail_install "$script" script && return 0
      ;;
  esac
  return 1
}

enable_mail_autostart() {
  local kind=""
  if [[ "${NATIVE_MAIL_AUTOSTART_MANAGED_BY_INSTALL:-true}" != true ]]; then
    warn "ORRERY Mail autostart was not updated because the running deployment is not identifiable; existing trigger files and registrations were left untouched"
    return 0
  fi
  case "$(uname -s)" in
    Darwin) command -v launchctl >/dev/null 2>&1 && kind="launchd" ;;
    Linux)  command -v systemctl >/dev/null 2>&1 && kind="systemd-user" ;;
  esac
  # Never register a trigger that is already known to fail: mailctl reads env.sh
  # and dies if the service env is missing, and a unit that fails only at boot is
  # worse than no unit at all — nothing reports it until mail is silently absent.
  if [[ "$DRY_RUN" != true && ! -f "$MAIL_ENV" ]]; then
    warn "ORRERY Mail service env is missing ($MAIL_ENV); skipping the login trigger because it would fail at boot. Stop the mail server and re-run install.sh to render one, then mail will restart automatically."
    return 0
  fi

  if [[ -z "$kind" ]]; then
    # Say it out loud. A missing autostart is invisible until the machine
    # reboots, which is exactly how this gap survived on the maintainer's Mac.
    warn "no supported service manager found; ORRERY Mail will NOT restart after a reboot. Start it manually with: $BIN_DIR/agentstack-mailctl start"
    return 0
  fi

  # Compute the path first so the existing unit can be preserved: the renderer
  # overwrites it, and a failed re-registration would otherwise leave the machine
  # with neither the old trigger nor the new one — visible only at the next boot.
  local previous=""
  if [[ "$kind" == "launchd" ]]; then
    AGENT_MAIL_AUTOSTART_PATH="$HOME/Library/LaunchAgents/$MAIL_AUTOSTART_LABEL.plist"
  else
    AGENT_MAIL_AUTOSTART_PATH="$HOME/.config/systemd/user/$MAIL_AUTOSTART_LABEL.timer"
  fi
  local previous_service=""
  if [[ "$DRY_RUN" != true && -f "$AGENT_MAIL_AUTOSTART_PATH" ]]; then
    previous="$AGENT_MAIL_AUTOSTART_PATH.prev"
    rm -f "$previous"
    cp "$AGENT_MAIL_AUTOSTART_PATH" "$previous"
  fi
  # The systemd timer is useless without the service it triggers: restoring only
  # the timer reports success while leaving nothing to activate.
  if [[ "$DRY_RUN" != true && "$kind" == "systemd-user" ]]; then
    local existing_service="$HOME/.config/systemd/user/$MAIL_AUTOSTART_LABEL.service"
    if [[ -f "$existing_service" ]]; then
      previous_service="$existing_service.prev"
      rm -f "$previous_service"
      cp "$existing_service" "$previous_service"
    fi
  fi

  render_mail_autostart_unit "$kind"

  if [[ "$DRY_RUN" == true ]]; then
    if [[ "$kind" == "launchd" ]]; then
      say "DRY-RUN would run: launchctl enable gui/$(id -u)/$MAIL_AUTOSTART_LABEL"
      say "DRY-RUN would run: launchctl bootout gui/$(id -u)/$MAIL_AUTOSTART_LABEL"
      say "DRY-RUN would wait for launchctl unload: gui/$(id -u)/$MAIL_AUTOSTART_LABEL"
      say "DRY-RUN would run: launchctl bootstrap gui/$(id -u) $AGENT_MAIL_AUTOSTART_PATH"
    else
      say "DRY-RUN would run: systemctl --user enable --now $MAIL_AUTOSTART_LABEL.timer"
    fi
    AGENT_MAIL_AUTOSTART_KIND="$kind"
    return 0
  fi

  if [[ "$kind" == "launchd" ]]; then
    local launchd_target="gui/$(id -u)/$MAIL_AUTOSTART_LABEL"
    if launchctl enable "$launchd_target"; then
      launchctl bootout "$launchd_target" 2>/dev/null || true
      # bootstrap is the capability probe only after disabled state is cleared
      # and the previous job has actually left the domain.
      if wait_for_launchd_unload "$launchd_target" && \
         launchctl bootstrap "gui/$(id -u)" "$AGENT_MAIL_AUTOSTART_PATH"
      then
        AGENT_MAIL_AUTOSTART_KIND="launchd"
        [[ -n "$previous" ]] && rm -f "$previous"
        say "ORRERY Mail will restart at login ($MAIL_AUTOSTART_LABEL)"
        return 0
      fi
    fi
    launchctl bootout "$launchd_target" 2>/dev/null || true
    wait_for_launchd_unload "$launchd_target" || true
    rm -f "$AGENT_MAIL_AUTOSTART_PATH"
    if [[ -n "$previous" ]]; then
      mv "$previous" "$AGENT_MAIL_AUTOSTART_PATH"
      if launchctl enable "$launchd_target" 2>/dev/null && \
         launchctl bootstrap "gui/$(id -u)" "$AGENT_MAIL_AUTOSTART_PATH" 2>/dev/null; then
        warn "kept the previous ORRERY Mail login trigger; the new one could not be registered"
        AGENT_MAIL_AUTOSTART_KIND="launchd"
        return 0
      fi
    fi
  else
    # `enable` (not `enable --now`): the server is already running from
    # start_native_mail, and a oneshot start here would only re-probe it.
    # An older install enabled the *service* directly; leave that symlink in
    # place and it keeps firing alongside the timer.
    systemctl --user disable "$MAIL_AUTOSTART_LABEL.service" 2>/dev/null || true
    # `enable` alone only writes the symlink — the timer does not start until the
    # next login, so a re-install would not supervise anything until then.
    if systemctl --user daemon-reload && \
       systemctl --user enable --now "$MAIL_AUTOSTART_LABEL.timer"
    then
      AGENT_MAIL_AUTOSTART_KIND="systemd-user"
      [[ -n "$previous" ]] && rm -f "$previous"
      [[ -n "$previous_service" ]] && rm -f "$previous_service"
      say "ORRERY Mail will restart at login ($MAIL_AUTOSTART_LABEL.timer)"
      return 0
    fi
    systemctl --user disable "$MAIL_AUTOSTART_LABEL.timer" 2>/dev/null || true
    rm -f "$AGENT_MAIL_AUTOSTART_PATH" "$AGENT_MAIL_AUTOSTART_SERVICE_PATH"
    if [[ -n "$previous" ]]; then
      mv "$previous" "$AGENT_MAIL_AUTOSTART_PATH"
      [[ -n "$previous_service" ]] && \
        mv "$previous_service" "$HOME/.config/systemd/user/$MAIL_AUTOSTART_LABEL.service"
      if [[ -f "$HOME/.config/systemd/user/$MAIL_AUTOSTART_LABEL.service" ]] && \
         systemctl --user daemon-reload 2>/dev/null && \
         systemctl --user enable --now "$MAIL_AUTOSTART_LABEL.timer" 2>/dev/null; then
        warn "kept the previous ORRERY Mail login trigger; the new one could not be registered"
        AGENT_MAIL_AUTOSTART_KIND="systemd-user"
        return 0
      fi
    fi
    # systemd caches unit files; without this the removed unit lingers in the
    # manager's view until something else reloads it.
    systemctl --user daemon-reload 2>/dev/null || true
  fi

  rm -f "${previous:-/nonexistent}" "${previous_service:-/nonexistent}" 2>/dev/null || true
  AGENT_MAIL_AUTOSTART_PATH=""
  warn "could not register the ORRERY Mail autostart unit; mail will NOT restart after a reboot. Start it manually with: $BIN_DIR/agentstack-mailctl start"
}

# --- ORRERY Mail watcher service ---------------------------------------------
# Unlike the mail autostart (a controller that exits), the watcher is the
# long-running process itself, so its unit is KeepAlive / Restart=always. It
# holds a single-instance lock (hooks/watch_agent_mail_signals.sh acquire_lock):
# a second copy exits 0 as a duplicate, which is why agent-start now checks the
# lock before starting its tmux fallback.
mail_watcher_environment() {
  cat <<ENVLIST
PATH=$PATH_VALUE
AGENTSTACK_MAIL_HOME=$MAIL_HOME
AGENTSTACK_SIGNALS_DIR=$SIGNALS_DIR
AGENTSTACK_RUNTIME_DIR=$RUNTIME_DIR
ENVLIST
} # end mail_watcher_environment

render_mail_watcher_unit() {
  local kind="$1"
  local watcher_script="$HOOKS_DIR/watch_agent_mail_signals.sh"
  local watcher_log="$RUNTIME_DIR/mail-watcher.log"
  if [[ "$kind" == "launchd" ]]; then
    AGENT_MAIL_WATCHER_PATH="$HOME/Library/LaunchAgents/$MAIL_WATCHER_LABEL.plist"
    plan "render launchd plist $AGENT_MAIL_WATCHER_PATH (ORRERY Mail watcher)"
    [[ "$DRY_RUN" == true ]] && return 0
    mkdir -p "$HOME/Library/LaunchAgents"
    "$PYTHON_BIN" - \
      "$AGENT_MAIL_WATCHER_PATH" "$MAIL_WATCHER_LABEL" \
      "$watcher_script" "$watcher_log" \
      "$(mail_watcher_environment)" <<'PY'
import pathlib
import plistlib
import sys

dst, label, script, logfile, env_blob = sys.argv[1:6]
env = {}
for line in env_blob.splitlines():
    if "=" in line:
        key, value = line.split("=", 1)
        env[key] = value

plist = {
    "Label": label,
    "ProgramArguments": ["/bin/bash", script],
    "RunAtLoad": True,
    # The watcher is the service: keep it alive and let ThrottleInterval pace
    # a crash loop instead of hammering tmux.
    "KeepAlive": True,
    "ThrottleInterval": 5,
    "StandardOutPath": logfile,
    "StandardErrorPath": logfile,
    "EnvironmentVariables": env,
}
path = pathlib.Path(dst)
tmp = path.with_suffix(path.suffix + ".tmp")
tmp.write_bytes(plistlib.dumps(plist))
tmp.replace(path)
PY
    return 0
  fi

  AGENT_MAIL_WATCHER_PATH="$HOME/.config/systemd/user/$MAIL_WATCHER_LABEL.service"
  plan "render systemd user unit $AGENT_MAIL_WATCHER_PATH (ORRERY Mail watcher)"
  [[ "$DRY_RUN" == true ]] && return 0
  mkdir -p "$HOME/.config/systemd/user"
  {
    printf '[Unit]\nDescription=ORRERY Mail watcher\nAfter=network.target\n\n'
    printf '[Service]\nType=simple\n'
    "$PYTHON_BIN" - "$watcher_script" "$(mail_watcher_environment)" <<'PY_UNIT'
import sys

script, env_blob = sys.argv[1:3]


def quote(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


for line in env_blob.splitlines():
    if "=" in line:
        key, value = line.split("=", 1)
        print(f"Environment={key}={quote(value)}")
print(f"ExecStart=/bin/bash {quote(script)}")
PY_UNIT
    printf 'Restart=always\nRestartSec=5\n'
    printf 'StandardOutput=append:%s\nStandardError=append:%s\n\n' \
      "$watcher_log" "$watcher_log"
    printf '[Install]\nWantedBy=default.target\n'
  } > "$AGENT_MAIL_WATCHER_PATH.tmp"
  mv "$AGENT_MAIL_WATCHER_PATH.tmp" "$AGENT_MAIL_WATCHER_PATH"
} # end render_mail_watcher_unit

# Before 2026-10-05 the watcher lock defaulted to /tmp (shared by every
# account on the Mac). Updating leaves the new per-user default empty while a
# dir nobody will ever look at again lingers at the old /tmp path. Clear it
# only if it is this user's own: a still-running old watcher is terminated by
# the service restart below and cleans up its own lock via its EXIT trap, and
# a dir owned by someone else (a second account on the same Mac) is never
# touched here.
migrate_legacy_mail_watcher_lock() {
  # Overridable only so tests can point this at an isolated tmp path instead
  # of the real, machine-wide /tmp default; there is no reason to set this in
  # a real install.
  local legacy_dir="${AGENTSTACK_TEST_LEGACY_MAIL_WATCHER_LOCK_DIR:-/tmp/orrery-mail-watcher.lock}"
  [[ -e "$legacy_dir" ]] || return 0
  local owner_uid my_uid
  # -c (GNU/BusyBox) before -f (BSD): GNU's -f means "filesystem status", and
  # even when the %u directive itself is invalid it still prints the
  # filesystem's default report to stdout before failing, which then runs
  # into -c's output under `||` and corrupts the UID comparison (confirmed on
  # WSL, GNU coreutils 9.4, ext4). -c fails on BSD with nothing on stdout
  # (the error goes to stderr only), so it falls through to -f safely there.
  owner_uid="$(stat -c '%u' "$legacy_dir" 2>/dev/null || stat -f '%u' "$legacy_dir" 2>/dev/null || true)"
  my_uid="$(id -u)"
  [[ -n "$owner_uid" && "$owner_uid" == "$my_uid" ]] || return 0
  local pid=""
  [[ -f "$legacy_dir/watcher.pid" ]] && pid="$(head -n1 "$legacy_dir/watcher.pid" 2>/dev/null | tr -d '[:space:]')"
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    return 0
  fi
  plan "remove the leftover pre-2026-10-05 watcher lock $legacy_dir (owned by this user, no live process)"
  [[ "$DRY_RUN" == true ]] && return 0
  rm -rf "$legacy_dir"
  say "removed the leftover pre-2026-10-05 watcher lock $legacy_dir"
} # end migrate_legacy_mail_watcher_lock

# A watcher left behind by agent-start (tmux session `mail-watcher`) holds the
# single-instance lock; the managed unit would then exit as a duplicate on
# every restart until that session dies. Retire it before registering.
stop_tmux_mail_watcher() {
  local watcher_session="${AGENTSTACK_MAIL_WATCHER_SESSION:-mail-watcher}"
  command -v tmux >/dev/null 2>&1 || return 0
  if tmux has-session -t "$watcher_session" 2>/dev/null; then
    tmux kill-session -t "$watcher_session" 2>/dev/null || true
    say "stopped the tmux mail-watcher session; the service unit takes over"
  fi
} # end stop_tmux_mail_watcher

enable_mail_watcher() {
  local kind=""
  case "$(uname -s)" in
    Darwin) command -v launchctl >/dev/null 2>&1 && kind="launchd" ;;
    Linux)  command -v systemctl >/dev/null 2>&1 && kind="systemd-user" ;;
  esac
  if [[ -z "$kind" ]]; then
    warn "no supported service manager found; the ORRERY Mail watcher will only run while an agent started with $BIN_DIR/agent-start keeps its tmux session alive"
    return 0
  fi

  if [[ "$kind" == "launchd" ]]; then
    AGENT_MAIL_WATCHER_PATH="$HOME/Library/LaunchAgents/$MAIL_WATCHER_LABEL.plist"
  else
    AGENT_MAIL_WATCHER_PATH="$HOME/.config/systemd/user/$MAIL_WATCHER_LABEL.service"
  fi
  local previous=""
  if [[ "$DRY_RUN" != true && -f "$AGENT_MAIL_WATCHER_PATH" ]]; then
    previous="$AGENT_MAIL_WATCHER_PATH.prev"
    rm -f "$previous"
    cp "$AGENT_MAIL_WATCHER_PATH" "$previous"
  fi

  render_mail_watcher_unit "$kind"
  migrate_legacy_mail_watcher_lock

  if [[ "$DRY_RUN" == true ]]; then
    if [[ "$kind" == "launchd" ]]; then
      say "DRY-RUN would run: launchctl bootstrap gui/$(id -u) $AGENT_MAIL_WATCHER_PATH"
    else
      say "DRY-RUN would run: systemctl --user enable --now $MAIL_WATCHER_LABEL.service"
    fi
    AGENT_MAIL_WATCHER_KIND="$kind"
    return 0
  fi

  stop_tmux_mail_watcher

  if [[ "$kind" == "launchd" ]]; then
    local launchd_target="gui/$(id -u)/$MAIL_WATCHER_LABEL"
    if launchctl enable "$launchd_target"; then
      launchctl bootout "$launchd_target" 2>/dev/null || true
      if wait_for_launchd_unload "$launchd_target" && \
         launchctl bootstrap "gui/$(id -u)" "$AGENT_MAIL_WATCHER_PATH"
      then
        # RunAtLoad does not fire when the bootstrap comes from a non-GUI
        # context (an ssh session on the Air: loaded, runs = 0, never started).
        # kickstart is idempotent for a job that is already running.
        launchctl kickstart "$launchd_target" 2>/dev/null || true
        AGENT_MAIL_WATCHER_KIND="launchd"
        [[ -n "$previous" ]] && rm -f "$previous"
        say "ORRERY Mail watcher runs under launchd ($MAIL_WATCHER_LABEL)"
        return 0
      fi
    fi
    launchctl bootout "$launchd_target" 2>/dev/null || true
    wait_for_launchd_unload "$launchd_target" || true
    rm -f "$AGENT_MAIL_WATCHER_PATH"
    if [[ -n "$previous" ]]; then
      mv "$previous" "$AGENT_MAIL_WATCHER_PATH"
      if launchctl enable "$launchd_target" 2>/dev/null && \
         launchctl bootstrap "gui/$(id -u)" "$AGENT_MAIL_WATCHER_PATH" 2>/dev/null; then
        warn "kept the previous ORRERY Mail watcher unit; the new one could not be registered"
        AGENT_MAIL_WATCHER_KIND="launchd"
        return 0
      fi
    fi
  else
    # `enable --now` leaves an already-running service on the old unit file;
    # restart so the rendered one is what actually runs.
    if systemctl --user daemon-reload && \
       systemctl --user enable --now "$MAIL_WATCHER_LABEL.service" && \
       systemctl --user restart "$MAIL_WATCHER_LABEL.service"
    then
      AGENT_MAIL_WATCHER_KIND="systemd-user"
      [[ -n "$previous" ]] && rm -f "$previous"
      say "ORRERY Mail watcher runs under systemd ($MAIL_WATCHER_LABEL.service)"
      return 0
    fi
    systemctl --user disable --now "$MAIL_WATCHER_LABEL.service" 2>/dev/null || true
    rm -f "$AGENT_MAIL_WATCHER_PATH"
    if [[ -n "$previous" ]]; then
      mv "$previous" "$AGENT_MAIL_WATCHER_PATH"
      if systemctl --user daemon-reload 2>/dev/null && \
         systemctl --user enable --now "$MAIL_WATCHER_LABEL.service" 2>/dev/null; then
        warn "kept the previous ORRERY Mail watcher unit; the new one could not be registered"
        AGENT_MAIL_WATCHER_KIND="systemd-user"
        return 0
      fi
    fi
    systemctl --user daemon-reload 2>/dev/null || true
  fi

  rm -f "${previous:-/nonexistent}" 2>/dev/null || true
  AGENT_MAIL_WATCHER_PATH=""
  warn "could not register the ORRERY Mail watcher unit; agents spawned from the dashboard will not receive Mail notifications until an agent started with $BIN_DIR/agent-start brings the watcher up"
} # end enable_mail_watcher

render_launchd_plist() {
  local plist="$HOME/Library/LaunchAgents/$LABEL.plist"
  plan "render launchd plist $plist"
  if [[ "$DRY_RUN" != true ]]; then
    mkdir -p "$HOME/Library/LaunchAgents"
    # Values travel as arguments and are XML-escaped there: a path with `&`, `<`
    # or `"` (an Obsidian vault named "Research & Notes") broke the plist, and
    # launchd will not load a broken one (review of #146).
    local pairs=(
      __LABEL_PREFIX__ "$LABEL_PREFIX"
      __INSTALL_DIR__ "$DASHBOARD_DIR"
      __PYTHON__ "$PYTHON_BIN"
      __PORT__ "$PORT"
      __MAIL_DB__ "$MAIL_DB"
      __MAIL_ENV__ "$MAIL_ENV"
      __MAIL_HOME__ "$MAIL_HOME"
      __MAIL_HTTP_BEARER_MODE__ "$MAIL_HTTP_BEARER_MODE"
      __SIGNALS_DIR__ "$SIGNALS_DIR"
      __MCP_URL__ "$MCP_URL"
      __TERMINAL__ "$TERMINAL"
      __AUTO_OPEN_CHILD__ "$AUTO_OPEN_CHILD_SETTING"
      __PROJECT_KEY__ "$PROJECT_KEY"
      __PROTECTED_ROOTS__ "$PROTECTED_ROOTS"
      __DELIVERABLE_ROOTS__ "$DELIVERABLE_ROOTS"
      __LANG__ "$LANG_SETTING"
      __MURMUR__ "$MURMUR_SETTING"
      __SPAWN_DIRS__ "$SPAWN_DIRS_SETTING"
      __SPAWN_ROOTS__ "$SPAWN_ROOTS_SETTING"
      __WORKTREE_ROOT__ "$WORKTREE_ROOT_SETTING"
      __CODEX_CHILD_APPROVAL__ "$CODEX_CHILD_APPROVAL_SETTING"
      __CODEX_NETWORK__ "$CODEX_NETWORK_SETTING"
      __CHILD_DEFAULT_TOOLS__ "$CHILD_DEFAULT_TOOLS_SETTING"
      __CODEX_ADD_DIRS__ "$CODEX_ADD_DIRS_SETTING"
      __CHILD_RESUME_RETENTION_DAYS__ "$CHILD_RESUME_RETENTION_DAYS_SETTING"
      __CODEX_BIN__ "$CODEX_BIN_SETTING"
      __PORTRAITS_DIR__ "$PORTRAITS_DIR_SETTING"
      __CUSTOM_PORTRAITS__ "$CUSTOM_PORTRAITS_SETTING"
      __CODEX_MODELS__ "$CODEX_MODELS_SETTING"
      __HOOKS_DIR__ "$HOOKS_DIR"
      __RUNTIME_DIR__ "$RUNTIME_DIR"
      __PERSISTENT_PROFILES_DIR__ "$PERSISTENT_PROFILES_DIR"
      __DASHBOARD_LOG__ "$DASHBOARD_LOG"
      __DASHBOARD_LOG_MAX_BYTES__ "$DASHBOARD_LOG_MAX_BYTES"
      __DASHBOARD_LOG_BACKUPS__ "$DASHBOARD_LOG_BACKUPS"
      __DASHBOARD_RESTART_DELAY__ "$DASHBOARD_RESTART_DELAY"
      __MANAGED_AGENTS_FILE__ "$MANAGED_AGENTS_FILE"
      __VAULT__ "$VAULT_SETTING"
      __PATH__ "$PATH_VALUE"
    )
    "$PYTHON_BIN" - "$REPO_ROOT/dashboard/agentdashboard.plist.template" "$plist" \
      "$CODEX_CHILD_CONFIG_OVERLAY_SETTING" "${pairs[@]}" <<'PY'
import os
import pathlib
import sys
import xml.sax.saxutils

src = pathlib.Path(sys.argv[1])
dst = pathlib.Path(sys.argv[2])
overlay = sys.argv[3]
pairs = sys.argv[4:]
repl = dict(zip(pairs[0::2], pairs[1::2]))
repl["__CLAUDE_MODELS__"] = os.environ.get("AGENTSTACK_CLAUDE_MODELS", "")
text = src.read_text(encoding="utf-8")
for key, value in repl.items():
    text = text.replace(key, xml.sax.saxutils.escape(value))
overlay_marker = "    <key>AGENTSTACK_CODEX_CHILD_APPROVAL</key>"
overlay_entry = (
    "    <key>AGENTSTACK_CODEX_CHILD_CONFIG_OVERLAY</key>\n"
    "    <string>"
    + xml.sax.saxutils.escape(overlay)
    + "</string>\n"
)
text = text.replace(overlay_marker, overlay_entry + overlay_marker)
tmp = dst.with_suffix(dst.suffix + ".tmp")
tmp.write_text(text, encoding="utf-8")
tmp.replace(dst)
PY
  fi
  SERVICE_PATH="$plist"
}

render_systemd_unit() {
  local dir="$HOME/.config/systemd/user"
  local unit="$dir/$LABEL.service"
  plan "render systemd user unit $unit"
  if [[ "$DRY_RUN" != true ]]; then
    mkdir -p "$dir"
    "$PYTHON_BIN" - "$unit" <<PY
import os
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
env = {
    "AGENTSTACK_PYTHON": "$PYTHON_BIN",
    "AGENTSTACK_PORT": "$PORT",
    "AGENTSTACK_LABEL_PREFIX": "$LABEL_PREFIX",
    "AGENTSTACK_MAIL_DB": "$MAIL_DB",
    "AGENTSTACK_MAIL_ENV": "$MAIL_ENV",
    "AGENTSTACK_MAIL_HOME": "$MAIL_HOME",
    "AGENTSTACK_MAIL_HTTP_BEARER_MODE": "$MAIL_HTTP_BEARER_MODE",
    "AGENTSTACK_SIGNALS_DIR": "$SIGNALS_DIR",
    "AGENTSTACK_MCP_URL": "$MCP_URL",
    "AGENTSTACK_TERMINAL": "$TERMINAL",
    "AGENTSTACK_AUTO_OPEN_CHILD": "$AUTO_OPEN_CHILD_SETTING",
    "AGENTSTACK_PROJECT_KEY": "$PROJECT_KEY",
    "AGENTSTACK_PROTECTED_ROOTS": "$PROTECTED_ROOTS",
    "AGENTSTACK_DELIVERABLE_ROOTS": "$DELIVERABLE_ROOTS",
    "AGENTSTACK_LANG": "$LANG_SETTING",
    "AGENTSTACK_MURMUR": "$MURMUR_SETTING",
    "AGENTSTACK_SPAWN_DIRS": "$SPAWN_DIRS_SETTING",
    "AGENTSTACK_SPAWN_ROOTS": "$SPAWN_ROOTS_SETTING",
    "AGENTSTACK_WORKTREE_ROOT": "$WORKTREE_ROOT_SETTING",
    "AGENTSTACK_CODEX_CHILD_APPROVAL": "$CODEX_CHILD_APPROVAL_SETTING",
    "AGENTSTACK_CODEX_CHILD_CONFIG_OVERLAY": "$CODEX_CHILD_CONFIG_OVERLAY_SETTING",
    "AGENTSTACK_CODEX_NETWORK": "$CODEX_NETWORK_SETTING",
    "AGENTSTACK_CHILD_DEFAULT_TOOLS": "$CHILD_DEFAULT_TOOLS_SETTING",
    "AGENTSTACK_CODEX_ADD_DIRS": "$CODEX_ADD_DIRS_SETTING",
    "AGENTSTACK_CHILD_RESUME_RETENTION_DAYS": "$CHILD_RESUME_RETENTION_DAYS_SETTING",
    "AGENTSTACK_CODEX_BIN": "$CODEX_BIN_SETTING",
    "AGENTSTACK_PORTRAITS_DIR": "$PORTRAITS_DIR_SETTING",
    "AGENTSTACK_CUSTOM_PORTRAITS": "$CUSTOM_PORTRAITS_SETTING",
    "AGENTSTACK_CLAUDE_MODELS": os.environ.get("AGENTSTACK_CLAUDE_MODELS", ""),
    "AGENTSTACK_CODEX_MODELS": "$CODEX_MODELS_SETTING",
    "AGENTSTACK_HOOKS_DIR": "$HOOKS_DIR",
    "AGENTSTACK_SKILLS_DIR": "$SKILLS_DIR",
    "AGENTSTACK_RUNTIME_DIR": "$RUNTIME_DIR",
    "AGENTSTACK_PERSISTENT_PROFILES_DIR": "$PERSISTENT_PROFILES_DIR",
    "AGENTSTACK_MANAGED_AGENTS_FILE": "$MANAGED_AGENTS_FILE",
    "AGENTSTACK_DASHBOARD_LOG": "$DASHBOARD_LOG",
    "AGENTSTACK_DASHBOARD_LOG_MAX_BYTES": "$DASHBOARD_LOG_MAX_BYTES",
    "AGENTSTACK_DASHBOARD_LOG_BACKUPS": "$DASHBOARD_LOG_BACKUPS",
    "AGENTSTACK_DASHBOARD_RESTART_DELAY": "$DASHBOARD_RESTART_DELAY",
    "AGENTSTACK_VAULT": "$VAULT_SETTING",
    "PATH": "$PATH_VALUE",
}
def esc(v):
    return str(v).replace("\\\\", "\\\\\\\\").replace('"', '\\"')
lines = [
    "[Unit]",
    "Description=ORRERY Telemetry dashboard",
    "After=network.target",
    "",
    "[Service]",
    "Type=simple",
    f"WorkingDirectory={esc('$DASHBOARD_DIR')}",
]
for key, value in env.items():
    lines.append(f'Environment="{key}={esc(value)}"')
lines.extend([
    f"ExecStart={esc('$PYTHON_BIN')} {esc('$DASHBOARD_DIR/service_runner.py')}",
    "Restart=always",
    "RestartSec=5",
    "",
    "[Install]",
    "WantedBy=default.target",
    "",
])
tmp = path.with_suffix(path.suffix + ".tmp")
tmp.write_text("\\n".join(lines), encoding="utf-8")
tmp.replace(path)
PY
  fi
  SERVICE_PATH="$unit"
}

start_service() {
  local kind="$1"
  local launchd_target=""
  case "$kind" in
    launchd)
      render_launchd_plist
      launchd_target="gui/$(id -u)/$LABEL"
      if [[ "$DRY_RUN" == true ]]; then
        say "DRY-RUN would run: launchctl enable $launchd_target"
        say "DRY-RUN would run: launchctl bootout $launchd_target"
        say "DRY-RUN would wait for launchctl unload: $launchd_target"
        say "DRY-RUN would run: launchctl bootstrap gui/$(id -u) $SERVICE_PATH"
        say "DRY-RUN would run: launchctl kickstart $launchd_target"
        say "DRY-RUN note: a real run treats those commands as the probe; if the"
        say "  gui/$(id -u) domain refuses them it switches to supervised background mode."
        ACTIVE_SERVICE_KIND="launchd"
        return
      fi
      # A GUI domain can disappear while the user is logged in (for example
      # while the display is asleep), so the bootstrap operation itself is the
      # capability probe. Never infer availability from login metadata.
      if launchctl enable "$launchd_target"; then
        launchctl bootout "$launchd_target" 2>/dev/null || true
        if wait_for_launchd_unload "$launchd_target" && \
           launchctl bootstrap "gui/$(id -u)" "$SERVICE_PATH" && \
           launchctl kickstart "$launchd_target"
        then
          ACTIVE_SERVICE_KIND="launchd"
          return
        fi
      fi
      warn "launchd could not bootstrap $LABEL in gui/$(id -u); falling back to supervised background mode"
      launchctl bootout "$launchd_target" 2>/dev/null || true
      wait_for_launchd_unload "$launchd_target" || true
      rm -f "$SERVICE_PATH"
      SERVICE_FALLBACK_USED=true
      start_supervised_background || true
      ;;
    systemd-user)
      render_systemd_unit
      if [[ "$DRY_RUN" == true ]]; then
        run systemctl --user daemon-reload
        run systemctl --user enable --now "$LABEL.service"
        ACTIVE_SERVICE_KIND="systemd-user"
        return
      fi
      if systemctl --user daemon-reload && \
         systemctl --user enable --now "$LABEL.service"
      then
        ACTIVE_SERVICE_KIND="systemd-user"
        return
      fi
      warn "systemd user service setup failed; falling back to supervised background mode"
      systemctl --user disable --now "$LABEL.service" 2>/dev/null || true
      rm -f "$SERVICE_PATH"
      SERVICE_FALLBACK_USED=true
      start_supervised_background || true
      ;;
    nohup)
      start_supervised_background || true
      ;;
    *)
      warn "unknown service kind '$kind'; dashboard was not started"
      ACTIVE_SERVICE_KIND="manual"
      SERVICE_PATH=""
      ;;
  esac
}

start_supervised_background() {
  SERVICE_PATH="$RUNTIME_DIR/dashboard.pid"
  ACTIVE_SERVICE_KIND="nohup"
  plan "start dashboard in supervised background mode, pidfile $SERVICE_PATH"
  if [[ "$DRY_RUN" == true ]]; then
    return 0
  fi
  mkdir -p "$RUNTIME_DIR"
  stop_supervised_background || return 1
  (
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    AGENTSTACK_DASHBOARD_SELF_RESTART=1 \
      nohup "$PYTHON_BIN" "$DASHBOARD_DIR/service_runner.py" >> "$DASHBOARD_LOG" 2>&1 &
    echo $! > "$SERVICE_PATH"
  )
  local supervisor_pid
  supervisor_pid="$(sed -n '1p' "$SERVICE_PATH" 2>/dev/null || true)"
  if [[ ! "$supervisor_pid" =~ ^[0-9]+$ ]] || ! kill -0 "$supervisor_pid" 2>/dev/null; then
    warn "could not start the supervised background dashboard"
    rm -f "$SERVICE_PATH"
    ACTIVE_SERVICE_KIND="manual"
    SERVICE_PATH=""
    return 1
  fi
}

supervised_pid_matches_state() {
  local pid="$1" state_file="$RUNTIME_DIR/dashboard-service.json"
  [[ -f "$state_file" ]] || return 1
  "$PYTHON_BIN" - "$state_file" "$pid" <<'PY'
import json
import pathlib
import sys

try:
    state = json.loads(pathlib.Path(sys.argv[1]).read_text())
    recorded = int(state.get("supervisor_pid", 0))
    expected = int(sys.argv[2])
except (AttributeError, json.JSONDecodeError, OSError, TypeError, ValueError):
    raise SystemExit(1)
raise SystemExit(0 if recorded == expected else 1)
PY
}

stop_supervised_background() {
  local pid attempts=0
  pid="$(sed -n '1p' "$RUNTIME_DIR/dashboard.pid" 2>/dev/null || true)"
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    if [[ "$pid" != "$MANAGED_SUPERVISED_PID" ]] && ! supervised_pid_matches_state "$pid"; then
      warn "refusing to stop unverified process $pid from $RUNTIME_DIR/dashboard.pid"
      ACTIVE_SERVICE_KIND="manual"
      SERVICE_PATH=""
      return 1
    fi
    say "stopping supervised background dashboard with pid $pid before replacement"
    kill "$pid" 2>/dev/null || true
    while kill -0 "$pid" 2>/dev/null && [[ "$attempts" -lt 50 ]]; do
      sleep 0.1
      attempts=$((attempts + 1))
    done
    if kill -0 "$pid" 2>/dev/null; then
      kill -KILL "$pid" 2>/dev/null || true
    fi
    attempts=0
    while port_in_use && [[ "$attempts" -lt 50 ]]; do
      sleep 0.1
      attempts=$((attempts + 1))
    done
    if port_in_use; then
      warn "supervised background dashboard did not release port $PORT"
      ACTIVE_SERVICE_KIND="manual"
      SERVICE_PATH=""
      return 1
    fi
  fi
  rm -f "$RUNTIME_DIR/dashboard.pid"
}

verify_dashboard_service() {
  plan "verify dashboard API responds at http://127.0.0.1:$PORT/api/agents"
  if [[ "$DRY_RUN" == true ]]; then
    return
  fi
  if "$PYTHON_BIN" - "$PORT" <<'PY'
import sys
import time
import urllib.error
import urllib.request

port = int(sys.argv[1])
url = f"http://127.0.0.1:{port}/api/agents"
deadline = time.monotonic() + 15
last_error = "no response"
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            if response.status == 200:
                raise SystemExit(0)
            last_error = f"HTTP {response.status}"
    except (OSError, urllib.error.URLError) as exc:
        last_error = str(exc)
    time.sleep(0.25)
print(f"dashboard health check failed: {last_error}", file=sys.stderr)
raise SystemExit(1)
PY
  then
    SERVICE_HEALTHY=true
    say "dashboard healthy: http://127.0.0.1:$PORT/api/agents"
  else
    SERVICE_HEALTHY=false
    warn "dashboard service did not become healthy; install files and managed blocks were still completed"
    warn "inspect $DASHBOARD_LOG and start the dashboard manually"
  fi
}

write_manifest() {
  plan "write manifest $MANIFEST"
  if [[ "$DRY_RUN" == true ]]; then
    return
  fi
  local service_kind="$1"
  local service_path="$2"
  local mail_service_kind="${3:-}"
  local mail_service_path="${4:-}"
  local tmp="$MANIFEST.tmp" mail_update=""
  if [[ -n "$MAIL_UPDATE_RESULT" ]]; then
    # Through the environment as JSON: the reason is free text and must not
    # be spliced into the Python source below.
    mail_update="$("$PYTHON_BIN" -c 'import json, sys; keys = sys.argv[1::2]; print(json.dumps(dict(zip(keys, sys.argv[2::2]))))' \
      result "$MAIL_UPDATE_RESULT" reason "$MAIL_UPDATE_REASON" \
      mode "$MAIL_UPDATE_MODE" reason_code "$MAIL_RESULT_CODE" outage_seconds "$MAIL_UPDATE_OUTAGE_SECONDS" \
      previous_service_env "${OLD_NATIVE_MAIL_ENV:-}" previous_candidate_venv "${OLD_NATIVE_MAIL_VENV:-}" \
      requested_service_env "${NEW_NATIVE_MAIL_ENV:-}" requested_candidate_venv "${NEW_NATIVE_MAIL_VENV:-}" \
      database_backup "$MAIL_UPDATE_BACKUP")"
  fi
  AGENTSTACK_INSTALL_MAIL_UPDATE="$mail_update" \
  "$PYTHON_BIN" - "$tmp" "$service_kind" "$service_path" \
    "$mail_service_kind" "$mail_service_path" "$AGENT_MAIL_NAME_CAPABILITY_JSON" \
    "${AGENT_MAIL_AUTOSTART_KIND:-}" "${AGENT_MAIL_AUTOSTART_PATH:-}" \
    "$MAIL_AUTOSTART_LABEL" "${AGENT_MAIL_AUTOSTART_SERVICE_PATH:-}" \
    "${AGENT_MAIL_WATCHER_KIND:-}" "${AGENT_MAIL_WATCHER_PATH:-}" \
    "$MAIL_WATCHER_LABEL" "$NATIVE_MAIL_DEPLOYMENT_IDENTIFIED" \
    "$NATIVE_MAIL_ENROLL_AVAILABLE" "$NATIVE_MAIL_AUTOSTART_MANAGED_BY_INSTALL" \
    "$CLAUDE_SKILLS_DIR" "$CODEX_SKILLS_DIR" <<PY
import json
import os
import pathlib
import time
import sys

out = pathlib.Path(sys.argv[1])
service_kind = sys.argv[2]
service_path = sys.argv[3]
mail_service_kind = sys.argv[4]
mail_service_path = sys.argv[5]
requested_name_honoring = json.loads(sys.argv[6])
mail_autostart_kind = sys.argv[7]
mail_autostart_path = sys.argv[8]
mail_autostart_label = sys.argv[9]
mail_autostart_service_path = sys.argv[10]
mail_watcher_kind = sys.argv[11]
mail_watcher_path = sys.argv[12]
mail_watcher_label = sys.argv[13]
mail_deployment_identified = sys.argv[14] == "true"
mail_enrollment_available = sys.argv[15] == "true"
mail_autostart_managed = sys.argv[16] == "true"
install_dir = pathlib.Path("$INSTALL_DIR")
# Skill discovery paths are data, not Python source. In particular CODEX_HOME
# may contain quotes or literal backslashes that Python would otherwise parse.
skill_dirs = tuple(pathlib.Path(raw) for raw in sys.argv[17:19])
owned_files = []
for rel in ("hooks", "skills", "dashboard", "bin", "codex", "claude", "integrations"):
    base = install_dir / rel
    if base.exists():
        for path in base.rglob("*"):
            if path == install_dir / "dashboard" / "annotations.json":
                # Pre-runtime releases wrote user state into the payload tree.
                # It is migrated before payload installation and is never an
                # installer-owned file if a best-effort cleanup leaves it here.
                continue
            if path.is_file() or path.is_symlink():
                owned_files.append(str(path))
version_path = install_dir / "VERSION"
if version_path.is_file() or version_path.is_symlink():
    owned_files.append(str(version_path))
owned_files.extend([str(pathlib.Path("$ENV_FILE")), str(pathlib.Path("$MANIFEST"))])
if service_path:
    owned_files.append(service_path)
if mail_autostart_path:
    owned_files.append(mail_autostart_path)
if mail_autostart_service_path:
    owned_files.append(mail_autostart_service_path)
if mail_watcher_path:
    owned_files.append(mail_watcher_path)
for raw in ("$NATIVE_MAIL_ENV", "$NATIVE_MAIL_RUNNER", "$NATIVE_MAIL_DEPLOYMENT"):
    path = pathlib.Path(raw)
    if path.is_file() or path.is_symlink():
        owned_files.append(str(path))
merge_result_path = pathlib.Path("$SAFE_MERGE_RESULT_FILE")
settings_merge = None
if merge_result_path.exists():
    settings_merge = json.loads(merge_result_path.read_text(encoding="utf-8"))
    owned_files.append(str(merge_result_path))
mcp_merge_result_path = pathlib.Path("$MCP_MERGE_RESULT_FILE")
claude_mcp_merge = None
if mcp_merge_result_path.exists():
    claude_mcp_merge = json.loads(
        mcp_merge_result_path.read_text(encoding="utf-8")
    )
    owned_files.append(str(mcp_merge_result_path))
skill_links = []
skills_root = install_dir / "skills"
if skills_root.is_dir():
    for skill_dir in skill_dirs:
        for skill_file in sorted(skills_root.glob("*/SKILL.md")):
            source = skill_file.parent
            link = skill_dir / source.name
            if not link.is_symlink():
                continue
            raw_target = pathlib.Path(os.readlink(link))
            target = raw_target if raw_target.is_absolute() else link.parent / raw_target
            if target.resolve(strict=False) != source.resolve(strict=False):
                continue
            skill_links.append({"path": str(link), "target": str(source)})
            owned_files.append(str(link))
owned_files = sorted(dict.fromkeys(owned_files))
owned_dir_paths = {
    install_dir / rel
    for rel in ("hooks", "skills", "dashboard", "bin", "runtime", "backups")
}
owned_dir_paths.add(install_dir)
for raw in owned_files:
    path = pathlib.Path(raw)
    try:
        path.relative_to(install_dir)
    except ValueError:
        # Service definitions live outside the install tree; their parent
        # directories belong to the user/system and are never installer-owned.
        continue
    parent = path.parent
    while True:
        owned_dir_paths.add(parent)
        if parent == install_dir:
            break
        parent = parent.parent
owned_dirs = sorted(str(path) for path in owned_dir_paths)
services = []
if service_kind == "launchd":
    services.append({"kind": "launchd", "label": "$LABEL", "path": service_path})
elif service_kind == "systemd-user":
    services.append({"kind": "systemd-user", "unit": "$LABEL.service", "path": service_path})
elif service_kind == "nohup":
    services.append({"kind": "nohup", "pidfile": service_path})
if mail_service_kind == "nohup" and mail_service_path:
    services.append({"kind": "nohup", "pidfile": mail_service_path, "role": "ORRERY Mail"})
# Recorded so uninstall.sh tears the autostart unit down through the same
# services loop it already uses for the dashboard.
if mail_autostart_kind == "launchd" and mail_autostart_path:
    services.append({"kind": "launchd", "label": mail_autostart_label,
                     "path": mail_autostart_path, "role": "agent-mail-autostart"})
elif mail_autostart_kind == "systemd-user" and mail_autostart_path:
    # The timer is the enabled unit; the service it triggers is a plain file.
    services.append({"kind": "systemd-user", "unit": f"{mail_autostart_label}.timer",
                     "path": mail_autostart_path, "role": "agent-mail-autostart"})
if mail_watcher_kind == "launchd" and mail_watcher_path:
    services.append({"kind": "launchd", "label": mail_watcher_label,
                     "path": mail_watcher_path, "role": "mail-watcher"})
elif mail_watcher_kind == "systemd-user" and mail_watcher_path:
    services.append({"kind": "systemd-user", "unit": f"{mail_watcher_label}.service",
                     "path": mail_watcher_path, "role": "mail-watcher"})
manifest = {
    "schema_version": 1,
    "tool": "claude-agent-stack",
    "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "install_dir": "$INSTALL_DIR",
    "repo_root": "$REPO_ROOT",
    "tier": "$TIER",
    "safe_merge_performed": bool(
        settings_merge
        and settings_merge.get("operation") == "merge"
        and settings_merge.get("changed")
    ),
    "settings_merge": settings_merge,
    "claude_mcp_merge": claude_mcp_merge,
    "agent_mail": {
        "requested_name_honoring": requested_name_honoring,
    },
    "env": {
        "AGENTSTACK_PORT": "$PORT",
        "AGENTSTACK_LABEL_PREFIX": "$LABEL_PREFIX",
        "AGENTSTACK_PROJECT_KEY": "$PROJECT_KEY",
        "AGENTSTACK_PROTECTED_ROOTS": "$PROTECTED_ROOTS",
        "AGENTSTACK_DELIVERABLE_ROOTS": "$DELIVERABLE_ROOTS",
        "AGENTSTACK_LANG": "$LANG_SETTING",
        "AGENTSTACK_MURMUR": "$MURMUR_SETTING",
        "AGENTSTACK_SPAWN_DIRS": "$SPAWN_DIRS_SETTING",
        "AGENTSTACK_SPAWN_ROOTS": "$SPAWN_ROOTS_SETTING",
        "AGENTSTACK_WORKTREE_ROOT": "$WORKTREE_ROOT_SETTING",
        "AGENTSTACK_CODEX_CHILD_APPROVAL": "$CODEX_CHILD_APPROVAL_SETTING",
        "AGENTSTACK_CODEX_CHILD_CONFIG_OVERLAY": "$CODEX_CHILD_CONFIG_OVERLAY_SETTING",
        "AGENTSTACK_CODEX_NETWORK": "$CODEX_NETWORK_SETTING",
        "AGENTSTACK_CHILD_DEFAULT_TOOLS": "$CHILD_DEFAULT_TOOLS_SETTING",
        "AGENTSTACK_CODEX_ADD_DIRS": "$CODEX_ADD_DIRS_SETTING",
        "AGENTSTACK_CHILD_RESUME_RETENTION_DAYS": "$CHILD_RESUME_RETENTION_DAYS_SETTING",
        "AGENTSTACK_CODEX_BIN": "$CODEX_BIN_SETTING",
        "AGENTSTACK_PORTRAITS_DIR": "$PORTRAITS_DIR_SETTING",
        "AGENTSTACK_CUSTOM_PORTRAITS": "$CUSTOM_PORTRAITS_SETTING",
        "AGENTSTACK_CLAUDE_MODELS": os.environ.get("AGENTSTACK_CLAUDE_MODELS", ""),
        "AGENTSTACK_CODEX_MODELS": "$CODEX_MODELS_SETTING",
        "AGENTSTACK_MAIL_LAUNCHD_LABEL": "$MAIL_LAUNCHD_LABEL_SETTING",
        "AGENTSTACK_HOOKS_DIR": "$HOOKS_DIR",
        "AGENTSTACK_SKILLS_DIR": "$SKILLS_DIR",
        "AGENTSTACK_RUNTIME_DIR": "$RUNTIME_DIR",
        "AGENTSTACK_PERSISTENT_PROFILES_DIR": "$PERSISTENT_PROFILES_DIR",
        "AGENTSTACK_DASHBOARD_LOG": "$DASHBOARD_LOG",
        "AGENTSTACK_DASHBOARD_LOG_MAX_BYTES": "$DASHBOARD_LOG_MAX_BYTES",
        "AGENTSTACK_DASHBOARD_LOG_BACKUPS": "$DASHBOARD_LOG_BACKUPS",
        "AGENTSTACK_DASHBOARD_RESTART_DELAY": "$DASHBOARD_RESTART_DELAY",
        "AGENTSTACK_MAIL_DB": "$MAIL_DB",
        "AGENTSTACK_MAIL_ENV": "$MAIL_ENV",
        "AGENTSTACK_MAIL_HOME": "$MAIL_HOME",
        "AGENTSTACK_SIGNALS_DIR": "$SIGNALS_DIR",
        "AGENTSTACK_MCP_URL": "$MCP_URL",
        "AGENTSTACK_CLAUDE_JSON": "$CLAUDE_JSON",
        "AGENTSTACK_TERMINAL": "$TERMINAL",
        "AGENTSTACK_AUTO_OPEN_CHILD": "$AUTO_OPEN_CHILD_SETTING",
    },
    "owned_files": owned_files,
    "owned_dirs": owned_dirs,
    "skill_links": skill_links,
    "services": services,
    "backups": [
        merge.get("backup")
        for merge in (settings_merge, claude_mcp_merge)
        if merge and merge.get("backup")
    ],
    "settings_backups": [settings_merge.get("backup")] if settings_merge and settings_merge.get("backup") else [],
    "retained_paths": [
        path for path in (
            "$MAIL_DIR",
            "$MAIL_HOME",
            "$MAIL_DB",
            "$MAIL_ENV",
            "$RUNTIME_DIR",
            "$PERSISTENT_PROFILES_DIR",
            "$CONNECTIONS_DIR",
        ) if path
    ],
    "purge_paths": [
        "$MAIL_DIR",
        "$MAIL_HOME",
        "$RUNTIME_DIR",
        "$PERSISTENT_PROFILES_DIR",
        "$CONNECTIONS_DIR",
    ],
    "notes": [
        "Tier1 user-settings merge is JSON-parser based, explicit-confirm only, and manifest recorded.",
        "Claude and Codex skills use manifest-owned symlinks; existing conflicts are preserved.",
        "Dashboard service logs persist under runtime with bounded rotation and crash restart diagnostics.",
        "Claude MCP user config uses an explicit-confirm, fixed-name structural merge.",
    ],
}
manifest["agent_mail"]["provider"] = "agentstack"
manifest["agent_mail"]["state_root"] = "$NATIVE_MAIL_STATE_ROOT"
manifest["agent_mail"]["candidate_venv"] = "$NATIVE_MAIL_VENV"
manifest["agent_mail"]["service_env"] = "$NATIVE_MAIL_ENV"
manifest["agent_mail"]["deployment_metadata"] = "$NATIVE_MAIL_DEPLOYMENT"
manifest["agent_mail"]["enroll_bin"] = "$NATIVE_MAIL_ENROLL_BIN"
manifest["agent_mail"]["deployment_identified"] = mail_deployment_identified
manifest["agent_mail"]["enrollment_available"] = mail_enrollment_available
manifest["agent_mail"]["autostart_managed_by_this_install"] = mail_autostart_managed
mail_update = json.loads(os.environ.get("AGENTSTACK_INSTALL_MAIL_UPDATE") or "null")
if mail_update:
    # The last --update-mail outcome. "rolled-back" and "not-switched" mean the
    # deployment above is the one that was already running.
    manifest["agent_mail"]["update"] = mail_update
manifest["env"].update({
    "AGENTSTACK_MAIL_DIR": "$NATIVE_MAIL_SERVICE_ROOT",
    "AGENTSTACK_MAIL_STATE_ROOT": "$NATIVE_MAIL_STATE_ROOT",
    "AGENTSTACK_MAIL_HTTP_BEARER_MODE": "$MAIL_HTTP_BEARER_MODE",
    "AGENTSTACK_MAIL_ENROLL_BIN": "$NATIVE_MAIL_ENROLL_BIN",
})
out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\\n", encoding="utf-8")
PY
  "$PYTHON_BIN" -m json.tool "$tmp" >/dev/null
  mv "$tmp" "$MANIFEST"
}

main() {
  say "ORRERY Telemetry core installer"
  say "tier: $TIER"
  say "install dir: $INSTALL_DIR"
  say "project key: $PROJECT_KEY"
  if [[ "$RESET_SETTINGS" == 1 ]]; then
    say "settings: reset (not inherited from the existing env.sh)"
  fi
  say "dashboard port: $PORT"
  say "label prefix: $LABEL_PREFIX"
  say "terminal: $TERMINAL"
  say "ORRERY Mail MCP URL: $MCP_URL"
  say "spawn dirs: ${SPAWN_DIRS_SETTING:-(default: the project key, then ~)}"
  say "spawn roots: ${SPAWN_ROOTS_SETTING:-(default: \$HOME)}"
  say "worktree root: $WORKTREE_ROOT_SETTING"
  say "automatically open child terminals: $AUTO_OPEN_CHILD_SETTING (set AGENTSTACK_AUTO_OPEN_CHILD=0 on reinstall to disable auto-open while keeping Deck Open tmux)"
  say "codex child approval: $CODEX_CHILD_APPROVAL_SETTING"
  say "codex child config overlay: ${CODEX_CHILD_CONFIG_OVERLAY_SETTING:-(disabled)}"
  say "codex network: $CODEX_NETWORK_SETTING"
  say "child default tools: ${CHILD_DEFAULT_TOOLS_SETTING:-none}"
  say "codex bin: ${CODEX_BIN_SETTING:-(not found on PATH; Codex spawns will fail until --codex-bin is set)}"
  say "codex add dirs: ${CODEX_ADD_DIRS_SETTING:-(none beyond project, spawn dirs/roots, install dir, worktrees, ~/.claude, ~/.codex)}"
  say "Claude/Codex child resume retention: $CHILD_RESUME_RETENTION_DAYS_SETTING day(s)"
  validate_assume_yes
  if ! run_preflight; then
    exit 1
  fi
  validate_codex_child_overlay
  if [[ "$TIER" == "tier1" ]]; then
    say "Tier1 will show MCP and user-settings dry-run diffs before any merge."
  elif [[ "$TIER" == "tier2" ]]; then
    say "Phase 3a note: Tier2 project enable is a placeholder; no project settings are modified."
  fi
  check_dependencies
  validate_repo_assets
  check_port
  # A legacy listener can answer health_check but report its database relative
  # to its own working directory. Honor the explicit retirement choice before
  # treating that listener as a reusable ORRERY Mail service.
  retire_legacy_mail_services
  resolve_native_mail_connection
  if ! check_agent_mail_provisioning_dependencies; then
    exit 1
  fi
  local service_kind
  service_kind="$(detect_service_kind)"
  # detect_service_kind knows which manager to try, not whether it will work:
  # the bootstrap below is the capability probe. Say "planned" so a dry-run,
  # which never reaches that probe, cannot be read as a promise.
  say "planned service mode: $service_kind (falls back to supervised background if it cannot start)"
  create_layout
  migrate_legacy_annotations
  migrate_legacy_dashboard_log
  install_payload
  install_claude_skill_links
  install_codex_skill_links
  render_installed_templates
  ensure_native_agentstack_mail
  say "ORRERY Mail requested-name handling: honored (passthrough)"
  write_enrollment_connection_profile
  write_env_file
  mail_update_disarm_switch
  # After write_env_file: the unit runs `agentstack-mailctl start`, which reads
  # env.sh. Registering it earlier would fire RunAtLoad against a config that
  # does not exist yet. This is outside ensure_native_agentstack_mail on purpose
  # — that function returns early when a healthy server is already running, which
  # is precisely the case for every existing user re-running install.sh to
  # update, and they need the autostart most.
  enable_mail_autostart
  enable_mail_watcher
  safe_merge_claude_mcp
  safe_merge_settings
  safe_managed_doc_setups
  start_service "$service_kind"
  write_manifest "${ACTIVE_SERVICE_KIND:-manual}" "${SERVICE_PATH:-}" \
    "${AGENT_MAIL_SERVICE_KIND:-}" "${AGENT_MAIL_SERVICE_PATH:-}"
  verify_dashboard_service
  if [[ "$DRY_RUN" == true ]]; then
    say "Dry-run complete: no files were written."
  else
    say "Install complete: $URL"
    say "Manifest: $MANIFEST"
    say "Dashboard log: $DASHBOARD_LOG"
    say "Run doctor: $BIN_DIR/agentstack-doctor"
    say "Verify operation: $BIN_DIR/agentstack-selftest"
    if [[ "$SERVICE_FALLBACK_USED" == true ]]; then
      say "Service mode: supervised background (launchd/systemd unavailable)"
    fi
    if [[ "$SERVICE_HEALTHY" != true ]]; then
      say "Dashboard was not started. Manual supervised start:"
      say "  . $ENV_FILE"
      say "  AGENTSTACK_DASHBOARD_SELF_RESTART=1 nohup $PYTHON_BIN $DASHBOARD_DIR/service_runner.py >> $DASHBOARD_LOG 2>&1 &"
      say "  echo \$! > $RUNTIME_DIR/dashboard.pid"
    fi
    if [[ "$TIER" != "tier0" ]]; then
      report_managed_instructions
    fi
  fi
  case "$MAIL_UPDATE_RESULT" in
    not-switched|rolled-back)
      if [[ "$MAIL_UPDATE_MODE" == auto ]]; then
        print_mail_stale_notice
        print_mail_result
        # auto chose not to, or could not, switch: the install itself is done
        # and Mail is serving its previous build.
        warn "ORRERY Mail was not updated ($MAIL_UPDATE_RESULT): $MAIL_UPDATE_REASON"
        warn "ORRERY Mail still runs $NATIVE_MAIL_ENV; to retry at a time you choose: install.sh --update-mail"
        return 0
      fi
      # The rest of the install finished against the build that is serving, but
      # what was asked for did not happen: exit non-zero so nobody reads this
      # run as an update.
      printf 'error: ORRERY Mail update %s: %s\n' "$MAIL_UPDATE_RESULT" "$MAIL_UPDATE_REASON" >&2
      printf 'error: ORRERY Mail still runs %s\n' "$NATIVE_MAIL_ENV" >&2
      exit 1
      ;;
  esac
  print_mail_stale_notice
  print_mail_result
}

# A Mail older than this checkout that this run did not replace: say what that
# costs, how to update, the risk of updating and the recovery, before the
# result line (which stays the last line of stdout).
print_mail_stale_notice() {
  local from="${OLD_NATIVE_MAIL_SOURCE_ID:-}" to="${NEW_NATIVE_MAIL_SOURCE_ID:-}"
  [[ "$DRY_RUN" != true && -n "$from" && -n "$to" && "$from" != "$to" ]] || return 0
  case "${MAIL_UPDATE_RESULT:-$MAIL_RESULT}" in
    kept|refused|not-switched|rolled-back) ;;
    *) return 0 ;;
  esac
  echo
  "$PYTHON_BIN" "$SCRIPT_DIR/lib/mail_update_notice.py" stale --running "$from" --to "$to" \
    --repo "$REPO_ROOT" --mcp-url "$MCP_URL" || true
  echo
}

# The last line about ORRERY Mail, for setup.sh/update.sh to read instead of
# prose: key=value pairs, no value contains a space, reason is a fixed code.
#   mail-result: <installed|switched|kept|unchanged|refused|rolled-back> mode=<m> from=<id> to=<id> running=<id> outage_s=<n> reason=<code>
# from: the build serving before this run (empty when none, or unidentified).
# to: the build this checkout would deploy. running: the build serving now --
# to after a switch, from after kept/refused/rolled-back. A run that dies
# (rollback failed, --update-mail refused) or is interrupted prints no line
# and exits non-zero; a reader must treat a missing line as a failure.
# A dry run prints the same fields as `mail-plan:` (nothing happened).
print_mail_result() {
  local result="$MAIL_RESULT" code="$MAIL_RESULT_CODE" from="${OLD_NATIVE_MAIL_SOURCE_ID:-}" to="${NEW_NATIVE_MAIL_SOURCE_ID:-}" running
  case "$MAIL_UPDATE_RESULT" in
    switched) result=switched code=newer_build ;;
    not-switched) result=refused ;;
    rolled-back) result=rolled-back ;;
    *)
      if [[ "$MAIL_UPDATE_PLANNED" == true ]]; then
        result=switch code=newer_build
      elif [[ "$PROVISION_NATIVE_MAIL" == true ]]; then
        result=installed code=no_running_mail from="" to="$NATIVE_MAIL_SOURCE_ID"
      fi
      ;;
  esac
  [[ -n "$result" ]] || { result=unchanged; code="${code:-adopted}"; }
  if [[ "$DRY_RUN" == true ]]; then
    case "$result" in installed) result=install ;; refused) result=refuse ;; kept) result=keep ;; esac
    printf 'mail-plan: %s mode=%s running=%s to=%s reason=%s\n' "$result" "$MAIL_UPDATE_MODE" "$from" "$to" "${code:-unknown}"
    return
  fi
  case "$result" in
    switched|installed) running="$to" ;;
    *) running="$from" ;;
  esac
  printf 'mail-result: %s mode=%s from=%s to=%s running=%s outage_s=%s reason=%s\n' \
    "$result" "$MAIL_UPDATE_MODE" "$from" "$to" "$running" "${MAIL_UPDATE_OUTAGE_SECONDS:-0}" "${code:-unknown}"
}

main "$@"
