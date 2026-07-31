#!/usr/bin/env bash
# Shared adjective+scientist name helpers for agent launchers.
# SOURCE this file; callers are expected to run with set -euo pipefail.

if [ -n "${BASH_SOURCE:-}" ]; then _ags_scientists_src="${BASH_SOURCE[0]}"; else _ags_scientists_src="$0"; fi
AGS_SCIENTISTS_LIB_DIR="$(cd "$(dirname "$_ags_scientists_src")" && pwd)"

# Keep byte-for-byte semantic parity with mcp-agent-mail utils.py
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

ags_pick_scientist() {
  local json_path="${1:-$(ags_scientists_json)}"
  [[ -f "$json_path" ]] || return 1
  python3 - "$json_path" <<'PY'
import json
import secrets
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    data = json.load(fh)
names = sorted(name for name in data if name.isascii() and name.isalpha())
if not names:
    sys.exit(1)
print(secrets.choice(names))
PY
}

ags_pick_adjective_scientist_name() {
  local json_path="${1:-$(ags_scientists_json)}"
  local adjective scientist
  adjective="$(ags_pick_adjective)" || return 1
  scientist="$(ags_pick_scientist "$json_path")" || return 1
  # Stock mcp-agent-mail preserves this spelling as the durable identity.
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
