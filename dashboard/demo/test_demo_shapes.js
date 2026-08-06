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
/* edge_messages_payload / messages_since_payload row shapes. */
const REAL_EDGEMSG_KEYS = [
  'id', 'ts', 'ts_unix', 'sender', 'recipient', 'subject', 'body',
  'importance', 'thread_id', 'topic', 'ack_required', 'kind',
  'read_ts', 'ack_ts',
];
const REAL_SINCE_KEYS = [
  'id', 'ts', 'sender', 'recipient', 'subject', 'excerpt', 'importance',
  'kind', 'thread_id',
];
const REAL_SINCE_TOP_KEYS = ['ok', 'now', 'since', 'messages'];
const REAL_SPAWN_NAMES_KEYS = [
  'names', 'adjectives', 'naming', 'dirs', 'models', 'default_model',
  'providers',
];
const REAL_SPAWN_NAME_KEYS = ['name', 'portrait', 'status'];
const REAL_SPAWN_PROVIDER_KEYS = [
  'id', 'label', 'program', 'models', 'default_model', 'efforts',
];
const REAL_SPAWN_CODEX_PROVIDER_KEYS = REAL_SPAWN_PROVIDER_KEYS.concat([
  'effort_default',
]);
const REAL_SUGGEST_NAME_KEYS = ['name'];
const REAL_SPAWN_DIR_KEYS = ['path', 'dirs', 'truncated'];
const REAL_SPAWN_DIR_ROW_KEYS = ['name', 'path'];
const REAL_NAME_STATUS_KEYS = ['name', 'status'];

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

/* Load the demo the way a browser would: every story file first, then the
   fixture, with the story selected on the query string. The suite then runs
   once per registered story — a second story that answers half the endpoints
   is exactly the failure this is here to catch. */
function storyFiles() {
  return fs.readdirSync(__dirname)
    .filter((f) => /^story_.*\.js$/.test(f)).sort();
}

function loadDemo(storyId) {
  const win = {
    location: { search: '?demo=1&story=' + storyId },
    fetch: () => Promise.reject(new Error('no network in this test')),
    dispatchEvent: () => true,
    URLSearchParams,
    CustomEvent: function (type, init) { this.type = type; this.detail = init; },
    Response: class { constructor(body) { this.body = body; } },
  };
  win.window = win;
  vm.createContext(win);
  storyFiles().forEach((f) => {
    vm.runInContext(fs.readFileSync(path.join(__dirname, f), 'utf8'), win);
  });
  vm.runInContext(
    fs.readFileSync(path.join(__dirname, 'demo_api.js'), 'utf8'), win);
  if (!win.AGENTSTACK_DEMO) throw new Error('demo did not install itself');
  if (win.AGENTSTACK_DEMO.story().id !== storyId) {
    throw new Error('asked for story ' + storyId + ', got ' +
      win.AGENTSTACK_DEMO.story().id);
  }
  return win;
}

let SANDBOX, P, SANDBOX_DATE, REAL_NOW, DEMO, STORY_ID;

/* The fixture reads the clock from inside the sandbox, which has its own
   Date. Overriding Date.now out here would move a clock nobody is looking
   at — the first version of this test did exactly that and reported a story
   that never advanced. Drive the one the code actually reads.

   `t` means "this many seconds into the story", not "this long after the
   page opened" — a story deliberately starts part-way through, so the two
   differ. Ask the fixture where it is and shift by the gap. */
function atSecond(t, fn) {
  const base = REAL_NOW();
  const shift = (t - DEMO.phase() + DEMO.loop()) % DEMO.loop();
  SANDBOX_DATE.now = () => base + shift * 1000;
  try { return fn(); } finally { SANDBOX_DATE.now = REAL_NOW; }
}

function suite() {
check('[' + STORY_ID + '] agents rows carry exactly the fields the server sends', () => {
  const rows = P.agents().agents;
  if (!rows.length) throw new Error('no agents at t=0');
  rows.forEach((r) => sameKeys(r, REAL_AGENT_KEYS, 'agent ' + r.name));
});

check('[' + STORY_ID + '] graph has the server’s top-level shape', () => {
  sameKeys(P.graph(), REAL_GRAPH_KEYS, 'graph payload');
});

check('[' + STORY_ID + '] graph nodes carry exactly the fields the server sends', () => {
  P.graph().nodes.forEach((n) => sameKeys(n, REAL_NODE_KEYS, 'node ' + n.name));
});

check('[' + STORY_ID + '] edges and spawn rows match the server’s shape', () => {
  const g = P.graph();
  g.edges.forEach((e) => sameKeys(e, REAL_EDGE_KEYS, 'edge'));
  g.spawn.forEach((s) => sameKeys(s, REAL_SPAWN_KEYS, 'spawn'));
});

check('[' + STORY_ID + '] no edge or lineage points at an agent that is not on screen', () => {
  const g = P.graph();
  const present = new Set(g.nodes.map((n) => n.name));
  g.edges.concat(g.spawn).forEach((e) => {
    if (!present.has(e.source) || !present.has(e.target)) {
      throw new Error('dangling: ' + e.source + ' -> ' + e.target);
    }
  });
});

check('[' + STORY_ID + '] the story actually moves — cast and edges grow through the loop', () => {
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

/* What the product is about is a parent handing work to a child. If the
   entry point sits past every spawn, a visitor sees a busy screen and none
   of the thing that makes it busy — the first version opened 147 seconds
   after the last spawn, which is most of a loop spent waiting. */
check('[' + STORY_ID + '] a visitor sees a child spawned soon after they start watching', () => {
  const open = DEMO.opensAt();
  const at = (t) => atSecond(t, () => P.graph().nodes.length);
  const start = at(open);
  let sawGrowth = 0;
  for (let d = 1; d <= 20; d += 1) {
    if (at(open + d) > start) { sawGrowth = d; break; }
  }
  if (!sawGrowth) {
    throw new Error(`no agent appears in the 20s after landing at t=${open}`);
  }
});

check('[' + STORY_ID + '] the opening is not an empty screen', () => {
  atSecond(DEMO.opensAt(), () => {
    const g = P.graph();
    const live = g.nodes.filter((n) => n.running);
    if (live.length < 2) throw new Error('landed on ' + live.length + ' running');
    live.forEach((n) => {
      if (!n.task) throw new Error(n.name + ' has nothing to show for itself');
    });
  });
});

check('[' + STORY_ID + '] an agent that finishes stops running but stays on screen', () => {
  const before = atSecond(150,
    () => P.graph().nodes.find((n) => n.name === 'IvoryNoether'));
  const after = atSecond(200,
    () => P.graph().nodes.find((n) => n.name === 'IvoryNoether'));
  if (!before || !before.running) throw new Error('never ran');
  if (!after) throw new Error('vanished instead of finishing');
  if (after.running) throw new Error('still running past its end');
  if (after.state !== 'finished') throw new Error('state is ' + after.state);
});

check('[' + STORY_ID + '] every human-blocked window lasts 15s and covers its beat', () => {
  const beats = DEMO.beats();
  DEMO.cast().forEach((agent) => {
    (agent.states || []).forEach((window) => {
      if (!Array.isArray(window) || window.length !== 3)
        throw new Error(agent.name + ' has a malformed states window');
      const [from, to, state] = window;
      if (!['ask', 'question'].includes(state))
        throw new Error(agent.name + ' has unsupported state ' + state);
      if (to - from < 15)
        throw new Error(`${agent.name} ${state} lasts only ${to - from}s`);
      const selector = `.bay[data-name="${agent.name}"]`;
      const beat = beats.find((beat) =>
        beat.at >= from && beat.at < to && beat.look === selector);
      if (!beat)
        throw new Error(`${agent.name} ${state} has no matching beat in its window`);
      if (!beat.net || !beat.net.en || !beat.net.ja)
        throw new Error(`${agent.name} ${state} has no Network narration`);
      if (!beat.net.en.includes(agent.name) || !beat.net.ja.includes(agent.name))
        throw new Error(`${agent.name} ${state} Network narration omits its name`);
    });
  });
});

check('[' + STORY_ID + '] ask and question both surface while their agents are running', () => {
  const seen = new Set();
  for (let t = 0; t < DEMO.loop(); t += 1) {
    atSecond(t, () => {
      const agents = new Map(P.agents().agents.map((row) => [row.name, row]));
      P.graph().nodes.forEach((node) => {
        if (node.act_state !== 'ask' && node.act_state !== 'question') return;
        const agent = agents.get(node.name);
        if (!node.running || !agent || !agent.running) {
          throw new Error(`t=${t} ${node.name} is ${node.act_state} but not running`);
        }
        if (agent.act_state !== node.act_state) {
          throw new Error(`t=${t} ${node.name} disagrees across payloads`);
        }
        seen.add(node.act_state);
      });
    });
  }
  ['ask', 'question'].forEach((state) => {
    if (!seen.has(state)) throw new Error('loop never emits ' + state);
  });
});

check('[' + STORY_ID + '] messages-since never returns the future, and honours the cursor', () => {
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
check('[' + STORY_ID + '] every agent on screen has a transcript, shaped like the server’s', () => {
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
check('[' + STORY_ID + '] no moment in the loop shows a present agent with nothing said', () => {
  for (let t = 0; t < DEMO.loop(); t += 1) {
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
check('[' + STORY_ID + '] the build-time flag turns the demo on without ?demo=1', () => {
  const mk = (extra) => {
    const win = Object.assign({
      location: { search: '' },
      fetch: () => Promise.reject(new Error('no network in this test')),
      dispatchEvent: () => true, URLSearchParams,
      CustomEvent: function () {}, Response: class {},
    }, extra);
    win.window = win;
    vm.createContext(win);
    /* A real bundle always ships its stories alongside the fixture. */
    storyFiles().forEach((f) => vm.runInContext(
      fs.readFileSync(path.join(__dirname, f), 'utf8'), win));
    vm.runInContext(
      fs.readFileSync(path.join(__dirname, 'demo_api.js'), 'utf8'), win);
    return win;
  };
  if (!mk({ AGENTSTACK_DEMO_FORCE: 1 }).AGENTSTACK_DEMO)
    throw new Error('flag did not install the fixture');
  if (mk({}).AGENTSTACK_DEMO)
    throw new Error('installed itself on a page that asked for neither');
});

/* A bundle assembled without its story files should be an ordinary dashboard,
   not a page that throws on the way up. Removing the last built-in story made
   this reachable for the first time. */
check('[' + STORY_ID + '] no stories means no fixture, not a crash', () => {
  const win = {
    location: { search: '?demo=1' }, AGENTSTACK_DEMO_FORCE: 1,
    fetch: () => Promise.reject(new Error('untouched')),
    dispatchEvent: () => true, URLSearchParams,
    CustomEvent: function () {}, Response: class {},
  };
  win.window = win;
  const originalFetch = win.fetch;
  vm.createContext(win);
  vm.runInContext(
    fs.readFileSync(path.join(__dirname, 'demo_api.js'), 'utf8'), win);
  if (win.AGENTSTACK_DEMO) throw new Error('installed a fixture with no story');
  if (win.fetch !== originalFetch) throw new Error('replaced fetch anyway');
});

check('[' + STORY_ID + '] an unknown agent gets the server’s refusal, not a blank transcript', () => {
  const h = P.history(new URLSearchParams('session=NobodyHere'));
  if (h.ok) throw new Error('invented a transcript for an agent that is gone');
  if (!h.error) throw new Error('refused without saying why');
});

check('[' + STORY_ID + '] the transcript grows as the story does', () => {
  const q = new URLSearchParams('session=AmberKepler');
  const early = atSecond(20, () => P.history(q).events.length);
  const late = atSecond(210, () => P.history(q).events.length);
  if (late <= early) throw new Error(`frozen at ${early} → ${late}`);
});

check('[' + STORY_ID + '] deliverables match the server’s shape and the badge count', () => {
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

check('[' + STORY_ID + '] launch picker endpoints match server.py response shapes', () => {
  const catalog = P.spawnNames();
  sameKeys(catalog, REAL_SPAWN_NAMES_KEYS, 'spawn-names payload');
  if (!catalog.names.length || !catalog.adjectives.length || !catalog.dirs.length)
    throw new Error('launch picker is empty');
  catalog.names.forEach((row) =>
    sameKeys(row, REAL_SPAWN_NAME_KEYS, 'spawn scientist'));
  catalog.providers.forEach((provider) => {
    const keys = provider.id === 'codex'
      ? REAL_SPAWN_CODEX_PROVIDER_KEYS : REAL_SPAWN_PROVIDER_KEYS;
    sameKeys(provider, keys, 'spawn provider ' + provider.id);
  });

  const scientist = catalog.names[0].name;
  const suggested = P.suggestName(
    new URLSearchParams('scientist=' + encodeURIComponent(scientist)));
  sameKeys(suggested, REAL_SUGGEST_NAME_KEYS, 'suggest-name payload');
  if (!suggested.name.endsWith('-' + scientist))
    throw new Error('suggested name lost the selected scientist');

  const root = P.spawnDirectories(new URLSearchParams());
  sameKeys(root, REAL_SPAWN_DIR_KEYS, 'fs/dirs payload');
  if (!root.path || !root.dirs.length) throw new Error('directory picker is empty');
  root.dirs.forEach((row) =>
    sameKeys(row, REAL_SPAWN_DIR_ROW_KEYS, 'directory suggestion'));
  const nested = P.spawnDirectories(new URLSearchParams(
    'path=' + encodeURIComponent(root.dirs[0].path)));
  sameKeys(nested, REAL_SPAWN_DIR_KEYS, 'fs/dirs ?path= payload');
  if (!nested.dirs.length) throw new Error('?path= has no child directories');

  const status = P.nameStatus(new URLSearchParams(
    'name=' + encodeURIComponent(suggested.name)));
  sameKeys(status, REAL_NAME_STATUS_KEYS, 'name-status payload');
  if (status.status !== 'available')
    throw new Error('suggested name is ' + status.status);
});

check('[' + STORY_ID + '] every launch scientist has a bundled portrait', () => {
  const portraitDir = path.join(__dirname, '..', 'portraits_64');
  /* Same list the build ships, asked of the fixture rather than re-derived
     here — the regex version read demo_api.js only and disagreed with what
     actually ends up in the bundle. */
  const bundled = new Set(DEMO.bundleSurnames());
  P.spawnNames().names.forEach((row) => {
    if (!row.portrait) throw new Error(row.name + ' says it has no portrait');
    if (!fs.existsSync(path.join(portraitDir, row.name + '.png')))
      throw new Error(row.name + ' is missing from portraits_64');
    if (!bundled.has(row.name))
      throw new Error(row.name + ' would be omitted from the static bundle');
  });
});

/* A half-translated demo is worse than an English one: the reader cannot
   tell whether the English line is untranslated or deliberately verbatim.
   So every string that reaches a reader must have an entry, and switching
   the language must actually change what the payloads carry. */
/* The drawer is opened to read the exchange. A row with an empty body is
   a header with nothing under it, which reads as "this product does not
   keep message contents" rather than "this is a demo". */
check('[' + STORY_ID + '] the drawer has something to read, in both languages', () => {
  const q = new URLSearchParams('a=AmberKepler&b=SlateHooke');
  ['en', 'ja'].forEach((l) => {
    DEMO.setLang(l);
    const j = atSecond(230, () => P.edgeMessages(q));
    if (j.count < 2) throw new Error(l + ': ' + j.count + ' messages on the edge');
    j.messages.forEach((m) => {
      sameKeys(m, REAL_EDGEMSG_KEYS, 'edge message');
      /* Japanese says the same thing in fewer characters, so count what
         actually matters: more than a header, and more than one line. */
      const body = m.body || '';
      if (body.length < 50 || body.indexOf('\n') === -1)
        throw new Error(l + ': body is a one-liner (' + body.length + ' chars)');
      if (l === 'ja' && !/[\u3040-\u30ff\u4e00-\u9faf]/.test(m.body))
        throw new Error('ja body is still English');
    });
    if (j.messages[0].ts_unix < j.messages[j.messages.length - 1].ts_unix)
      throw new Error('oldest first; the server sends newest first');
  });
  DEMO.setLang('en');
});

check('[' + STORY_ID + '] every message was written in both languages', () => {
  DEMO.script().forEach((m) => {
    if (!m.body) throw new Error('at ' + m.at + ' has no body');
    if (!m.body_ja) throw new Error('at ' + m.at + ' has no Japanese body');
    if (!/[\u3040-\u30ff\u4e00-\u9faf]/.test(m.body_ja))
      throw new Error('at ' + m.at + ' left English in body_ja');
  });
});

/* The page advances its comet cursor from `now`. Returning `ts` instead
   left it pinned at zero and only the seen-key set stopped the replay. */
check('[' + STORY_ID + '] messages-since carries the cursor the page reads', () => {
  atSecond(120, () => {
    const j = P.messagesSince(0);
    sameKeys(j, REAL_SINCE_TOP_KEYS, 'messages-since payload');
    if (!Number(j.now)) throw new Error('no now for the page to advance to');
    j.messages.forEach((m) => {
      sameKeys(m, REAL_SINCE_KEYS, 'comet');
      if (!m.excerpt) throw new Error('comet with nothing to say');
    });
  });
});

/* Replay asks with `names=A,B,C`. The fixture read `name` only, so Replay
   opened on an empty timeline and had nothing to play — and an empty event
   list is a legal answer, so nothing anywhere said so. */
check('[' + STORY_ID + '] replay gets a timeline, not an empty one', () => {
  atSecond(210, () => {
    const q = new URLSearchParams(
      'names=AmberKepler,SlateHooke,IvoryNoether&include_pane_states=1');
    const j = P.agentHistory(q);
    if (!j.ok) throw new Error('refused');
    if (j.events.length < 6)
      throw new Error('only ' + j.events.length + ' events to replay');
    const kinds = new Set(j.events.map((e) => e.kind));
    ['mail_sent', 'spawn', 'retire'].forEach((k) => {
      if (!kinds.has(k)) throw new Error('no ' + k + ' in the playback');
    });
    /* One message must not appear twice when both ends are selected. */
    const sent = j.events.filter((e) => e.kind === 'mail_sent').map((e) => e.id);
    const recv = j.events.filter((e) => e.kind === 'mail_recv').map((e) => e.id);
    const both = sent.filter((i) => recv.includes(i));
    if (both.length) throw new Error('message ' + both[0] + ' played twice');

    if (!j.names || !j.agents) throw new Error('multi payload is missing names');
    if (!j.initial_state || !j.initial_state.alive_agents)
      throw new Error('no starting state for the graph to rewind to');
    if (!(j.range.start_ts < j.range.end_ts))
      throw new Error('the scrubber has no range to move along');
    j.events.forEach((e) => {
      if (e.ts < j.range.start_ts || e.ts > j.range.end_ts)
        throw new Error('an event falls outside the scrubber range');
    });
  });
});

check('[' + STORY_ID + '] the single-agent chart still answers name=', () => {
  atSecond(210, () => {
    const j = P.agentHistory(new URLSearchParams('name=SlateHooke'));
    if (!j.ok || !j.events.length) throw new Error('no history for one agent');
    if (j.name !== 'SlateHooke') throw new Error('did not name the agent');
    j.events.forEach((e) => {
      if (e.agent !== 'SlateHooke')
        throw new Error('someone else’s event: ' + e.agent);
    });
  });
});

check('[' + STORY_ID + '] every reader-facing string exists in both languages', () => {
  DEMO.setLang('ja');
  const missing = DEMO.translatable().filter((s) => DEMO.translate(s) === s);
  DEMO.setLang('en');
  if (missing.length) {
    throw new Error(missing.length + ' untranslated, first: ' +
      JSON.stringify(missing[0].slice(0, 60)));
  }
});

check('[' + STORY_ID + '] switching language changes what the payloads say', () => {
  const q = new URLSearchParams('session=SlateHooke');
  /* Pinned to a moment when this agent exists. Reading "now" passed only
     because the demo used to open late enough that everyone was born. */
  const sample = () => atSecond(150, () => ({
    task: P.agents().agents.find((r) => r.name === 'SlateHooke').task,
    subject: P.messagesSince(0).messages.map((m) => m.subject).join('|'),
    said: P.history(q).events.filter((e) => e.kind === 'text')
            .map((e) => e.text).join('|'),
    cmd: P.history(q).events.filter((e) => e.kind === 'tool_use')
           .map((e) => e.text).join('|'),
  }));
  DEMO.setLang('en');
  const en = sample();
  DEMO.setLang('ja');
  const ja = sample();
  DEMO.setLang('en');
  ['task', 'subject', 'said'].forEach((k) => {
    if (en[k] === ja[k]) throw new Error(k + ' did not change with the language');
    if (!/[\u3040-\u30ff\u4e00-\u9faf]/.test(ja[k]))
      throw new Error(k + ' has no Japanese in it: ' + ja[k].slice(0, 60));
  });
  /* Commands are deliberately not translated — a terminal prints English. */
  if (en.cmd !== ja.cmd) throw new Error('tool calls were translated');
});

check('[' + STORY_ID + '] relative times match the format the server writes', () => {
  /* server.py _rel: "21s 前" / "3m 前" / "2h 前" / "1d 前". */
  P.graph().nodes.forEach((n) => {
    if (!/^(—|\d+[smhd] 前)$/.test(n.rel))
      throw new Error(n.name + ' rel is ' + JSON.stringify(n.rel));
  });
});

check('[' + STORY_ID + '] nothing in the fixture came from a real mailbox', () => {
  /* Names of things that exist on the author’s machine. If one of these
     ever appears here, someone exported instead of writing. */
  ['demo_api.js', 'demo_tour.js'].concat(storyFiles()).forEach((f) => {
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
   nothing is worse than no ring — it says the story has drifted from the
   page. Renaming an agent is the way that happens. */
check('[' + STORY_ID + '] the narration points at agents that exist', () => {
  const beats = DEMO.beats();
  if (!beats || !beats.length) throw new Error('no beats');
  const cast = new Set(DEMO.cast().map((c) => c.name));
  let prev = -1;
  beats.forEach((b) => {
    if (!b.en) throw new Error('beat at ' + b.at + ' says nothing');
    if (!b.ja) throw new Error('beat at ' + b.at + ' has no Japanese');
    if (!/[\u3040-\u30ff\u4e00-\u9faf]/.test(b.ja))
      throw new Error('beat at ' + b.at + ' left English in the ja slot');
    if (b.at <= prev) throw new Error('beats out of order at ' + b.at);
    if (b.at < 0 || b.at >= DEMO.loop())
      throw new Error('beat at ' + b.at + ' falls outside the loop');
    prev = b.at;
    const m = /\.bay\[data-name="([^"]+)"\]/.exec(b.look || '');
    if (m) {
      if (!cast.has(m[1]))
        throw new Error('rings an agent that is not in the cast: ' + m[1]);
      if (!b.en.includes(m[1]) || !b.ja.includes(m[1]))
        throw new Error('beat at ' + b.at + ' does not name ' + m[1] +
          ' in both languages');
      if (b.net && (!b.net.en.includes(m[1]) || !b.net.ja.includes(m[1])))
        throw new Error('Network beat at ' + b.at + ' does not name ' + m[1] +
          ' in both languages');
    }
  });
});

}

/* Every registered story, not just the default one. */
const IDS = storyFiles().map((f) => f.replace(/^story_|\.js$/g, ''));
if (!IDS.length) throw new Error('no stories to test');

for (const id of IDS) {
  STORY_ID = id;
  SANDBOX = loadDemo(id);
  P = SANDBOX.AGENTSTACK_DEMO.payloads;
  DEMO = SANDBOX.AGENTSTACK_DEMO;
  SANDBOX_DATE = vm.runInContext('Date', SANDBOX);
  REAL_NOW = SANDBOX_DATE.now;
  suite();
}

/* The opening card belongs to the tour, not to a story. */
check('the opening card is written in both languages', () => {
  const win = {
    location: { search: '?demo=1' }, URLSearchParams,
    document: { readyState: 'loading', addEventListener: () => {} },
    addEventListener: () => {},
  };
  win.window = win;
  vm.createContext(win);
  vm.runInContext(
    fs.readFileSync(path.join(__dirname, 'demo_tour.js'), 'utf8'), win);
  const card = win.AGENTSTACK_TOUR && win.AGENTSTACK_TOUR.card;
  if (!card || !card.en || !card.ja) throw new Error('the card is one-sided');
});

/* The card counts the cast in front of the reader. It used to say "nine
   agents" from a constant, which was true of one story and wrong about the
   one the demo now opens on — a claim the visitor can disprove by counting. */
check('the opening card counts the story it is opening', () => {
  for (const id of IDS) {
    const win = loadDemo(id);
    win.document = { readyState: 'loading', addEventListener: () => {} };
    win.addEventListener = () => {};
    vm.runInContext(
      fs.readFileSync(path.join(__dirname, 'demo_tour.js'), 'utf8'), win);
    const html = win.AGENTSTACK_TOUR.cardHTML;
    if (!html) throw new Error('the card does not render');
    const cast = win.AGENTSTACK_DEMO.cast().length;
    const min = Math.round(win.AGENTSTACK_DEMO.loop() / 60);
    for (const l of ['en', 'ja']) {
      const s = html(l);
      if (/\{n\}|\{min\}/.test(s))
        throw new Error('[' + id + '/' + l + '] card left a placeholder in');
      if (!s.includes(String(cast)))
        throw new Error('[' + id + '/' + l + '] card does not say ' + cast +
          ' agents');
      if (!s.includes(String(min)))
        throw new Error('[' + id + '/' + l + '] card does not say ' + min +
          ' minutes');
    }
  }
});

console.log('');
console.log(failures ? failures + ' FAILED' : 'all passed');
process.exit(failures ? 1 : 0);
