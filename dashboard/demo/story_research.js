/* Decision: keep one four-minute story so five parallel literature pipelines feed
 * the same meeting, analysis, manuscript revision, and collaborator handoff. */
(function () {
  'use strict';

  window.AGENTSTACK_STORIES = window.AGENTSTACK_STORIES || {};

  var CAST = [
    { name: 'AmberKepler', role: 'research lead', emoji: '🔬', group: 'demo',
      model: 'Opus 5', model_raw: 'claude-opus-5', provider: 'anthropic',
      program: 'claude-code', born: 0, dies: null, ctx0: 34, ctxRate: 0.05,
      task: 'Turn today\'s evidence into a claim the paper can defend' },
    { name: 'MossSomerville', role: 'meeting lead', emoji: '🧭', group: 'demo',
      model: 'Opus 5', model_raw: 'claude-opus-5', provider: 'anthropic',
      program: 'claude-code', born: 0, dies: null, ctx0: 46, ctxRate: 0.04,
      task: 'Sit in on the meeting and hand the result to the collaborator' },
    { name: 'SlateHooke', parent: 'AmberKepler', role: 'how it recovers', emoji: '📚',
      model: 'GPT 5.6', model_raw: 'gpt-5.6', provider: 'openai',
      program: 'codex', born: 15, dies: null, ctx0: 10, ctxRate: 0.13,
      task: 'Read what is known about how it recovers, and say where the evidence stops' },
    { name: 'TealLamarr', parent: 'AmberKepler', role: 'steady vs bursts', emoji: '🌊',
      model: 'GPT 5.6', model_raw: 'gpt-5.6', provider: 'openai',
      program: 'codex', born: 15, dies: 50, ctx0: 9, ctxRate: 0.16,
      task: 'Compare studies that pushed the system steadily with ones that pushed it in bursts' },
    { name: 'VioletDarwin', parent: 'AmberKepler', role: 'equipment errors', emoji: '🔎',
      model: 'Sonnet 5', model_raw: 'claude-sonnet-5', provider: 'anthropic',
      program: 'claude-code', born: 15, dies: 54, ctx0: 8, ctxRate: 0.15,
      task: 'Look for cases where the equipment, not the sample, produced the result' },
    { name: 'CopperBose', parent: 'AmberKepler', role: 'how it is measured', emoji: '🧪',
      model: 'GPT 5.6', model_raw: 'gpt-5.6', provider: 'openai',
      program: 'codex', born: 15, dies: 58, ctx0: 11, ctxRate: 0.14,
      task: 'Find how these measurements are made, and how they are known to go wrong' },
    { name: 'SaffronPlanck', parent: 'AmberKepler', role: 'how long it takes', emoji: '⏱️',
      model: 'Sonnet 5', model_raw: 'claude-sonnet-5', provider: 'anthropic',
      program: 'claude-code', born: 15, dies: 62, ctx0: 9, ctxRate: 0.14,
      task: 'Collect how long recovery takes, without assuming what causes it' },
    { name: 'IvoryNoether', parent: 'AmberKepler', role: 'analysis', emoji: '📈',
      model: 'GPT 5.6', model_raw: 'gpt-5.6', provider: 'openai',
      program: 'codex', born: 44, dies: 182, ctx0: 8, ctxRate: 0.24,
      states: [[125, 151, 'ask']],
      task: 'Rebuild the result from the raw measurements and check anything odd' },
    { name: 'CoralFaraday', parent: 'MossSomerville', role: 'listening', emoji: '🎙️',
      model: 'Sonnet 5', model_raw: 'claude-sonnet-5', provider: 'anthropic',
      program: 'claude-code', born: 64, dies: 190, ctx0: 7, ctxRate: 0.18,
      task: 'Follow the discussion and produce evidence, without inventing conclusions' },
    { name: 'RustPasteur', parent: 'AmberKepler', role: 'writing', emoji: '✍️',
      model: 'Sonnet 5', model_raw: 'claude-sonnet-5', provider: 'anthropic',
      program: 'claude-code', born: 96, dies: null, ctx0: 6, ctxRate: 0.11,
      states: [[175, 194, 'question']],
      task: 'Write up only what has actually been checked' },
  ];

  var PAST = [
    { name: 'CedarLovelace', parent: 'AmberKepler', model: 'GPT 5.6',
      model_raw: 'gpt-5.6', provider: 'openai', program: 'codex',
      task: 'Check how the measurements would be taken, before the new run',
      retired: true, ago: 5800 },
    { name: 'OchreCurie', parent: 'MossSomerville', model: 'Sonnet 5',
      model_raw: 'claude-sonnet-5', provider: 'anthropic',
      program: 'claude-code',
      task: 'Find out where an old correction came from',
      retired: true, ago: 10800 },
  ];

  var SCRIPT = [
    { at: 16, from: 'AmberKepler', to: 'SlateHooke',
      subject: 'Task: survey recovery mechanisms',
      importance: 'high', ack: true,
      body:
        'Own the mechanism track. Search primary studies on recovery after forcing stops, then download the strongest papers, convert them to Markdown, extract figures, and write lit/mechanism-recovery.md.\n' +
        '\n' +
        'Separate what was measured from what the authors inferred. Register the final note in the shared reference library, but coordinate the lease by mail instead of retrying against another child.',
      body_ja:
        '機構のトラックを担当します。負荷停止後の回復に関する一次研究を探し、最も強い論文のダウンロード、Markdown 変換、図版抽出を行い、lit/mechanism-recovery.md を書いてください。\n' +
        '\n' +
        '測定されたことと著者の推論を分けます。最終ノートは共有文献ライブラリに登録しますが、他の子がリース中なら再試行せず、mail で調整してください。' },
    { at: 17, from: 'AmberKepler', to: 'TealLamarr',
      subject: 'Task: compare forcing regimes',
      importance: 'high', ack: true,
      body:
        'Own the forcing-regime track. Find primary studies that distinguish steady, stopped, and pulsed forcing, then run the full PDF-to-Lit-note pipeline into lit/forcing-regimes.md.\n' +
        '\n' +
        'Report which comparisons are direct and which are analogies. If another child holds the shared reference library, negotiate priority by mail and wait for a handoff.',
      body_ja:
        '負荷条件のトラックを担当します。定常、停止、パルス状の負荷を区別する一次研究を探し、PDF から Lit ノートまでの全工程を実行して lit/forcing-regimes.md へまとめてください。\n' +
        '\n' +
        'どの比較が直接的で、どれが類推かを報告します。他の子が共有文献ライブラリを押さえている場合は、mail で順番を交渉し、引き継ぎを待ちます。' },
    { at: 18, from: 'AmberKepler', to: 'VioletDarwin',
      subject: 'Task: audit acquisition artifacts in prior work',
      importance: 'high', ack: true,
      body:
        'Own the acquisition-artifact track. Search for exposure changes, detector correction, and normalization failures, then download, convert, extract figures, and write lit/acquisition-artifacts.md.\n' +
        '\n' +
        'Flag whether each paper detected the artifact from raw data or only after normalization. Keep local work moving while the shared reference library is leased elsewhere.',
      body_ja:
        '取得アーティファクトのトラックを担当します。露光変更、検出器補正、正規化の失敗を探し、ダウンロード、変換、図版抽出を行って lit/acquisition-artifacts.md を書いてください。\n' +
        '\n' +
        '各論文がアーティファクトを生データから検出したのか、正規化後に初めて検出したのかを明示します。共有文献ライブラリが別の子にリースされている間も、ローカル作業を進めます。' },
    { at: 19, from: 'AmberKepler', to: 'CopperBose',
      subject: 'Task: trace measurement failure modes',
      importance: 'high', ack: true,
      body:
        'Own the measurement-methods track. Find primary methods papers on signal drift, missing frames, and calibration boundaries, then build lit/measurement-failures.md through the complete document pipeline.\n' +
        '\n' +
        'Extract the diagnostic figures, not just abstracts. Return checks that analysis can apply to raw measurements and use the negotiated library handoff.',
      body_ja:
        '測定手法のトラックを担当します。信号ドリフト、欠損フレーム、較正境界に関する一次の手法論文を探し、文書処理の全工程で lit/measurement-failures.md を作ってください。\n' +
        '\n' +
        '抄録だけでなく診断図も抽出します。解析が生の測定値に適用できる検査を返し、交渉済みのライブラリ引き継ぎを使います。' },
    { at: 20, from: 'AmberKepler', to: 'SaffronPlanck',
      subject: 'Task: map recovery timescales',
      importance: 'high', ack: true,
      body:
        'Own the recovery-timescale track. Search primary kinetics studies, download the best matches, convert them to Markdown, extract figures, and write lit/recovery-timescales.md.\n' +
        '\n' +
        'Record the observation window and uncertainty for every timescale. Do not import a cause from a paper that only measured timing; wait for your turn at the shared reference library.',
      body_ja:
        '回復時間スケールのトラックを担当します。一次の動態研究を探し、最も近い論文をダウンロードし、Markdown 変換と図版抽出を行って lit/recovery-timescales.md を書いてください。\n' +
        '\n' +
        '各時間スケールの観察窓と不確かさを記録します。時間しか測定していない論文から原因を持ち込まず、共有文献ライブラリの順番を待ちます。' },
    { at: 28, from: 'TealLamarr', to: 'SlateHooke',
      subject: 'Could I register the comparison paper first?',
      importance: 'high',
      body:
        'I can finish the forcing-regime track if I register one comparison paper now, but the shared reference library reports your active lease. I am waiting rather than retrying.\n' +
        '\n' +
        'Could you checkpoint and let this one metadata row go first? I will import it, release immediately, and hand the lease to the artifact track.',
      body_ja:
        '比較論文1本を今登録できれば負荷条件トラックを完了できますが、共有文献ライブラリはあなたがリース中と表示しています。再試行せず待っています。\n' +
        '\n' +
        '一度チェックポイントを作り、このメタデータ1行を先に入れさせてもらえますか。インポート後すぐ解放し、リースをアーティファクト担当へ渡します。' },
    { at: 30, from: 'SlateHooke', to: 'TealLamarr',
      subject: 'RE: yes — checkpointing for your priority row',
      importance: 'high',
      body:
        'Yes. My local Lit note and extracted figures are complete, so I am checkpointing the mechanism batch without committing its library rows.\n' +
        '\n' +
        'I will release at second 32. Take the lease next, register your comparison row, then mail the handoff instead of leaving the others to poll.',
      body_ja:
        'はい。ローカルの Lit ノートと抽出図版は完了したので、機構バッチのライブラリ行をコミットせずにチェックポイント化します。\n' +
        '\n' +
        '32秒で解放します。次にリースを取得し、比較行を登録したら、他の担当をポーリングさせず mail で引き継いでください。' },
    { at: 33, from: 'SlateHooke', to: 'TealLamarr',
      subject: 'Handoff: the shared reference library is released',
      importance: 'high',
      body:
        'The mechanism batch is checkpointed locally and the shared reference library lease is released. Your comparison row has priority now.\n' +
        '\n' +
        'Nothing was overwritten and no second writer was opened. Please pass the lease to VioletDarwin when your import is visible.',
      body_ja:
        '機構バッチはローカルにチェックポイント化し、共有文献ライブラリのリースを解放しました。現在はあなたの比較行が優先です。\n' +
        '\n' +
        '何も上書きされず、2人目の書き込み者も開いていません。インポートが見えたら、リースを VioletDarwin に渡してください。' },
    { at: 35, from: 'TealLamarr', to: 'VioletDarwin',
      subject: 'Handoff: artifact batch may use the shared library',
      importance: 'normal',
      body:
        'My priority row is registered and verified. I released the shared reference library after one import, exactly as negotiated.\n' +
        '\n' +
        'Your artifact batch is next. The forcing note remains local and no pending write is attached to the lease.',
      body_ja:
        '優先行の登録と確認が終わりました。交渉どおり1回のインポート後に共有文献ライブラリを解放しました。\n' +
        '\n' +
        '次はあなたのアーティファクトバッチです。負荷ノートはローカルに残り、リースに保留中の書き込みはありません。' },
    { at: 37, from: 'VioletDarwin', to: 'CopperBose',
      subject: 'Handoff: measurement batch is next',
      importance: 'normal',
      body:
        'The artifact records are committed and the shared reference library is released cleanly. The extracted diagnostic figures remain in my Lit-note folder.\n' +
        '\n' +
        'Take the lease for the measurement batch, verify your rows, then hand it to SaffronPlanck for the final literature import.',
      body_ja:
        'アーティファクトのレコードをコミットし、共有文献ライブラリを正常に解放しました。抽出した診断図は私の Lit ノートフォルダに残っています。\n' +
        '\n' +
        '測定バッチのリースを取得して行を確認し、最後の文献インポート用に SaffronPlanck へ渡してください。' },
    { at: 39, from: 'CopperBose', to: 'SaffronPlanck',
      subject: 'Handoff: final literature import is ready',
      importance: 'normal',
      body:
        'The measurement-method rows pass validation, so I released the shared reference library. No repair or conflict resolution was needed.\n' +
        '\n' +
        'You have the final lease for the recovery-timescale batch. Please release it after verification so SlateHooke can consolidate the evidence map later.',
      body_ja:
        '測定手法の行が検証を通過したので、共有文献ライブラリを解放しました。修復も衝突解決も必要ありませんでした。\n' +
        '\n' +
        '回復時間スケールバッチ用の最後のリースです。確認後に解放し、後で SlateHooke が根拠対応表を統合できるようにしてください。' },
    { at: 40, from: 'SaffronPlanck', to: 'CopperBose',
      subject: 'RE: received — one import, then release',
      importance: 'normal',
      body:
        'Received. My PDF, Markdown, figures, and Lit note are already local, so the shared reference library is the only remaining step.\n' +
        '\n' +
        'I will import once, verify the recovery-window fields, release the lease, and report directly to the parent.',
      body_ja:
        '受け取りました。PDF、Markdown、図版、Lit ノートはすでにローカルにあり、残るのは共有文献ライブラリだけです。\n' +
        '\n' +
        '1回インポートし、回復窓のフィールドを確認してリースを解放し、親へ直接報告します。' },
    { at: 42, from: 'SlateHooke', to: 'AmberKepler',
      subject: 'RE: mechanism track mapped with a hard boundary',
      importance: 'normal',
      body:
        'Three primary mechanism studies completed the full document pipeline. Two report recovery under steady forcing; only one separates signal loss from redistribution.\n' +
        '\n' +
        'None uses a pulsed exposure sequence like ours. The closest paper supports a possible mechanism, not our measured plateau. I marked that boundary in every row of the evidence map.',
      body_ja:
        '一次の機構研究3本で文書処理の全工程を完了しました。定常的な負荷の下で回復を報告するものが2本あり、信号低下と再分布を分けているのは1本だけです。\n' +
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
    { at: 46, from: 'TealLamarr', to: 'AmberKepler',
      subject: 'RE: forcing track distinguishes direct tests from analogies',
      importance: 'normal',
      body:
        'Four forcing studies completed the PDF, Markdown, figure, and Lit-note pipeline. Steady-stop experiments are informative analogies; none directly tests alternating pulses.\n' +
        '\n' +
        'The one priority library row is registered and the lease was handed onward. I marked every comparison as direct, partial, or contextual.',
      body_ja:
        '負荷条件の研究4本で、PDF、Markdown、図版、Lit ノートの工程を完了しました。定常負荷停止の実験は有用な類推ですが、交互パルスを直接試験したものはありません。\n' +
        '\n' +
        '優先したライブラリ行は登録済みで、リースも次へ引き継ぎました。各比較を直接、部分的、文脈的に分けています。' },
    { at: 50, from: 'VioletDarwin', to: 'AmberKepler',
      subject: 'RE: artifact track found the raw-versus-normalized warning',
      importance: 'high',
      body:
        'Three acquisition studies completed the full pipeline. Two show that an exposure step can survive normalization and resemble a response change unless raw frames are inspected.\n' +
        '\n' +
        'I extracted the diagnostic panels and recorded their checks in the Lit note. The shared reference library was released before I reported.',
      body_ja:
        '取得研究3本で全工程を完了しました。2本は、生フレームを確認しないと、露光の段差が正規化後も残り、応答変化に見え得ると示しています。\n' +
        '\n' +
        '診断パネルを抽出し、その検査を Lit ノートに記録しました。報告前に共有文献ライブラリを解放済みです。' },
    { at: 54, from: 'CopperBose', to: 'AmberKepler',
      subject: 'RE: methods track returned three raw-data checks',
      importance: 'normal',
      body:
        'The methods track completed three primary papers and extracted their failure-diagnostic figures. The reusable checks are exposure-boundary alignment, dropped-frame masking, and per-run uncertainty.\n' +
        '\n' +
        'All checks are written in lit/measurement-failures.md. The library rows validate and the lease is already with the final literature child.',
      body_ja:
        '手法トラックで一次論文3本を完了し、それぞれの失敗診断図を抽出しました。再利用できる検査は、露光境界との対応、欠損フレームのマスク、run ごとの不確かさです。\n' +
        '\n' +
        'すべての検査を lit/measurement-failures.md に書きました。ライブラリ行は検証済みで、リースはすでに最後の文献担当へ渡しています。' },
    { at: 58, from: 'SaffronPlanck', to: 'AmberKepler',
      subject: 'RE: timescale track keeps timing separate from cause',
      importance: 'normal',
      body:
        'Three kinetics studies completed the full pipeline. Their recovery windows overlap ours, but none identifies the cause under a pulsed acquisition sequence.\n' +
        '\n' +
        'The final library import is verified and the lease is released for consolidation. My Lit note preserves observation windows, uncertainty, and the causal boundary separately.',
      body_ja:
        '動態研究3本で全工程を完了しました。回復窓はこちらと重なりますが、パルス状の取得系列で原因を特定したものはありません。\n' +
        '\n' +
        '最後のライブラリインポートを確認し、統合用にリースを解放しました。Lit ノートには観察窓、不確かさ、因果の境界を分けて残しています。' },
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
      [7, 'assistant', 'text', 'I will split literature into five fields and start them together. Analysis and writing stay separate; the second parent owns the live meeting and final brief.'],
      [13, 'assistant', 'tool_use', 'Bash  agentstack spawn --roles mechanism,forcing,artifacts,methods,kinetics --parallel'],
      [14, 'assistant', 'tool_result', 'started SlateHooke, TealLamarr, VioletDarwin, CopperBose, SaffronPlanck'],
      [16, 'assistant', 'tool_use', 'SendMessage  to=SlateHooke "survey recovery mechanisms"'],
      [17, 'assistant', 'tool_use', 'SendMessage  to=TealLamarr "compare forcing regimes"'],
      [18, 'assistant', 'tool_use', 'SendMessage  to=VioletDarwin "audit acquisition artifacts"'],
      [19, 'assistant', 'tool_use', 'SendMessage  to=CopperBose "trace measurement failure modes"'],
      [20, 'assistant', 'tool_use', 'SendMessage  to=SaffronPlanck "map recovery timescales"'],
      [41, 'assistant', 'thinking', 'Five literature tracks are returning bounded notes in sequence. Their library access was negotiated, so consolidation starts from intact work instead of repaired conflicts.'],
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
      [15, 'user', 'text', 'Read what is known about how it recovers, and say where the evidence stops.'],
      [17, 'assistant', 'tool_use', 'WebSearch  query="recovery after forcing stops primary study"'],
      [19, 'assistant', 'tool_result', '18 candidates · 3 primary mechanism studies retained'],
      [20, 'assistant', 'tool_use', 'Bash  curl -o papers/mechanism-recovery.pdf https://archive.invalid/mechanism-recovery.pdf'],
      [21, 'assistant', 'tool_result', 'downloaded 2.4 MB PDF'],
      [22, 'assistant', 'tool_use', 'Bash  pdf-to-md papers/mechanism-recovery.pdf --output papers/mechanism-recovery.md'],
      [23, 'assistant', 'tool_result', 'converted 14 pages · references preserved'],
      [24, 'assistant', 'tool_use', 'Bash  extract-figures papers/mechanism-recovery.pdf --output figures/mechanism-recovery/'],
      [25, 'assistant', 'tool_result', 'extracted 5 figures · 2 diagnostic panels'],
      [26, 'assistant', 'tool_use', 'Write  lit/mechanism-recovery.md'],
      [27, 'assistant', 'tool_result', 'wrote observations, inference, and claim boundary'],
      [27, 'assistant', 'tool_use', 'Bash  ref-library acquire --collection evidence-map'],
      [28, 'assistant', 'tool_result', 'lease acquired · TealLamarr requested one priority row'],
      [29, 'assistant', 'thinking', 'TealLamarr can close a comparison track with one row. Checkpointing locally and handing over is safer than making four children poll the same writer.'],
      [30, 'assistant', 'tool_use', 'SendMessage  to=TealLamarr "checkpointing for your priority row"'],
      [32, 'assistant', 'tool_use', 'Bash  ref-library release --collection evidence-map'],
      [33, 'assistant', 'tool_result', 'lease released · handoff mailed to TealLamarr'],
      [35, 'assistant', 'text', 'Three mechanism studies are mapped. The closest supports only a possible mechanism, not the measured plateau.'],
      [76, 'assistant', 'tool_result', 'CoralFaraday: which source supports recovery after a pulse?'],
      [78, 'assistant', 'thinking', 'Answer with what was imposed and measured, then state the mismatch before the sentence can grow stronger in the meeting.'],
      [80, 'assistant', 'tool_use', 'SendMessage  to=CoralFaraday "closest study used steady flow, not pulses"'],
      [83, 'assistant', 'text', 'The safe comparison is now in the meeting: plausible mechanism, no direct validation.'],
      [226, 'assistant', 'tool_use', 'Bash  node scripts/check-claim-links.js'],
      [227, 'assistant', 'tool_result', '12 claims linked · 0 unbounded'],
      [229, 'assistant', 'text', 'Evidence map final. The limitation carried through to the collaborator brief.'],
    ],
    TealLamarr: [
      [15, 'user', 'text', 'Compare studies that pushed the system steadily with ones that pushed it in bursts.'],
      [17, 'assistant', 'tool_use', 'WebSearch  query="steady stopped pulsed forcing primary comparison"'],
      [19, 'assistant', 'tool_result', '22 candidates · 4 primary comparisons retained'],
      [20, 'assistant', 'tool_use', 'Bash  curl -o papers/forcing-regimes.pdf https://archive.invalid/forcing-regimes.pdf'],
      [21, 'assistant', 'tool_result', 'downloaded 3.1 MB PDF'],
      [22, 'assistant', 'tool_use', 'Bash  pdf-to-md papers/forcing-regimes.pdf --output papers/forcing-regimes.md'],
      [23, 'assistant', 'tool_result', 'converted 18 pages · tables preserved'],
      [24, 'assistant', 'tool_use', 'Bash  extract-figures papers/forcing-regimes.pdf --output figures/forcing-regimes/'],
      [25, 'assistant', 'tool_result', 'extracted 6 figures · forcing timeline found'],
      [26, 'assistant', 'tool_use', 'Write  lit/forcing-regimes.md'],
      [27, 'assistant', 'tool_result', 'wrote direct, partial, and contextual comparisons'],
      [28, 'assistant', 'tool_use', 'SendMessage  to=SlateHooke "could I register the comparison paper first?"'],
      [30, 'assistant', 'tool_result', 'SlateHooke: checkpointing; lease will be released'],
      [32, 'assistant', 'tool_use', 'Bash  ref-library acquire --collection evidence-map'],
      [33, 'assistant', 'tool_result', 'lease acquired after explicit handoff'],
      [34, 'assistant', 'tool_use', 'Bash  ref-library import lit/forcing-regimes.md --release'],
      [35, 'assistant', 'tool_result', '1 priority row registered · lease released'],
      [36, 'assistant', 'tool_use', 'SendMessage  to=VioletDarwin "artifact batch may use the shared library"'],
      [41, 'assistant', 'text', 'The forcing track separates direct tests from analogies, and the shared library handoff is complete.'],
    ],
    VioletDarwin: [
      [15, 'user', 'text', 'Look for cases where the equipment, not the sample, produced the result.'],
      [17, 'assistant', 'tool_use', 'WebSearch  query="exposure step normalization artifact primary methods"'],
      [19, 'assistant', 'tool_result', '16 candidates · 3 acquisition studies retained'],
      [20, 'assistant', 'tool_use', 'Bash  curl -o papers/acquisition-artifacts.pdf https://archive.invalid/acquisition-artifacts.pdf'],
      [21, 'assistant', 'tool_result', 'downloaded 2.8 MB PDF'],
      [22, 'assistant', 'tool_use', 'Bash  pdf-to-md papers/acquisition-artifacts.pdf --output papers/acquisition-artifacts.md'],
      [23, 'assistant', 'tool_result', 'converted 12 pages · methods headings preserved'],
      [24, 'assistant', 'tool_use', 'Bash  extract-figures papers/acquisition-artifacts.pdf --output figures/acquisition-artifacts/'],
      [25, 'assistant', 'tool_result', 'extracted 4 figures · raw/normalized pair found'],
      [26, 'assistant', 'tool_use', 'Write  lit/acquisition-artifacts.md'],
      [27, 'assistant', 'tool_result', 'wrote raw-data and post-normalization warnings'],
      [31, 'assistant', 'tool_result', 'shared reference library held elsewhere · local note remains writable'],
      [35, 'assistant', 'tool_result', 'TealLamarr: artifact batch may use the shared library'],
      [36, 'assistant', 'tool_use', 'Bash  ref-library import lit/acquisition-artifacts.md --acquire --release'],
      [37, 'assistant', 'tool_result', 'artifact rows registered · lease released'],
      [38, 'assistant', 'tool_use', 'SendMessage  to=CopperBose "measurement batch is next"'],
      [47, 'assistant', 'text', 'The artifact track found why raw frames must stay beside normalized results.'],
    ],
    CopperBose: [
      [15, 'user', 'text', 'Find how these measurements are made, and how they are known to go wrong.'],
      [17, 'assistant', 'tool_use', 'WebSearch  query="signal drift missing frames calibration boundary primary methods"'],
      [19, 'assistant', 'tool_result', '20 candidates · 3 methods papers retained'],
      [20, 'assistant', 'tool_use', 'Bash  curl -o papers/measurement-failures.pdf https://archive.invalid/measurement-failures.pdf'],
      [21, 'assistant', 'tool_result', 'downloaded 3.5 MB PDF'],
      [22, 'assistant', 'tool_use', 'Bash  pdf-to-md papers/measurement-failures.pdf --output papers/measurement-failures.md'],
      [23, 'assistant', 'tool_result', 'converted 21 pages · equations preserved'],
      [24, 'assistant', 'tool_use', 'Bash  extract-figures papers/measurement-failures.pdf --output figures/measurement-failures/'],
      [25, 'assistant', 'tool_result', 'extracted 7 figures · 3 failure diagnostics'],
      [26, 'assistant', 'tool_use', 'Write  lit/measurement-failures.md'],
      [27, 'assistant', 'tool_result', 'wrote exposure, frame, and uncertainty checks'],
      [37, 'assistant', 'tool_result', 'VioletDarwin: measurement batch is next'],
      [38, 'assistant', 'tool_use', 'Bash  ref-library import lit/measurement-failures.md --acquire --release'],
      [39, 'assistant', 'tool_result', 'methods rows validated · lease released'],
      [39, 'assistant', 'tool_use', 'SendMessage  to=SaffronPlanck "final literature import is ready"'],
      [53, 'assistant', 'text', 'The methods track returned three checks that analysis can apply directly to raw measurements.'],
    ],
    SaffronPlanck: [
      [15, 'user', 'text', 'Collect how long recovery takes, without assuming what causes it.'],
      [17, 'assistant', 'tool_use', 'WebSearch  query="recovery kinetics observation window primary study"'],
      [19, 'assistant', 'tool_result', '14 candidates · 3 kinetics studies retained'],
      [20, 'assistant', 'tool_use', 'Bash  curl -o papers/recovery-timescales.pdf https://archive.invalid/recovery-timescales.pdf'],
      [21, 'assistant', 'tool_result', 'downloaded 2.2 MB PDF'],
      [22, 'assistant', 'tool_use', 'Bash  pdf-to-md papers/recovery-timescales.pdf --output papers/recovery-timescales.md'],
      [23, 'assistant', 'tool_result', 'converted 11 pages · intervals preserved'],
      [24, 'assistant', 'tool_use', 'Bash  extract-figures papers/recovery-timescales.pdf --output figures/recovery-timescales/'],
      [25, 'assistant', 'tool_result', 'extracted 5 figures · 3 recovery curves'],
      [26, 'assistant', 'tool_use', 'Write  lit/recovery-timescales.md'],
      [27, 'assistant', 'tool_result', 'wrote observation windows, uncertainty, and causal limits'],
      [39, 'assistant', 'tool_result', 'CopperBose: final literature import is ready'],
      [40, 'assistant', 'tool_use', 'SendMessage  to=CopperBose "received; one import, then release"'],
      [41, 'assistant', 'tool_use', 'Bash  ref-library import lit/recovery-timescales.md --acquire --release'],
      [42, 'assistant', 'tool_result', 'final literature rows verified · lease released'],
      [57, 'assistant', 'text', 'The timescale track preserves timing as evidence without turning it into a cause.'],
    ],
    IvoryNoether: [
      [44, 'user', 'text', 'Rebuild the result from the raw measurements and check anything odd.'],
      [48, 'assistant', 'tool_use', 'Computer  open the analysis suite'],
      [49, 'assistant', 'tool_result', 'analysis suite opened · no dataset loaded'],
      [52, 'assistant', 'tool_use', 'Computer  open "Raw measurements" from the dataset dialog'],
      [53, 'assistant', 'tool_result', '12 runs · 960 frames · 2 exposure segments'],
      [56, 'assistant', 'tool_use', 'Computer  click "Baseline correction"'],
      [57, 'assistant', 'tool_result', 'dialog opened · global mean selected from prior session'],
      [60, 'assistant', 'tool_use', 'Computer  select "Preview without writing" and click "Run"'],
      [71, 'assistant', 'tool_result', 'preview complete · 3 dropped frames flagged'],
      [73, 'assistant', 'tool_use', 'Screenshot  analysis suite raw curve with exposure timeline'],
      [74, 'assistant', 'tool_result', 'curve step aligns with exposure change at frame 480'],
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
      [64, 'user', 'text', 'Follow the discussion and produce evidence, without inventing conclusions.'],
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
      [96, 'user', 'text', 'Write up only what has actually been checked.'],
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
      [0, 'user', 'text', 'Check how the measurements would be taken, before the new run.'],
      [0, 'assistant', 'tool_use', 'Read  protocols/acquisition.md'],
      [0, 'assistant', 'tool_result', '14 steps · 2 operator choices'],
      [0, 'assistant', 'text', 'Two operator choices can change exposure. Both are flagged in the protocol audit.'],
    ],
    OchreCurie: [
      [0, 'user', 'text', 'Find out where an old correction came from.'],
      [0, 'assistant', 'tool_use', 'Bash  git log -S global_mean -- analysis/'],
      [0, 'assistant', 'tool_result', 'introduced three revisions ago'],
      [0, 'assistant', 'text', 'The global-mean correction predates the pulse sequence and was never revalidated for exposure changes.'],
    ],
  };

  var DELIVERABLES = {
    AmberKepler: [['Decision trail for the claim', 'notes/decision-trail.md', 120]],
    MossSomerville: [['Collaborator brief', 'reports/collaborator-brief.md', 30]],
    SlateHooke: [['Mechanism Lit note with claim boundaries', 'lit/mechanism-recovery.md', 240],
                 ['Evidence map with claim boundaries', 'notes/evidence-map.md', 220]],
    TealLamarr: [['Forcing-regime Lit note', 'lit/forcing-regimes.md', 210]],
    VioletDarwin: [['Acquisition-artifact Lit note', 'lit/acquisition-artifacts.md', 200]],
    CopperBose: [['Measurement-failure Lit note', 'lit/measurement-failures.md', 190]],
    SaffronPlanck: [['Recovery-timescale Lit note', 'lit/recovery-timescales.md', 180]],
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
      en: 'Two agents lead a piece of research, under one rule: a conclusion is only as strong as the weakest evidence under it.',
      ja: '2体のエージェントが研究をまとめます。結論の強さは、その下にある最も弱い根拠を超えない、というルールで動きます。' },
    { at: 14, look: '#v-net', view: 'net',
      en: 'AmberKepler starts five agents at once, each reading a different corner of what has already been published. In Network their portraits fan out from one parent, so you can see the work split five ways.',
      ja: 'AmberKepler が5体を同時に起動します。それぞれが、すでに published された研究の別の一角を読みます。Network では1体の親から肖像が扇状に広がり、仕事が5つに分かれた様子が見えます。' },
    { at: 22, look: '.bay[data-name="VioletDarwin"]',
      en: 'VioletDarwin does by itself what a person would do by hand: search, read the papers, pull out the figures, write a summary the others can use.',
      ja: 'VioletDarwin は、人が手でやることをそのまま自分で行います。探し、論文を読み、図を取り出し、他の担当が使える要約を書きます。' },
    { at: 28, look: '#v-net', view: 'net',
      en: 'TealLamarr and SlateHooke need the same shared tool, and only one of them can use it at a time. They settle the order between themselves by message, without a person stepping in.',
      ja: 'TealLamarr と SlateHooke は同じ共有ツールを必要としますが、一度に使えるのは1体だけです。人が入らずに、メッセージで順番を決めます。' },
    { at: 35, look: '#v-net', view: 'net',
      en: 'The first answer comes back with an honest limit attached: the idea is plausible, but nobody has published this exact result before.',
      ja: '最初の回答は、正直な限界付きで返ってきます。考え方としてはあり得るが、この結果そのものを報告した先行研究は無い、というものです。' },
    { at: 44, look: '.bay[data-name="IvoryNoether"]',
      en: 'IvoryNoether opens the analysis software and works through the measurements. The writing waits rather than guessing a number.',
      ja: 'IvoryNoether が解析ソフトを開き、測定値を確かめます。書く側は、数値を推測せずに待ちます。' },
    { at: 58, look: '.bay[data-name="SaffronPlanck"]',
      en: 'SaffronPlanck sends the fifth and last report. All five worked at the same time and still finished cleanly, each leaving a summary the others can build on.',
      ja: 'SaffronPlanck が5件目、最後の報告を返します。5体は同時に動きながら互いを壊すことなく終わり、それぞれが他の担当の土台になる要約を残しました。' },
    { at: 64, look: '.bay[data-name="CoralFaraday"]',
      en: 'CoralFaraday listens to the discussion and asks SlateHooke for the evidence, before a phrase people keep repeating turns into an accepted fact.',
      ja: 'CoralFaraday は議論を聞き、繰り返されている言い回しが事実として定着する前に、SlateHooke へ根拠を尋ねます。' },
    { at: 96, look: '.bay[data-name="RustPasteur"]',
      en: 'RustPasteur writes the shape of the paper but leaves every number blank until IvoryNoether has checked them.',
      ja: 'RustPasteur は論文の骨組みだけ書き、IvoryNoether が確かめるまで数値は空欄のままにします。' },
    { at: 111, look: '#v-net', view: 'net',
      en: 'The meeting ends on a question worth asking: is the effect real, or is it something the equipment did?',
      ja: 'ミーティングは、確かめる価値のある問いで終わります。効果は本物か、それとも装置がつくったものか。' },
    { at: 122, look: '.bay[data-name="IvoryNoether"]',
      en: 'IvoryNoether finds that the effect starts exactly where a camera setting changed — a sign it may not be real — and says so before anyone edits the paper.',
      ja: 'IvoryNoether は、効果がカメラ設定の変わった所からちょうど始まっていることに気づきます。本物ではないかもしれない合図です。誰かが論文を直す前に、それを伝えます。' },
    { at: 125, look: '.bay[data-name="IvoryNoether"]',
      en: 'IvoryNoether has stopped before overwriting data it cannot get back, and is asking a person for permission. Its card shows red APPROVAL while it waits.',
      ja: 'IvoryNoether は、元に戻せないデータを上書きする前で止まり、人に許可を求めています。待っている間、カードに赤い APPROVAL が出ます。',
      net: {
        en: 'IvoryNoether has stopped before overwriting data it cannot get back, and is asking a person for permission. A ! sits over its portrait while it waits.',
        ja: 'IvoryNoether は、元に戻せないデータを上書きする前で止まり、人に許可を求めています。待っている間、肖像の上に ! が出ます。' } },
    { at: 151, look: '.bay[data-name="IvoryNoether"]',
      en: 'A person approves after 26 seconds. Only then does IvoryNoether touch the data.',
      ja: '人が26秒後に許可します。そこで初めて IvoryNoether はデータに触ります。' },
    { at: 172, look: '.bay[data-name="IvoryNoether"]',
      en: 'IvoryNoether finds the effect is still there once corrected, but much smaller than it looked. The direction holds; the size does not.',
      ja: 'IvoryNoether は、補正しても効果は残るが、見えていたよりずっと小さいと分かります。向きは変わりませんが、大きさは変わります。' },
    { at: 175, look: '.bay[data-name="RustPasteur"]',
      en: 'RustPasteur is asking a person to choose: correct the figure and take back the larger number, or keep it with a warning. Its card shows a cyan ? until someone answers.',
      ja: 'RustPasteur は人に選択を求めています。図を直して大きい方の数値を取り下げるか、警告を付けたまま残すか。誰かが答えるまで、カードにシアンの ? が出ます。',
      net: {
        en: 'RustPasteur is asking a person to choose: correct the figure and take back the larger number, or keep it with a warning. A cyan ? sits over its portrait until someone answers.',
        ja: 'RustPasteur は人に選択を求めています。図を直して大きい方の数値を取り下げるか、警告を付けたまま残すか。誰かが答えるまで、肖像の上にシアンの ? が出ます。' } },
    { at: 182, look: '.bay[data-name="IvoryNoether"]',
      en: 'IvoryNoether finishes, and its card stays on screen with everything it produced still attached to it.',
      ja: 'IvoryNoether は終了しますが、カードは画面に残り、作ったものも一緒に確認できます。' },
    { at: 194, look: '.bay[data-name="RustPasteur"]',
      en: 'The answer arrives after 19 seconds. RustPasteur corrects the figure, with a person’s decision on record behind the change.',
      ja: '19秒後に答えが届きます。RustPasteur は図を直し、その変更の裏には人の判断が記録として残ります。' },
    { at: 214, look: '.bay[data-name="MossSomerville"]',
      en: 'MossSomerville writes the handover: what is known, what changed once it was checked, what is still open, and what to do next.',
      ja: 'MossSomerville が引き継ぎを書きます。分かったこと、確かめて変わったこと、まだ未決のこと、次にやること。' },
    { at: 226, look: '.gauge.tot',
      en: 'The collaborator gets a smaller claim than the team started with, and every step behind it. That is progress anyone can check.',
      ja: '共同研究者には、最初より小さくなった主張と、そこに至る全ての手順が届きます。誰でも検証できる進展です。' },
  ];

  var JA = {};
  [
    ['Turn today\'s evidence into a claim the paper can defend', '今日の根拠を、論文が守れる主張へ変える'],
    ['Sit in on the meeting and hand the result to the collaborator', 'ミーティングに立ち会い、結果を共同研究者へ渡す'],
    ['Read what is known about how it recovers, and say where the evidence stops', '回復の仕方について分かっていることを読み、根拠がどこで尽きるかを言う'],
    ['Read what is known about how it recovers, and say where the evidence stops.', '回復の仕方について分かっていることを読み、根拠がどこで尽きるかを言う。'],
    ['Compare studies that pushed the system steadily with ones that pushed it in bursts', '系を一定に押した研究と、断続的に押した研究を比べる'],
    ['Compare studies that pushed the system steadily with ones that pushed it in bursts.', '系を一定に押した研究と、断続的に押した研究を比べる。'],
    ['Look for cases where the equipment, not the sample, produced the result', '試料ではなく装置が結果をつくっていた例を探す'],
    ['Look for cases where the equipment, not the sample, produced the result.', '試料ではなく装置が結果をつくっていた例を探す。'],
    ['Find how these measurements are made, and how they are known to go wrong', 'この測定がどう行われ、どう失敗すると知られているかを調べる'],
    ['Find how these measurements are made, and how they are known to go wrong.', 'この測定がどう行われ、どう失敗すると知られているかを調べる。'],
    ['Collect how long recovery takes, without assuming what causes it', '原因を決めつけずに、回復にかかる時間を集める'],
    ['Collect how long recovery takes, without assuming what causes it.', '原因を決めつけずに、回復にかかる時間を集める。'],
    ['Rebuild the result from the raw measurements and check anything odd', '生の測定値から結果を組み直し、おかしな点を確かめる'],
    ['Rebuild the result from the raw measurements and check anything odd.', '生の測定値から結果を組み直し、おかしな点を確かめる。'],
    ['Follow the discussion and produce evidence, without inventing conclusions', '議論を追い、結論を作らずに根拠を出す'],
    ['Follow the discussion and produce evidence, without inventing conclusions.', '議論を追い、結論を作らずに根拠を出す。'],
    ['Write up only what has actually been checked', '実際に確かめられたことだけを書く'],
    ['Write up only what has actually been checked.', '実際に確かめられたことだけを書く。'],
    ['Check how the measurements would be taken, before the new run', '新しい測定の前に、どう測るのかを確かめる'],
    ['Check how the measurements would be taken, before the new run.', '新しい測定の前に、どう測るのかを確かめる。'],
    ['Find out where an old correction came from', '古い補正がどこから来たのかを突き止める'],
    ['Find out where an old correction came from.', '古い補正がどこから来たのかを突き止める。'],

    ['Task: survey recovery mechanisms', '依頼: 回復機構を調査する'],
    ['Task: compare forcing regimes', '依頼: 負荷条件を比較する'],
    ['Task: audit acquisition artifacts in prior work', '依頼: 先行研究の取得アーティファクトを監査する'],
    ['Task: trace measurement failure modes', '依頼: 測定の失敗モードを追跡する'],
    ['Task: map recovery timescales', '依頼: 回復時間スケールを対応付ける'],
    ['Could I register the comparison paper first?', '比較論文を先に登録してもよいですか'],
    ['RE: yes — checkpointing for your priority row', 'RE: はい — 優先行のためチェックポイント化します'],
    ['Handoff: the shared reference library is released', '引き継ぎ: 共有文献ライブラリを解放しました'],
    ['Handoff: artifact batch may use the shared library', '引き継ぎ: アーティファクトバッチが共有ライブラリを使えます'],
    ['Handoff: measurement batch is next', '引き継ぎ: 次は測定バッチです'],
    ['Handoff: final literature import is ready', '引き継ぎ: 最後の文献インポートの準備ができました'],
    ['RE: received — one import, then release', 'RE: 受領 — 1回インポートして解放します'],
    ['RE: mechanism track mapped with a hard boundary', 'RE: 機構トラックを明確な境界付きで対応付けました'],
    ['RE: forcing track distinguishes direct tests from analogies', 'RE: 負荷トラックで直接試験と類推を区別しました'],
    ['RE: artifact track found the raw-versus-normalized warning', 'RE: アーティファクトトラックで生値と正規化値の警告を見つけました'],
    ['RE: methods track returned three raw-data checks', 'RE: 手法トラックから生データ検査3件が戻りました'],
    ['RE: timescale track keeps timing separate from cause', 'RE: 時間スケールトラックで時間と原因を分けました'],
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
    ['I will split literature into five fields and start them together. Analysis and writing stay separate; the second parent owns the live meeting and final brief.', '文献を5分野に分けて同時に起動する。解析と執筆は分けたままにし、2体目の親がライブミーティングと最終報告を担当する。'],
    ['Five literature tracks are returning bounded notes in sequence. Their library access was negotiated, so consolidation starts from intact work instead of repaired conflicts.', '5つの文献トラックから境界付きノートが順次戻っている。ライブラリ利用は交渉済みなので、衝突を修復するのではなく、壊れていない作業から統合を始められる。'],
    ['The anomaly lands on the exposure boundary. The old effect size is unusable until the segments are calibrated separately.', '異常は露光境界に重なる。区間を別々に較正するまで、古い効果量は使えない。'],
    ['The direction survives correction, but the magnitude is much smaller. That is a useful result and a narrower claim.', '補正後も方向は残るが、大きさはずっと小さい。有用な結果であり、より狭い主張だ。'],
    ['The brief is out with one result, one correction, one open cause, and one next experiment. Handing back.', '報告には、結果1つ、補正1つ、未決の原因1つ、次の実験1つを入れた。引き継ぎます。'],

    ['Accompany the meeting and make the collaborator handoff distinguish results from open questions.', 'ミーティングに伴走し、共同研究者への引き継ぎで結果と未決の問いを区別してください。'],
    ['I will keep decisions, requests, and open questions separate, then report only what survives the evidence and analysis rails.', '決定、依頼、未決の問いを分け、文献と解析の系統を通過したものだけを報告する。'],
    ['That is the right unresolved question. Ask analysis for a figure that exposes the acquisition boundary instead of debating it from the normalized plot.', 'それが正しい未決の問いだ。正規化済みプロットだけで議論せず、取得境界を露出する図を解析担当へ求める。'],
    ['Meeting record closed: decisions and open questions are separate, and every literature statement carries its limitation.', 'ミーティング記録を閉じた。決定と未決の問いを分け、文献に関する各記述に限界を添えた。'],
    ['Lead with the corrected estimate. The provenance matters, but the collaborator needs the result before the audit trail.', '補正後の推定値を先頭に置く。来歴も重要だが、共同研究者には監査履歴より先に結果が必要だ。'],
    ['Brief sent: known, changed after QC, unresolved, and next experiment.', '報告を送信。既知、QC 後の変更、未決、次の実験を分けた。'],

    ['TealLamarr can close a comparison track with one row. Checkpointing locally and handing over is safer than making four children poll the same writer.', 'TealLamarr は1行で比較トラックを閉じられる。子を4体とも1つの書き込み先をポーリングさせるより、ローカルでチェックポイントを作り引き継ぐ方が安全だ。'],
    ['Three mechanism studies are mapped. The closest supports only a possible mechanism, not the measured plateau.', '機構研究3本を対応付けた。最も近い研究が支えるのは機構の可能性だけで、測定したプラトーではない。'],
    ['Answer with what was imposed and measured, then state the mismatch before the sentence can grow stronger in the meeting.', '何を与え何を測ったかで答え、ミーティング中に文が強くなる前に不一致を示す。'],
    ['The safe comparison is now in the meeting: plausible mechanism, no direct validation.', '安全な比較をミーティングへ出した。機構は妥当、直接の検証は無し。'],
    ['Evidence map final. The limitation carried through to the collaborator brief.', '根拠対応表を確定。限界も共同研究者向け報告まで引き継いだ。'],

    ['The forcing track separates direct tests from analogies, and the shared library handoff is complete.', '負荷トラックは直接試験と類推を分け、共有ライブラリの引き継ぎも完了した。'],
    ['The artifact track found why raw frames must stay beside normalized results.', 'アーティファクトトラックは、生フレームを正規化結果の横に残すべき理由を見つけた。'],
    ['The methods track returned three checks that analysis can apply directly to raw measurements.', '手法トラックから、解析が生の測定値に直接適用できる検査3件が戻った。'],
    ['The timescale track preserves timing as evidence without turning it into a cause.', '時間スケールトラックは、時間を原因にすり替えず根拠として保った。'],

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
    ['Mechanism Lit note with claim boundaries', '主張の境界付き機構 Lit ノート'],
    ['Evidence map with claim boundaries', '主張の境界付き根拠対応表'],
    ['Forcing-regime Lit note', '負荷条件 Lit ノート'],
    ['Acquisition-artifact Lit note', '取得アーティファクト Lit ノート'],
    ['Measurement-failure Lit note', '測定失敗 Lit ノート'],
    ['Recovery-timescale Lit note', '回復時間スケール Lit ノート'],
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
