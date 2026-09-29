# agentstack-managed-block.sh — compare an installed managed block with the
# block the installed template renders today. Sourced by agentstack-codex-setup
# and agentstack-claude-setup (their --check mode); doctor and the installer's
# summary call those scripts, so there is one comparison for all three.
#
# Only the text between the markers is compared. Personal instructions outside
# the block never make it stale, and neither mtime nor VERSION is used: a file
# touched yesterday can still hold an old block, and the project path is
# substituted into the block.
#
# Bash 3.2 compatible (macOS /bin/bash).

# ags_managed_block_state <target> <begin-marker> <end-marker> <rendered-block>
# Prints one word: current | differs | missing | malformed:<why>.
ags_managed_block_state() {
  local target="$1" begin="$2" end="$3" rendered="$4" extracted status
  if [[ ! -f "$target" ]]; then
    echo "missing"
    return 0
  fi
  # The end marker is shared by the Codex and Claude blocks, so only the first
  # end marker after this block's begin marker closes it.
  extracted="$(awk -v b="$begin" -v e="$end" '
    $0 == b { begins++; if (begins == 1) { inside = 1; print; next } }
    inside { print; if ($0 == e) { inside = 0; closed = 1 } }
    END {
      if (begins == 0) exit 3
      if (begins > 1) exit 4
      if (!closed) exit 5
    }
  ' "$target")" && status=0 || status=$?
  case "$status" in
    0) ;;
    3) echo "missing"; return 0 ;;
    4) echo "malformed:more than one begin marker"; return 0 ;;
    5) echo "malformed:no end marker after the begin marker"; return 0 ;;
    *) echo "malformed:could not read the file"; return 0 ;;
  esac
  # A nested begin marker inside the block is also broken.
  if [[ "$(printf '%s\n' "$extracted" | grep -cxF "$begin")" -ne 1 ]]; then
    echo "malformed:more than one begin marker"
    return 0
  fi
  if [[ "$extracted" == "$rendered" ]]; then
    echo "current"
  else
    echo "differs"
  fi
}

# ags_managed_block_report <label> <target> <state> <update-command>
# Prints the doctor-style line for one target; returns 0 only when current.
ags_managed_block_report() {
  local label="$1" target="$2" state="$3" update="$4"
  case "$state" in
    current)
      echo "ok: $label managed block in $target matches the installed template"
      return 0
      ;;
    differs)
      echo "warn: $label managed block in $target differs from the installed template (older instructions, or edited by hand); update it with: $update"
      ;;
    missing)
      echo "warn: $label managed block not found in $target; add it with: $update"
      ;;
    malformed:*)
      echo "warn: $label managed block in $target is malformed (${state#malformed:}); fix the markers by hand, then run: $update"
      ;;
    *)
      echo "warn: cannot check $label managed block in $target: $state"
      ;;
  esac
  return 1
}

# Shell-quote one word for a command line the user can paste.
ags_managed_block_quote() {
  printf '%q' "$1"
}
