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
  var params = new URLSearchParams(location.search);
  if (params.get('demo') !== '1') return;

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
    { name: 'OchreFranklin', parent: 'AmberKepler', model: 'Sonnet 5',
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

  function relOf(sec) {
    if (sec < 60) return Math.round(sec) + '秒前';
    if (sec < 3600) return Math.round(sec / 60) + '分前';
    return Math.round(sec / 3600) + '時間前';
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
      task: a.task, instruction: null, deliv: 0,
    };
  }

  function pastRow(p) {
    return {
      name: p.name, category: 'agent', running: false, attached: false,
      cmd: 'zsh', live: '', model: p.model, model_raw: p.model_raw,
      provider: p.provider, ctx_window: '', ctx_used: null, act_state: '',
      work_disp: null, last_disp: relOf(p.ago),
      last_active: epoch() - p.ago, last_active_rel: relOf(p.ago),
      created: epoch() - p.ago - 3600, task: p.task, instruction: null,
      deliv: 0,
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
      task: a.task, retired: false, last_active: epoch() - Math.round(since),
      act: actState(a, t) === 'work' ? 1 : 0, rel: relOf(since), deliv: 0,
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
      task: p.task, retired: true, last_active: epoch() - p.ago, act: 0,
      rel: relOf(p.ago), deliv: 0, annot: null, present: false,
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
          id: id, sender: m.from, recipient: m.to, subject: m.subject,
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
          ref: m.from === name ? m.to : m.from, subject: m.subject,
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
                   subject: m.subject, body: '' });
      }
      id++;
    });
    return { ok: true, a: a, b: b, count: out.length, messages: out };
  }

  var ROUTES = {
    '/api/agents': agentsPayload,
    '/api/graph': graphPayload,
    '/api/deliverables': function () {
      return { ok: true, agent: '', items: [] };
    },
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
        error: 'demo mode — nothing was started, stopped or changed' }));
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

  window.AGENTSTACK_DEMO = { loop: LOOP, cast: CAST, script: SCRIPT,
                             portraitURL: portraitURL, phase: phase,
                             payloads: { agents: agentsPayload,
                                         graph: graphPayload,
                                         messagesSince: messagesSince } };
})();
