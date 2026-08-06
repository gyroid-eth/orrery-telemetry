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

  /* ── the cast ────────────────────────────────────────────────────────
     `born` is when the agent first appears, `dies` when it stops running
     (it stays on screen as a finished husk, which is what a real one does).
     Times are seconds into the loop. */
  var CAST_MIGRATION = [
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
  var PAST_MIGRATION = [
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
  var SCRIPT_MIGRATION = [
    { at: 16, from: 'AmberKepler', to: 'SlateHooke',
      subject: 'Task: map the old schema field by field',
      importance: 'high',
      body:
        'Read store/schema_v1.py against store/schema_v2.py and write docs/mapping.md, one row per field.\n' +
        '\n' +
        'What I want back is the list of fields with no counterpart — not the ones that map cleanly. Change no code: the tests go in before anything moves.\n' +
        '\n' +
        'Reserve docs/mapping.md so the docs child cannot write over you.',
      body_ja:
        'store/schema_v1.py と store/schema_v2.py を突き合わせて、docs/mapping.md にフィールド1行ずつの対応表を書いてください。\n' +
        '\n' +
        '欲しいのは、きれいに対応する側ではなく「対応先が無いフィールド」の一覧です。コードは変更しないこと。データを動かす前にテストを入れます。\n' +
        '\n' +
        'docs/mapping.md は予約してください。ドキュメント担当と衝突します。' },
    { at: 34, from: 'SlateHooke', to: 'AmberKepler',
      subject: 'RE: three fields have no counterpart — listing them',
      importance: 'normal',
      body:
        'legacy_ref — dropped. No reader since the v1.4 cleanup. Safe.\n' +
        'shard_hint — dropped. Only the old partitioner read it. Safe.\n' +
        'retry_budget — no counterpart, but still written from two places: store/migrate.py and the queue consumer. Whichever runs second wins.\n' +
        '\n' +
        'The third is the risk. The other two are bookkeeping.',
      body_ja:
        'legacy_ref — 廃止。v1.4 の整理以降、読み手はいません。安全です。\n' +
        'shard_hint — 廃止。旧パーティショナだけが読んでいました。安全です。\n' +
        'retry_budget — 対応先が無いのに、store/migrate.py とキューのコンシューマの2箇所から今も書かれています。後に走った方が勝ちます。\n' +
        '\n' +
        'リスクは3つ目です。前の2つは帳簿上の処理にすぎません。' },
    { at: 41, from: 'AmberKepler', to: 'IvoryNoether',
      subject: 'Task: cover the mapping before anything moves',
      importance: 'high', ack: true,
      body:
        'docs/mapping.md is up — 38 rows, 3 without a counterpart.\n' +
        '\n' +
        'Assert the three unmapped ones first. Ask the schema child which to start with rather than guessing; it has read both sides.\n' +
        '\n' +
        'Green tests are the gate for the release on the other rail, so this is on the critical path.',
      body_ja:
        'docs/mapping.md ができました。38行、うち3つが対応先なしです。\n' +
        '\n' +
        'まず対応先の無い3つを assert してください。どれから始めるかは推測せず、両側を読んでいるスキーマ担当に聞くこと。\n' +
        '\n' +
        'テストが緑になることが、もう一方の系統のリリース条件です。クリティカルパス上にあります。' },
    { at: 62, from: 'MossSomerville', to: 'FlintGauss',
      subject: 'Task: run the release checks on the current branch',
      importance: 'high',
      body:
        'Run ./scripts/release-check.sh and report what fails.\n' +
        '\n' +
        'Do not retry a failure to see whether it goes away. If something fails, run it again with -v and tell me whether it reproduces. A migration is in flight, so a failing check is more likely real than flaky.',
      body_ja:
        './scripts/release-check.sh を回して、落ちた項目を報告してください。\n' +
        '\n' +
        '落ちたものを「消えるかどうか」再実行で確かめるのは禁止です。落ちたら -v を付けてもう一度回し、再現するかどうかを報告すること。移行の最中なので、落ちた検査は flake より本物である確率の方が高い。' },
    { at: 78, from: 'IvoryNoether', to: 'SlateHooke',
      subject: 'Which of the three do you want asserted first?',
      importance: 'normal',
      body:
        'You listed legacy_ref, shard_hint and retry_budget. I can cover all three, but the order decides what the parent sees first if I run short.\n' +
        '\n' +
        'My read is retry_budget, because it is the only one with live writers. Confirm or correct me.',
      body_ja:
        'legacy_ref・shard_hint・retry_budget の3つを挙げてもらいました。全部やりますが、順序次第で親が最初に見るものが変わります。\n' +
        '\n' +
        '私の読みは retry_budget です。生きた書き手があるのはこれだけなので。合っていれば確認を、違えば訂正をください。' },
    { at: 88, from: 'SlateHooke', to: 'IvoryNoether',
      subject: 'RE: the one with two writers — that is where it breaks',
      importance: 'normal',
      body:
        'retry_budget, yes.\n' +
        '\n' +
        'store/migrate.py sets it from the v1 column; the queue consumer sets it from its own default. Neither knows about the other. Under v1 that was harmless because the reader took whichever was non-null.\n' +
        '\n' +
        'Assert that a value written by the migration survives a consumer pass.',
      body_ja:
        'retry_budget です。\n' +
        '\n' +
        'store/migrate.py は v1 のカラムから、キューのコンシューマは自前の既定値から、それぞれ書きます。互いを知りません。v1 では読み手が non-null の方を採っていたので無害でした。\n' +
        '\n' +
        '「移行が書いた値がコンシューマを1周しても残る」ことを assert してください。' },
    { at: 96, from: 'AmberKepler', to: 'RustPasteur',
      subject: 'Task: write the upgrade note for operators',
      importance: 'normal',
      body:
        'docs/mapping.md has the field-level detail. Write docs/upgrade-v2.md for someone running the upgrade at 2am.\n' +
        '\n' +
        'What breaks and what to do about it, in that order. The schema table belongs at the bottom.\n' +
        '\n' +
        'Three fields are being dropped; say what happens to their values.',
      body_ja:
        'フィールド単位の詳細は docs/mapping.md にあります。深夜2時に作業する人向けに docs/upgrade-v2.md を書いてください。\n' +
        '\n' +
        '「何が壊れるか」「どうすればいいか」の順で。スキーマの表は末尾に置くこと。\n' +
        '\n' +
        '廃止するフィールドが3つあります。その値がどうなるかを明記してください。' },
    { at: 118, from: 'FlintGauss', to: 'MossSomerville',
      subject: 'RE: one check fails — it is the gate, not a flake',
      importance: 'high',
      body:
        '7 checks, 6 ok, 1 failed: schema-compat.\n' +
        '\n' +
        'Re-ran with -v. Same failure both times: a v1 payload is rejected by the v2 reader. Not timing, not ordering.\n' +
        '\n' +
        'This is the check doing its job — the migration is not finished. I would not release on this.',
      body_ja:
        '検査7件、6件 ok、1件 fail: schema-compat。\n' +
        '\n' +
        '-v を付けて再実行しました。2回とも同じ落ち方です。v1 のペイロードが v2 のリーダーに弾かれています。タイミングでも順序でもありません。\n' +
        '\n' +
        '検査が仕事をしている状態です。移行が終わっていません。この状態ではリリースしません。' },
    { at: 129, from: 'MossSomerville', to: 'AmberKepler',
      subject: 'Holding the release until the mapping lands',
      importance: 'normal',
      body:
        'schema-compat fails and reproduces, so the gate stays shut on my side.\n' +
        '\n' +
        'No action needed from you — this is the correct order. Tell me when the mapping is covered and I will re-run.',
      body_ja:
        'schema-compat が落ち、再現もするので、こちら側のゲートは閉じたままにします。\n' +
        '\n' +
        'そちらの対応は不要です。順序としてこれで正しい。対応表がテストで固まったら教えてください、再実行します。' },
    { at: 146, from: 'IvoryNoether', to: 'AmberKepler',
      subject: 'RE: tests are green, including the two-writer case',
      importance: 'high',
      body:
        '24 passed, including the two-writer case.\n' +
        '\n' +
        'One real bug on the way: the consumer overwrote a value the migration had set. Fixed in store/migrate.py by writing retry_budget last. The test that caught it is tests/mapping/test_retry_budget.py.\n' +
        '\n' +
        'That failure belonged to the mapping, not to the test.',
      body_ja:
        '24件パス。書き手が2つあるケースも含みます。\n' +
        '\n' +
        '途中で本物のバグが1件出ました。コンシューマが、移行の書いた値を上書きしていました。retry_budget を最後に書くよう store/migrate.py を修正済みです。捕まえたテストは tests/mapping/test_retry_budget.py。\n' +
        '\n' +
        'あの fail はテストの問題ではなく、対応表の問題でした。' },
    { at: 168, from: 'RustPasteur', to: 'AmberKepler',
      subject: 'RE: draft is up — one open question about defaults',
      importance: 'normal',
      body:
        'docs/upgrade-v2.md, 96 lines. Structure: what changes, what to do, then the field table.\n' +
        '\n' +
        'One thing I cannot decide alone — the defaults for the three dropped fields. Document them as historical, or remove them outright? Documenting is safer for anyone reading an old dump; removing is cleaner.\n' +
        '\n' +
        'I have left them in, marked.',
      body_ja:
        'docs/upgrade-v2.md、96行です。構成は「何が変わるか」「何をするか」、そのあとにフィールドの表。\n' +
        '\n' +
        '1点だけ独断できません。廃止する3フィールドの既定値を、履歴として文書に残すか、消し切るか。残す方が古いダンプを読む人には安全で、消す方が読み物としてはきれいです。\n' +
        '\n' +
        'いまは印を付けて残してあります。' },
    { at: 181, from: 'IvoryNoether', to: 'AmberKepler',
      subject: 'Done. Handing back.',
      importance: 'normal',
      body:
        'Nothing outstanding. tests/mapping/ is committed, and docs/two-writers.md explains the failure for whoever reads it next.\n' +
        '\n' +
        'Exiting.',
      body_ja:
        '未処理はありません。tests/mapping/ はコミット済み、docs/two-writers.md に次に読む人向けの説明を残しました。\n' +
        '\n' +
        '終了します。' },
    { at: 203, from: 'AmberKepler', to: 'MossSomerville',
      subject: 'Mapping is covered — the gate can open',
      importance: 'high',
      body:
        '38 rows mapped, the three unmapped ones asserted, 24 tests green. The two-writer bug is fixed rather than worked around.\n' +
        '\n' +
        'Safe to re-run schema-compat.',
      body_ja:
        '38行を対応付け、対応先の無い3つも assert 済み、テスト24件が緑です。書き手が2つある問題は回避ではなく修正しました。\n' +
        '\n' +
        'schema-compat を再実行して大丈夫です。' },
    { at: 221, from: 'SlateHooke', to: 'AmberKepler',
      subject: 'RE: mapping is complete, nothing unresolved',
      importance: 'normal',
      body:
        'docs/mapping.md is final. Nothing unresolved on my side.\n' +
        '\n' +
        'pytest tests/mapping: 24 passed against the finished table.',
      body_ja:
        'docs/mapping.md は確定です。こちら側に未解決はありません。\n' +
        '\n' +
        'pytest tests/mapping: 完成した表に対して24件パス。' },
  ];

  /* ── the narration ───────────────────────────────────────────────────
     What the strip says, and what it rings while saying it. `at` is seconds
     into the loop; a beat runs until the next one starts. `look` is a CSS
     selector for the thing being described — one that matches nothing costs
     the ring, not the caption. Beats belong to the story, not to the tour,
     so a second story narrates itself. */
  var BEATS_MIGRATION = [
    { at: 0, look: '.gauge.run',
      en: 'Two orchestrators, nothing delegated yet. Every number on this ' +
          'strip is read from the machines, not reported by the agents.',
      ja: '親エージェントが2体、まだ何も委任していません。上の数字はエージェントの' +
          '自己申告ではなく、実機から読んだ値です。' },
    { at: 14, look: '.bay[data-name="SlateHooke"]',
      en: 'The first orchestrator starts a child. A card appears the moment ' +
          'the process does — nothing had to announce itself.',
      ja: '1体目の親が子を起動しました。プロセスが立った瞬間にカードが現れます。' +
          '何かが名乗り出る必要はありません。' },
    { at: 33, look: '#v-net', view: 'net',
      en: 'The child answers its parent. In Network view that reply is a ' +
          'line between them.',
      ja: '子が親に返信しました。Network 表示では、その返信が2体を結ぶ線になります。' },
    { at: 40, look: '.bay[data-name="IvoryNoether"]',
      en: 'A second child, for tests. The ring on each card is its context ' +
          'window filling up.',
      ja: '2体目の子はテスト担当です。カードのリングは、その子のコンテキスト窓が' +
          '埋まっていく様子を示します。' },
    { at: 60, look: '.bay[data-name="FlintGauss"]',
      en: 'A third child on the other rail. Running versus standby is the ' +
          'one number worth watching on a busy day.',
      ja: 'もう一方の系統で3体目が動き出しました。忙しい日に見るべき数字は、' +
          '稼働中と待機中の比です。' },
    { at: 95, look: '.bay[data-name="RustPasteur"]',
      en: 'Four children now. Click any card to read what that agent is ' +
          'actually doing, line by line.',
      ja: '子が4体になりました。カードを押すと、そのエージェントが実際に何をしているかを' +
          '1行ずつ読めます。' },
    { at: 118, look: '#v-net', view: 'net',
      en: 'A release check fails and the second orchestrator holds the gate. ' +
          'The traffic that decided it is on the graph.',
      ja: 'リリース検査が1件落ち、2体目の親がゲートを閉じました。その判断に至った' +
          'やり取りはグラフ上に残っています。' },
    { at: 146, look: '.bay[data-name="IvoryNoether"]',
      en: 'Tests come back green. The context rings show which agents have ' +
          'room left and which are nearly full.',
      ja: 'テストが緑で返ってきました。リングを見れば、どの子にまだ余裕があり、' +
          'どの子がもう一杯かが分かります。' },
    { at: 182, look: '.bay[data-name="IvoryNoether"]',
      en: 'A child finishes. It stops running but stays on screen — what it ' +
          'did is still there to read.',
      ja: '子が1体終わりました。稼働は止まりますが画面には残り、何をしたかは' +
          'あとから読めます。' },
    { at: 203, look: '.gauge.tot',
      en: 'The gate opens and the work lands. In a moment this starts over.',
      ja: 'ゲートが開き、作業が入りました。まもなく最初から繰り返します。' },
  ];

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

  /* ── the transcripts ─────────────────────────────────────────────────
     Opening a pane is how anyone finds out what an agent is actually
     doing, so an empty one reads as "there is nothing to see in this
     product". These are the same six jobs the mail script describes, told
     from the inside. `at` is seconds into the loop; lines past the current
     moment have not been said yet, so a pane opened late shows more than
     one opened early — which is the behaviour of the real thing.

     tool_use text is "name  args", two spaces, the way the page splits it. */
  var TRANSCRIPTS_MIGRATION = {
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
  var DELIVERABLES_MIGRATION = {
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
  var JA_MIGRATION = (function () {
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
    return out;
  }

  /* Registered here rather than beside the declarations above: `var` hoists
     the name but not the value, so a story assembled before its transcripts
     and deliverables exist gets undefined for both — and useStory would then
     hand the payload builders nothing at all. */
  STORIES.migration = {
    id: 'migration',
    label: { en: 'Schema migration', ja: 'スキーマ移行' },
    loop: 240,
    /* Where a visitor comes in. This was 108 — far enough along that all
       four children already existed, the screen looked busy, and the one
       thing the product is about, a parent handing work to a child, was 147
       seconds away. Land just before the first spawn instead: the two
       orchestrators are already working, so it is not an empty page. */
    opensAt: 9,
    cast: CAST_MIGRATION, past: PAST_MIGRATION, script: SCRIPT_MIGRATION,
    transcripts: TRANSCRIPTS_MIGRATION, deliverables: DELIVERABLES_MIGRATION,
    beats: BEATS_MIGRATION, ja: JA_MIGRATION,
  };

  var STORY, CAST, PAST, SCRIPT, TRANSCRIPTS, DELIVERABLES, JA, BEATS;
  var LOOP, OPENS_AT, START;

  function useStory(id) {
    var st = STORIES[id] || STORIES.migration;
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

  useStory(params.get('story') || 'migration');

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

  window.AGENTSTACK_DEMO = { loop: function () { return LOOP; },
                             cast: function () { return CAST; },
                             script: function () { return SCRIPT; },
                             lang: lang, setLang: setLang, translate: tx,
                             restart: restart,
                             opensAt: function () { return OPENS_AT; },
                             beats: function () { return BEATS; },
                             stories: STORIES, story: function () { return STORY; },
                             useStory: useStory,
                             translatable: translatable,
                             portraitURL: portraitURL, assetURL: assetURL, phase: phase,
                             payloads: { agents: agentsPayload,
                                         graph: graphPayload,
                                         history: historyPayload,
                                         deliverables: deliverables,
                                         edgeMessages: edgeMessages,
                                         agentHistory: agentHistory,
                                         messagesSince: messagesSince } };
})();
