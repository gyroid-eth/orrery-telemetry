#!/usr/bin/env bash
# Dashboard adapter for a Gemini child that ORRERY Mail already registered.
# Accepts the same pre-registered contract used by dashboard/server.py, then
# launches Antigravity headless in an isolated worktree.
set -euo pipefail

PROG="spawn_gemini_preregistered.sh"
HOOKS_DIR="${AGENTSTACK_HOOKS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
AGENTSTACK_HOME_DIR="${AGENTSTACK_HOME:-$(cd "$HOOKS_DIR/.." && pwd)}"
PROJECT_KEY="${AGENTSTACK_PROJECT_KEY:-${PROJECT_KEY:-}}"
MCP_URL="${AGENTSTACK_MCP_URL:-http://127.0.0.1:18765/mcp}"
MAIL_ENV="${AGENTSTACK_MAIL_ENV:-$HOME/.agentstack/mail/.env}"
HTTP_BEARER_MODE="${AGENTSTACK_MAIL_HTTP_BEARER_MODE:-auto}"
RUNTIME_DIR="${AGENTSTACK_RUNTIME_DIR:-$HOME/.agentstack/runtime}"
MANAGED_FILE="${AGENTSTACK_MANAGED_AGENTS_FILE:-$RUNTIME_DIR/managed_agents.txt}"
PYTHON_BIN="${AGENTSTACK_PYTHON:-python3}"
GEMINI_BIN="${AGENTSTACK_GEMINI_BIN:-agy}"
EFFORT="${AGENTSTACK_GEMINI_EFFORT:-high}"
RESOURCES="${AGENTSTACK_GEMINI_RESOURCES:-}"
TASK_FILE="${AGENTSTACK_GEMINI_TASK_FILE:-}"
PRINT_TIMEOUT="${AGENTSTACK_GEMINI_PRINT_TIMEOUT:-30m}"
RESOURCE_TTL="${AGENTSTACK_GEMINI_RESOURCE_TTL:-14400}"
WORKTREE_ROOT="${AGENTSTACK_WORKTREE_ROOT:-/tmp/cc-worktrees}"
WORKTREE_BASE_REV=""
CHILD_NAME=""
CHILD_TOKEN_FILE=""
MODEL="${AGENTSTACK_GEMINI_MODEL:-gemini-3.8-flash-high}"
WORK_DIR=""

MAIL_HELPER="$AGENTSTACK_HOME_DIR/bin/agentstack-gemini-child-mail"
STREAM_HELPER="$AGENTSTACK_HOME_DIR/bin/agentstack-gemini-stream"
MCP_WRAPPER="$AGENTSTACK_HOME_DIR/bin/agentstack-gemini-mcp"
CLEANUP_HELPER="$HOOKS_DIR/cleanup-child-agent.sh"

usage() {
  cat >&2 <<'EOF'
Usage: spawn_gemini_preregistered.sh --pre-registered NAME --child-token-file FILE \
  [--worktree] [--worktree-base REV] --model MODEL [TASK_SHORT] [WORKDIR]
EOF
}

while [[ "${1:-}" == --* ]]; do
  case "$1" in
    --pre-registered) CHILD_NAME="${2:-}"; shift 2 ;;
    --child-token-file|--token-file) CHILD_TOKEN_FILE="${2:-}"; shift 2 ;;
    --model) MODEL="${2:-}"; shift 2 ;;
    --worktree) shift ;;
    --worktree-base) WORKTREE_BASE_REV="${2:-}"; shift 2 ;;
    --standalone)
      echo "$PROG: standalone Gemini launch is not supported by the dashboard adapter" >&2
      exit 2
      ;;
    *) echo "$PROG: unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

TASK_SHORT="${1:-}"
WORK_DIR="${2:-$PWD}"
[[ -n "$CHILD_NAME" ]] || { usage; exit 2; }
[[ -n "$CHILD_TOKEN_FILE" && -s "$CHILD_TOKEN_FILE" ]] || { echo "$PROG: child token file is required" >&2; exit 2; }
[[ -n "$PROJECT_KEY" ]] || { echo "$PROG: AGENTSTACK_PROJECT_KEY is required" >&2; exit 2; }
[[ -n "${PARENT_AGENT:-}" ]] || { echo "$PROG: PARENT_AGENT is required" >&2; exit 2; }
[[ -n "$RESOURCES" ]] || { echo "$PROG: Gemini dashboard launch requires declared resources" >&2; exit 2; }
[[ -n "$TASK_FILE" && -s "$TASK_FILE" ]] || { echo "$PROG: full task handoff file is missing" >&2; exit 2; }
[[ -d "$WORK_DIR" ]] || { echo "$PROG: workdir does not exist: $WORK_DIR" >&2; exit 2; }
case "$EFFORT" in low|medium|high) ;; *) echo "$PROG: invalid effort: $EFFORT" >&2; exit 2 ;; esac
command -v tmux >/dev/null 2>&1 || { echo "$PROG: tmux not found" >&2; exit 1; }
command -v "$GEMINI_BIN" >/dev/null 2>&1 || { echo "$PROG: Antigravity CLI not found (expected agy)" >&2; exit 1; }
command -v "$PYTHON_BIN" >/dev/null 2>&1 || [[ -x "$PYTHON_BIN" ]] || { echo "$PROG: selected Python is unavailable" >&2; exit 1; }
[[ -x "$MAIL_HELPER" && -x "$STREAM_HELPER" && -x "$MCP_WRAPPER" ]] || { echo "$PROG: Gemini provider helpers are not installed" >&2; exit 1; }

# Defend the resource boundary independently of the Dashboard. Without ROOT,
# check declaration syntax and print the canonical form; with ROOT, also
# reject any existing symlink component, at any depth of any path a resource
# covers, that resolves outside ROOT.
validate_resources() {
  "$PYTHON_BIN" - "$RESOURCES" "${1:-}" <<'PY'
import fnmatch, os, re, sys, unicodedata
raw, root = sys.argv[1], sys.argv[2]
def fail(message):
    sys.stderr.write("spawn_gemini_preregistered.sh: " + message + "\n")
    raise SystemExit(2)
if re.search(r"[\x00-\x1f\x7f]", raw):
    fail("resource contains a control character")
canonical = []
for item in (value.strip() for value in raw.split(",")):
    if not item:
        continue
    if item.startswith(("~", "/", "-")) or "\\" in item:
        fail("unsafe resource declaration: " + item)
    parts = [part for part in item.split("/") if part not in ("", ".")]
    # ./-rf normalizes to -rf, so check the form that is handed on as well.
    if (not parts or parts[0].startswith("-") or ".." in parts
            or any(part.lower() == ".git" for part in parts)):
        fail("unsafe resource declaration: " + item)
    value = "/".join(parts)
    if value not in canonical:
        canonical.append(value)
if not canonical:
    fail("Gemini dashboard launch requires declared resources")
if root:
    base = os.path.realpath(root)
    budget = [200000]
    def inside(path):
        return path == base or path.startswith(base.rstrip(os.sep) + os.sep)
    def names_git(path):
        return inside(path) and any(
            part.casefold() == ".git" for part in os.path.relpath(path, base).split(os.sep))
    # Resolve one hop at a time and classify every location visited inside the
    # worktree, so src/meta -> ../.git is metadata whether .git is a directory,
    # a gitdir file, or a symlink to a differently named store.
    def reaches_git(start, names):
        current, pending, hops = start, list(reversed(names)), 0
        while pending and hops <= 40:
            name = pending.pop()
            if name in ("", "."):
                continue
            if name == "..":
                current = os.path.dirname(current)
                continue
            candidate = os.path.join(current, name)
            if names_git(candidate):
                return True
            try:
                target = os.readlink(candidate)
            except OSError:
                current = candidate
                continue
            hops += 1
            if target.startswith("/"):
                current = "/"
            pending.extend(reversed(target.split("/")))
        return names_git(os.path.realpath(os.path.join(start, *names)))
    def closure(pattern, positions):
        closed, stack = set(), list(positions)
        while stack:
            index = stack.pop()
            if index not in closed:
                closed.add(index)
                if index < len(pattern) and pattern[index] == "**":
                    stack.append(index + 1)
        return frozenset((len(pattern),)) if len(pattern) in closed else frozenset(closed)
    # Default APFS equates case variants (with full folding) and normalization
    # forms, so SR? opens src and .GI? opens .git. A component also matches
    # after casefolding both sides, or through folded(), which compares the
    # canonical caseless forms: a positive ASCII class folds to lowercase
    # members; ? and any other class widen to one or more characters, since
    # one character can fold to several and [!s] excludes only one case; and
    # combining marks at a literal edge next to such a wildcard can be
    # reordered across it, so the wildcard absorbs them. Folding only adds
    # matches and reads names alone, so case-sensitive roots get the same answer.
    def fold(text):
        return unicodedata.normalize("NFD", unicodedata.normalize("NFD", text).casefold())
    def trim(text, start, end):
        while start and text and unicodedata.combining(text[0]):
            text = text[1:]
        while end and text and unicodedata.combining(text[-1]):
            text = text[:-1]
        return text
    folded_cache = {}
    def folded(part):
        if part in folded_cache:
            return folded_cache[part]
        tokens, index = [], 0
        while index < len(part):
            char = part[index]
            index += 1
            if char == "[":
                end = index + (part[index:index + 1] == "!")
                end = part.find("]", end + (part[end:end + 1] == "]"))
                if end >= 0:
                    token, index = part[index - 1:end + 1], end + 1
                    # fnmatch decides negation: [a-[!.] drops its empty range to [!.].
                    if token.isascii() and not fnmatch.fnmatchcase("\u0100", token):
                        members = sorted({chr(code).lower() for code in range(128)
                                          if fnmatch.fnmatchcase(chr(code), token)})
                        tokens.append(("class", "[" + "".join(map(re.escape, members)) + "]"
                                       if members else "(?!)"))
                    else:
                        tokens.append(("wild", ".+"))
                    continue
            if char in "*?":
                tokens.append(("wild", ".*" if char == "*" else ".+"))
            elif tokens and tokens[-1][0] == "literal":
                tokens[-1] = ("literal", tokens[-1][1] + char)
            else:
                tokens.append(("literal", char))
        regex = []
        for position, (kind, value) in enumerate(tokens):
            if kind == "literal":
                start = position > 0 and tokens[position - 1][0] == "wild"
                end = position + 1 < len(tokens) and tokens[position + 1][0] == "wild"
                value = trim(unicodedata.normalize("NFD", value), start, end)
                value = re.escape(trim(fold(value), start, end))
            regex.append(value)
        folded_cache[part] = re.compile("".join(regex), re.S)
        return folded_cache[part]
    def matches(name, part):
        try:
            return (fnmatch.fnmatchcase(name, part)
                    or fnmatch.fnmatchcase(name.casefold(), part.casefold())
                    or folded(part).fullmatch(fold(name)) is not None)
        except re.error:
            # Some interpreters reject a reversed range such as casefolded [Z-a].
            return True
    def step(pattern, states, name):
        # len(pattern) marks a match; a matched directory covers its subtree.
        if len(pattern) in states:
            return frozenset((len(pattern),))
        nxt = set()
        for index in states:
            if pattern[index] == "**":
                nxt.add(index)
            elif matches(name, pattern[index]):
                nxt.add(index + 1)
        return closure(pattern, nxt)
    def unverifiable(value, where):
        if budget[0] < 0:
            fail("resource covers too many paths to verify the worktree boundary: " + value)
        fail("could not verify the worktree boundary for resource: " + value + " (" + where + ")")
    # Walk every existing path the pattern covers at any depth. Symlinks are
    # resolved, never followed implicitly; an inside directory target is walked
    # once per (real directory, pattern state), so cycles terminate. Missing
    # paths pass; unreadable directories and an exhausted budget fail closed.
    # An inside symlink on a covered path that reaches git metadata is covered.
    def escape(value, literal, pattern):
        pending = [(os.path.realpath(os.path.join(base, *literal)), "/".join(literal),
                    closure(pattern, {0}), reaches_git(base, literal))]
        seen = set()
        first_escape = ""
        while pending:
            directory, relative, states, in_git = pending.pop()
            if (directory, states, in_git) in seen:
                continue
            seen.add((directory, states, in_git))
            try:
                entries = os.scandir(directory)
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError:
                unverifiable(value, relative or ".")
            try:
                with entries:
                    for entry in entries:
                        budget[0] -= 1
                        if budget[0] < 0:
                            unverifiable(value, relative or ".")
                        nxt = step(pattern, states, entry.name)
                        if not nxt:
                            continue
                        path = relative + "/" + entry.name if relative else entry.name
                        covered_git = in_git or entry.name.casefold() == ".git"
                        if covered_git and len(pattern) in nxt:
                            return ("git", path)
                        if entry.is_symlink():
                            target = os.path.realpath(entry.path)
                            if not inside(target):
                                if not first_escape:
                                    first_escape = path
                                continue
                            if reaches_git(directory, [entry.name]):
                                return ("git", path)
                            if os.path.isdir(target):
                                pending.append((target, path, nxt, covered_git))
                        elif entry.is_dir(follow_symlinks=False):
                            pending.append((entry.path, path, nxt, covered_git))
            except OSError:
                unverifiable(value, relative or ".")
        return ("escape", first_escape) if first_escape else ("", "")
    for value in canonical:
        parts = value.split("/")
        literal = []
        for part in parts:
            if set("*?[") & set(part):
                break
            literal.append(part)
        for index in range(1, len(literal) + 1):
            if not inside(os.path.realpath(os.path.join(base, *literal[:index]))):
                fail("resource escapes the worktree through a symlink: " + value)
        for index in range(1, len(literal) + 1):
            if reaches_git(base, literal[:index]):
                fail("resource must not cover git metadata: " + value + " ("
                     + "/".join(literal[:index]) + ")")
        found_kind, found_path = escape(value, literal, parts[len(literal):])
        if found_kind == "git":
            fail("resource must not cover git metadata: " + value + " (" + found_path + ")")
        if found_kind:
            fail("resource escapes the worktree through a symlink: " + value + " (" + found_path + ")")
print(",".join(canonical))
PY
}
RESOURCES="$(validate_resources)" || exit 2

SOURCE_REPO="$(git -C "$WORK_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
[[ -n "$SOURCE_REPO" ]] || { echo "$PROG: Gemini dashboard launch requires a git repository" >&2; exit 1; }
if [[ -n "$WORKTREE_BASE_REV" ]]; then
  BASE_REV="$(git -C "$SOURCE_REPO" rev-parse --verify "$WORKTREE_BASE_REV^{commit}" 2>/dev/null || true)"
else
  BASE_REV="$(git -C "$SOURCE_REPO" rev-parse --verify HEAD 2>/dev/null || true)"
fi
[[ -n "$BASE_REV" ]] || { echo "$PROG: could not resolve worktree base" >&2; exit 1; }

mkdir -p "$RUNTIME_DIR" "$WORKTREE_ROOT"
chmod 700 "$RUNTIME_DIR" 2>/dev/null || true
TOKEN_KEY="$(printf '%s' "$CHILD_NAME" | LC_ALL=C tr -c 'A-Za-z0-9_.-' '_')"
DURABLE_TOKEN="$RUNTIME_DIR/agent_token_$TOKEN_KEY"
WORKTREE_DIR="$WORKTREE_ROOT/$CHILD_NAME"
BRANCH_NAME="exp/$CHILD_NAME"
MCP_CONFIG=""
GIT_EXCLUDES_FILE=""
TASK_EVENT_FILE="$RUNTIME_DIR/gemini-task-$CHILD_NAME.ndjson"
RUNNER_FILE="$RUNTIME_DIR/gemini-runner-$CHILD_NAME.sh"
RESULT_LOG="$RUNTIME_DIR/gemini-$CHILD_NAME.ndjson"
STDERR_LOG="$RUNTIME_DIR/gemini-$CHILD_NAME.stderr.log"
WORKTREE_CREATED=false
RESERVED=false
TMUX_STARTED=false

mail_helper() {
  AGENTSTACK_HOME="$AGENTSTACK_HOME_DIR" \
  AGENTSTACK_MCP_URL="$MCP_URL" \
  AGENTSTACK_MAIL_ENV="$MAIL_ENV" \
  AGENTSTACK_MAIL_HTTP_BEARER_MODE="$HTTP_BEARER_MODE" \
    "$PYTHON_BIN" "$MAIL_HELPER" "$@"
}

cleanup_failure() {
  status=$?
  if [[ $status -ne 0 ]]; then
    if [[ "$TMUX_STARTED" == true && -n "$CHILD_NAME" ]]; then
      tmux kill-session -t "=$CHILD_NAME" >/dev/null 2>&1 || true
      TMUX_STARTED=false
    fi
    if [[ "$RESERVED" == true && -s "$DURABLE_TOKEN" ]]; then
      mail_helper release --project-key "$PROJECT_KEY" --agent-name "$CHILD_NAME" \
        --token-file "$DURABLE_TOKEN" --paths "$RESOURCES" >/dev/null 2>&1 || true
    fi
    if [[ -s "$DURABLE_TOKEN" ]]; then
      mail_helper retire --project-key "$PROJECT_KEY" --agent-name "$CHILD_NAME" \
        --token-file "$DURABLE_TOKEN" >/dev/null 2>&1 || true
    fi
    if [[ "$WORKTREE_CREATED" == true ]]; then
      git -C "$SOURCE_REPO" worktree remove --force "$WORKTREE_DIR" >/dev/null 2>&1 || true
      git -C "$SOURCE_REPO" branch -D "$BRANCH_NAME" >/dev/null 2>&1 || true
    fi
    rm -f "$TASK_EVENT_FILE" "$RUNNER_FILE" "$MCP_CONFIG" "$DURABLE_TOKEN" "$GIT_EXCLUDES_FILE" "$TASK_FILE"
  fi
}
trap cleanup_failure EXIT
# A readiness timeout signals this launcher; exit non-zero so the EXIT cleanup
# releases, retires, and removes what was created so far.
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# Consume the one-shot token into the stable per-agent runtime path expected by
# the MCP wrapper. The token value and token-file path are not embedded in the
# workspace MCP config.
( umask 077 && cat "$CHILD_TOKEN_FILE" > "$DURABLE_TOKEN" )
chmod 600 "$DURABLE_TOKEN"
rm -f "$CHILD_TOKEN_FILE"

[[ ! -e "$WORKTREE_DIR" ]] || { echo "$PROG: worktree path already exists: $WORKTREE_DIR" >&2; exit 1; }
if git -C "$SOURCE_REPO" show-ref --verify --quiet "refs/heads/$BRANCH_NAME"; then
  echo "$PROG: child branch already exists: $BRANCH_NAME" >&2
  exit 1
fi
git -C "$SOURCE_REPO" worktree add -b "$BRANCH_NAME" "$WORKTREE_DIR" "$BASE_REV" >/dev/null
WORKTREE_CREATED=true

# Linked worktrees share .git/info/exclude. Use a child-owned excludes file
# injected only into the Antigravity runner instead of mutating shared repo
# metadata, so overlapping delegated children cannot remove each other's rule.
GIT_EXCLUDES_FILE="$RUNTIME_DIR/gemini-git-excludes-$CHILD_NAME"
( umask 077 && printf '%s\n' '.agents/mcp_config.json' > "$GIT_EXCLUDES_FILE" )

# The worktree is what the child sees; check it before reserving anything.
RESOURCES="$(validate_resources "$WORKTREE_DIR")" || exit 2

mail_helper reserve --project-key "$PROJECT_KEY" --agent-name "$CHILD_NAME" \
  --token-file "$DURABLE_TOKEN" --paths "$RESOURCES" --ttl "$RESOURCE_TTL"
RESERVED=true

if git -C "$WORKTREE_DIR" ls-files --error-unmatch .agents/mcp_config.json >/dev/null 2>&1; then
  echo "$PROG: tracked .agents/mcp_config.json exists; refusing to overwrite it" >&2
  exit 1
fi
mkdir -p "$WORKTREE_DIR/.agents"
MCP_CONFIG="$WORKTREE_DIR/.agents/mcp_config.json"
AGS_GEMINI_MCP_PATH="$MCP_CONFIG" \
AGS_GEMINI_MCP_COMMAND="$MCP_WRAPPER" \
"$PYTHON_BIN" - <<'PY'
import json, os
from pathlib import Path
path = Path(os.environ["AGS_GEMINI_MCP_PATH"])
entry = {
    "command": os.environ["AGS_GEMINI_MCP_COMMAND"],
    "args": [],
}
path.write_text(json.dumps({"mcpServers": {"orrery-mail": entry}}, indent=2) + "\n", encoding="utf-8")
os.chmod(path, 0o600)
PY

"$PYTHON_BIN" - "$TASK_FILE" "$TASK_EVENT_FILE" "$CHILD_NAME" "$PARENT_AGENT" "$RESOURCES" "$WORKTREE_DIR" "$SOURCE_REPO" <<'PY'
import json, os, sys
from pathlib import Path
raw_path, event_path, child, parent, resources, worktree, source_repo = sys.argv[1:8]
workspace_root = Path(worktree).resolve(strict=False)
task = open(raw_path, encoding="utf-8").read()
# validate_resources already produced canonical repository-relative entries,
# so each one anchors lexically inside the worktree.
anchored_resources = [
    os.fspath(workspace_root / item) for item in resources.split(",") if item
]
resource_text = "\n".join(f"- {item}" for item in anchored_resources) or "- (explicitly none)"
prefix = (
    f"You are {child}, a delegated Antigravity child of {parent}. "
    f"Your active workspace root is {workspace_root}. "
    "Work only in this isolated worktree. The launcher already reserved the declared resources. "
    "Resolve every relative path in the task and every declared resource strictly against this workspace root. "
    "Use the worktree-anchored resource paths below. Do not search the source checkout, $HOME, or parent directories for them. "
    "If a declared resource is absent from this worktree, report that exact failure instead of searching elsewhere. "
    "Any matching path mentioned in the canonical task refers to the worktree-anchored path below. "
    "Do not register another ORRERY identity. The launcher will report your final result to the parent automatically.\n"
    f"Declared resources (worktree-anchored):\n{resource_text}\n\nCanonical task:\n"
)
with open(event_path, "w", encoding="utf-8") as fh:
    json.dump({"event": "user", "message": {"content": prefix + task}}, fh, ensure_ascii=False, separators=(",", ":"))
    fh.write("\n")
os.chmod(event_path, 0o600)
PY
rm -f "$TASK_FILE"

cat > "$RUNNER_FILE" <<EOF
#!/usr/bin/env bash
set -uo pipefail
cd $(printf '%q' "$WORKTREE_DIR") || exit 1
export AGENT_NAME=$(printf '%q' "$CHILD_NAME")
export PARENT_AGENT=$(printf '%q' "$PARENT_AGENT")
export AGENTSTACK_RESERVED_IDENTITY=1
export AGENTSTACK_PROJECT_KEY=$(printf '%q' "$PROJECT_KEY")
export AGENTSTACK_HOME=$(printf '%q' "$AGENTSTACK_HOME_DIR")
export AGENTSTACK_HOOKS_DIR=$(printf '%q' "$HOOKS_DIR")
export AGENTSTACK_RUNTIME_DIR=$(printf '%q' "$RUNTIME_DIR")
export AGENTSTACK_MCP_URL=$(printf '%q' "$MCP_URL")
export AGENTSTACK_MAIL_ENV=$(printf '%q' "$MAIL_ENV")
export AGENTSTACK_MAIL_HTTP_BEARER_MODE=$(printf '%q' "$HTTP_BEARER_MODE")
export AGENTSTACK_PYTHON=$(printf '%q' "$PYTHON_BIN")
git_excludes_file=$(printf '%q' "$GIT_EXCLUDES_FILE")
git_config_count="\${GIT_CONFIG_COUNT:-0}"
case "\$git_config_count" in
  ''|*[!0-9]*) git_config_count=0 ;;
esac
export "GIT_CONFIG_KEY_\${git_config_count}=core.excludesFile"
export "GIT_CONFIG_VALUE_\${git_config_count}=\$git_excludes_file"
export GIT_CONFIG_COUNT="\$((git_config_count + 1))"

set +e
cat $(printf '%q' "$TASK_EVENT_FILE") | \
  $(printf '%q' "$GEMINI_BIN") --input-format stream-json --output-format stream-json \
    --model $(printf '%q' "$MODEL") --effort $(printf '%q' "$EFFORT") \
    --print-timeout $(printf '%q' "$PRINT_TIMEOUT") \
    2> >(tee $(printf '%q' "$STDERR_LOG") >&2) | \
  $(printf '%q' "$PYTHON_BIN") $(printf '%q' "$STREAM_HELPER") $(printf '%q' "$RESULT_LOG")
pipeline_status=("\${PIPESTATUS[@]}")
agy_status="\${pipeline_status[1]:-1}"
stream_status="\${pipeline_status[2]:-1}"
if [[ "\$agy_status" -ne 0 ]]; then
  child_status="\$agy_status"
else
  child_status="\$stream_status"
fi
set -e
AGENTSTACK_HOME=$(printf '%q' "$AGENTSTACK_HOME_DIR") \
AGENTSTACK_MCP_URL=$(printf '%q' "$MCP_URL") \
AGENTSTACK_MAIL_ENV=$(printf '%q' "$MAIL_ENV") \
AGENTSTACK_MAIL_HTTP_BEARER_MODE=$(printf '%q' "$HTTP_BEARER_MODE") \
  $(printf '%q' "$PYTHON_BIN") $(printf '%q' "$MAIL_HELPER") report --project-key $(printf '%q' "$PROJECT_KEY") \
    --agent-name $(printf '%q' "$CHILD_NAME") --token-file $(printf '%q' "$DURABLE_TOKEN") \
    --parent $(printf '%q' "$PARENT_AGENT") --result-log $(printf '%q' "$RESULT_LOG") \
    --worktree $(printf '%q' "$WORKTREE_DIR") --runner-status "\$child_status" || true
AGENTSTACK_HOME=$(printf '%q' "$AGENTSTACK_HOME_DIR") \
AGENTSTACK_MCP_URL=$(printf '%q' "$MCP_URL") \
AGENTSTACK_MAIL_ENV=$(printf '%q' "$MAIL_ENV") \
AGENTSTACK_MAIL_HTTP_BEARER_MODE=$(printf '%q' "$HTTP_BEARER_MODE") \
  $(printf '%q' "$PYTHON_BIN") $(printf '%q' "$MAIL_HELPER") release --project-key $(printf '%q' "$PROJECT_KEY") \
    --agent-name $(printf '%q' "$CHILD_NAME") --token-file $(printf '%q' "$DURABLE_TOKEN") \
    --paths $(printf '%q' "$RESOURCES") || true
AGENTSTACK_HOME=$(printf '%q' "$AGENTSTACK_HOME_DIR") \
AGENTSTACK_MCP_URL=$(printf '%q' "$MCP_URL") \
AGENTSTACK_MAIL_ENV=$(printf '%q' "$MAIL_ENV") \
AGENTSTACK_MAIL_HTTP_BEARER_MODE=$(printf '%q' "$HTTP_BEARER_MODE") \
  $(printf '%q' "$PYTHON_BIN") $(printf '%q' "$MAIL_HELPER") retire --project-key $(printf '%q' "$PROJECT_KEY") \
    --agent-name $(printf '%q' "$CHILD_NAME") --token-file $(printf '%q' "$DURABLE_TOKEN") || true
[[ -x $(printf '%q' "$CLEANUP_HELPER") ]] && $(printf '%q' "$CLEANUP_HELPER") || true
rm -f $(printf '%q' "$TASK_EVENT_FILE") $(printf '%q' "$DURABLE_TOKEN") $(printf '%q' "$MCP_CONFIG") \
  $(printf '%q' "$GIT_EXCLUDES_FILE") $(printf '%q' "$RUNNER_FILE")
echo "[antigravity] child finished; worktree retained at $(printf '%q' "$WORKTREE_DIR")"
exit "\$child_status"
EOF
chmod 700 "$RUNNER_FILE"

tmux new-session -d -s "$CHILD_NAME" -c "$WORKTREE_DIR" \
  -e "AGENT_NAME=$CHILD_NAME" -e "PARENT_AGENT=$PARENT_AGENT" \
  -e "AGENTSTACK_RESERVED_IDENTITY=1" \
  "/bin/bash $(printf '%q' "$RUNNER_FILE")"
TMUX_STARTED=true

mkdir -p "$(dirname "$MANAGED_FILE")"
grep -qxF "$CHILD_NAME" "$MANAGED_FILE" 2>/dev/null || printf '%s\n' "$CHILD_NAME" >> "$MANAGED_FILE"
trap - EXIT HUP INT TERM
printf '%s\n' "$CHILD_NAME"
