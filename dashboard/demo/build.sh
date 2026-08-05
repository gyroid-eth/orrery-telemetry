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

cp "$DASH/index.html" "$OUT/index.html"
cp "$HERE/demo_api.js" "$HERE/demo_tour.js" "$OUT/demo/"
cp "$DASH"/assets/*.svg "$OUT/assets/"

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

# The demo only runs with ?demo=1, and nobody types that. Land them on it.
cat > "$OUT/demo.html" <<'HTML'
<!doctype html><meta charset="utf-8">
<title>agentstack telemetry — demo</title>
<meta http-equiv="refresh" content="0; url=index.html?demo=1">
<link rel="canonical" href="index.html?demo=1">
<p>Loading the demo… <a href="index.html?demo=1">continue</a></p>
HTML

echo "built $OUT"
du -sh "$OUT"
find "$OUT" -type f | wc -l | xargs echo "files:"
