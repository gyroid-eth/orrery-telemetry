/* The demo is only useful if the page cannot tell it from the server.
 *
 * A missing field does not throw here — it renders as an empty badge, a
 * node with no portrait, an arc that never appears. That is the failure
 * mode worth guarding: the page keeps working and quietly shows less.
 *
 * So this asserts the key sets against what the real dashboard returned,
 * captured from a running instance on 2026-08-05. If server.py grows a
 * field, this goes red and the demo gets it too.
 *
 *   node demo/test_demo_shapes.js
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

/* Captured from GET /api/agents and /api/graph on a live dashboard. */
const REAL_AGENT_KEYS = [
  'act_state', 'attached', 'category', 'cmd', 'created', 'ctx_used',
  'ctx_window', 'deliv', 'instruction', 'last_active', 'last_active_rel',
  'last_disp', 'live', 'model', 'model_raw', 'name', 'provider', 'running',
  'task', 'work_disp',
];
const REAL_NODE_KEYS = [
  'act', 'act_state', 'annot', 'attached', 'ctx_used', 'ctx_window', 'deliv',
  'last_active', 'last_disp', 'live', 'model', 'name', 'pane_model',
  'present', 'program', 'provider', 'rel', 'retired', 'running', 'sig',
  'state', 'task', 'work_disp', 'work_secs',
];
const REAL_EDGE_KEYS = ['count', 'kind', 'last_ts', 'source', 'target'];
const REAL_SPAWN_KEYS = ['source', 'target', 'type'];
const REAL_GRAPH_KEYS = ['edges', 'nodes', 'shown', 'spawn', 'total', 'ts'];
const REAL_HISTORY_KEYS = [
  'ok', 'session', 'file', 'source', 'total', 'shown', 'events',
];
const REAL_EVENT_KEYS = ['role', 'kind', 'text', 'ts'];
const REAL_DELIV_KEYS = ['ok', 'agent', 'vault', 'items'];
const REAL_DELIV_ITEM_KEYS = ['title', 'rel', 'vault', 'mtime'];

let failures = 0;
function check(name, fn) {
  try {
    fn();
    console.log('ok   ' + name);
  } catch (err) {
    failures += 1;
    console.log('FAIL ' + name + ': ' + err.message);
  }
}
function sameKeys(actual, expected, what) {
  const got = Object.keys(actual).sort();
  const want = expected.slice().sort();
  const missing = want.filter((k) => !got.includes(k));
  const extra = got.filter((k) => !want.includes(k));
  if (missing.length || extra.length) {
    throw new Error(
      what + ' — missing: [' + missing + '] unexpected: [' + extra + ']');
  }
}

/* Load the demo the way a browser would, with ?demo=1 present. */
function loadDemo() {
  const src = fs.readFileSync(
    path.join(__dirname, 'demo_api.js'), 'utf8');
  const win = {
    location: { search: '?demo=1' },
    fetch: () => Promise.reject(new Error('no network in this test')),
    dispatchEvent: () => true,
    URLSearchParams,
    CustomEvent: function (type, init) { this.type = type; this.detail = init; },
    Response: class { constructor(body) { this.body = body; } },
  };
  win.window = win;
  vm.createContext(win);
  vm.runInContext(src, win);
  if (!win.AGENTSTACK_DEMO) throw new Error('demo did not install itself');
  return win;
}

const SANDBOX = loadDemo();
const P = SANDBOX.AGENTSTACK_DEMO.payloads;

/* The fixture reads the clock from inside the sandbox, which has its own
   Date. Overriding Date.now out here would move a clock nobody is looking
   at — the first version of this test did exactly that and reported a story
   that never advanced. Drive the one the code actually reads. */
const SANDBOX_DATE = vm.runInContext('Date', SANDBOX);
const REAL_NOW = SANDBOX_DATE.now;
const DEMO = SANDBOX.AGENTSTACK_DEMO;

/* `t` here means "this many seconds into the story", not "this long after
   the page opened" — the demo deliberately starts part-way through, so the
   two differ. Ask the fixture where it currently is and shift by the gap,
   rather than assuming the story begins when the clock does. */
function atSecond(t, fn) {
  const base = REAL_NOW();
  const shift = (t - DEMO.phase() + DEMO.loop) % DEMO.loop;
  SANDBOX_DATE.now = () => base + shift * 1000;
  try { return fn(); } finally { SANDBOX_DATE.now = REAL_NOW; }
}

check('agents rows carry exactly the fields the server sends', () => {
  const rows = P.agents().agents;
  if (!rows.length) throw new Error('no agents at t=0');
  rows.forEach((r) => sameKeys(r, REAL_AGENT_KEYS, 'agent ' + r.name));
});

check('graph has the server’s top-level shape', () => {
  sameKeys(P.graph(), REAL_GRAPH_KEYS, 'graph payload');
});

check('graph nodes carry exactly the fields the server sends', () => {
  P.graph().nodes.forEach((n) => sameKeys(n, REAL_NODE_KEYS, 'node ' + n.name));
});

check('edges and spawn rows match the server’s shape', () => {
  const g = P.graph();
  g.edges.forEach((e) => sameKeys(e, REAL_EDGE_KEYS, 'edge'));
  g.spawn.forEach((s) => sameKeys(s, REAL_SPAWN_KEYS, 'spawn'));
});

check('no edge or lineage points at an agent that is not on screen', () => {
  const g = P.graph();
  const present = new Set(g.nodes.map((n) => n.name));
  g.edges.concat(g.spawn).forEach((e) => {
    if (!present.has(e.source) || !present.has(e.target)) {
      throw new Error('dangling: ' + e.source + ' -> ' + e.target);
    }
  });
});

check('the story actually moves — cast and edges grow through the loop', () => {
  /* Sampling the same builders the page calls, at points across the loop.
     The builders read the wall clock, so drive them by faking it. */
  const seen = [1, 50, 100, 150, 200, 235].map((t) => atSecond(t, () => {
    const g = P.graph();
    return { t, nodes: g.nodes.length, edges: g.edges.length };
  }));
  const first = seen[0], last = seen[seen.length - 1];
  if (last.nodes <= first.nodes) {
    throw new Error('cast never grows: ' + JSON.stringify(seen));
  }
  if (last.edges <= first.edges) {
    throw new Error('nothing is ever said: ' + JSON.stringify(seen));
  }
});

check('an agent that finishes stops running but stays on screen', () => {
  const before = atSecond(150,
    () => P.graph().nodes.find((n) => n.name === 'IvoryNoether'));
  const after = atSecond(200,
    () => P.graph().nodes.find((n) => n.name === 'IvoryNoether'));
  if (!before || !before.running) throw new Error('never ran');
  if (!after) throw new Error('vanished instead of finishing');
  if (after.running) throw new Error('still running past its end');
  if (after.state !== 'finished') throw new Error('state is ' + after.state);
});

check('messages-since never returns the future, and honours the cursor', () => {
  atSecond(120, () => {
    const all = P.messagesSince(0).messages;
    if (!all.length) throw new Error('nothing delivered by t=120');
    const newest = Math.max.apply(null, all.map((m) => m.ts));
    const fresh = P.messagesSince(newest).messages;
    if (fresh.length) throw new Error('returned mail at or before the cursor');
    const later = P.messagesSince(newest - 60).messages;
    if (!later.length) throw new Error('cursor 60s back returned nothing');
  });
});

/* The pane is where a visitor finds out what an agent does. An empty one
   renders as "comm failed" or a blank column — the page survives, and the
   product looks like it has no transcript feature at all. */
check('every agent on screen has a transcript, shaped like the server’s', () => {
  const q = (n) => new URLSearchParams('session=' + encodeURIComponent(n));
  P.graph().nodes.forEach((n) => {
    const h = P.history(q(n.name));
    if (!h.ok) throw new Error(n.name + ': ' + h.error);
    sameKeys(h, REAL_HISTORY_KEYS, 'history ' + n.name);
    if (!h.events.length) throw new Error(n.name + ' has an empty transcript');
    h.events.forEach((e) => {
      sameKeys(e, REAL_EVENT_KEYS, 'event in ' + n.name);
      if (Number.isNaN(Date.parse(e.ts)))
        throw new Error(n.name + ': unparseable ts ' + e.ts);
      if (!['text', 'thinking', 'tool_use', 'tool_result'].includes(e.kind))
        throw new Error(n.name + ': unknown kind ' + e.kind);
    });
    if (h.shown !== h.events.length || h.total < h.shown)
      throw new Error(n.name + ': counts disagree with the events');
  });
});

/* Checking only the current moment would pass for most of the loop and fail
   for whoever opens it during the two seconds after an agent spawns. Sweep. */
check('no moment in the loop shows a present agent with nothing said', () => {
  for (let t = 0; t < DEMO.loop; t += 1) {
    atSecond(t, () => {
      P.graph().nodes.forEach((n) => {
        const h = P.history(
          new URLSearchParams('session=' + encodeURIComponent(n.name)));
        if (!h.ok || !h.events.length)
          throw new Error(`t=${t} ${n.name}: empty pane`);
      });
    });
  }
});

/* The static bundle has no server to fall back to, so it turns the demo on
   itself rather than relying on a query string nobody types. If that flag
   stops working the bare URL renders a dashboard waiting forever on /api. */
check('the build-time flag turns the demo on without ?demo=1', () => {
  const src = fs.readFileSync(path.join(__dirname, 'demo_api.js'), 'utf8');
  const win = {
    location: { search: '' }, AGENTSTACK_DEMO_FORCE: 1,
    fetch: () => Promise.reject(new Error('no network in this test')),
    dispatchEvent: () => true, URLSearchParams,
    CustomEvent: function () {}, Response: class {},
  };
  win.window = win;
  vm.createContext(win);
  vm.runInContext(src, win);
  if (!win.AGENTSTACK_DEMO) throw new Error('flag did not install the fixture');

  const off = { location: { search: '' }, URLSearchParams,
                fetch: () => {}, dispatchEvent: () => true,
                CustomEvent: function () {}, Response: class {} };
  off.window = off;
  vm.createContext(off);
  vm.runInContext(src, off);
  if (off.AGENTSTACK_DEMO)
    throw new Error('installed itself on a page that asked for neither');
});

check('an unknown agent gets the server’s refusal, not a blank transcript', () => {
  const h = P.history(new URLSearchParams('session=NobodyHere'));
  if (h.ok) throw new Error('invented a transcript for an agent that is gone');
  if (!h.error) throw new Error('refused without saying why');
});

check('the transcript grows as the story does', () => {
  const q = new URLSearchParams('session=AmberKepler');
  const early = atSecond(20, () => P.history(q).events.length);
  const late = atSecond(210, () => P.history(q).events.length);
  if (late <= early) throw new Error(`frozen at ${early} → ${late}`);
});

check('deliverables match the server’s shape and the badge count', () => {
  P.graph().nodes.forEach((n) => {
    const d = P.deliverables(
      new URLSearchParams('agent=' + encodeURIComponent(n.name)));
    sameKeys(d, REAL_DELIV_KEYS, 'deliverables ' + n.name);
    d.items.forEach((it) => sameKeys(it, REAL_DELIV_ITEM_KEYS, 'item'));
    if (d.items.length !== n.deliv)
      throw new Error(`${n.name}: badge says ${n.deliv}, list has ` +
        d.items.length);
  });
});

check('nothing in the fixture came from a real mailbox', () => {
  /* Names of things that exist on the author’s machine. If one of these
     ever appears here, someone exported instead of writing. */
  ['demo_api.js', 'demo_tour.js'].forEach((f) => {
    const src = fs.readFileSync(path.join(__dirname, f), 'utf8');
    ['Syncthing', '<vault-directory>', 'mcp_agent_mail', '/Users/',
     'ProOpus', 'PluckyEinstein', 'biomatterlab',
    ].forEach((needle) => {
      if (src.includes(needle))
        throw new Error(f + ' leaked reference: ' + needle);
    });
  });
});

/* The narration rings whatever it is talking about. A ring that points at
   nothing is worse than no ring — it says the demo has drifted from the
   page. Renaming an agent is the way that happens. */
check('the narration points at agents that exist', () => {
  const src = fs.readFileSync(path.join(__dirname, 'demo_tour.js'), 'utf8');
  const win = {
    location: { search: '?demo=1' },
    URLSearchParams,
    document: { readyState: 'loading', addEventListener: () => {} },
    addEventListener: () => {},
  };
  win.window = win;
  vm.createContext(win);
  vm.runInContext(src, win);      // build() waits for DOMContentLoaded here
  const beats = win.AGENTSTACK_TOUR && win.AGENTSTACK_TOUR.beats;
  if (!beats || !beats.length) throw new Error('no beats');

  const cast = new Set(DEMO.cast.map((c) => c.name));
  let prev = -1;
  beats.forEach((b) => {
    if (!b.text) throw new Error('beat at ' + b.at + ' says nothing');
    if (b.at <= prev) throw new Error('beats out of order at ' + b.at);
    if (b.at < 0 || b.at >= DEMO.loop)
      throw new Error('beat at ' + b.at + ' falls outside the loop');
    prev = b.at;
    const m = /\.bay\[data-name="([^"]+)"\]/.exec(b.look || '');
    if (m && !cast.has(m[1]))
      throw new Error('rings an agent that is not in the cast: ' + m[1]);
  });
});

console.log('');
console.log(failures ? failures + ' FAILED' : 'all passed');
process.exit(failures ? 1 : 0);
