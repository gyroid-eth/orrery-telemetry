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

  var CORE_JA = {
    'demo mode — launch stopped here; no agent was started':
      'デモです。起動はここで停止し、エージェントは開始されていません',
  };

  function setLang(l) { LANG = l === 'ja' ? 'ja' : 'en'; return LANG; }
  function lang() { return LANG; }
  function tx(s) {
    if (LANG !== 'ja') return s;
    return (JA && JA[s]) || CORE_JA[s] || s;
  }





  /* ── stories ─────────────────────────────────────────────────────────
     A story is one self-contained cast, script and set of transcripts.
     Others register themselves on window.AGENTSTACK_STORIES before this
     file loads; the contract they write to is demo/STORY_CONTRACT.md. */
  var STORIES = window.AGENTSTACK_STORIES = window.AGENTSTACK_STORIES || {};

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

  /* A story may pin the human-blocked states to explicit windows. Outside
     them, keep the synthetic work/wait rhythm so every card still moves. */
  function actState(a, t) {
    if (!alive(a, t)) return '';
    var states = Array.isArray(a.states) ? a.states : [];
    for (var i = 0; i < states.length; i++) {
      var window = states[i];
      if (Array.isArray(window) && t >= window[0] && t < window[1] &&
          (window[2] === 'ask' || window[2] === 'question')) {
        return window[2];
      }
    }
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
    var state = actState(a, t);
    return {
      name: a.name, category: 'agent', running: live, attached: live,
      cmd: live ? (a.program === 'codex' ? 'codex' : 'claude') : 'zsh',
      live: live ? 'agentstack-demo' : '', model: a.model,
      model_raw: a.model_raw, provider: a.provider, ctx_window: '',
      ctx_used: ctxOf(a, t), act_state: state,
      work_disp: state === 'work' ? Math.round(since) + 's' : null,
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
    var state = actState(a, t);
    return {
      name: a.name, model: a.model, program: a.program, provider: a.provider,
      task: tx(a.task), retired: false, last_active: epoch() - Math.round(since),
      act: state === 'work' ? 1 : 0, rel: relOf(since),
      deliv: delivCount(a.name),
      annot: annotOf(a), present: live, running: live,
      state: live ? 'run' : 'finished', act_state: state,
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

  function bodyOf(m) { return LANG === 'ja' ? m.body_ja : m.body; }

  /* The comet carries the first line of the body, the way the server builds
     it (messages_since_payload strips leading markdown from line one). */
  function excerptOf(m) {
    var first = (bodyOf(m) || '').split('\n')[0] || '';
    return first.replace(/^[#>*\-\s`]+/, '').slice(0, 120);
  }

  /* messages-since drives the comets. Return what the script said between
     the caller's cursor and now, translated into wall-clock seconds.
     The page advances its cursor from `now` — returning `ts` instead left
     it at zero forever, and only the seen-key set kept comets from
     repeating. Same keys as the server, in the same order of meaning. */
  function messagesSince(sinceTs) {
    var t = phase(), out = [], id = 1;
    SCRIPT.forEach(function (m) {
      if (m.at > t) { id++; return; }
      var ts = epoch() - Math.round(t - m.at);
      if (ts > sinceTs) {
        out.push({
          id: id, ts: ts, sender: m.from, recipient: m.to,
          subject: tx(m.subject).slice(0, 90), excerpt: excerptOf(m),
          importance: m.importance || 'normal', kind: 'to', thread_id: null,
        });
      }
      id++;
    });
    return { ok: true, now: epoch(), since: sinceTs, messages: out };
  }

  /* The per-agent panel draws an activity chart from these, and Replay
     plays them back. Replay asks with `names=A,B,C`, which this used to
     ignore entirely — it read `name` only, so Replay opened on an empty
     timeline and simply had nothing to play. Empty is a legal answer, so
     nothing complained.

     Kinds the page renders: mail_sent, mail_recv, spawn, retire. Spawn and
     retire are what make the playback a story rather than a mail log. */
  function historyEvent(id, ts, kind, agent, sender, recipient, subject, imp) {
    return { id: id, ts: ts, kind: kind, ref: sender === agent ? recipient : sender,
             subject: subject, importance: imp || 'normal', agent: agent,
             sender: sender, recipient: recipient, thread_id: null };
  }

  function agentHistory(query) {
    var t = phase();
    var raw = (query.get('names') || query.get('name') || '');
    var names = raw.split(',').map(function (n) { return n.trim(); })
                   .filter(Boolean);
    var multi = Boolean(query.get('names'));
    var picked = {};
    names.forEach(function (n) { picked[n] = true; });

    var events = [], id = 1;
    SCRIPT.forEach(function (m) {
      var ts = epoch() - Math.round(t - m.at);
      if (m.at <= t) {
        /* Same dedupe as the server: when both ends are selected the send
           is kept and the receive dropped, so one message is one event. */
        if (picked[m.from]) {
          events.push(historyEvent(id, ts, 'mail_sent', m.from, m.from, m.to,
                                   tx(m.subject), m.importance));
        } else if (picked[m.to]) {
          events.push(historyEvent(id, ts, 'mail_recv', m.to, m.from, m.to,
                                   tx(m.subject), m.importance));
        }
      }
      id++;
    });

    CAST.forEach(function (a) {
      /* The server derives spawn from the parent's own sent mail, so it
         belongs to the parent's timeline — selecting only the child
         must not put someone else's event on their chart. */
      if (a.parent && a.born <= t && picked[a.parent]) {
        events.push(historyEvent(null, epoch() - Math.round(t - a.born),
          'spawn', a.parent, a.parent, a.name, 'spawned ' + a.name, 'normal'));
      }
      if (a.dies !== null && a.dies <= t && picked[a.name]) {
        events.push(historyEvent(null, epoch() - Math.round(t - a.dies),
          'retire', a.name, a.name, '', 'agent retired', 'normal'));
      }
    });
    events.sort(function (x, y) { return x.ts - y.ts; });

    var start = epoch() - Math.round(t);
    var end = epoch();
    if (events.length) {
      var span = Math.max(1, events[events.length - 1].ts - events[0].ts);
      var pad = Math.round(span * 0.05);
      start = Math.max(start, events[0].ts - pad);
      end = Math.min(end, events[events.length - 1].ts + pad);
    }

    var alive = names.filter(function (n) {
      var a = castOf(n);
      if (!a) return false;
      if (a.retired) return false;
      var born = epoch() - Math.round(t - (a.born || 0));
      var died = a.dies === null || a.dies === undefined
        ? 0 : epoch() - Math.round(t - a.dies);
      return born <= start && (!died || died > start);
    });

    var payload = {
      ok: true, hours: null, auto_range: true, since_ts: start, now_ts: end,
      range: { start_ts: start, end_ts: end },
      total_raw: events.length, events: events,
      initial_state: { ts: start, alive_agents: alive },
      include_pane_states: (query.get('include_pane_states') || '') === '1',
    };
    if (multi) {
      payload.names = names;
      payload.agents = {};
      names.forEach(function (n) {
        var a = castOf(n) || {};
        payload.agents[n] = {
          inception_ts: epoch() - Math.round(t - (a.born || 0)) - 60,
          retired_ts: a.dies === null || a.dies === undefined
            ? null : epoch() - Math.round(t - a.dies),
        };
      });
    } else {
      var one = castOf(names[0]) || {};
      payload.name = names[0] || '';
      payload.inception_ts = epoch() - Math.round(t - (one.born || 0)) - 60;
      payload.retired_ts = one.dies === null || one.dies === undefined
        ? null : epoch() - Math.round(t - one.dies);
    }
    return payload;
  }

  function bodyOf(m) { return LANG === 'ja' ? m.body_ja : m.body; }

  /* The comet carries the first line of the body, the way the server builds
     it (messages_since_payload strips leading markdown from line one). */
  function excerptOf(m) {
    var first = (bodyOf(m) || '').split('\n')[0] || '';
    return first.replace(/^[#>*\-\s`]+/, '').slice(0, 120);
  }

  /* messages-since drives the comets. Return what the script said between
     the caller's cursor and now, translated into wall-clock seconds.
     The page advances its cursor from `now` — returning `ts` instead left
     it at zero forever, and only the seen-key set kept comets from
     repeating. Same keys as the server, in the same order of meaning. */
  function messagesSince(sinceTs) {
    var t = phase(), out = [], id = 1;
    SCRIPT.forEach(function (m) {
      if (m.at > t) { id++; return; }
      var ts = epoch() - Math.round(t - m.at);
      if (ts > sinceTs) {
        out.push({
          id: id, ts: ts, sender: m.from, recipient: m.to,
          subject: tx(m.subject).slice(0, 90), excerpt: excerptOf(m),
          importance: m.importance || 'normal', kind: 'to', thread_id: null,
        });
      }
      id++;
    });
    return { ok: true, now: epoch(), since: sinceTs, messages: out };
  }

  /* The drawer shows sender, time, importance, subject and body — the body
     is the part anyone opens it for. Returning '' for it left a list of
     one-line headers and made the product look like it stores nothing.
     Keys match edge_messages_payload exactly, ack and read receipts too. */
  function edgeMessages(query) {
    var t = phase(), a = query.get('a') || '', b = query.get('b') || '';
    var out = [], id = 1;
    SCRIPT.forEach(function (m) {
      var between = (m.from === a && m.to === b) || (m.from === b && m.to === a);
      if (m.at <= t && between) {
        var ts = epoch() - Math.round(t - m.at);
        var replied = SCRIPT.some(function (r) {
          return r.at > m.at && r.at <= t && r.from === m.to && r.to === m.from;
        });
        out.push({
          id: id, ts: new Date(ts * 1000).toISOString()
                      .replace('T', ' ').replace('Z', ''),
          ts_unix: ts, sender: m.from, recipient: m.to,
          subject: tx(m.subject), body: bodyOf(m),
          importance: m.importance || 'normal', thread_id: null, topic: null,
          ack_required: m.ack === true, kind: 'to',
          /* An unanswered message is still unread; one that drew a reply is
             not. Leaving both null made every row look unattended. */
          read_ts: replied ? new Date((ts + 4) * 1000).toISOString()
                     .replace('T', ' ').replace('Z', '') : null,
          ack_ts: m.ack === true && replied
                    ? new Date((ts + 6) * 1000).toISOString()
                        .replace('T', ' ').replace('Z', '') : null,
        });
      }
      id++;
    });
    out.reverse();                       // newest first, like the server
    return { ok: true, a: a, b: b, count: out.length, messages: out };
  }


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


  function deliverables(query) {
    var ag = query.get('agent') || '', rows = DELIVERABLES[ag] || [];
    return { ok: true, agent: ag, vault: '',
             items: rows.map(function (r) {
               return { title: tx(r[0]), rel: r[1],
                        mtime: epoch() - r[2], vault: '' };
             }) };
  }


  /* Everything a reader can end up looking at. The tests walk this. */
  function translatable() {
    var out = [];
    CAST.concat(PAST).forEach(function (a) { out.push(a.task); });
    SCRIPT.forEach(function (m) { out.push(m.subject); });
    /* bodies are paired on the entry (body / body_ja) and checked
       by their own test, not through this table */
    Object.keys(TRANSCRIPTS).forEach(function (n) {
      TRANSCRIPTS[n].forEach(function (r) { if (prose(r[2])) out.push(r[3]); });
    });
    Object.keys(DELIVERABLES).forEach(function (n) {
      DELIVERABLES[n].forEach(function (r) { out.push(r[0]); });
    });
    out.push('no transcript on disk for this agent');
    out.push('demo mode — nothing was started, stopped or changed');
    out.push('demo mode — launch stopped here; no agent was started');
    return out;
  }


  var STORY, CAST, PAST, SCRIPT, TRANSCRIPTS, DELIVERABLES, JA, BEATS;
  var LOOP, OPENS_AT, START;

  function useStory(id) {
    var st = STORIES[id] || STORIES[DEFAULT_STORY];
    STORY = st;
    CAST = st.cast; PAST = st.past; SCRIPT = st.script;
    TRANSCRIPTS = st.transcripts; DELIVERABLES = st.deliverables;
    BEATS = st.beats; JA = st.ja;
    LOOP = st.loop; OPENS_AT = st.opensAt;
    restart();
    return st.id;
  }


  /* The clock starts when someone starts watching, not when the page loads.
     Otherwise time spent reading the opening card comes out of the opening
     of the story, and a slow reader lands after the spawns they came to see. */
  function restart() { START = Date.now() - OPENS_AT * 1000; return OPENS_AT; }

  /* No story lives in this file any more. The engine plays whatever the
     story files registered; the default is the first one they declare as
     `preferred`, falling back to whichever sorts first so the page never
     opens on nothing. */
  var DEFAULT_STORY = (function () {
    var ids = Object.keys(STORIES);
    for (var i = 0; i < ids.length; i++)
      if (STORIES[ids[i]].preferred) return ids[i];
    return ids.sort()[0];
  })();
  /* Nothing to play. Leave fetch alone and let the page be an ordinary
     dashboard rather than throwing on the way up — a bundle built without
     its story files should degrade, not break. */
  if (!DEFAULT_STORY) return;
  useStory(params.get('story') || DEFAULT_STORY);

  /* ── launch modal fixtures ───────────────────────────────────────────
     These mirror server.py's successful response shapes. Every scientist
     has a bundled portrait, and the directory tree is deliberately invented:
     it looks like a workstation without disclosing or probing this one. */
  var DEMO_SPAWN_ADJECTIVES = [
    'Loyal', 'Vivid', 'Copper', 'Swift', 'Quiet', 'Mossy', 'Bright', 'Nimble',
  ];
  var DEMO_SPAWN_SCIENTISTS = [
    'Bohr', 'Curie', 'Gauss', 'Hooke', 'Kepler', 'Lovelace', 'Noether',
    'Pasteur', 'Somerville',
  ];
  var DEMO_SPAWN_ROOT = '/workspaces';
  var DEMO_SPAWN_PROJECTS = [
    'meridian-console', 'orbit-ledger', 'signal-garden',
  ];

  function demoSpawnNameStatusValue(name) {
    var value = String(name || '').trim();
    var match = /^([A-Z][A-Za-z]*)-([A-Z][A-Za-z]*)$/.exec(value);
    if (!match || DEMO_SPAWN_ADJECTIVES.indexOf(match[1]) === -1 ||
        DEMO_SPAWN_SCIENTISTS.indexOf(match[2]) === -1) return 'unknown';
    var key = value.replace(/-/g, '').toLowerCase();
    var occupied = CAST.concat(PAST).some(function (a) {
      return a.name.replace(/-/g, '').toLowerCase() === key;
    });
    return occupied ? 'occupied' : 'available';
  }

  function spawnNamesPayload() {
    var claudeModels = [
      'claude-sonnet-5', 'claude-opus-5', 'claude-haiku-4-5-20251001',
    ];
    var codexModels = ['gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna'];
    return {
      names: DEMO_SPAWN_SCIENTISTS.map(function (name) {
        return { name: name, portrait: true, status: 'available' };
      }),
      adjectives: DEMO_SPAWN_ADJECTIVES.slice(),
      naming: 'adjective-scientist',
      dirs: DEMO_SPAWN_PROJECTS.map(function (name) {
        return DEMO_SPAWN_ROOT + '/' + name;
      }),
      models: claudeModels.slice(),
      default_model: 'claude-sonnet-5',
      providers: [
        { id: 'claude', label: 'Claude', program: 'claude-code',
          models: claudeModels.slice(), default_model: 'claude-sonnet-5',
          efforts: null },
        { id: 'codex', label: 'Codex', program: 'codex-cli',
          models: codexModels, default_model: 'gpt-5.6-sol',
          efforts: ['low', 'medium', 'high', 'xhigh'],
          effort_default: 'xhigh' },
      ],
    };
  }

  function suggestSpawnName(query) {
    var scientist = query.get('scientist') || '';
    var scientistIndex = DEMO_SPAWN_SCIENTISTS.indexOf(scientist);
    if (scientistIndex === -1) return { error: 'no available name found' };
    for (var i = 0; i < DEMO_SPAWN_ADJECTIVES.length; i++) {
      var adjective = DEMO_SPAWN_ADJECTIVES[
        (scientistIndex + i) % DEMO_SPAWN_ADJECTIVES.length];
      var candidate = adjective + '-' + scientist;
      if (demoSpawnNameStatusValue(candidate) === 'available') {
        return { name: candidate };
      }
    }
    return { error: 'no available name found' };
  }

  function spawnDirectorySuggestions(query) {
    var requested = String(query.get('path') || '').trim();
    var target = requested || DEMO_SPAWN_ROOT;
    var dirs = [];
    if (target === DEMO_SPAWN_ROOT) {
      dirs = DEMO_SPAWN_PROJECTS.map(function (name) {
        return { name: name, path: DEMO_SPAWN_ROOT + '/' + name };
      });
    } else if (DEMO_SPAWN_PROJECTS.some(function (name) {
      return target === DEMO_SPAWN_ROOT + '/' + name;
    })) {
      dirs = ['docs', 'src', 'tests'].map(function (name) {
        return { name: name, path: target + '/' + name };
      });
    } else if (target.indexOf(DEMO_SPAWN_ROOT + '/') !== 0) {
      return { path: null, dirs: [] };
    }
    return { path: target, dirs: dirs, truncated: false };
  }

  function spawnNameStatus(query) {
    var name = query.get('name') || '';
    return { name: name, status: demoSpawnNameStatusValue(name) };
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
    '/api/spawn-names': spawnNamesPayload,
    '/api/fs/dirs': spawnDirectorySuggestions,
    '/api/name-status': spawnNameStatus,
  };

  /* Anything that would change the machine answers politely and does
     nothing. The buttons stay live on purpose — the page is here to show
     how it is used, and a control you cannot press teaches nothing. */
  var WRITES = ['/api/spawn', '/api/exit', '/api/kill', '/api/jump',
                '/api/annotate', '/api/jserr'];

  function json(body, status) {
    return new Response(JSON.stringify(body), {
      status: status || 200, headers: { 'Content-Type': 'application/json' },
    });
  }

  var realFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    var url = typeof input === 'string' ? input : (input && input.url) || '';
    var path = url.split('?')[0].replace(/^https?:\/\/[^/]+/, '');

    if (WRITES.indexOf(path) !== -1) {
      window.dispatchEvent(new CustomEvent('demo:blocked', { detail: path }));
      if (path === '/api/spawn') {
        return Promise.resolve(json({ ok: false, demo: true,
          error: tx('demo mode — launch stopped here; no agent was started') },
        400));
      }
      return Promise.resolve(json({ ok: false, demo: true,
        error: tx('demo mode — nothing was started, stopped or changed') },
      400));
    }
    if (path === '/api/suggest-name') {
      var sq = new URLSearchParams(url.split('?')[1] || '');
      var suggestion = suggestSpawnName(sq);
      return Promise.resolve(json(suggestion, suggestion.name ? 200 : 409));
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

  /* Every surname the bundle has to carry: the cast of every story, plus the
     scientists the launch form offers. This used to be re-derived by regex in
     three places — build.sh, the tests, and by eye — and they disagreed: a
     story's cast shipped no portraits at all, and the launch form offered a
     name whose picture was not in the bundle. One list, three readers. */
  function bundleSurnames() {
    var out = {};
    Object.keys(STORIES).forEach(function (id) {
      var st = STORIES[id];
      st.cast.concat(st.past || []).forEach(function (a) {
        var m = /^[A-Z][a-z]+([A-Z][A-Za-z]+)$/.exec(a.name);
        if (m) out[m[1]] = true;
      });
    });
    DEMO_SPAWN_SCIENTISTS.forEach(function (n) { out[n] = true; });
    return Object.keys(out).sort();
  }

  window.AGENTSTACK_DEMO = { loop: function () { return LOOP; },
                             cast: function () { return CAST; },
                             script: function () { return SCRIPT; },
                             lang: lang, setLang: setLang, translate: tx,
                             restart: restart,
                             opensAt: function () { return OPENS_AT; },
                             beats: function () { return BEATS; },
                             stories: STORIES, story: function () { return STORY; },
                             bundleSurnames: bundleSurnames,
                             useStory: useStory,
                             translatable: translatable,
                             portraitURL: portraitURL, assetURL: assetURL, phase: phase,
                             payloads: { agents: agentsPayload,
                                         graph: graphPayload,
                                         history: historyPayload,
                                         deliverables: deliverables,
                                         edgeMessages: edgeMessages,
                                         agentHistory: agentHistory,
                                         messagesSince: messagesSince,
                                         spawnNames: spawnNamesPayload,
                                         suggestName: suggestSpawnName,
                                         spawnDirectories: spawnDirectorySuggestions,
                                         nameStatus: spawnNameStatus } };
})();
