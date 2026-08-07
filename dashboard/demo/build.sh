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
cp "$DASH/theme_core.js" "$DASH/theme_controller.js" "$DASH/theme_light.css" "$OUT/"

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
import os, sys
src, dst = sys.argv[1], sys.argv[2]
stories = [l.strip() for l in (sys.argv[3] if len(sys.argv) > 3 else "").split("\n") if l.strip()]
s = open(src, encoding="utf-8").read()
anchor = '<script src="demo/demo_api.js"></script>'
assert s.count(anchor) == 1, "demo loader moved; build needs updating"
tags = "".join('<script src="demo/%s"></script>\n' % f for f in stories)
# Fingerprint every demo script. A CDN can serve a stale copy of an
# unversioned file for a while after a deploy — measured at roughly 1 request
# in 12 — which pairs new HTML with old JS and breaks the page in a way that
# looks like our bug. A content hash makes that pairing impossible: the old
# cache entry is a different URL and is simply never asked for.
import hashlib, re as _re
def stamp(rel):
    data = open(os.path.join(os.path.dirname(dst), rel), "rb").read()
    return rel + "?v=" + hashlib.sha256(data).hexdigest()[:12]
tags = "".join('<script src="%s"></script>\n' % stamp("demo/" + f) for f in stories)
anchor_stamped = '<script src="%s"></script>' % stamp("demo/demo_api.js")
s = s.replace(anchor, "<script>window.AGENTSTACK_DEMO_FORCE=1</script>\n"
              + tags + anchor_stamped, 1)
s = s.replace('<script src="demo/demo_tour.js" defer></script>',
              '<script src="%s" defer></script>' % stamp("demo/demo_tour.js"), 1)
open(dst, "w", encoding="utf-8").write(s)
PY

# Surnames the cast actually uses. Read them out of the fixture so a rename
# cannot leave the bundle shipping a portrait nobody sees — or missing one.
# Collected into a variable rather than piped: a `while read` on the far side
# of a pipe runs in a subshell, so its exit could not fail this script — and
# it drops a final line with no newline, which is how Bohr went missing from
# the first build without a word.
SCI="$(node -e '
  const fs = require("fs"), path = require("path"), vm = require("vm");
  const dir = "'"$HERE"'";
  /* Ask the fixture rather than re-deriving its cast with a regex out here.
     The regex version disagreed with the page twice: it read demo_api.js
     only, so a story file\u0027s cast shipped no portraits, and it never saw
     the launch form\u0027s own list of scientists. */
  const win = { location: { search: "?demo=1" }, URLSearchParams,
                fetch: () => {}, dispatchEvent: () => true,
                CustomEvent: function () {}, Response: class {} };
  win.window = win; vm.createContext(win);
  for (const f of fs.readdirSync(dir).filter((f) => /^story_.*[.]js$/.test(f)))
    vm.runInContext(fs.readFileSync(path.join(dir, f), "utf8"), win);
  vm.runInContext(fs.readFileSync(path.join(dir, "demo_api.js"), "utf8"), win);
  const d = win.AGENTSTACK_DEMO;
  if (!d || !d.bundleSurnames) throw new Error("fixture did not load");
  for (const n of d.bundleSurnames()) console.log(n);
')"
[ -n "$SCI" ] || { echo "no cast found in demo_api.js" >&2; exit 1; }

n=0
while IFS= read -r sci; do
  [ -n "$sci" ] || continue
  [ -f "$DASH/portraits_64/$sci.png" ] || {
    echo "missing portrait: $sci" >&2; exit 1; }
  # The licence check used to live in someone's head, and that is where it
  # failed: a story added an agent whose Commons portrait is CC BY-SA, the
  # build shipped it, and only a screenshot showed anything was wrong.
  grep -qx "$sci" "$HERE/PORTRAITS_CLEARED.txt" || {
    echo "portrait not cleared for publication: $sci" >&2
    echo "  verify its Commons licence, then add it to PORTRAITS_CLEARED.txt" >&2
    exit 1; }
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
# Every demo script must be fingerprinted, or a stale CDN copy can pair with
# fresh HTML. Four tags: the two engine files and however many stories.
stamped=$(grep -c 'script src="demo/[^"]*?v=' "$OUT/index.html" || true)
[ "$stamped" = "$((want + 2))" ] \
  || { echo "$stamped of $((want + 2)) demo scripts carry a version" >&2; exit 1; }
[ "$want" = "$got" ] \
  || { echo "$want story files copied but $got loaded by index.html" >&2; exit 1; }
echo "stories: $want"

echo "built $OUT"
du -sh "$OUT"
find "$OUT" -type f | wc -l | xargs echo "files:"
