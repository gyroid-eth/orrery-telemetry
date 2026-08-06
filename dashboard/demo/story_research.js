/* Decision: follow one claim from literature through a meeting, raw-data QC,
 * manuscript revision, and collaborator handoff so the four-minute loop stays legible. */
(function () {
  'use strict';

  window.AGENTSTACK_STORIES = window.AGENTSTACK_STORIES || {};

  var CAST = [
    { name: 'AmberKepler', role: 'research lead', emoji: '🔬', group: 'demo',
      model: 'Opus 5', model_raw: 'claude-opus-5', provider: 'anthropic',
      program: 'claude-code', born: 0, dies: null, ctx0: 34, ctxRate: 0.05,
      task: 'Turn today\'s evidence into a claim the manuscript can defend' },
    { name: 'MossSomerville', role: 'meeting lead', emoji: '🧭', group: 'demo',
      model: 'Opus 5', model_raw: 'claude-opus-5', provider: 'anthropic',
      program: 'claude-code', born: 0, dies: null, ctx0: 46, ctxRate: 0.04,
      task: 'Accompany the meeting and own the collaborator handoff' },
    { name: 'SlateHooke', parent: 'AmberKepler', role: 'literature', emoji: '📚',
      model: 'GPT 5.6', model_raw: 'gpt-5.6', provider: 'openai',
      program: 'codex', born: 15, dies: null, ctx0: 10, ctxRate: 0.13,
      task: 'Find the closest primary evidence and record where it stops' },
    { name: 'IvoryNoether', parent: 'AmberKepler', role: 'analysis', emoji: '📈',
      model: 'GPT 5.6', model_raw: 'gpt-5.6', provider: 'openai',
      program: 'codex', born: 44, dies: 182, ctx0: 8, ctxRate: 0.24,
      states: [[125, 151, 'ask']],
      task: 'Rebuild the response curve from raw measurements and audit anomalies' },
    { name: 'CoralFaraday', parent: 'MossSomerville', role: 'companion', emoji: '🎙️',
      model: 'Sonnet 5', model_raw: 'claude-sonnet-5', provider: 'anthropic',
      program: 'claude-code', born: 64, dies: 190, ctx0: 7, ctxRate: 0.18,
      task: 'Track the live discussion and surface evidence without inventing conclusions' },
    { name: 'RustPasteur', parent: 'AmberKepler', role: 'manuscript', emoji: '✍️',
      model: 'Sonnet 5', model_raw: 'claude-sonnet-5', provider: 'anthropic',
      program: 'claude-code', born: 96, dies: null, ctx0: 6, ctxRate: 0.11,
      states: [[175, 194, 'question']],
      task: 'Revise the Results section and figure around verified statements' },
  ];

  var PAST = [
    { name: 'CedarLovelace', parent: 'AmberKepler', model: 'GPT 5.6',
      model_raw: 'gpt-5.6', provider: 'openai', program: 'codex',
      task: 'Audit the measurement protocol before the new run',
      retired: true, ago: 5800 },
    { name: 'OchreCurie', parent: 'MossSomerville', model: 'Sonnet 5',
      model_raw: 'claude-sonnet-5', provider: 'anthropic',
      program: 'claude-code',
      task: 'Trace the origin of the reference correction',
      retired: true, ago: 10800 },
  ];

  var SCRIPT = [
    { at: 16, from: 'AmberKepler', to: 'SlateHooke',
      subject: 'Task: build an evidence map for the recovery claim',
      importance: 'high', ack: true,
      body:
        'Find primary studies that bear on recovery after flow is pulsed, then write notes/evidence-map.md.\n' +
        '\n' +
        'For each study, separate what was measured from what the authors inferred. I need the nearest evidence and the boundary beyond which it stops supporting us. Do not turn a related mechanism into direct precedent.',
      body_ja:
        '流れをパルス状に与えた後の回復に関係する一次研究を探し、notes/evidence-map.md にまとめてください。\n' +
        '\n' +
        '各研究について、測定されたことと著者の推論を分けます。欲しいのは最も近い根拠と、それ以上は支えられない境界です。関連する機構を直接の先行例にすり替えないこと。' },
    { at: 35, from: 'SlateHooke', to: 'AmberKepler',
      subject: 'RE: six studies mapped, one supports only the mechanism',
      importance: 'normal',
      body:
        'Six primary studies mapped. Two report recovery under steady forcing; only one separates signal loss from redistribution.\n' +
        '\n' +
        'None uses a pulsed exposure sequence like ours. The closest paper supports a possible mechanism, not our measured plateau. I marked that boundary in every row of the evidence map.',
      body_ja:
        '一次研究6本を対応付けました。定常的な負荷の下で回復を報告するものが2本あり、信号低下と再分布を分けているのは1本だけです。\n' +
        '\n' +
        'こちらのようなパルス露光系列を使った研究はありません。最も近い論文が支えるのは機構の可能性で、測定したプラトーそのものではありません。根拠の境界を対応表の各行に記しました。' },
    { at: 45, from: 'AmberKepler', to: 'IvoryNoether',
      subject: 'Task: rebuild the curve from raw measurements',
      importance: 'high', ack: true,
      body:
        'Rebuild Figure 3 from runs/pulse-set/*.csv and the acquisition metadata. Start with raw values, not the plotted table.\n' +
        '\n' +
        'Audit missing frames, exposure changes, and normalization boundaries. Leave analysis/curve-audit.md beside the figure so the writing child can see which comparisons survive.',
      body_ja:
        'runs/pulse-set/*.csv と取得メタデータから Figure 3 を作り直してください。プロット済みの表ではなく、生値から始めます。\n' +
        '\n' +
        '欠損フレーム、露光変更、正規化の境界を監査してください。執筆担当がどの比較なら残せるか分かるよう、図の横に analysis/curve-audit.md を置きます。' },
    { at: 65, from: 'MossSomerville', to: 'CoralFaraday',
      subject: 'Task: accompany the meeting and keep claims bounded',
      importance: 'high',
      body:
        'Follow the meeting transcript in short chunks. Capture decisions, open questions, and requests as different lists.\n' +
        '\n' +
        'When someone makes a literature claim, ask the literature child for the closest source and its limitation. Surface prior evidence in the moment, but do not silently turn discussion into a conclusion.',
      body_ja:
        'ミーティングの転写を短いチャンクごとに追ってください。決定事項、未決の問い、依頼を別々の一覧にします。\n' +
        '\n' +
        '文献に関する主張が出たら、最も近い出典とその限界を文献担当に確認します。その場で過去の根拠を出しますが、議論を黙って結論に変えないこと。' },
    { at: 74, from: 'CoralFaraday', to: 'SlateHooke',
      subject: 'Which source actually supports recovery after a pulse?',
      importance: 'normal',
      body:
        'The meeting just reached the sentence “recovery after a pulse is expected.”\n' +
        '\n' +
        'Which primary source is closest, and what did it actually impose and measure? I need one sentence we can say aloud now, plus the reason it is not direct validation.',
      body_ja:
        'ミーティングで「パルス後の回復は予想どおり」という文が出ました。\n' +
        '\n' +
        '最も近い一次資料はどれで、実際には何を与え、何を測ったのでしょうか。今その場で言える1文と、直接の検証ではない理由が必要です。' },
    { at: 82, from: 'SlateHooke', to: 'CoralFaraday',
      subject: 'RE: closest study used steady flow, not pulses',
      importance: 'normal',
      body:
        'The closest study measured redistribution after steady flow stopped. It did not alternate exposure or forcing.\n' +
        '\n' +
        'Safe sentence: “A related steady-flow study makes recovery plausible.” Unsafe sentence: “Prior work predicts our plateau.” The pulse-specific result still belongs to our data.',
      body_ja:
        '最も近い研究は、定常流を止めた後の再分布を測っています。露光も負荷も交互には切り替えていません。\n' +
        '\n' +
        '安全な言い方は「関連する定常流の研究から回復はあり得る」。危険なのは「先行研究がこちらのプラトーを予測する」です。パルス固有の結果は、まだこちらのデータに委ねるべきです。' },
    { at: 97, from: 'AmberKepler', to: 'RustPasteur',
      subject: 'Task: draft the Results around verified statements',
      importance: 'normal',
      body:
        'Open the evidence map and make a Results skeleton with three slots: observation, comparison, limitation.\n' +
        '\n' +
        'Do not copy the current effect size; analysis is rebuilding it from raw measurements. Draft the structure now, then replace Figure 3 and the response paragraph when the audited result arrives.',
      body_ja:
        '根拠の対応表を開き、「観察」「比較」「限界」の3枠で Results の骨組みを作ってください。\n' +
        '\n' +
        '現在の効果量は写さないこと。解析担当が生値から作り直しています。先に構造だけ起草し、監査済み結果が届いたら Figure 3 と回答段落を差し替えます。' },
    { at: 111, from: 'CoralFaraday', to: 'MossSomerville',
      subject: 'Meeting question: effect or camera correction?',
      importance: 'high',
      body:
        'The group agrees the direction is interesting, but nobody wants to call the plateau biological yet.\n' +
        '\n' +
        'They asked for a raw-versus-normalized panel and the exposure timeline on the same axis. I recorded this as an open question, not a decision: does the plateau survive acquisition correction?',
      body_ja:
        '方向性は興味深いという点で合意しましたが、まだ誰もプラトーを生物学的とは呼びたくありません。\n' +
        '\n' +
        '生値と正規化値を並べ、同じ軸に露光の時間線を載せるよう依頼されました。これは決定ではなく未決の問いとして記録しています。取得補正後もプラトーは残るのか。' },
    { at: 122, from: 'IvoryNoether', to: 'AmberKepler',
      subject: 'An exposure step is inside the reported effect',
      importance: 'high',
      body:
        'The apparent plateau begins at frame 480, exactly where exposure changes from 40 ms to 25 ms. The plotted table normalized both segments by one global mean.\n' +
        '\n' +
        'That makes the pre/post values incomparable as drawn. I have not changed the manuscript yet. Next I will calibrate each exposure segment, mask three dropped frames, and bootstrap by run.',
      body_ja:
        '見かけのプラトーは frame 480 から始まり、露光が 40 ms から 25 ms に変わる位置と完全に一致します。プロット済みの表は両区間を1つの全体平均で正規化していました。\n' +
        '\n' +
        'この図のままでは前後を比較できません。論文はまだ変更していません。次に露光区間ごとに較正し、欠損3フレームをマスクし、run 単位でブートストラップします。' },
    { at: 125, from: 'IvoryNoether', to: 'AmberKepler',
      subject: 'Approval needed: regenerate the derived figure inputs?',
      importance: 'high', ack: true,
      body:
        'The raw files are locked and unchanged. The next command will overwrite derived tables and the inputs currently feeding Figure 3.\n' +
        '\n' +
        'Approve a clean regeneration with exposure-specific calibration, three dropped frames masked, and uncertainty bootstrapped by run? I am stopped before the write.',
      body_ja:
        '生ファイルはロックされ、変更していません。次のコマンドは派生表と、現在 Figure 3 へ入っている入力を上書きします。\n' +
        '\n' +
        '露光ごとの較正、欠損3フレームのマスク、run 単位の不確かさブートストラップで、クリーンに再生成してよいでしょうか。書き込み前で停止しています。' },
    { at: 151, from: 'AmberKepler', to: 'IvoryNoether',
      subject: 'RE: correct by segment and report what remains',
      importance: 'high',
      body:
        'Good catch. Reprocess by exposure segment and keep the raw panel beside the corrected one.\n' +
        '\n' +
        'Hand back the effect size, uncertainty, and any exclusion separately. The question is not whether the old number can be rescued; it is what the measurements still support after the acquisition boundary is made visible.',
      body_ja:
        'よく気づきました。露光区間ごとに再処理し、補正後のパネルの横に生値も残してください。\n' +
        '\n' +
        '効果量、不確かさ、除外対象を分けて返してください。問うべきは古い数値を救えるかではなく、取得境界を見えるようにした後で測定が何を支えるかです。' },
    { at: 172, from: 'IvoryNoether', to: 'AmberKepler',
      subject: 'RE: corrected effect is smaller but still resolves',
      importance: 'high',
      body:
        'After per-segment calibration, the median change is 7%, with a run-level 95% interval of 4–10%. The old figure said 18%.\n' +
        '\n' +
        'The direction survives; the magnitude does not. One run has a timestamp gap and is excluded from the headline estimate but shown in gray. The cause remains unresolved.',
      body_ja:
        '区間ごとの較正後、変化量の中央値は 7%、run 単位の 95% 区間は 4–10% です。旧図は 18% としていました。\n' +
        '\n' +
        '方向は残りましたが、大きさは残りません。1 run は時刻の欠落があるため主要推定から除外し、灰色で表示します。原因は未決のままです。' },
    { at: 175, from: 'RustPasteur', to: 'AmberKepler',
      subject: 'Question: replace Figure 3 or annotate the old panel?',
      importance: 'high',
      body:
        'The corrected estimate changes the headline number and the visual shape. I need a choice before editing the manuscript.\n' +
        '\n' +
        'Option A replaces Figure 3 and withdraws 18%. Option B keeps the old panel with an acquisition warning. I recommend A because annotation cannot make the two exposure regimes comparable.',
      body_ja:
        '補正後の推定値によって主要数値と図の形が変わります。論文を編集する前に選択が必要です。\n' +
        '\n' +
        'A は Figure 3 を差し替え、18% を撤回します。B は旧パネルを残し、取得上の警告を付けます。注釈では2つの露光条件を比較可能にできないため、A を推奨します。' },
    { at: 181, from: 'IvoryNoether', to: 'AmberKepler',
      subject: 'Done. Audit, figure inputs, and checks handed back.',
      importance: 'normal',
      body:
        'analysis/curve-audit.md records the exposure boundary, dropped frames, excluded run, and bootstrap unit. Figure inputs are under figures/figure-3-data/.\n' +
        '\n' +
        'The rebuild command passes from raw inputs. Nothing remains open on the processing side; interpretation is deliberately left with the parent.',
      body_ja:
        'analysis/curve-audit.md に露光境界、欠損フレーム、除外 run、ブートストラップ単位を記録しました。図の入力は figures/figure-3-data/ にあります。\n' +
        '\n' +
        '生の入力から再構築するコマンドも通ります。処理側に未完はありません。解釈は意図的に親へ残します。' },
    { at: 189, from: 'CoralFaraday', to: 'MossSomerville',
      subject: 'Done. Decisions and open questions are separated.',
      importance: 'normal',
      body:
        'The meeting record has three decisions, two requests, and one open question. Every literature statement links to the evidence-map row that bounds it.\n' +
        '\n' +
        'The acquisition-correction question now points to the audited figure. No discussion-only statement is marked as a result. Handing back and exiting.',
      body_ja:
        'ミーティング記録には決定3件、依頼2件、未決の問い1件があります。文献に関する各記述は、その範囲を定める根拠対応表の行へリンクしています。\n' +
        '\n' +
        '取得補正の問いは監査済み図を参照するよう更新しました。議論だけの発言を結果として扱ってはいません。引き継いで終了します。' },
    { at: 194, from: 'AmberKepler', to: 'RustPasteur',
      subject: 'Replace Figure 3 and narrow the claim',
      importance: 'high',
      body:
        'Use the audited 7% estimate and 4–10% interval. Replace Figure 3 with raw, exposure timeline, and corrected panels. Show the excluded run in gray.\n' +
        '\n' +
        'The Results may say the change persists after correction. The Discussion may offer redistribution as one explanation, but must say the acquisition study did not identify cause.',
      body_ja:
        '監査済みの 7% 推定値と 4–10% 区間を使います。Figure 3 を、生値、露光の時間線、補正後の3パネルに差し替え、除外した run は灰色で示してください。\n' +
        '\n' +
        'Results では補正後も変化が残ると言えます。Discussion では再分布を説明候補にできますが、取得研究では原因を特定していないと明記します。' },
    { at: 210, from: 'RustPasteur', to: 'AmberKepler',
      subject: 'RE: figure replaced, causal language removed',
      importance: 'normal',
      body:
        'Figure 3 now shows all three panels and the excluded run. Results reports 7% with its interval; the response paragraph explains why 18% was withdrawn.\n' +
        '\n' +
        'I removed “demonstrates recovery” and wrote “remains consistent with recovery.” The unresolved cause is explicit in both the caption and collaborator note.',
      body_ja:
        'Figure 3 は3パネルと除外 run を示す形になりました。Results は 7% とその区間を報告し、回答段落では 18% を撤回した理由を説明しています。\n' +
        '\n' +
        '「回復を実証する」を削り、「回復と整合する」にしました。原因が未決であることはキャプションと共同研究者向けメモの両方に明記しています。' },
    { at: 214, from: 'AmberKepler', to: 'MossSomerville',
      subject: 'Task: send the collaborator brief with one open question',
      importance: 'high',
      body:
        'Package what we observed, what changed after QC, and what remains unresolved. Lead with the corrected estimate, not the story of how we found it.\n' +
        '\n' +
        'End with one next experiment: lock exposure and interleave a dark reference. Do not call the mechanism settled; the literature makes it plausible, not proven.',
      body_ja:
        '観察したこと、QC 後に変わったこと、未決のことをまとめてください。発見までの経緯ではなく、補正後の推定値を先頭に置きます。\n' +
        '\n' +
        '最後に次の実験を1つ示します。露光を固定し、暗参照を交互に入れること。機構は確定と呼ばないでください。文献が示すのは妥当性で、証明ではありません。' },
    { at: 226, from: 'MossSomerville', to: 'AmberKepler',
      subject: 'RE: brief sent — known, changed, unresolved, next',
      importance: 'high',
      body:
        'Brief sent in four blocks: 7% median change; old 18% withdrawn after exposure-aware QC; cause unresolved; next run locks exposure and interleaves a dark reference.\n' +
        '\n' +
        'The evidence link is framed as mechanism context only. The collaborator can see both the corrected figure and the reason the claim became narrower.',
      body_ja:
        '報告を4ブロックで送りました。変化量中央値 7%、露光を考慮した QC 後に旧 18% を撤回、原因は未決、次回は露光を固定して暗参照を交互に入れる、です。\n' +
        '\n' +
        '文献リンクは機構の文脈に限ると明示しました。共同研究者は補正済み図と、主張を狭めた理由の両方を確認できます。' },
    { at: 229, from: 'SlateHooke', to: 'AmberKepler',
      subject: 'Evidence map final — limitation carried into the brief',
      importance: 'normal',
      body:
        'The final evidence map points from each manuscript sentence to a primary source or to our audited figure. No sentence uses both as if they were interchangeable.\n' +
        '\n' +
        'The steady-flow study remains mechanism context, and the pulse-specific claim remains tied to our corrected measurement. Nothing unresolved on the literature side.',
      body_ja:
        '最終版の根拠対応表では、論文の各文を一次資料または監査済み図へ結びました。両者を交換可能な根拠として扱う文はありません。\n' +
        '\n' +
        '定常流の研究は機構の文脈に留め、パルス固有の主張は補正済み測定へ結びました。文献側に未処理はありません。' },
  ];

  var TRANSCRIPTS = {
    AmberKepler: [
      [0, 'user', 'text', 'Prepare today\'s collaborator update. Connect literature, meeting questions, raw analysis, and the manuscript without letting causal language outrun the evidence.'],
      [4, 'assistant', 'thinking', 'The dependency is evidence first, raw-data audit second, writing last. The meeting can run alongside them if every claim keeps its source boundary.'],
      [7, 'assistant', 'text', 'I will give literature, analysis, and writing to separate children. The second parent owns the live meeting and the final collaborator brief.'],
      [13, 'assistant', 'tool_use', 'Bash  agentstack spawn --role literature'],
      [14, 'assistant', 'tool_result', 'started SlateHooke (gpt-5.6)'],
      [16, 'assistant', 'tool_use', 'SendMessage  to=SlateHooke "build an evidence map"'],
      [37, 'assistant', 'thinking', 'The literature makes a mechanism plausible but does not validate this pulse sequence. Keep that boundary visible while analysis checks the number.'],
      [42, 'assistant', 'tool_use', 'Bash  agentstack spawn --role analysis'],
      [43, 'assistant', 'tool_result', 'started IvoryNoether (gpt-5.6)'],
      [45, 'assistant', 'tool_use', 'SendMessage  to=IvoryNoether "rebuild the curve from raw measurements"'],
      [94, 'assistant', 'tool_use', 'Bash  agentstack spawn --role manuscript'],
      [95, 'assistant', 'tool_result', 'started RustPasteur (claude-sonnet-5)'],
      [124, 'assistant', 'thinking', 'The anomaly lands on the exposure boundary. The old effect size is unusable until the segments are calibrated separately.'],
      [151, 'assistant', 'tool_use', 'SendMessage  to=IvoryNoether "correct by segment and report what remains"'],
      [173, 'assistant', 'text', 'The direction survives correction, but the magnitude is much smaller. That is a useful result and a narrower claim.'],
      [194, 'assistant', 'tool_use', 'SendMessage  to=RustPasteur "replace Figure 3 and narrow the claim"'],
      [214, 'assistant', 'tool_use', 'SendMessage  to=MossSomerville "send the collaborator brief"'],
      [230, 'assistant', 'text', 'The brief is out with one result, one correction, one open cause, and one next experiment. Handing back.'],
    ],
    MossSomerville: [
      [0, 'user', 'text', 'Accompany the meeting and make the collaborator handoff distinguish results from open questions.'],
      [3, 'assistant', 'text', 'I will keep decisions, requests, and open questions separate, then report only what survives the evidence and analysis rails.'],
      [62, 'assistant', 'tool_use', 'Bash  agentstack spawn --role companion'],
      [63, 'assistant', 'tool_result', 'started CoralFaraday (claude-sonnet-5)'],
      [65, 'assistant', 'tool_use', 'SendMessage  to=CoralFaraday "accompany the meeting and keep claims bounded"'],
      [113, 'assistant', 'tool_result', 'CoralFaraday: effect or camera correction?'],
      [115, 'assistant', 'thinking', 'That is the right unresolved question. Ask analysis for a figure that exposes the acquisition boundary instead of debating it from the normalized plot.'],
      [191, 'assistant', 'text', 'Meeting record closed: decisions and open questions are separate, and every literature statement carries its limitation.'],
      [215, 'assistant', 'tool_result', 'AmberKepler: send the collaborator brief with one open question'],
      [217, 'assistant', 'thinking', 'Lead with the corrected estimate. The provenance matters, but the collaborator needs the result before the audit trail.'],
      [222, 'assistant', 'tool_use', 'Write  reports/collaborator-brief.md'],
      [223, 'assistant', 'tool_result', 'wrote four blocks'],
      [226, 'assistant', 'text', 'Brief sent: known, changed after QC, unresolved, and next experiment.'],
    ],
    SlateHooke: [
      [15, 'user', 'text', 'Find the closest primary evidence and record where it stops.'],
      [18, 'assistant', 'tool_use', 'Search  query="pulsed flow recovery measurement artifact"'],
      [21, 'assistant', 'tool_result', '27 candidates · 6 primary studies retained'],
      [23, 'assistant', 'tool_use', 'Read  papers/candidate-04.pdf'],
      [26, 'assistant', 'tool_result', 'steady flow · redistribution measured after stop'],
      [29, 'assistant', 'thinking', 'The closest experiment changes the forcing, not the exposure. It supports a mechanism comparison but cannot predict the pulse-specific plateau.'],
      [31, 'assistant', 'tool_use', 'Write  notes/evidence-map.md'],
      [32, 'assistant', 'tool_result', 'wrote 6 rows with claim boundaries'],
      [35, 'assistant', 'text', 'Six studies mapped. The closest supports only a possible mechanism, not the measured plateau.'],
      [76, 'assistant', 'tool_result', 'CoralFaraday: which source supports recovery after a pulse?'],
      [78, 'assistant', 'thinking', 'Answer with what was imposed and measured, then state the mismatch before the sentence can grow stronger in the meeting.'],
      [80, 'assistant', 'tool_use', 'SendMessage  to=CoralFaraday "closest study used steady flow, not pulses"'],
      [83, 'assistant', 'text', 'The safe comparison is now in the meeting: plausible mechanism, no direct validation.'],
      [226, 'assistant', 'tool_use', 'Bash  node scripts/check-claim-links.js'],
      [227, 'assistant', 'tool_result', '12 claims linked · 0 unbounded'],
      [229, 'assistant', 'text', 'Evidence map final. The limitation carried through to the collaborator brief.'],
    ],
    IvoryNoether: [
      [44, 'user', 'text', 'Rebuild the response curve from raw measurements and audit anomalies.'],
      [48, 'assistant', 'tool_use', 'Read  runs/pulse-set/acquisition.json'],
      [49, 'assistant', 'tool_result', '12 runs · 960 frames · 2 exposure segments'],
      [53, 'assistant', 'tool_use', 'Bash  python analysis/rebuild_curve.py --raw'],
      [72, 'assistant', 'tool_result', 'figure written · 3 dropped frames flagged'],
      [117, 'assistant', 'thinking', 'The curve step and the exposure step share frame 480. A global mean has made the two acquisition regimes look directly comparable.'],
      [121, 'assistant', 'tool_use', 'Write  analysis/curve-audit.md'],
      [122, 'assistant', 'text', 'The reported effect contains an exposure boundary. I am stopping the manuscript number and reporting the anomaly first.'],
      [125, 'assistant', 'text', 'Approval requested before derived tables and Figure 3 inputs are regenerated. Raw measurements remain locked.'],
      [152, 'assistant', 'tool_result', 'AmberKepler approved clean regeneration by exposure segment'],
      [154, 'assistant', 'tool_use', 'Bash  python analysis/rebuild_curve.py --calibrate-by-exposure --mask-drops --bootstrap-by-run'],
      [170, 'assistant', 'tool_result', 'median=0.07 · interval=0.04..0.10 · excluded=1 run'],
      [172, 'assistant', 'text', 'Corrected effect is smaller but still resolves: 7%, with a run-level 95% interval of 4–10%.'],
      [177, 'assistant', 'tool_use', 'Bash  python -m pytest analysis/tests'],
      [178, 'assistant', 'tool_result', '18 passed'],
      [181, 'assistant', 'text', 'Audit, figure inputs, and checks handed back. Interpretation remains with the parent.'],
    ],
    CoralFaraday: [
      [64, 'user', 'text', 'Track the live discussion and surface evidence without inventing conclusions.'],
      [68, 'assistant', 'tool_use', 'Read  meetings/live/chunk-07.txt'],
      [69, 'assistant', 'tool_result', 'four speakers · one literature claim · one request'],
      [72, 'assistant', 'thinking', 'The phrase “expected recovery” needs a source now, before repetition turns it into a settled premise.'],
      [74, 'assistant', 'tool_use', 'SendMessage  to=SlateHooke "which source supports recovery after a pulse?"'],
      [84, 'assistant', 'text', 'I surfaced the closest evidence with its steady-flow limitation while the question was still on screen.'],
      [108, 'assistant', 'thinking', 'The group is asking whether the plateau is physical or introduced by correction. That belongs under open questions, with a concrete figure request.'],
      [111, 'assistant', 'text', 'Recorded one open question: does the plateau survive acquisition correction?'],
      [185, 'assistant', 'tool_use', 'Write  meetings/question-log.md'],
      [186, 'assistant', 'tool_result', '3 decisions · 2 requests · 1 open question'],
      [189, 'assistant', 'text', 'Decisions and open questions are separated. Handing back and exiting.'],
    ],
    RustPasteur: [
      [96, 'user', 'text', 'Revise the Results section and figure around verified statements.'],
      [100, 'assistant', 'tool_use', 'Read  notes/evidence-map.md'],
      [101, 'assistant', 'tool_result', '6 rows · 1 mechanism comparison · 0 direct precedents'],
      [105, 'assistant', 'thinking', 'Write the structure before the number arrives: observation, bounded comparison, limitation. Leave the effect size blank.'],
      [109, 'assistant', 'tool_use', 'Write  manuscript/results.md'],
      [110, 'assistant', 'tool_result', 'wrote outline with one numeric placeholder'],
      [175, 'assistant', 'text', 'I need a human choice: replace Figure 3 and withdraw 18%, or keep the old panel with an acquisition warning. I recommend replacement.'],
      [195, 'assistant', 'tool_result', 'AmberKepler: replace Figure 3 and narrow the claim'],
      [198, 'assistant', 'tool_use', 'Edit  figures/figure-3.svg'],
      [205, 'assistant', 'tool_result', 'raw + exposure + corrected panels written'],
      [206, 'assistant', 'tool_use', 'Edit  manuscript/results.md'],
      [207, 'assistant', 'thinking', 'The direction survives, but causality does not. “Consistent with” is the strongest phrase shared by data and literature.'],
      [210, 'assistant', 'text', 'Figure replaced, effect narrowed to 7%, and causal language removed.'],
    ],
    CedarLovelace: [
      [0, 'user', 'text', 'Audit the measurement protocol before the new run.'],
      [0, 'assistant', 'tool_use', 'Read  protocols/acquisition.md'],
      [0, 'assistant', 'tool_result', '14 steps · 2 operator choices'],
      [0, 'assistant', 'text', 'Two operator choices can change exposure. Both are flagged in the protocol audit.'],
    ],
    OchreCurie: [
      [0, 'user', 'text', 'Trace the origin of the reference correction.'],
      [0, 'assistant', 'tool_use', 'Bash  git log -S global_mean -- analysis/'],
      [0, 'assistant', 'tool_result', 'introduced three revisions ago'],
      [0, 'assistant', 'text', 'The global-mean correction predates the pulse sequence and was never revalidated for exposure changes.'],
    ],
  };

  var DELIVERABLES = {
    AmberKepler: [['Decision trail for the claim', 'notes/decision-trail.md', 120]],
    MossSomerville: [['Collaborator brief', 'reports/collaborator-brief.md', 30]],
    SlateHooke: [['Evidence map with claim boundaries', 'notes/evidence-map.md', 240]],
    IvoryNoether: [['Raw-to-corrected curve audit', 'analysis/curve-audit.md', 90],
                   ['Audited Figure 3 inputs', 'figures/figure-3-data/', 80]],
    CoralFaraday: [['Meeting decisions and open questions', 'meetings/question-log.md', 60]],
    RustPasteur: [['Revised Results section', 'manuscript/results.md', 45],
                  ['Corrected three-panel Figure 3', 'figures/figure-3.svg', 45]],
    CedarLovelace: [['Measurement protocol audit', 'protocols/acquisition-audit.md', 5800]],
    OchreCurie: [['Reference-correction provenance', 'analysis/reference-history.md', 10800]],
  };

  var BEATS = [
    { at: 0, look: '.gauge.run',
      en: 'Two research leads begin with one rule: the claim can only be as strong as the weakest evidence beneath it.',
      ja: '2体の研究リードが、主張の強さはその下にある最も弱い根拠を超えない、という1つのルールから始めます。' },
    { at: 14, look: '.bay[data-name="SlateHooke"]',
      en: 'The first child maps primary literature. It records not just what supports the claim, but where that support ends.',
      ja: '最初の子が一次文献を対応付けます。主張を支える内容だけでなく、その支えがどこで終わるかも記録します。' },
    { at: 35, look: '#v-net', view: 'net',
      en: 'The literature answer comes back bounded: a mechanism is plausible, but this pulse-specific result has no direct precedent.',
      ja: '文献担当の回答には境界があります。機構はあり得ますが、このパルス固有の結果に直接の先行例はありません。' },
    { at: 44, look: '.bay[data-name="IvoryNoether"]',
      en: 'An analysis child starts from raw measurements while the manuscript still waits for a defensible number.',
      ja: '解析担当が生の測定値から始めます。論文側は、守れる数値が出るまで待ちます。' },
    { at: 64, look: '.bay[data-name="CoralFaraday"]',
      en: 'A meeting companion follows the live discussion and asks the literature child for evidence before a repeated phrase hardens into fact.',
      ja: 'ミーティング伴走役が議論を追い、繰り返された言葉が事実として固まる前に文献担当へ根拠を尋ねます。' },
    { at: 96, look: '.bay[data-name="RustPasteur"]',
      en: 'The writing child drafts structure, not certainty. Numeric slots stay empty until the audit returns.',
      ja: '執筆担当が起草するのは構造で、確信ではありません。監査結果が戻るまで数値欄は空のままです。' },
    { at: 111, look: '#v-net', view: 'net',
      en: 'The meeting produces a useful open question: does the plateau survive the camera correction?',
      ja: 'ミーティングから有用な未決の問いが生まれます。カメラ補正後もプラトーは残るのでしょうか。' },
    { at: 122, look: '.bay[data-name="IvoryNoether"]',
      en: 'Analysis finds the apparent effect begins exactly at an exposure change. The anomaly is reported before anyone edits the paper.',
      ja: '解析担当は、見かけの効果が露光変更と同時に始まると気づきます。論文を直す前に異常を報告します。' },
    { at: 125, look: '.bay[data-name="IvoryNoether"]',
      en: 'The red APPROVAL state is deliberate: analysis has stopped before overwriting derived data and is waiting for a human permission boundary.',
      ja: '赤い APPROVAL 状態は意図的です。解析担当は派生データを上書きする前で止まり、人間の許可境界を待っています。' },
    { at: 151, look: '.bay[data-name="IvoryNoether"]',
      en: 'A human approves after 26 seconds. The red state clears, and only now does analysis regenerate the derived figure inputs.',
      ja: '26秒後に人間が承認します。赤い状態が消え、ここで初めて解析担当が派生図入力を再生成します。' },
    { at: 172, look: '.bay[data-name="IvoryNoether"]',
      en: 'The corrected effect survives, but shrinks from 18% to 7%. Direction remains; magnitude and cause do not.',
      ja: '補正後も効果は残りますが、18% から 7% へ縮みます。方向は残り、大きさと原因は残りません。' },
    { at: 175, look: '.bay[data-name="RustPasteur"]',
      en: 'The cyan question state asks a human to choose: replace the figure and withdraw 18%, or leave the old panel with a warning.',
      ja: 'シアンの question 状態が人間へ選択を求めます。図を差し替えて 18% を撤回するか、旧パネルへ警告を付けて残すか。' },
    { at: 182, look: '.bay[data-name="IvoryNoether"]',
      en: 'The analysis child finishes and stays visible with its audit, figure inputs, and checks still attached.',
      ja: '解析担当は終了しても画面に残り、監査、図の入力、検査結果を引き続き確認できます。' },
    { at: 194, look: '.bay[data-name="RustPasteur"]',
      en: 'After 19 seconds, the human chooses replacement. The cyan question clears and the writer edits the figure with an explicit decision behind it.',
      ja: '19秒後、人間が差し替えを選びます。シアンの question が消え、執筆担当は明示的な判断を根拠に図を編集します。' },
    { at: 214, look: '.bay[data-name="MossSomerville"]',
      en: 'The final handoff separates what is known, what changed after QC, what is unresolved, and what experiment comes next.',
      ja: '最後の引き継ぎでは、分かったこと、QC 後に変わったこと、未決のこと、次の実験を分けます。' },
    { at: 226, look: '.gauge.tot',
      en: 'The collaborator receives a smaller claim with a complete trail. That is progress the whole team can inspect.',
      ja: '共同研究者には、完全な履歴を伴う、より狭い主張が届きます。チーム全体が検証できる進展です。' },
  ];

  var JA = {};
  [
    ['Turn today\'s evidence into a claim the manuscript can defend', '今日の根拠を、論文が守れる主張へ変える'],
    ['Accompany the meeting and own the collaborator handoff', 'ミーティングに伴走し、共同研究者への引き継ぎを担う'],
    ['Find the closest primary evidence and record where it stops', '最も近い一次根拠を探し、どこまでしか支えないか記録する'],
    ['Find the closest primary evidence and record where it stops.', '最も近い一次根拠を探し、どこまでしか支えないか記録してください。'],
    ['Rebuild the response curve from raw measurements and audit anomalies', '生の測定値から応答曲線を再構築し、異常を監査する'],
    ['Rebuild the response curve from raw measurements and audit anomalies.', '生の測定値から応答曲線を再構築し、異常を監査してください。'],
    ['Track the live discussion and surface evidence without inventing conclusions', 'ライブ議論を追い、結論を捏造せず根拠を提示する'],
    ['Track the live discussion and surface evidence without inventing conclusions.', 'ライブ議論を追い、結論を捏造せず根拠を提示してください。'],
    ['Revise the Results section and figure around verified statements', '検証済みの記述を軸に Results と図を改訂する'],
    ['Revise the Results section and figure around verified statements.', '検証済みの記述を軸に Results と図を改訂してください。'],
    ['Audit the measurement protocol before the new run', '新しい run の前に測定プロトコルを監査する'],
    ['Audit the measurement protocol before the new run.', '新しい run の前に測定プロトコルを監査してください。'],
    ['Trace the origin of the reference correction', '参照補正の由来を追跡する'],
    ['Trace the origin of the reference correction.', '参照補正の由来を追跡してください。'],

    ['Task: build an evidence map for the recovery claim', '依頼: 回復の主張に対する根拠対応表を作る'],
    ['RE: six studies mapped, one supports only the mechanism', 'RE: 6研究を対応付け、1本が支えるのは機構だけ'],
    ['Task: rebuild the curve from raw measurements', '依頼: 生の測定値から曲線を再構築する'],
    ['Task: accompany the meeting and keep claims bounded', '依頼: ミーティングに伴走し、主張の境界を保つ'],
    ['Which source actually supports recovery after a pulse?', 'パルス後の回復を実際に支える出典はどれですか'],
    ['RE: closest study used steady flow, not pulses', 'RE: 最も近い研究はパルスではなく定常流を使用'],
    ['Task: draft the Results around verified statements', '依頼: 検証済みの記述を軸に Results を起草する'],
    ['Meeting question: effect or camera correction?', 'ミーティングの問い: 効果か、カメラ補正か'],
    ['An exposure step is inside the reported effect', '報告した効果の中に露光の段差があります'],
    ['Approval needed: regenerate the derived figure inputs?', '承認依頼: 派生図入力を再生成してよいですか'],
    ['RE: correct by segment and report what remains', 'RE: 区間ごとに補正し、残るものを報告する'],
    ['RE: corrected effect is smaller but still resolves', 'RE: 補正後の効果は小さいが、なお分離する'],
    ['Question: replace Figure 3 or annotate the old panel?', '質問: Figure 3 を差し替えるか、旧パネルへ注釈するか'],
    ['Replace Figure 3 and narrow the claim', 'Figure 3 を差し替え、主張を狭める'],
    ['RE: figure replaced, causal language removed', 'RE: 図を差し替え、因果表現を削除しました'],
    ['Done. Audit, figure inputs, and checks handed back.', '完了。監査、図の入力、検査結果を引き継ぎました。'],
    ['Done. Decisions and open questions are separated.', '完了。決定事項と未決の問いを分けました。'],
    ['Task: send the collaborator brief with one open question', '依頼: 未決の問いを1つ含む共同研究者向け報告を送る'],
    ['RE: brief sent — known, changed, unresolved, next', 'RE: 報告送信済み — 既知、変更、未決、次'],
    ['Evidence map final — limitation carried into the brief', '根拠対応表を確定 — 限界も報告へ引き継ぎました'],

    ['Prepare today\'s collaborator update. Connect literature, meeting questions, raw analysis, and the manuscript without letting causal language outrun the evidence.', '今日の共同研究者向け報告を準備してください。因果表現が根拠を追い越さないように、文献、ミーティングの問い、生データ解析、論文をつなぎます。'],
    ['The dependency is evidence first, raw-data audit second, writing last. The meeting can run alongside them if every claim keeps its source boundary.', '依存関係は、根拠が先、生データ監査が次、執筆が最後。各主張が出典の境界を保てば、ミーティングは並行して進められる。'],
    ['I will give literature, analysis, and writing to separate children. The second parent owns the live meeting and the final collaborator brief.', '文献、解析、執筆を別々の子に渡す。2体目の親がライブミーティングと最終報告を担当する。'],
    ['The literature makes a mechanism plausible but does not validate this pulse sequence. Keep that boundary visible while analysis checks the number.', '文献は機構を妥当にはするが、このパルス系列を検証はしない。解析が数値を確認する間、その境界を見えるままにする。'],
    ['The anomaly lands on the exposure boundary. The old effect size is unusable until the segments are calibrated separately.', '異常は露光境界に重なる。区間を別々に較正するまで、古い効果量は使えない。'],
    ['The direction survives correction, but the magnitude is much smaller. That is a useful result and a narrower claim.', '補正後も方向は残るが、大きさはずっと小さい。有用な結果であり、より狭い主張だ。'],
    ['The brief is out with one result, one correction, one open cause, and one next experiment. Handing back.', '報告には、結果1つ、補正1つ、未決の原因1つ、次の実験1つを入れた。引き継ぎます。'],

    ['Accompany the meeting and make the collaborator handoff distinguish results from open questions.', 'ミーティングに伴走し、共同研究者への引き継ぎで結果と未決の問いを区別してください。'],
    ['I will keep decisions, requests, and open questions separate, then report only what survives the evidence and analysis rails.', '決定、依頼、未決の問いを分け、文献と解析の系統を通過したものだけを報告する。'],
    ['That is the right unresolved question. Ask analysis for a figure that exposes the acquisition boundary instead of debating it from the normalized plot.', 'それが正しい未決の問いだ。正規化済みプロットだけで議論せず、取得境界を露出する図を解析担当へ求める。'],
    ['Meeting record closed: decisions and open questions are separate, and every literature statement carries its limitation.', 'ミーティング記録を閉じた。決定と未決の問いを分け、文献に関する各記述に限界を添えた。'],
    ['Lead with the corrected estimate. The provenance matters, but the collaborator needs the result before the audit trail.', '補正後の推定値を先頭に置く。来歴も重要だが、共同研究者には監査履歴より先に結果が必要だ。'],
    ['Brief sent: known, changed after QC, unresolved, and next experiment.', '報告を送信。既知、QC 後の変更、未決、次の実験を分けた。'],

    ['The closest experiment changes the forcing, not the exposure. It supports a mechanism comparison but cannot predict the pulse-specific plateau.', '最も近い実験が変えるのは負荷で、露光ではない。機構の比較は支えるが、パルス固有のプラトーは予測できない。'],
    ['Six studies mapped. The closest supports only a possible mechanism, not the measured plateau.', '6研究を対応付けた。最も近い研究が支えるのは機構の可能性だけで、測定したプラトーではない。'],
    ['Answer with what was imposed and measured, then state the mismatch before the sentence can grow stronger in the meeting.', '何を与え何を測ったかで答え、ミーティング中に文が強くなる前に不一致を示す。'],
    ['The safe comparison is now in the meeting: plausible mechanism, no direct validation.', '安全な比較をミーティングへ出した。機構は妥当、直接の検証は無し。'],
    ['Evidence map final. The limitation carried through to the collaborator brief.', '根拠対応表を確定。限界も共同研究者向け報告まで引き継いだ。'],

    ['The curve step and the exposure step share frame 480. A global mean has made the two acquisition regimes look directly comparable.', '曲線の段差と露光の段差は frame 480 で一致する。全体平均により、2つの取得条件が直接比較可能に見えていた。'],
    ['The reported effect contains an exposure boundary. I am stopping the manuscript number and reporting the anomaly first.', '報告した効果には露光境界が含まれる。論文の数値を止め、先に異常を報告する。'],
    ['Approval requested before derived tables and Figure 3 inputs are regenerated. Raw measurements remain locked.', '派生表と Figure 3 入力を再生成する前に承認を求めた。生の測定値はロックされたまま。'],
    ['Corrected effect is smaller but still resolves: 7%, with a run-level 95% interval of 4–10%.', '補正後の効果は小さいが分離する。7%、run 単位の 95% 区間は 4–10%。'],
    ['Audit, figure inputs, and checks handed back. Interpretation remains with the parent.', '監査、図の入力、検査結果を引き継いだ。解釈は親に残す。'],

    ['The phrase “expected recovery” needs a source now, before repetition turns it into a settled premise.', '「予想どおりの回復」という表現には今すぐ出典が必要だ。反復によって確定した前提になる前に。'],
    ['I surfaced the closest evidence with its steady-flow limitation while the question was still on screen.', '問いが画面にあるうちに、定常流という限界を添えて最も近い根拠を提示した。'],
    ['The group is asking whether the plateau is physical or introduced by correction. That belongs under open questions, with a concrete figure request.', 'グループはプラトーが物理現象か補正由来かを問うている。具体的な図の依頼とともに未決の問いへ置くべきだ。'],
    ['Recorded one open question: does the plateau survive acquisition correction?', '未決の問いを1つ記録した。取得補正後もプラトーは残るか。'],
    ['Decisions and open questions are separated. Handing back and exiting.', '決定と未決の問いを分けた。引き継いで終了する。'],

    ['Write the structure before the number arrives: observation, bounded comparison, limitation. Leave the effect size blank.', '数値が届く前に、観察、境界付き比較、限界という構造を書く。効果量は空欄にする。'],
    ['I need a human choice: replace Figure 3 and withdraw 18%, or keep the old panel with an acquisition warning. I recommend replacement.', '人間の選択が必要だ。Figure 3 を差し替えて 18% を撤回するか、取得上の警告付きで旧パネルを残すか。差し替えを推奨する。'],
    ['The direction survives, but causality does not. “Consistent with” is the strongest phrase shared by data and literature.', '方向は残るが、因果は残らない。「整合する」がデータと文献の双方で許される最も強い表現だ。'],
    ['Figure replaced, effect narrowed to 7%, and causal language removed.', '図を差し替え、効果を 7% に狭め、因果表現を削除した。'],

    ['Two operator choices can change exposure. Both are flagged in the protocol audit.', '操作者の選択2つが露光を変え得る。両方をプロトコル監査で指摘した。'],
    ['The global-mean correction predates the pulse sequence and was never revalidated for exposure changes.', '全体平均による補正はパルス系列より古く、露光変更に対して再検証されていなかった。'],

    ['Decision trail for the claim', '主張の判断履歴'],
    ['Collaborator brief', '共同研究者向け報告'],
    ['Evidence map with claim boundaries', '主張の境界付き根拠対応表'],
    ['Raw-to-corrected curve audit', '生値から補正値までの曲線監査'],
    ['Audited Figure 3 inputs', '監査済み Figure 3 入力'],
    ['Meeting decisions and open questions', 'ミーティングの決定事項と未決の問い'],
    ['Revised Results section', '改訂済み Results 節'],
    ['Corrected three-panel Figure 3', '補正済み3パネル Figure 3'],
    ['Measurement protocol audit', '測定プロトコル監査'],
    ['Reference-correction provenance', '参照補正の来歴'],
    ['no transcript on disk for this agent', 'このエージェントの会話ログはディスク上にありません'],
    ['demo mode — nothing was started, stopped or changed', 'デモモード — 何も起動・停止・変更していません'],
  ].forEach(function (pair) { JA[pair[0]] = pair[1]; });

  window.AGENTSTACK_STORIES.research = {
    id: 'research',
    label: { en: 'Research workflow', ja: '研究ワークフロー' },
    loop: 240,
    opensAt: 9,
    cast: CAST,
    past: PAST,
    script: SCRIPT,
    transcripts: TRANSCRIPTS,
    deliverables: DELIVERABLES,
    beats: BEATS,
    ja: JA,
  };
})();
