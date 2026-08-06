#!/usr/bin/env bash
# Assemble the static demo into a directory that can be uploaded anywhere.
#
# The page is the real dashboard, not a copy — the whole point of demo_api.js
# is that there is one index.html. So this copies rather than transforms, and
# the only thing it decides is which portraits ship: the nine the cast uses,
# not all fifty. Anything the bundle does not contain, a visitor cannot load.
#
#   dashboard/demo/build.sh [outdir]     # default: dashboard/demo/dist
#
# Serve the result at any path; nothing in it is absolute.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DASH="$(dirname "$HERE")"
OUT="${1:-$HERE/dist}"

rm -rf "$OUT"
mkdir -p "$OUT/demo" "$OUT/assets" "$OUT/portraits_64"

# Turn the demo on in the bundle itself. Without this the bare URL loads a
# dashboard with no server behind it and sits on ACQUIRING TELEMETRY —
# and a demo whose front door has to be typed with ?demo=1 is not one.
python3 - "$DASH/index.html" "$OUT/index.html" <<'PY'
import sys
src, dst = sys.argv[1], sys.argv[2]
s = open(src, encoding="utf-8").read()
anchor = '<script src="demo/demo_api.js"></script>'
assert s.count(anchor) == 1, "demo loader moved; build needs updating"
s = s.replace(anchor, "<script>window.AGENTSTACK_DEMO_FORCE=1</script>\n" + anchor, 1)
open(dst, "w", encoding="utf-8").write(s)
PY
cp "$HERE/demo_api.js" "$HERE/demo_tour.js" "$OUT/demo/"
cp "$DASH"/assets/*.svg "$OUT/assets/"
cp "$HERE/PORTRAITS.txt" "$OUT/PORTRAITS.txt"

# Surnames the cast actually uses. Read them out of the fixture so a rename
# cannot leave the bundle shipping a portrait nobody sees — or missing one.
# Collected into a variable rather than piped: a `while read` on the far side
# of a pipe runs in a subshell, so its exit could not fail this script — and
# it drops a final line with no newline, which is how Bohr went missing from
# the first build without a word.
SCI="$(node -e '
  const fs = require("fs"), path = require("path");
  const src = fs.readFileSync(path.join("'"$HERE"'", "demo_api.js"), "utf8");
  const names = [...src.matchAll(/name: .([A-Z][a-z]+)([A-Z][A-Za-z]+)./g)]
    .map((m) => m[2]);
  for (const n of new Set(names)) console.log(n);
')"
[ -n "$SCI" ] || { echo "no cast found in demo_api.js" >&2; exit 1; }

n=0
while IFS= read -r sci; do
  [ -n "$sci" ] || continue
  [ -f "$DASH/portraits_64/$sci.png" ] || {
    echo "missing portrait: $sci" >&2; exit 1; }
  cp "$DASH/portraits_64/$sci.png" "$OUT/portraits_64/"
  n=$((n + 1))
done <<< "$SCI"
echo "portraits: $n"

echo "built $OUT"
du -sh "$OUT"
find "$OUT" -type f | wc -l | xargs echo "files:"
