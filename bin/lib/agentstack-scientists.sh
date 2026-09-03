#!/usr/bin/env bash
# Shared adjective+scientist name helpers for agent launchers.
# SOURCE this file; callers are expected to run with set -euo pipefail.

if [ -n "${BASH_SOURCE:-}" ]; then _ags_scientists_src="${BASH_SOURCE[0]}"; else _ags_scientists_src="$0"; fi
AGS_SCIENTISTS_LIB_DIR="$(cd "$(dirname "$_ags_scientists_src")" && pwd)"

# Keep byte-for-byte semantic parity with the frozen predecessor vocabulary.
# SIMPLE_ADJECTIVES (Round 3, 2026-06-26).  Do not extend independently:
# strict agent-mail deployments validate generated names against that canon.
AGS_SIMPLE_ADJECTIVES=(
  Red Orange Pink Black Purple Blue Brown White Green Gold Gray Navy Silver
  Amber Coral Crimson Cyan Indigo Jade Olive Rose Ruby Sage Scarlet Teal Violet
  Copper Bronze Emerald Azure Sunny Foggy Stormy Windy Frosty Cloudy Rainy Misty
  Hazy Dusty Breezy Snowy Starry Wintry Sandy Rocky Leafy Mossy Swift Quiet Bold
  Calm Bright Dark Wild Brave Noble Curious Sharp Gentle Silent Keen Vivid Sturdy
  Lucky Happy Merry Jolly Lively Clever Nimble Hardy Mighty Sleek Cozy Grand Royal
  Loyal Proud Humble Eager Warm Cool Fresh Crisp Pure Kind Quick Wise Witty Spry
  Brisk Steady Mellow Agile Trusty Tan Mint Lime Aqua Slate Ash Hazel Cream Peach
  Steel Brass Plum Cherry Cocoa Icy Balmy Chilly Gusty Dewy Polar Solar Lunar Cosmic
  Stellar Jovial Cheery Plucky Deft Astute Dapper Neat Smart Hearty Snug Zesty Jaunty
  Rosy Lush
)

ags_scientists_json() {
  if [[ -n "${AGENTSTACK_SCIENTISTS_JSON:-}" ]]; then
    printf '%s\n' "$AGENTSTACK_SCIENTISTS_JSON"
    return 0
  fi

  local lib_dir root
  lib_dir="$AGS_SCIENTISTS_LIB_DIR"
  root="$(cd "$lib_dir/../.." && pwd)"
  printf '%s\n' "$root/dashboard/scientist_portraits.json"
}

ags_adjective_list() {
  printf '%s\n' "${AGS_SIMPLE_ADJECTIVES[@]}"
}

ags_pick_adjective() {
  python3 - "${AGS_SIMPLE_ADJECTIVES[@]}" <<'PY'
import secrets
import sys

names = sys.argv[1:]
if not names:
    sys.exit(1)
print(secrets.choice(names))
PY
}

ags_scientist_list() {
  local json_path="${1:-$(ags_scientists_json)}"
  [[ -f "$json_path" ]] || return 1
  python3 - "$json_path" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    data = json.load(fh)
for name in sorted(data):
    if name.isascii() and name.isalpha():
        print(name)
PY
}

# Surnames handed out recently, most recent last.  The draw itself is uniform
# (secrets.choice over the 50 portrait names), but uniform draws collide far
# more than people expect: with 17 agents in a day, the chance that some
# surname lands three times is ~21%.  2026-08-10 produced FoggyPauling,
# VioletPauling and MossyPauling and the operator misread three different
# agents as one.  The fix is not a fairer coin, it is remembering.
AGS_SCIENTIST_RECENT_LIMIT="${AGENTSTACK_SCIENTIST_RECENT_LIMIT:-12}"

ags_scientist_recent_file() {
  printf '%s\n' "${AGENTSTACK_HOME:-$HOME/.agentstack}/state/recent-scientists"
}

ags_scientist_recent_list() {
  local f; f="$(ags_scientist_recent_file)"
  [[ -f "$f" ]] && tail -n "$AGS_SCIENTIST_RECENT_LIMIT" "$f" || true
}

# Call once the name is actually claimed, not once it is drawn: the picker may
# discard many candidates, and recording those would exhaust the roster with
# surnames nobody is using.
ags_note_scientist_used() {
  local agent_name="$1" json_path="${2:-$(ags_scientists_json)}" surname f
  [[ -f "$json_path" ]] || return 0
  surname="$(python3 -c '
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    names = [n for n in json.load(fh) if n.isascii() and n.isalpha()]
name = sys.argv[2]
hit = [n for n in names if name.endswith(n)]
print(max(hit, key=len) if hit else "")
' "$json_path" "$agent_name")" || return 0
  [[ -n "$surname" ]] || return 0
  f="$(ags_scientist_recent_file)"
  mkdir -p "$(dirname "$f")" 2>/dev/null || return 0
  printf '%s\n' "$surname" >>"$f" 2>/dev/null || return 0
  # Keep the ledger from growing without bound; only the tail is ever read.
  if [[ "$(wc -l <"$f" 2>/dev/null || echo 0)" -gt $((AGS_SCIENTIST_RECENT_LIMIT * 8)) ]]; then
    tail -n "$AGS_SCIENTIST_RECENT_LIMIT" "$f" >"$f.tmp" 2>/dev/null && mv "$f.tmp" "$f"
  fi
}

ags_pick_scientist() {
  local json_path="${1:-$(ags_scientists_json)}"
  [[ -f "$json_path" ]] || return 1
  # The recent list goes through argv, not stdin: a heredoc program and a piped
  # payload both claim stdin, and the heredoc wins silently — the filter would
  # simply never apply and nothing would say so.
  #
  # Read into an array rather than splitting an unquoted string.  Word splitting
  # on unquoted expansion is a bash behaviour that zsh does not share, so the
  # string form passes one argument instead of twelve when this file is sourced
  # from a zsh shell — the filter degrades to a no-op and still prints a name.
  local -a recent=()
  local _line
  while IFS= read -r _line; do
    [[ -n "$_line" ]] && recent+=("$_line")
  done < <(ags_scientist_recent_list)
  python3 -c '
import json
import secrets
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    data = json.load(fh)
names = sorted(name for name in data if name.isascii() and name.isalpha())
if not names:
    sys.exit(1)
recent = {n for n in sys.argv[2:] if n}
# Never fail because everything is recent: a smaller roster or a long ledger
# must degrade to the old uniform draw, not to no name at all.
fresh = [n for n in names if n not in recent] or names
print(secrets.choice(fresh))
' "$json_path" ${recent[@]+"${recent[@]}"}
}

ags_pick_adjective_scientist_name() {
  local json_path="${1:-$(ags_scientists_json)}"
  local adjective scientist
  adjective="$(ags_pick_adjective)" || return 1
  scientist="$(ags_pick_scientist "$json_path")" || return 1
  # The predecessor preserved this spelling as the durable identity.
  # Some local patched deployments coerce it to AdjectiveScientist instead;
  # callers must always adopt register_agent's returned name after registering.
  printf '%s-%s\n' "$adjective" "$scientist"
}

ags_has_scientist_suffix() {
  local agent_name="$1"
  local json_path="${2:-$(ags_scientists_json)}"
  [[ -f "$json_path" ]] || return 1
  python3 - "$json_path" "$agent_name" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    scientists = [name for name in json.load(fh) if name.isascii() and name.isalpha()]
agent_name = sys.argv[2]
sys.exit(0 if any(agent_name.endswith(name) for name in scientists) else 1)
PY
}
