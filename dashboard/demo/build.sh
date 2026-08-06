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

cp "$HERE/demo_api.js" "$HERE/demo_tour.js" "$OUT/demo/"

# Story files, if any. Each registers itself on window and must load
# before demo_api.js, which picks among whatever it finds there.
STORIES=""
for f in "$HERE"/story_*.js; do
  [ -e "$f" ] || continue
  cp "$f" "$OUT/demo/"
  STORIES="${STORIES}$(basename "$f")
"
done
cp "$DASH"/assets/*.svg "$OUT/assets/"
cp "$HERE/PORTRAITS.txt" "$OUT/PORTRAITS.txt"

# Turn the demo on in the bundle itself. Without this the bare URL loads a
# dashboard with no server behind it and sits on ACQUIRING TELEMETRY —
# and a demo whose front door has to be typed with ?demo=1 is not one.
python3 - "$DASH/index.html" "$OUT/index.html" "$STORIES" <<'PY'
import sys
src, dst = sys.argv[1], sys.argv[2]
stories = [l.strip() for l in (sys.argv[3] if len(sys.argv) > 3 else "").split("\n") if l.strip()]
s = open(src, encoding="utf-8").read()
anchor = '<script src="demo/demo_api.js"></script>'
assert s.count(anchor) == 1, "demo loader moved; build needs updating"
tags = "".join('<script src="demo/%s"></script>\n' % f for f in stories)
s = s.replace(anchor, "<script>window.AGENTSTACK_DEMO_FORCE=1</script>\n" + tags + anchor, 1)
open(dst, "w", encoding="utf-8").write(s)
PY

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

# The injection above is the difference between a demo and a dead dashboard,
# and a shell variable that went missing would not have stopped the copy.
grep -q 'AGENTSTACK_DEMO_FORCE' "$OUT/index.html" \
  || { echo "index.html did not get the demo flag" >&2; exit 1; }
# Counted without a pipeline that can fail: under `set -o pipefail` an `ls`
# with no match takes the whole build down, which is how the zero-story case
# died here while still printing most of its output.
want=0
for f in "$OUT"/demo/story_*.js; do [ -e "$f" ] && want=$((want + 1)); done
got=$(grep -c 'script src="demo/story_' "$OUT/index.html" || true)
[ "$want" = "$got" ] \
  || { echo "$want story files copied but $got loaded by index.html" >&2; exit 1; }
echo "stories: $want"

echo "built $OUT"
du -sh "$OUT"
find "$OUT" -type f | wc -l | xargs echo "files:"
