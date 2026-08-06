/* Demo mode — the dashboard with no machine behind it.
 *
 * Everything that moves on this page comes from polling seven GET endpoints.
 * Answer those from a script instead of a server and the whole thing runs:
 * agents appear, context arcs fill, mail crosses the graph, a child finishes.
 * No Python, no SQLite, no tmux, nothing to keep alive.
 *
 * Two rules this file exists to keep:
 *
 *   It lives inside the real page, not beside it. A copied dashboard would
 *   be wrong the first time somebody changed the real one, and nobody would
 *   notice until a visitor saw the old version.
 *
 *   The data is written, not exported. Anonymising a real mailbox leaks —
 *   paths, repository names, whatever a message body happened to contain.
 *   Nothing here was ever real, so there is nothing to leak.
 *
 * Loads on every page but returns immediately unless ?demo=1 is present.
 */
(function () {
  'use strict';
  /* The query switch is for trying demo mode against a real dashboard. The
     flag is for the static bundle, where there is no server to fall back to
     and a bare URL must not render a dashboard waiting forever on /api. */
  var params = new URLSearchParams(location.search);
  if (params.get('demo') !== '1' && !window.AGENTSTACK_DEMO_FORCE) return;

  /* ── language ────────────────────────────────────────────────────────
     The fixture is written in English and translated through one table
     keyed by the English string, so a line cannot exist in one language
     only — the tests walk every translatable string and fail on a miss.
     Commands and program output stay in English on purpose: that is what
     a Japanese developer's terminal actually prints. */
  var LANG = (function () {
    var q = params.get('lang');
    if (q === 'ja' || q === 'en') return q;
    var nav = typeof navigator === 'undefined' ? null : navigator;
    var tags = nav ? [nav.language].concat(nav.languages || []) : [];
    return tags.some(function (l) {
      return typeof l === 'string' && /^ja(?:-|$)/i.test(l);
    }) ? 'ja' : 'en';
  })();

  function setLang(l) { LANG = l === 'ja' ? 'ja' : 'en'; return LANG; }
  function lang() { return LANG; }
  function tx(s) { return LANG === 'ja' && JA[s] ? JA[s] : s; }

  var LOOP = 240;          // seconds; the story repeats from the top
  /* Enter the story already in progress. Starting at zero means a visitor
     spends the first quarter-minute looking at two idle agents and an empty
     history, and decides the product does not do much. At this offset the
     first thing on screen is four agents, live traffic and a filled chart. */
  var OPENS_AT = 108;
  var START = Date.now() - OPENS_AT * 1000;

  /* ── the cast ────────────────────────────────────────────────────────
     `born` is when the agent first appears, `dies` when it stops running
     (it stays on screen as a finished husk, which is what a real one does).
     Times are seconds into the loop. */
  var CAST = [
    { name: 'AmberKepler', role: 'orchestrator', emoji: '🎯', group: 'demo',
      model: 'Opus 5', model_raw: 'claude-opus-5', provider: 'anthropic',
      program: 'claude-code', born: 0, dies: null, ctx0: 38, ctxRate: 0.045,
      task: 'Plan the migration and hand each piece to a child' },
    { name: 'SlateHooke', parent: 'AmberKepler', role: 'schema', emoji: '🧩',
      model: 'GPT 5.6', model_raw: 'gpt-5.6', provider: 'openai',
      program: 'codex', born: 15, dies: null, ctx0: 12, ctxRate: 0.12,
      task: 'Read the old schema and write the field-by-field mapping' },
    { name: 'IvoryNoether', parent: 'AmberKepler', role: 'tests', emoji: '🧪',
      model: 'GPT 5.6', model_raw: 'gpt-5.6', provider: 'openai',
      program: 'codex', born: 40, dies: 182, ctx0: 9, ctxRate: 0.2,
      task: 'Cover the mapping with tests before anything is moved' },
    { name: 'RustPasteur', parent: 'AmberKepler', role: 'docs', emoji: '📄',
      model: 'Sonnet 5', model_raw: 'claude-sonnet-5', provider: 'anthropic',
      program: 'claude-code', born: 95, dies: null, ctx0: 7, ctxRate: 0.09,
      task: 'Write the upgrade note the way an operator would read it' },
    { name: 'MossSomerville', role: 'orchestrator', emoji: '🎯', group: 'demo',
      model: 'Opus 5', model_raw: 'claude-opus-5', provider: 'anthropic',
      program: 'claude-code', born: 0, dies: null, ctx0: 55, ctxRate: 0.03,
      task: 'Watch the release rail and hold the gate' },
    { name: 'FlintGauss', parent: 'MossSomerville', role: 'release', emoji: '🚦',
      model: 'GPT 5.6', model_raw: 'gpt-5.6', provider: 'openai',
      program: 'codex', born: 60, dies: null, ctx0: 14, ctxRate: 0.1,
      task: 'Run the release checks and report what fails' },
  ];

  /* Agents that finished before the loop starts. They give the graph a past,
     which is most of what the view is for. */
  var PAST = [
    { name: 'CedarLovelace', parent: 'AmberKepler', model: 'GPT 5.6',
      model_raw: 'gpt-5.6', provider: 'openai', program: 'codex',
      task: 'Survey the call sites', retired: true, ago: 5400 },
    { name: 'OchreCurie', parent: 'AmberKepler', model: 'Sonnet 5',
      model_raw: 'claude-sonnet-5', provider: 'anthropic',
      program: 'claude-code', task: 'Draft the rollback plan',
      retired: true, ago: 9200 },
    { name: 'UmberBohr', parent: 'MossSomerville', model: 'GPT 5.6',
      model_raw: 'gpt-5.6', provider: 'openai', program: 'codex',
      task: 'Reproduce the reported failure', retired: true, ago: 14400 },
  ];

  /* ── what gets said ──────────────────────────────────────────────────
     One entry per message. `at` is seconds into the loop. Subjects are the
     only text a visitor reads, so they carry the explanation. */
  var SCRIPT = [
    { at: 16, from: 'AmberKepler', to: 'SlateHooke',
      subject: 'Task: map the old schema field by field' },
    { at: 34, from: 'SlateHooke', to: 'AmberKepler',
      subject: 'RE: three fields have no counterpart — listing them' },
    { at: 41, from: 'AmberKepler', to: 'IvoryNoether',
      subject: 'Task: cover the mapping before anything moves' },
    { at: 62, from: 'MossSomerville', to: 'FlintGauss',
      subject: 'Task: run the release checks on the current branch' },
    { at: 78, from: 'IvoryNoether', to: 'SlateHooke',
      subject: 'Which of the three do you want asserted first?' },
    { at: 88, from: 'SlateHooke', to: 'IvoryNoether',
      subject: 'RE: the one with two writers — that is where it breaks' },
    { at: 96, from: 'AmberKepler', to: 'RustPasteur',
      subject: 'Task: write the upgrade note for operators' },
    { at: 118, from: 'FlintGauss', to: 'MossSomerville',
      subject: 'RE: one check fails — it is the gate, not a flake' },
    { at: 129, from: 'MossSomerville', to: 'AmberKepler',
      subject: 'Holding the release until the mapping lands' },
    { at: 146, from: 'IvoryNoether', to: 'AmberKepler',
      subject: 'RE: tests are green, including the two-writer case' },
    { at: 168, from: 'RustPasteur', to: 'AmberKepler',
      subject: 'RE: draft is up — one open question about defaults' },
    { at: 181, from: 'IvoryNoether', to: 'AmberKepler',
      subject: 'Done. Handing back.' },
    { at: 203, from: 'AmberKepler', to: 'MossSomerville',
      subject: 'Mapping is covered — the gate can open' },
    { at: 221, from: 'SlateHooke', to: 'AmberKepler',
      subject: 'RE: mapping is complete, nothing unresolved' },
  ];

  function now() { return (Date.now() - START) / 1000; }
  function phase() { return now() % LOOP; }
  function epoch() { return Math.floor(Date.now() / 1000); }

  function alive(a, t) {
    if (t < a.born) return false;
    return a.dies === null || t < a.dies;
  }
  function appeared(a, t) { return t >= a.born; }

  function ctxOf(a, t) {
    if (!appeared(a, t)) return null;
    var end = a.dies === null ? t : Math.min(t, a.dies);
    return Math.min(96, Math.round(a.ctx0 + (end - a.born) * a.ctxRate));
  }

  /* Working vs waiting, so the arcs are not all doing the same thing. */
  function actState(a, t) {
    if (!alive(a, t)) return '';
    var swing = Math.sin((t + a.name.length * 7) / 11);
    return swing > 0.2 ? 'work' : 'wait';
  }

  /* The server writes these as "21s 前" regardless of language (server.py
     _rel). Matching it matters more than reading well — the whole premise
     of the fixture is that the page cannot tell the two apart. */
  function relOf(sec) {
    var d = Math.max(0, Math.round(sec));
    if (d < 60) return d + 's 前';
    if (d < 3600) return Math.floor(d / 60) + 'm 前';
    if (d < 86400) return Math.floor(d / 3600) + 'h 前';
    return Math.floor(d / 86400) + 'd 前';
  }

  function delivCount(name) {
    return (DELIVERABLES[name] || []).length;
  }

  function lastSpokeAt(name, t) {
    var last = null;
    for (var i = 0; i < SCRIPT.length; i++) {
      var m = SCRIPT[i];
      if (m.at > t) break;
      if (m.from === name || m.to === name) last = m.at;
    }
    return last;
  }

  function agentRow(a, t) {
    var live = alive(a, t);
    var spoke = lastSpokeAt(a.name, t);
    var since = spoke === null ? t - a.born : t - spoke;
    return {
      name: a.name, category: 'agent', running: live, attached: live,
      cmd: live ? (a.program === 'codex' ? 'codex' : 'claude') : 'zsh',
      live: live ? 'agentstack-demo' : '', model: a.model,
      model_raw: a.model_raw, provider: a.provider, ctx_window: '',
      ctx_used: ctxOf(a, t), act_state: actState(a, t),
      work_disp: actState(a, t) === 'work' ? Math.round(since) + 's' : null,
      last_disp: Math.round(since) + 's',
      last_active: epoch() - Math.round(since),
      last_active_rel: relOf(since),
      created: epoch() - Math.round(t - a.born) - 600,
      task: tx(a.task), instruction: null, deliv: delivCount(a.name),
    };
  }

  function pastRow(p) {
    return {
      name: p.name, category: 'agent', running: false, attached: false,
      cmd: 'zsh', live: '', model: p.model, model_raw: p.model_raw,
      provider: p.provider, ctx_window: '', ctx_used: null, act_state: '',
      work_disp: null, last_disp: relOf(p.ago),
      last_active: epoch() - p.ago, last_active_rel: relOf(p.ago),
      created: epoch() - p.ago - 3600, task: tx(p.task), instruction: null,
      deliv: delivCount(p.name),
    };
  }

  function agentsPayload() {
    var t = phase();
    var rows = [];
    CAST.forEach(function (a) { if (appeared(a, t)) rows.push(agentRow(a, t)); });
    PAST.forEach(function (p) { rows.push(pastRow(p)); });
    return { ts: epoch(), agents: rows };
  }

  function annotOf(a) {
    if (!a.role) return null;
    return { role: a.role, emoji: a.emoji || '', group: a.group || 'demo' };
  }

  function graphNode(a, t) {
    var live = alive(a, t);
    var spoke = lastSpokeAt(a.name, t);
    var since = spoke === null ? t - a.born : t - spoke;
    return {
      name: a.name, model: a.model, program: a.program, provider: a.provider,
      task: tx(a.task), retired: false, last_active: epoch() - Math.round(since),
      act: actState(a, t) === 'work' ? 1 : 0, rel: relOf(since),
      deliv: delivCount(a.name),
      annot: annotOf(a), present: live, running: live,
      state: live ? 'run' : 'finished', act_state: actState(a, t),
      ctx_used: ctxOf(a, t), ctx_window: '', attached: live,
      work_disp: null, work_secs: 0, last_disp: Math.round(since) + 's',
      live: live ? 'agentstack-demo' : '', pane_model: a.model, sig: '',
    };
  }

  function graphPastNode(p) {
    return {
      name: p.name, model: p.model, program: p.program, provider: p.provider,
      task: tx(p.task), retired: true, last_active: epoch() - p.ago, act: 0,
      rel: relOf(p.ago), deliv: delivCount(p.name), annot: null,
      present: false,
      running: false, state: 'gone', act_state: '', ctx_used: null,
      ctx_window: '', attached: false, work_disp: null, work_secs: 0,
      last_disp: relOf(p.ago), live: '', pane_model: p.model, sig: '',
    };
  }

  function graphPayload() {
    var t = phase();
    var nodes = [], present = {};
    CAST.forEach(function (a) {
      if (!appeared(a, t)) return;
      nodes.push(graphNode(a, t)); present[a.name] = true;
    });
    PAST.forEach(function (p) {
      nodes.push(graphPastNode(p)); present[p.name] = true;
    });

    var counts = {};
    SCRIPT.forEach(function (m) {
      if (m.at > t) return;
      if (!present[m.from] || !present[m.to]) return;
      var k = m.from + '\u001f' + m.to;
      if (!counts[k]) counts[k] = { count: 0, last: 0 };
      counts[k].count += 1;
      counts[k].last = m.at;
    });
    var edges = Object.keys(counts).map(function (k) {
      var parts = k.split('\u001f');
      return {
        source: parts[0], target: parts[1], count: counts[k].count,
        last_ts: epoch() - Math.round(t - counts[k].last), kind: 'to',
      };
    });

    var spawn = [];
    CAST.concat(PAST).forEach(function (a) {
      if (a.parent && present[a.name] && present[a.parent]) {
        spawn.push({ source: a.parent, target: a.name, type: 'spawn' });
      }
    });

    return { nodes: nodes, edges: edges, spawn: spawn,
             total: nodes.length, shown: nodes.length, ts: epoch() };
  }

  /* messages-since drives the comets. Return what the script said between
     the caller's cursor and now, translated into wall-clock seconds. */
  function messagesSince(sinceTs) {
    var t = phase(), out = [], id = 1;
    SCRIPT.forEach(function (m) {
      if (m.at > t) { id++; return; }
      var ts = epoch() - Math.round(t - m.at);
      if (ts > sinceTs) {
        out.push({
          id: id, sender: m.from, recipient: m.to, subject: tx(m.subject),
          body: '', ts: ts, rcpt_n: 1, kind: 'to',
        });
      }
      id++;
    });
    return { ok: true, ts: epoch(), messages: out };
  }

  /* The per-agent panel draws an activity chart from these, and the edge
     drawer lists the traffic between a pair. Empty arrays are legal and the
     page keeps working — it just says NO ACTIVITY and shows an empty band,
     which reads as "this product has no history" rather than "this is a
     demo". Answer them from the same script the graph uses. */
  function agentHistory(query) {
    var t = phase(), name = query.get('name') || '', events = [], id = 1;
    SCRIPT.forEach(function (m) {
      if (m.at <= t && (m.from === name || m.to === name)) {
        events.push({
          id: id, ts: epoch() - Math.round(t - m.at),
          kind: m.from === name ? 'mail_sent' : 'mail_recv',
          ref: m.from === name ? m.to : m.from, subject: tx(m.subject),
          importance: 'normal', agent: name, sender: m.from,
          recipient: m.to, thread_id: null,
        });
      }
      id++;
    });
    var start = epoch() - Math.round(t);
    return { ok: true, hours: null, auto_range: true, since_ts: start,
             now_ts: epoch(), range: { start_ts: start, end_ts: epoch() },
             total_raw: events.length, events: events };
  }

  function edgeMessages(query) {
    var t = phase(), a = query.get('a') || '', b = query.get('b') || '';
    var out = [], id = 1;
    SCRIPT.forEach(function (m) {
      var between = (m.from === a && m.to === b) || (m.from === b && m.to === a);
      if (m.at <= t && between) {
        var ts = epoch() - Math.round(t - m.at);
        out.push({ id: id, ts: new Date(ts * 1000).toISOString()
                     .replace('T', ' ').replace('Z', ''),
                   ts_unix: ts, sender: m.from, recipient: m.to,
                   subject: tx(m.subject), body: '' });
      }
      id++;
    });
    return { ok: true, a: a, b: b, count: out.length, messages: out };
  }

  /* ── the transcripts ─────────────────────────────────────────────────
     Opening a pane is how anyone finds out what an agent is actually
     doing, so an empty one reads as "there is nothing to see in this
     product". These are the same six jobs the mail script describes, told
     from the inside. `at` is seconds into the loop; lines past the current
     moment have not been said yet, so a pane opened late shows more than
     one opened early — which is the behaviour of the real thing.

     tool_use text is "name  args", two spaces, the way the page splits it. */
  var TRANSCRIPTS = {
    AmberKepler: [
      [0, 'user', 'text',
       'Move the store onto the v2 schema. Nothing ships until the mapping ' +
       'is covered by tests.'],
      [4, 'assistant', 'thinking',
       'Three pieces: the mapping, the tests over it, and the note operators ' +
       'read. Only the tests depend on the mapping, so the note can start ' +
       'whenever someone is free.'],
      [6, 'assistant', 'text',
       'Plan: one child reads the old schema and writes the mapping, a ' +
       'second covers it with tests, a third drafts the upgrade note. I hold ' +
       'the release until the second one is green.'],
      [13, 'assistant', 'tool_use', 'Bash  agentstack spawn --role schema'],
      [14, 'assistant', 'tool_result', 'started SlateHooke (gpt-5.6)'],
      [16, 'assistant', 'tool_use',
       'SendMessage  to=SlateHooke "map the old schema field by field"'],
      [36, 'assistant', 'tool_result',
       'SlateHooke: three fields have no counterpart — listing them'],
      [38, 'assistant', 'thinking',
       'Three unmapped fields is the whole risk. Get them asserted before ' +
       'anything is written.'],
      [40, 'assistant', 'tool_use', 'Bash  agentstack spawn --role tests'],
      [41, 'assistant', 'tool_result', 'started IvoryNoether (gpt-5.6)'],
      [95, 'assistant', 'tool_use', 'Bash  agentstack spawn --role docs'],
      [96, 'assistant', 'tool_result', 'started RustPasteur (claude-sonnet-5)'],
      [131, 'assistant', 'text',
       'Release is held on the other rail until the mapping lands. That is ' +
       'the right order — nothing to unwind here.'],
      [147, 'assistant', 'text',
       'Tests green, including the field with two writers. That was the one ' +
       'I was worried about.'],
      [205, 'assistant', 'text',
       'Mapping covered, note drafted, gate released. Handing back.'],
    ],
    SlateHooke: [
      [15, 'user', 'text', 'Map the old schema field by field.'],
      [19, 'assistant', 'tool_use', 'Read  store/schema_v1.py'],
      [20, 'assistant', 'tool_result', '412 lines, 38 fields'],
      [22, 'assistant', 'tool_use', 'Read  store/schema_v2.py'],
      [23, 'assistant', 'tool_result', '297 lines, 31 fields'],
      [26, 'assistant', 'thinking',
       'Fewer fields on the far side, so some collapse and some are gone. ' +
       'The gone ones are the only ones worth a message.'],
      [30, 'assistant', 'tool_use', 'Write  docs/mapping.md'],
      [31, 'assistant', 'tool_result', 'wrote 38 rows'],
      [34, 'assistant', 'text',
       'Three fields have no counterpart: legacy_ref, shard_hint, and ' +
       'retry_budget. The first two are dead. retry_budget is still written ' +
       'in two places, which is the one that will bite.'],
      [90, 'assistant', 'text',
       'Answered the tests child: assert retry_budget first, it has two ' +
       'writers.'],
      [219, 'assistant', 'tool_use', 'Bash  python -m pytest tests/mapping'],
      [220, 'assistant', 'tool_result', '24 passed'],
      [222, 'assistant', 'text',
       'Mapping complete, nothing unresolved. Report sent.'],
    ],
    IvoryNoether: [
      [40, 'user', 'text', 'Cover the mapping before anything is moved.'],
      [44, 'assistant', 'tool_use', 'Read  docs/mapping.md'],
      [45, 'assistant', 'tool_result', '38 rows, 3 marked unmapped'],
      [48, 'assistant', 'thinking',
       'A test per row is cheap and proves nothing interesting. The three ' +
       'unmapped rows are where the behaviour is undecided.'],
      [76, 'assistant', 'text',
       'Asking the schema child which of the three to assert first rather ' +
       'than guessing the intent.'],
      [92, 'assistant', 'tool_use', 'Write  tests/mapping/test_retry_budget.py'],
      [93, 'assistant', 'tool_result', 'wrote 3 cases'],
      [112, 'assistant', 'tool_use', 'Bash  python -m pytest tests/mapping'],
      [113, 'assistant', 'tool_result', '1 failed, 23 passed'],
      [116, 'assistant', 'thinking',
       'The failure is the second writer overwriting a value the first one ' +
       'set. That is the bug the mapping was hiding, not a bad test.'],
      [128, 'assistant', 'tool_use', 'Edit  store/migrate.py'],
      [129, 'assistant', 'tool_result', 'applied'],
      [143, 'assistant', 'tool_use', 'Bash  python -m pytest tests/mapping'],
      [144, 'assistant', 'tool_result', '24 passed'],
      [146, 'assistant', 'text',
       'Green, including the two-writer case. Reporting back.'],
      [181, 'assistant', 'text', 'Done. Handing back.'],
    ],
    RustPasteur: [
      [95, 'user', 'text', 'Write the upgrade note the way an operator reads it.'],
      [99, 'assistant', 'thinking',
       'An operator wants to know what breaks and what to do about it, in ' +
       'that order. The schema detail belongs underneath, not on top.'],
      [104, 'assistant', 'tool_use', 'Read  docs/mapping.md'],
      [105, 'assistant', 'tool_result', '38 rows'],
      [122, 'assistant', 'tool_use', 'Write  docs/upgrade-v2.md'],
      [123, 'assistant', 'tool_result', 'wrote 96 lines'],
      [140, 'assistant', 'text',
       'Draft is up. One thing I cannot decide alone: whether the defaults ' +
       'for the dropped fields should be documented or removed outright.'],
      [168, 'assistant', 'text', 'Question sent up with the draft.'],
    ],
    MossSomerville: [
      [0, 'user', 'text',
       'Hold the release rail. Nothing goes out while the schema work is ' +
       'open.'],
      [3, 'assistant', 'text',
       'Understood. I will run the checks so we know where we stand, but the ' +
       'gate stays shut.'],
      [58, 'assistant', 'tool_use', 'Bash  agentstack spawn --role release'],
      [59, 'assistant', 'tool_result', 'started FlintGauss (gpt-5.6)'],
      [120, 'assistant', 'thinking',
       'A failing check during a migration is usually the migration. Worth ' +
       'confirming before it is called a flake and retried away.'],
      [126, 'assistant', 'text',
       'The failing check is the gate itself, not a flaky test. Telling the ' +
       'other rail we are holding.'],
      [204, 'assistant', 'text',
       'Mapping is covered upstream. Opening the gate.'],
    ],
    FlintGauss: [
      [60, 'user', 'text', 'Run the release checks on the current branch.'],
      [64, 'assistant', 'tool_use', 'Bash  ./scripts/release-check.sh'],
      [92, 'assistant', 'tool_result',
       '7 checks · 6 ok · 1 failed (schema-compat)'],
      [96, 'assistant', 'tool_use', 'Bash  ./scripts/release-check.sh --only schema-compat -v'],
      [110, 'assistant', 'tool_result',
       'schema-compat: v1 payload rejected by v2 reader'],
      [114, 'assistant', 'thinking',
       'It fails the same way twice, so it is not timing. This is the check ' +
       'doing its job.'],
      [118, 'assistant', 'text',
       'One check fails and it reproduces. It is the gate, not a flake — ' +
       'a v1 payload is rejected by the v2 reader.'],
    ],
    CedarLovelace: [
      [0, 'user', 'text', 'Survey the call sites before we plan anything.'],
      [0, 'assistant', 'tool_use', 'Grep  schema_v1\\.'],
      [0, 'assistant', 'tool_result', '61 matches in 19 files'],
      [0, 'assistant', 'text',
       '19 files touch the old schema; 4 of them write. Listed in the report.'],
    ],
    OchreCurie: [
      [0, 'user', 'text', 'Draft the rollback plan.'],
      [0, 'assistant', 'text',
       'Rollback is a read-side switch, not a data restore — the v2 writer ' +
       'keeps the v1 columns populated for one release. Written up.'],
    ],
    UmberBohr: [
      [0, 'user', 'text', 'Reproduce the reported failure.'],
      [0, 'assistant', 'tool_use', 'Bash  python -m pytest tests/store -k retry'],
      [0, 'assistant', 'tool_result', '1 failed'],
      [0, 'assistant', 'text', 'Reproduces every run. Not a flake.'],
    ],
  };

  /* Commands and their output are left alone; only what an agent says or
     thinks is translated. */
  function prose(kind) { return kind === 'text' || kind === 'thinking'; }

  function castOf(name) {
    for (var i = 0; i < CAST.length; i++)
      if (CAST[i].name === name) return CAST[i];
    for (var j = 0; j < PAST.length; j++)
      if (PAST[j].name === name) return PAST[j];
    return null;
  }

  function historyPayload(query) {
    var name = query.get('session') || '';
    var rows = TRANSCRIPTS[name], who = castOf(name);
    if (!rows || !who) {
      return { ok: false,
               error: tx('no transcript on disk for this agent') };
    }
    var t = phase(), gone = who.retired === true, events = [];
    rows.forEach(function (r) {
      if (!gone && r[0] > t) return;
      var ts = gone ? epoch() - who.ago : epoch() - Math.round(t - r[0]);
      events.push({ role: r[1], kind: r[2], text: prose(r[2]) ? tx(r[3]) : r[3],
                    ts: new Date(ts * 1000).toISOString() });
    });
    var codex = who.program === 'codex';
    return { ok: true, session: name,
             file: (codex ? 'rollout-' : '') + name.toLowerCase() + '.jsonl',
             source: codex ? 'codex' : 'claude',
             total: events.length, shown: events.length, events: events };
  }

  /* What an agent left behind. The real server links these into a vault;
     there is none here, so they render as plain rows rather than links
     that would open nothing. */
  var DELIVERABLES = {
    SlateHooke: [['Field-by-field mapping, v1 to v2', 'docs/mapping.md', 1500]],
    IvoryNoether: [['Mapping test suite', 'tests/mapping/', 900],
                   ['Note on the two-writer failure', 'docs/two-writers.md', 600]],
    RustPasteur: [['Operator upgrade note', 'docs/upgrade-v2.md', 300]],
    FlintGauss: [['Release check run', 'reports/release-check.txt', 400]],
    CedarLovelace: [['Call-site survey', 'docs/call-sites.md', 5400]],
    OchreCurie: [['Rollback plan', 'docs/rollback.md', 9200]],
  };

  function deliverables(query) {
    var ag = query.get('agent') || '', rows = DELIVERABLES[ag] || [];
    return { ok: true, agent: ag, vault: '',
             items: rows.map(function (r) {
               return { title: tx(r[0]), rel: r[1],
                        mtime: epoch() - r[2], vault: '' };
             }) };
  }

  /* ── the translation ─────────────────────────────────────────────────
     Keyed by the English string. `translatable()` below enumerates every
     string that reaches a reader, and a test asserts each one has an entry
     here — so a line added to the fixture cannot quietly ship English to a
     Japanese visitor.

     Built from pairs rather than an object literal because the English side
     is often a concatenation, which is not a legal key. */
  var JA = (function () {
    var t = {};
    [
      ['Read the old schema and write the field-by-field mapping',
       '旧スキーマを読み、フィールド単位の対応表を書く'],
      ['Cover the mapping with tests before anything is moved',
       'データを動かす前に対応表をテストで固める'],
      ['Write the upgrade note the way an operator would read it',
       '運用者が読む形で移行手順を書く'],
      ['Watch the release rail and hold the gate',
       'リリース系統を監視し、ゲートを閉じておく'],
      ['Run the release checks and report what fails',
       'リリース検査を回し、落ちた項目を報告する'],
      ['Survey the call sites',
       '呼び出し箇所を洗い出す'],
      ['Draft the rollback plan',
       'ロールバック手順を起草する'],
      ['Reproduce the reported failure',
       '報告された不具合を再現する'],
      ['RE: three fields have no counterpart — listing them',
       'RE: 対応先が無いフィールドが3つ。列挙します'],
      ['Task: cover the mapping before anything moves',
       '依頼: 何かを動かす前に対応表をテストで固める'],
      ['Task: run the release checks on the current branch',
       '依頼: 現ブランチでリリース検査を回す'],
      ['Which of the three do you want asserted first?',
       '3つのうち、どれから assert しますか'],
      ['RE: the one with two writers — that is where it breaks',
       'RE: 書き手が2つあるやつ。壊れるのはそこ'],
      ['Task: write the upgrade note for operators',
       '依頼: 運用者向けの移行手順を書く'],
      ['RE: one check fails — it is the gate, not a flake',
       'RE: 1件落ちました。flake ではなくゲートです'],
      ['Holding the release until the mapping lands',
       '対応表が入るまでリリースを止めます'],
      ['RE: tests are green, including the two-writer case',
       'RE: テスト全緑。書き手が2つあるケースも通りました'],
      ['RE: draft is up — one open question about defaults',
       'RE: 草稿できました。既定値について1点未決です'],
      ['Done. Handing back.',
       '完了しました。引き継ぎます。'],
      ['Mapping is covered — the gate can open',
       '対応表はテスト済みです。ゲートを開けて大丈夫です'],
      ['RE: mapping is complete, nothing unresolved',
       'RE: 対応表は完成、未解決はありません'],
      ['Mapping test suite',
       '対応表のテスト一式'],
      ['Note on the two-writer failure',
       '書き手が2つある不具合のメモ'],
      ['Operator upgrade note',
       '運用者向け移行手順'],
      ['Release check run',
       'リリース検査の実行結果'],
      ['Call-site survey',
       '呼び出し箇所の調査'],
      ['Rollback plan',
       'ロールバック手順'],
      ['Three pieces: the mapping, the tests over it, and the note ' +
         'operators read. Only the tests depend on the mapping, so the note ' +
         'can start whenever someone is free.',
       '仕事は3つ。対応表、その上のテスト、運用者が読む手順。対応表に依存するのはテストだけなので、手順は手が空いた時点で始められる。'],
      ['Plan: one child reads the old schema and writes the mapping, a ' +
         'second covers it with tests, a third drafts the upgrade note. I ' +
         'hold the release until the second one is green.',
       '方針: 1体が旧スキーマを読んで対応表を書き、2体目がテストで固め、3体目が移行手順を起草する。2体目が緑になるまでリリースは止める。'],
      ['Release is held on the other rail until the mapping lands. That is' +
         ' the right order — nothing to unwind here.',
       '対応表が入るまで、もう一方の系統でリリースを止めてもらっている。順序としては正しく、巻き戻すものは無い。'],
      ['Tests green, including the field with two writers. That was the ' +
         'one I was worried about.',
       'テスト全緑。書き手が2つあるフィールドも通った。心配していたのはそこだった。'],
      ['Mapping covered, note drafted, gate released. Handing back.',
       '対応表はテスト済み、手順も草稿ができ、ゲートも開いた。引き継ぎます。'],
      ['Map the old schema field by field.',
       '旧スキーマをフィールド単位で対応付けてください。'],
      ['Fewer fields on the far side, so some collapse and some are gone. ' +
         'The gone ones are the only ones worth a message.',
       '移行先の方がフィールドが少ない。統合されたものと消えたものがある。報告に値するのは消えた方だけ。'],
      ['Three fields have no counterpart: legacy_ref, shard_hint, and ' +
         'retry_budget. The first two are dead. retry_budget is still ' +
         'written in two places, which is the one that will bite.',
       '対応先が無いのは legacy_ref・shard_hint・retry_budget ' +
         'の3つ。前の2つは死んでいる。retry_budget はまだ2箇所から書かれていて、危ないのはこれ。'],
      ['Answered the tests child: assert retry_budget first, it has two ' +
         'writers.',
       'テスト担当に回答: retry_budget から assert してほしい。書き手が2つある。'],
      ['Mapping complete, nothing unresolved. Report sent.',
       '対応表は完成、未解決なし。報告を送った。'],
      ['Cover the mapping before anything is moved.',
       'データを動かす前に対応表をテストで固めてください。'],
      ['A test per row is cheap and proves nothing interesting. The three ' +
         'unmapped rows are where the behaviour is undecided.',
       '1行1テストは安いが、面白いことは何も証明しない。挙動が未決なのは対応先の無い3行だ。'],
      ['Asking the schema child which of the three to assert first rather ' +
         'than guessing the intent.',
       '意図を推測せず、3つのどれから assert するかスキーマ担当に聞く。'],
      ['The failure is the second writer overwriting a value the first one' +
         ' set. That is the bug the mapping was hiding, not a bad test.',
       '落ちたのは、2つ目の書き手が1つ目の値を上書きしているから。テストが悪いのではなく、対応表が隠していたバグ。'],
      ['Green, including the two-writer case. Reporting back.',
       '書き手が2つあるケースも含めて全緑。報告する。'],
      ['Write the upgrade note the way an operator reads it.',
       '運用者が読む順序で移行手順を書いてください。'],
      ['An operator wants to know what breaks and what to do about it, in ' +
         'that order. The schema detail belongs underneath, not on top.',
       '運用者が知りたいのは「何が壊れるか」「どうすればいいか」の順。スキーマの詳細はその下に置くもので、先頭ではない。'],
      ['Draft is up. One thing I cannot decide alone: whether the defaults' +
         ' for the dropped fields should be documented or removed outright.',
       '草稿ができた。1点だけ独断できない: 廃止するフィールドの既定値を、文書に残すか消し切るか。'],
      ['Question sent up with the draft.',
       '草稿と一緒に質問を上げた。'],
      ['Hold the release rail. Nothing goes out while the schema work is ' +
         'open.',
       'リリース系統を止めてください。スキーマ作業が開いている間は何も出しません。'],
      ['Understood. I will run the checks so we know where we stand, but ' +
         'the gate stays shut.',
       '了解。現状把握のために検査は回すが、ゲートは閉じたままにする。'],
      ['A failing check during a migration is usually the migration. Worth' +
         ' confirming before it is called a flake and retried away.',
       '移行中に落ちた検査は、たいてい移行そのものが原因。flake 扱いで再実行に流す前に確かめる価値がある。'],
      ['The failing check is the gate itself, not a flaky test. Telling ' +
         'the other rail we are holding.',
       '落ちた検査は flake ではなくゲートそのもの。止める旨をもう一方の系統に伝える。'],
      ['Mapping is covered upstream. Opening the gate.',
       '上流で対応表がテスト済みになった。ゲートを開ける。'],
      ['Run the release checks on the current branch.',
       '現ブランチでリリース検査を回してください。'],
      ['It fails the same way twice, so it is not timing. This is the ' +
         'check doing its job.',
       '2回とも同じ落ち方なのでタイミングではない。検査が仕事をしている。'],
      ['One check fails and it reproduces. It is the gate, not a flake — a' +
         ' v1 payload is rejected by the v2 reader.',
       '1件落ちて、再現もする。flake ではなくゲート。v1 のペイロードが v2 のリーダーに弾かれている。'],
      ['Survey the call sites before we plan anything.',
       '設計に入る前に呼び出し箇所を洗い出してください。'],
      ['19 files touch the old schema; 4 of them write. Listed in the ' +
         'report.',
       '旧スキーマに触れているのは19ファイル、うち4つが書き込み。報告に列挙した。'],
      ['Draft the rollback plan.',
       'ロールバック手順を起草してください。'],
      ['Rollback is a read-side switch, not a data restore — the v2 writer' +
         ' keeps the v1 columns populated for one release. Written up.',
       'ロールバックはデータ復元ではなく読み側の切り替え。v2 の書き手が1リリースの間 v1 のカラムも埋め続ける。文書化した。'],
      ['Reproduce the reported failure.',
       '報告された不具合を再現してください。'],
      ['Reproduces every run. Not a flake.',
       '毎回再現する。flake ではない。'],
      ['demo mode — nothing was started, stopped or changed',
       'デモです。何も起動・停止・変更されていません'],
      ['Plan the migration and hand each piece to a child',
       '移行を設計し、各パートを子エージェントに渡す'],
      ['Task: map the old schema field by field',
       '依頼: 旧スキーマをフィールド単位で対応付ける'],
      ['Move the store onto the v2 schema. Nothing ships until the ' +
       'mapping is covered by tests.',
       'ストアを v2 スキーマに移してください。対応表がテストで固まるまで出荷はしません。'],
      ['Three unmapped fields is the whole risk. Get them asserted ' +
       'before anything is written.',
       'リスクは対応先の無い3フィールドに集中している。何かを書く前に assert させる。'],
      ['Field-by-field mapping, v1 to v2', 'v1→v2 フィールド対応表'],
      ['no transcript on disk for this agent',
       'このエージェントの会話ログはディスク上にありません'],
    ].forEach(function (pair) { t[pair[0]] = pair[1]; });
    return t;
  })();

  /* Everything a reader can end up looking at. The tests walk this. */
  function translatable() {
    var out = [];
    CAST.concat(PAST).forEach(function (a) { out.push(a.task); });
    SCRIPT.forEach(function (m) { out.push(m.subject); });
    Object.keys(TRANSCRIPTS).forEach(function (n) {
      TRANSCRIPTS[n].forEach(function (r) { if (prose(r[2])) out.push(r[3]); });
    });
    Object.keys(DELIVERABLES).forEach(function (n) {
      DELIVERABLES[n].forEach(function (r) { out.push(r[0]); });
    });
    out.push('no transcript on disk for this agent');
    out.push('demo mode — nothing was started, stopped or changed');
    return out;
  }

  var ROUTES = {
    '/api/agents': agentsPayload,
    '/api/graph': graphPayload,
    '/api/history': historyPayload,
    '/api/deliverables': deliverables,
    '/api/mail-watcher-health': function () {
      return { ok: true, ts: epoch(), last_success_ts: epoch() - 3,
               last_success_age_s: 3, recent_results: {}, signal_count: 0,
               daemon_running: true, watcher_running: true, status: 'green' };
    },
    '/api/agent-history': agentHistory,
    '/api/edge-messages': edgeMessages,
    '/api/spawn-names': function () { return { ok: true, names: [] }; },
    '/api/fs/dirs': function () { return { ok: true, dirs: [] }; },
  };

  /* Anything that would change the machine answers politely and does
     nothing. The buttons stay live on purpose — the page is here to show
     how it is used, and a control you cannot press teaches nothing. */
  var WRITES = ['/api/spawn', '/api/exit', '/api/kill', '/api/jump',
                '/api/annotate', '/api/jserr', '/api/suggest-name'];

  function json(body) {
    return new Response(JSON.stringify(body), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    });
  }

  var realFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    var url = typeof input === 'string' ? input : (input && input.url) || '';
    var path = url.split('?')[0].replace(/^https?:\/\/[^/]+/, '');

    if (WRITES.indexOf(path) !== -1) {
      window.dispatchEvent(new CustomEvent('demo:blocked', { detail: path }));
      return Promise.resolve(json({ ok: false, demo: true,
        error: tx('demo mode — nothing was started, stopped or changed') }));
    }
    if (path === '/api/messages-since') {
      var m = /[?&]since=(\d+)/.exec(url);
      return Promise.resolve(json(messagesSince(m ? Number(m[1]) : 0)));
    }
    if (ROUTES[path]) {
      var q = new URLSearchParams(url.split('?')[1] || '');
      return Promise.resolve(json(ROUTES[path](q)));
    }
    return realFetch(input, init);
  };

  /* The server turns a name into a portrait file; a static build cannot,
     and <img src> never reaches the fetch shim above. Point at the files
     directly. Only the surnames the cast uses need to ship. */
  function portraitURL(sci) {
    return 'portraits_64/' + encodeURIComponent(sci) + '.png';
  }

  /* Provider logos are absolute on a served dashboard. The static build has
     to work wherever it is uploaded, including a subdirectory, so keep them
     relative to the page. */
  function assetURL(name, v) {
    return 'assets/' + encodeURIComponent(name) + '.svg?v=' + v;
  }

  window.AGENTSTACK_DEMO = { loop: LOOP, cast: CAST, script: SCRIPT,
                             lang: lang, setLang: setLang, translate: tx,
                             translatable: translatable,
                             portraitURL: portraitURL, assetURL: assetURL, phase: phase,
                             payloads: { agents: agentsPayload,
                                         graph: graphPayload,
                                         history: historyPayload,
                                         deliverables: deliverables,
                                         messagesSince: messagesSince } };
})();
