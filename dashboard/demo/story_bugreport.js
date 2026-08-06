/* Keep this story small and linear: the first-run demo should be readable
   without knowing how payments or software releases work. */
(function () {
  'use strict';

  window.AGENTSTACK_STORIES = window.AGENTSTACK_STORIES || {};

  var CAST = [
    { name: 'AmberKepler', role: 'coordinator', emoji: '🎯', group: 'demo',
      model: 'Opus 5', model_raw: 'claude-opus-5', provider: 'anthropic',
      program: 'claude-code', born: 0, dies: null, ctx0: 24, ctxRate: 0.07,
      task: 'Turn one duplicate-charge report into a verified fix' },
    { name: 'MossSomerville', role: 'release', emoji: '🚦', group: 'demo',
      model: 'Opus 5', model_raw: 'claude-opus-5', provider: 'anthropic',
      program: 'claude-code', born: 0, dies: null, ctx0: 31, ctxRate: 0.05,
      states: [[154, 175, 'question']],
      task: 'Keep the release closed until the fix has proof' },
    { name: 'SlateHooke', parent: 'AmberKepler', role: 'reproduce', emoji: '🔁',
      group: 'demo', model: 'GPT 5.6', model_raw: 'gpt-5.6',
      provider: 'openai', program: 'codex', born: 15, dies: 88,
      ctx0: 8, ctxRate: 0.18,
      task: 'Reproduce the report with one click in a safe checkout' },
    { name: 'TealLamarr', parent: 'AmberKepler', role: 'cause', emoji: '🔎',
      group: 'demo', model: 'GPT 5.6', model_raw: 'gpt-5.6',
      provider: 'openai', program: 'codex', born: 20, dies: 101,
      ctx0: 10, ctxRate: 0.16,
      task: 'Find why one payment attempt can be recorded twice' },
    { name: 'CopperBose', parent: 'AmberKepler', role: 'impact', emoji: '👥',
      group: 'demo', model: 'Sonnet 5', model_raw: 'claude-sonnet-5',
      provider: 'anthropic', program: 'claude-code', born: 25, dies: 112,
      ctx0: 7, ctxRate: 0.14, states: [[55, 76, 'ask']],
      task: 'Count who was affected without exposing customer details' },
    { name: 'IvoryNoether', parent: 'AmberKepler', role: 'fix', emoji: '🛠️',
      group: 'demo', model: 'GPT 5.6', model_raw: 'gpt-5.6',
      provider: 'openai', program: 'codex', born: 66, dies: 184,
      ctx0: 9, ctxRate: 0.2,
      task: 'Fix the duplicate record and prove one click stays one charge' },
  ];

  var SCRIPT = [
    { at: 16, from: 'AmberKepler', to: 'SlateHooke',
      subject: 'Task: reproduce the double charge with one click',
      importance: 'high', ack: true,
      body:
        'Use the safe checkout and press Pay exactly once. Slow the confirmation, because that is what the customer described.\n' +
        '\n' +
        'Bring back a short list of steps and the two records side by side. Do not touch real customer records.',
      body_ja:
        '安全なテスト用の支払い画面で、支払うボタンを1回だけ押してください。利用者の説明どおり、確認の返事を遅らせます。\n' +
        '\n' +
        '短い再現手順と、2つの記録を並べた証拠を返してください。本物の利用者記録には触れないこと。' },
    { at: 21, from: 'AmberKepler', to: 'TealLamarr',
      subject: 'Task: find why one payment becomes two records',
      importance: 'high',
      body:
        'Trace one payment from the button press to the saved record. Compare the first attempt with anything that happens while confirmation is slow.\n' +
        '\n' +
        'Explain the cause in ordinary words. Change nothing yet; IvoryNoether will make the fix after we know what is wrong.',
      body_ja:
        '支払うボタンを押してから記録が保存されるまで、1回の支払いを追ってください。確認が遅い間に起きる別の動きと、最初の処理を比べます。\n' +
        '\n' +
        '原因は普通の言葉で説明してください。まだ変更はしないこと。原因が分かってから IvoryNoether が直します。' },
    { at: 26, from: 'AmberKepler', to: 'CopperBose',
      subject: 'Task: count affected customers, with privacy intact',
      importance: 'high',
      body:
        'First prepare a count that uses masked customer numbers, order number, amount and time only.\n' +
        '\n' +
        'Before reading production payment records, stop and ask for approval. Return totals, not names or payment details.',
      body_ja:
        '伏せ字の利用者番号、注文番号、金額、時刻だけを使う集計を先に準備してください。\n' +
        '\n' +
        '本番の支払い記録を読む前に止まり、承認を求めること。名前や支払い情報ではなく、件数だけを返してください。' },
    { at: 42, from: 'SlateHooke', to: 'AmberKepler',
      subject: 'RE: reproduced — one click created two charges',
      importance: 'high',
      body:
        'Reproduced three times. I pressed Pay once, the confirmation stayed blank for eight seconds, and two payment records appeared.\n' +
        '\n' +
        'Both records have the same order and amount. The second arrives four seconds after the first. Screenshots and exact steps are attached to the reproduction note.',
      body_ja:
        '3回再現しました。支払うボタンは1回だけ押し、確認が8秒間空白のままになると、支払い記録が2つ現れます。\n' +
        '\n' +
        '2つは注文番号も金額も同じで、2つ目は1つ目の4秒後です。画面と正確な手順を再現ノートに残しました。' },
    { at: 55, from: 'TealLamarr', to: 'AmberKepler',
      subject: 'RE: cause found — the wait starts a second attempt',
      importance: 'high',
      body:
        'The first payment keeps running while the page waits for confirmation. After five seconds, the page starts the same payment again.\n' +
        '\n' +
        'The second attempt does not recognize the first one, so both are saved. One payment attempt needs one shared identity from start to finish.',
      body_ja:
        '画面が確認を待っている間も、最初の支払い処理は動き続けています。5秒たつと、画面が同じ支払いをもう一度始めます。\n' +
        '\n' +
        '2回目は1回目を同じものと認識しないため、両方が保存されます。1回の支払いには、最初から最後まで共通の目印が必要です。' },
    { at: 57, from: 'CopperBose', to: 'AmberKepler',
      subject: 'Approval needed before reading production payment records',
      importance: 'high', ack: true,
      body:
        'The count is ready, but it must read production payment records. It selects only masked customer number, order number, amount and time.\n' +
        '\n' +
        'Approve that read, or I will stop here. The result will contain totals only.',
      body_ja:
        '集計の準備はできましたが、本番の支払い記録を読む必要があります。選ぶのは伏せ字の利用者番号、注文番号、金額、時刻だけです。\n' +
        '\n' +
        'この読み取りを承認してください。承認がなければ、ここで止まります。結果には件数だけを残します。' },
    { at: 67, from: 'AmberKepler', to: 'IvoryNoether',
      subject: 'Task: stop a repeated attempt from charging twice',
      importance: 'high', ack: true,
      body:
        'SlateHooke reproduced the report and TealLamarr found the cause: a slow confirmation starts the same payment again.\n' +
        '\n' +
        'Make both attempts share one payment identity, then add a repeat test: one click, slow confirmation, one saved charge. Bring back the failing result before the passing one.',
      body_ja:
        'SlateHooke が報告を再現し、TealLamarr が原因を見つけました。確認が遅いと、同じ支払いをもう一度始めています。\n' +
        '\n' +
        '2回の処理が1つの支払いの目印を共有するよう直し、再発テストを追加してください。ボタン1回、遅い確認、保存される請求は1つ。失敗した結果と、直した後の成功結果を両方返すこと。' },
    { at: 76, from: 'AmberKepler', to: 'CopperBose',
      subject: 'Approved: masked fields only, totals only',
      importance: 'high',
      body:
        'Approved for this incident and date range. Read only the four masked fields you listed.\n' +
        '\n' +
        'Do not copy rows into the report. Keep the count and the earliest and latest affected times, then close the access.',
      body_ja:
        'この不具合と対象期間に限って承認します。挙げた4つの伏せ字項目だけを読んでください。\n' +
        '\n' +
        '行を報告書へ写さないこと。件数と、影響が始まった時刻・最後の時刻だけを残し、アクセスを閉じてください。' },
    { at: 94, from: 'CopperBose', to: 'AmberKepler',
      subject: 'RE: 23 customers, 27 extra charges',
      importance: 'high',
      body:
        'The approved count found 23 customers and 27 extra charges. The earliest was shortly after the slow-confirmation change; the latest was this morning.\n' +
        '\n' +
        'Access is closed. The report contains totals and time bounds only, with no customer names or payment details.',
      body_ja:
        '承認された集計で、影響を受けた利用者23人、余分な請求27件が見つかりました。最初は確認が遅くなる変更の直後、最後は今朝です。\n' +
        '\n' +
        'アクセスは閉じました。報告には件数と期間だけを残し、利用者名や支払い情報は含めていません。' },
    { at: 118, from: 'IvoryNoether', to: 'AmberKepler',
      subject: 'RE: fixed — one click stays one charge',
      importance: 'high',
      body:
        'Before the fix, the repeat test made two charges from one click. After the fix, the same slow confirmation makes one charge, ten times in a row.\n' +
        '\n' +
        'All payment tests pass. The second attempt now finds the first payment and returns its result instead of saving another charge.',
      body_ja:
        '修正前は、再発テストで1回のクリックから請求が2つできました。修正後は、同じ遅い確認でも請求は1つで、10回続けて成功しました。\n' +
        '\n' +
        '支払いテストはすべて通過。2回目の処理は最初の支払いを見つけ、別の請求を保存せず、その結果を返します。' },
    { at: 132, from: 'MossSomerville', to: 'AmberKepler',
      subject: 'Release still closed — send the impact and refund plan',
      importance: 'normal',
      body:
        'The fix has a good repeat test, but the release record still needs the affected count and what happens to the extra charges.\n' +
        '\n' +
        'The gate stays closed until those two facts are attached. A passing test alone does not finish the incident.',
      body_ja:
        '修正には良い再発テストがありますが、リリース記録には影響件数と余分な請求への対応がまだ必要です。\n' +
        '\n' +
        'その2点が添付されるまでゲートは閉じたままです。テストが通っただけでは、不具合対応は終わりません。' },
    { at: 144, from: 'AmberKepler', to: 'MossSomerville',
      subject: 'Evidence complete: fix, affected count and refund plan',
      importance: 'high', ack: true,
      body:
        'Evidence attached: the failure reproduced, the cause explained, the repeat test passes ten times, and 23 customers have 27 extra charges.\n' +
        '\n' +
        'The support plan reverses every extra charge and sends a plain-language notice. The release can now move to a human timing decision.',
      body_ja:
        '証拠を添付します。不具合を再現し、原因を説明し、再発テストは10回通過。影響は利用者23人、余分な請求27件です。\n' +
        '\n' +
        '案内計画では余分な請求をすべて取り消し、普通の言葉で通知します。これでリリース時刻を人が決める段階へ進めます。' },
    { at: 176, from: 'MossSomerville', to: 'AmberKepler',
      subject: 'Gate open: release now, then watch for repeats',
      importance: 'high',
      body:
        'The human chose release now. The evidence pack is complete, so the gate is open.\n' +
        '\n' +
        'Release the fix, then watch new payment records for the same order and amount. If one appears twice, stop and roll back.',
      body_ja:
        '人が今すぐリリースすると選びました。証拠一式が揃ったので、ゲートを開きます。\n' +
        '\n' +
        '修正を出した後、同じ注文番号と金額の新しい支払い記録を監視してください。2つ現れたら止めて元へ戻します。' },
    { at: 201, from: 'MossSomerville', to: 'AmberKepler',
      subject: 'Release check: no new duplicate charges',
      importance: 'high',
      body:
        'The fix is live. New payments are completing, and no order has produced a second charge during the watch period.\n' +
        '\n' +
        'The release gate is closed behind the finished change. Keep the incident open only for refunds and customer notices.',
      body_ja:
        '修正は反映済みです。新しい支払いは完了しており、監視時間中に2つ目の請求ができた注文はありません。\n' +
        '\n' +
        '完了した変更の後ろでリリースゲートを閉じました。不具合対応は返金と利用者への案内だけ継続します。' },
    { at: 218, from: 'AmberKepler', to: 'MossSomerville',
      subject: 'Incident report complete: what happened, impact and fix',
      importance: 'normal',
      body:
        'What happened: a slow confirmation started the same payment twice. Impact: 23 customers received 27 extra charges. Fix: repeated attempts now return the first result.\n' +
        '\n' +
        'The repeat test stayed green after release, and no new duplicate appeared. Refund and notification work is recorded for follow-up.',
      body_ja:
        '何が起きたか: 確認が遅いと同じ支払いを2回始めていました。影響: 利用者23人に余分な請求27件。修正: 繰り返された処理は最初の結果を返します。\n' +
        '\n' +
        'リリース後も再発テストは成功し、新しい二重請求はありません。返金と案内は後続作業として記録しました。' },
  ];

  var TRANSCRIPTS = {
    AmberKepler: [
      [0, 'user', 'text',
       'A customer reports: “I pressed Pay once, but I was charged twice.”'],
      [3, 'assistant', 'thinking',
       'First prove the report, then explain the cause, count the impact and fix it. Real payment records stay closed until a person approves access.'],
      [7, 'assistant', 'text',
       'Plan: SlateHooke reproduces it, TealLamarr traces the cause, and CopperBose prepares a privacy-safe count. I will hand the proven cause to a fixer.'],
      [13, 'assistant', 'tool_use', 'Bash  agentstack spawn --role reproduce'],
      [14, 'assistant', 'tool_result', 'started SlateHooke (gpt-5.6)'],
      [18, 'assistant', 'tool_use', 'Bash  agentstack spawn --role cause'],
      [19, 'assistant', 'tool_result', 'started TealLamarr (gpt-5.6)'],
      [23, 'assistant', 'tool_use', 'Bash  agentstack spawn --role impact'],
      [24, 'assistant', 'tool_result', 'started CopperBose (claude-sonnet-5)'],
      [43, 'assistant', 'text',
       'SlateHooke reproduced the report with one click. This is a real failure, not a misunderstanding.'],
      [56, 'assistant', 'thinking',
       'TealLamarr found the handoff: the page starts again while the first payment is still running. Now the fix can be narrow and testable.'],
      [64, 'assistant', 'tool_use', 'Bash  agentstack spawn --role fix'],
      [66, 'assistant', 'tool_result', 'started IvoryNoether (gpt-5.6)'],
      [95, 'assistant', 'text',
       'CopperBose counted 23 affected customers without bringing names or payment details into the report.'],
      [119, 'assistant', 'text',
       'IvoryNoether showed the failure before the fix and ten clean runs after it. That is the proof the release gate needs.'],
      [143, 'assistant', 'tool_use', 'SendMessage  to=MossSomerville "evidence complete"'],
      [177, 'assistant', 'tool_result', 'MossSomerville opened the release gate'],
      [181, 'assistant', 'tool_use', 'Computer  release the approved fix'],
      [183, 'assistant', 'tool_result', 'release completed'],
      [211, 'assistant', 'tool_use', 'Write  reports/duplicate-charge-incident.md'],
      [214, 'assistant', 'text',
       'Final report: a slow confirmation repeated one payment; 23 customers had 27 extra charges; repeated attempts now return the first result; monitoring found no new duplicates.'],
    ],
    MossSomerville: [
      [0, 'user', 'text',
       'Hold the release until the duplicate charge is reproduced, fixed and counted.'],
      [4, 'assistant', 'text',
       'Gate rule: no release on a promise. I need the failing example, a passing repeat test, the affected count and a refund plan.'],
      [126, 'assistant', 'tool_use', 'Read  evidence/fix-and-repeat-test.md'],
      [127, 'assistant', 'tool_result', 'one failure before fix; ten clean runs after fix'],
      [130, 'assistant', 'thinking',
       'The code proof is good, but people were already charged twice. The impact and refund plan belong in the same decision.'],
      [146, 'assistant', 'tool_use', 'Read  reports/affected-count.md'],
      [147, 'assistant', 'tool_result', '23 customers; 27 extra charges; refund plan attached'],
      [154, 'assistant', 'text',
       'The proof is complete. Human choice needed: release now and watch closely, or wait for the planned release window?'],
      [175, 'user', 'text',
       'Release now. Watch every new payment for a repeat, and roll back if one appears.'],
      [177, 'assistant', 'tool_use', 'Computer  open the release gate'],
      [178, 'assistant', 'tool_result', 'gate open'],
      [188, 'assistant', 'tool_use', 'Computer  watch new payment records'],
      [199, 'assistant', 'tool_result', 'no duplicate orders found'],
      [204, 'assistant', 'text',
       'The fix is live and the watch stayed clean. The remaining work is refunds and plain-language customer notices.'],
    ],
    SlateHooke: [
      [15, 'user', 'text',
       'Reproduce the customer report with one click in the safe checkout.'],
      [18, 'assistant', 'tool_use', 'Computer  open the safe checkout'],
      [22, 'assistant', 'tool_use', 'Computer  set confirmation delay to eight seconds'],
      [25, 'assistant', 'tool_use', 'Computer  click “Pay” once'],
      [34, 'assistant', 'tool_result', 'two payment records appeared'],
      [36, 'assistant', 'tool_use', 'Screenshot  one click and two charge records'],
      [39, 'assistant', 'thinking',
       'The two records share the same order and amount. Their four-second gap is the clue the cause trace needs.'],
      [41, 'assistant', 'text',
       'Reproduced three times: one click, a slow confirmation, then two charges. Exact steps and screenshots sent to AmberKepler.'],
      [82, 'assistant', 'tool_use', 'Write  evidence/reproduction.md'],
      [84, 'assistant', 'tool_result', 'saved steps and screenshots'],
    ],
    TealLamarr: [
      [20, 'user', 'text',
       'Find why one payment attempt can be recorded twice. Explain it without internal names.'],
      [24, 'assistant', 'tool_use', 'Read  the payment steps from button to saved record'],
      [31, 'assistant', 'tool_use', 'Screenshot  first and second attempt on one timeline'],
      [38, 'assistant', 'thinking',
       'The first attempt is still running when the waiting page starts the second. The two attempts never learn they belong together.'],
      [52, 'assistant', 'text',
       'Cause found: after five seconds without confirmation, the page starts the same payment again. Both attempts are saved because they do not share one identity.'],
      [96, 'assistant', 'tool_use', 'Write  evidence/cause-timeline.md'],
      [98, 'assistant', 'tool_result', 'saved plain-language timeline'],
    ],
    CopperBose: [
      [25, 'user', 'text',
       'Count affected customers with masked fields only. Ask before reading production records.'],
      [29, 'assistant', 'tool_use', 'Write  checks/affected-count.sql'],
      [34, 'assistant', 'tool_result', 'query selects masked customer, order, amount and time only'],
      [52, 'assistant', 'thinking',
       'The count is ready, but running it crosses the line into real payment records. Stop and make the boundary visible.'],
      [56, 'assistant', 'text',
       'Approval requested: read four masked fields from production payment records and return totals only.'],
      [76, 'user', 'text',
       'Approved for this incident and date range. Keep no rows and close access after the count.'],
      [78, 'assistant', 'tool_use', 'Query  duplicate orders within five minutes'],
      [89, 'assistant', 'tool_result', '23 customers; 27 extra charges'],
      [92, 'assistant', 'text',
       'Impact confirmed: 23 customers and 27 extra charges. Access is closed; the report contains no names or payment details.'],
      [107, 'assistant', 'tool_use', 'Write  reports/affected-count.md'],
      [109, 'assistant', 'tool_result', 'saved totals and time bounds only'],
    ],
    IvoryNoether: [
      [66, 'user', 'text',
       'Stop a repeated payment attempt from creating a second charge, then prove it.'],
      [70, 'assistant', 'tool_use', 'Read  evidence/reproduction.md'],
      [72, 'assistant', 'tool_use', 'Read  evidence/cause-timeline.md'],
      [77, 'assistant', 'thinking',
       'The retry is useful when a request is lost. The safe fix is not to remove it, but to make both attempts point to the same payment.'],
      [82, 'assistant', 'tool_use', 'Write  tests/one-click-one-charge.test'],
      [86, 'assistant', 'tool_result', 'failed: expected one charge, found two'],
      [91, 'assistant', 'tool_use', 'Edit  make repeated attempts reuse the first payment identity'],
      [101, 'assistant', 'tool_use', 'Test  one click with slow confirmation, repeat ten times'],
      [112, 'assistant', 'tool_result', '10 passed; one charge in every run'],
      [116, 'assistant', 'text',
       'Fixed and proved: the test fails before the change and passes ten times after it. A repeated attempt now returns the first payment result.'],
      [152, 'assistant', 'tool_use', 'Write  evidence/fix-and-repeat-test.md'],
      [180, 'assistant', 'text',
       'Evidence handed off. The release decision belongs to MossSomerville, so I am stopping here.'],
    ],
  };

  var DELIVERABLES = {
    AmberKepler: [
      ['Duplicate-charge incident report', 'reports/duplicate-charge-incident.md', 12],
    ],
    MossSomerville: [
      ['Release decision record', 'reports/release-decision.md', 18],
    ],
    SlateHooke: [
      ['Reproduction steps and screenshots', 'evidence/reproduction.md', 156],
    ],
    TealLamarr: [
      ['Plain-language cause timeline', 'evidence/cause-timeline.md', 142],
    ],
    CopperBose: [
      ['Affected-customer count', 'reports/affected-count.md', 130],
    ],
    IvoryNoether: [
      ['Fix and repeat-test evidence', 'evidence/fix-and-repeat-test.md', 64],
    ],
  };

  var BEATS = [
    { at: 0, look: '.gauge.run',
      en: 'A customer says one click caused two charges. AmberKepler coordinates the investigation while MossSomerville keeps the release closed.',
      ja: '利用者から「1回押したのに2回請求された」と届きました。AmberKepler が調査をまとめ、MossSomerville がリリースを閉じておきます。' },
    { at: 14, look: '.bay[data-name="SlateHooke"]',
      en: 'AmberKepler starts SlateHooke to reproduce the report safely. SlateHooke must press Pay exactly once.',
      ja: 'AmberKepler が安全な再現のため SlateHooke を起動します。SlateHooke は支払うボタンを1回だけ押します。' },
    { at: 20, look: '.bay[data-name="TealLamarr"]',
      en: 'TealLamarr traces why a slow confirmation turns one payment into two records.',
      ja: 'TealLamarr は、確認が遅いと1回の支払いが2つの記録になる理由を追います。' },
    { at: 25, look: '.bay[data-name="CopperBose"]',
      en: 'CopperBose sets out to count how many customers were hit, arranging it so that it never has to see anyone’s personal details.',
      ja: 'CopperBose は何人の利用者に影響したかを数えにかかります。誰の個人情報も見なくて済むように用意します。' },
    { at: 42, look: '.bay[data-name="SlateHooke"]',
      en: 'SlateHooke proves the report: one click and a slow confirmation create two charges, three times in a row.',
      ja: 'SlateHooke が報告を確かめました。1回のクリックと遅い確認で請求が2つでき、3回続けて再現しました。' },
    { at: 55, look: '.bay[data-name="CopperBose"]',
      en: 'CopperBose has stopped before looking at real customers’ records and is asking a person for permission. Its card shows red APPROVAL while it waits.',
      ja: 'CopperBose は実際の利用者の記録を見る前で止まり、人に許可を求めています。待っている間、CopperBose のカードに赤い APPROVAL が出ます。',
      net: {
        en: 'CopperBose has stopped before looking at real customers’ records and is asking a person for permission. A ! sits over the CopperBose portrait while it waits.',
        ja: 'CopperBose は実際の利用者の記録を見る前で止まり、人に許可を求めています。待っている間、CopperBose の肖像の上に ! が出ます。' } },
    { at: 66, look: '.bay[data-name="IvoryNoether"]',
      en: 'Now that the cause is known, AmberKepler starts IvoryNoether to fix it, and to show the fix works rather than say it does.',
      ja: '原因が分かったので、AmberKepler は IvoryNoether を起動して修正させます。直ったと言うのではなく、直ったことを見せるところまでやらせます。' },
    { at: 76, look: '.bay[data-name="CopperBose"]',
      en: 'The permission comes back narrowed: CopperBose may see only what the count needs, and only until the count is done.',
      ja: '許可は狭められて返ってきます。CopperBose は数えるのに要る分だけを、数え終わるまでの間だけ見られます。' },
    { at: 94, look: '.bay[data-name="CopperBose"]',
      en: 'CopperBose reports the human impact without exposing anyone: 23 customers and 27 extra charges.',
      ja: 'CopperBose は誰の情報も見せずに影響を報告します。利用者23人、余分な請求27件です。' },
    { at: 118, look: '.bay[data-name="IvoryNoether"]',
      en: 'IvoryNoether shows the proof rather than claiming it: two charges before the fix, and one charge every time across ten tries after it.',
      ja: 'IvoryNoether は主張ではなく証拠を出します。修正前は請求が2つ、修正後は10回試して毎回1つです。' },
    { at: 132, look: '.bay[data-name="MossSomerville"]',
      en: 'MossSomerville still will not let it out. A working fix is not enough on its own: the people already charged need a plan too.',
      ja: 'MossSomerville はまだ出させません。修正が動くだけでは足りず、すでに請求された人たちへの手当ても要るという判断です。' },
    { at: 154, look: '.bay[data-name="MossSomerville"]',
      en: 'MossSomerville has all the proof and is asking a person to choose: release now or wait for the planned window. Its card shows a cyan ? until the choice arrives.',
      ja: 'MossSomerville は証拠をすべて揃え、人に選択を求めています。今すぐ出すか、予定した時間を待つか。選ばれるまで MossSomerville のカードにシアンの ? が出ます。',
      net: {
        en: 'MossSomerville has all the proof and is asking a person to choose: release now or wait for the planned window. A ? sits over the MossSomerville portrait until the choice arrives.',
        ja: 'MossSomerville は証拠をすべて揃え、人に選択を求めています。今すぐ出すか、予定した時間を待つか。選ばれるまで MossSomerville の肖像の上に ? が出ます。' } },
    { at: 175, look: '.bay[data-name="MossSomerville"]',
      en: 'The person chooses to release now. MossSomerville lets it out and keeps watching for the problem coming back.',
      ja: '人は今すぐ出すことを選びました。MossSomerville はそれを通し、問題が再発しないか見張り続けます。' },
    { at: 184, look: '.bay[data-name="IvoryNoether"]',
      en: 'IvoryNoether finishes after handing over the fix and its proof. The finished IvoryNoether card stays visible as part of the record.',
      ja: 'IvoryNoether は修正と証拠を引き渡して終了します。完了した IvoryNoether のカードは記録として画面に残ります。' },
    { at: 201, look: '.bay[data-name="MossSomerville"]',
      en: 'MossSomerville watches real payments go through again, each charged once. The fix is out and nothing has come back.',
      ja: 'MossSomerville は、実際の支払いがまた通り始め、どれも請求が1回だけであることを見届けます。修正は世に出て、問題は戻ってきていません。' },
    { at: 218, look: '.bay[data-name="AmberKepler"]',
      en: 'AmberKepler closes with one plain report: what happened, who was affected, how it was fixed and what follow-up remains.',
      ja: 'AmberKepler は1つの分かりやすい報告で締めます。何が起きたか、誰に影響したか、どう直したか、残る対応は何かをまとめます。' },
  ];

  var JA = (function () {
    var pairs = [
      ['Turn one duplicate-charge report into a verified fix', '二重請求の報告1件を、証拠のある修正まで進める'],
      ['Keep the release closed until the fix has proof', '修正の証拠が揃うまでリリースを閉じておく'],
      ['Reproduce the report with one click in a safe checkout', '安全な支払い画面で1回だけ押し、報告を再現する'],
      ['Find why one payment attempt can be recorded twice', '1回の支払いが2回記録される理由を見つける'],
      ['Count who was affected without exposing customer details', '利用者情報を見せず、影響を受けた人数を数える'],
      ['Fix the duplicate record and prove one click stays one charge', '重複記録を直し、1回のクリックが1件の請求になると証明する'],

      ['Task: reproduce the double charge with one click', '依頼: 1回のクリックで二重請求を再現する'],
      ['Task: find why one payment becomes two records', '依頼: 1回の支払いが2つの記録になる理由を探す'],
      ['Task: count affected customers, with privacy intact', '依頼: 利用者情報を守りながら影響人数を数える'],
      ['RE: reproduced — one click created two charges', '返信: 再現しました — 1回のクリックで請求が2つできました'],
      ['RE: cause found — the wait starts a second attempt', '返信: 原因を発見 — 待っている間に2回目が始まります'],
      ['Approval needed before reading production payment records', '本番の支払い記録を読む前に承認が必要です'],
      ['Task: stop a repeated attempt from charging twice', '依頼: 繰り返された処理で2回請求されないようにする'],
      ['Approved: masked fields only, totals only', '承認: 伏せ字の項目だけ、結果は件数だけ'],
      ['RE: 23 customers, 27 extra charges', '返信: 利用者23人、余分な請求27件'],
      ['RE: fixed — one click stays one charge', '返信: 修正完了 — 1回のクリックは1件の請求になりました'],
      ['Release still closed — send the impact and refund plan', 'リリースはまだ閉鎖中 — 影響件数と返金計画が必要です'],
      ['Evidence complete: fix, affected count and refund plan', '証拠が完成: 修正、影響件数、返金計画'],
      ['Gate open: release now, then watch for repeats', 'ゲートを開放: 今すぐリリースし、繰り返しを監視します'],
      ['Release check: no new duplicate charges', 'リリース確認: 新しい二重請求はありません'],
      ['Incident report complete: what happened, impact and fix', '不具合報告が完成: 原因、影響、修正'],

      ['A customer reports: “I pressed Pay once, but I was charged twice.”', '利用者から報告です。「支払うボタンは1回しか押していないのに、2回請求されました」'],
      ['First prove the report, then explain the cause, count the impact and fix it. Real payment records stay closed until a person approves access.', 'まず報告を確かめ、原因を説明し、影響を数えて直します。本物の支払い記録は、人が承認するまで閉じておきます。'],
      ['Plan: SlateHooke reproduces it, TealLamarr traces the cause, and CopperBose prepares a privacy-safe count. I will hand the proven cause to a fixer.', '計画: SlateHooke が再現し、TealLamarr が原因を追い、CopperBose が利用者情報を守った集計を準備します。確かめた原因を修正担当へ渡します。'],
      ['SlateHooke reproduced the report with one click. This is a real failure, not a misunderstanding.', 'SlateHooke が1回のクリックで報告を再現しました。勘違いではなく、本当の不具合です。'],
      ['TealLamarr found the handoff: the page starts again while the first payment is still running. Now the fix can be narrow and testable.', 'TealLamarr がつなぎ目を見つけました。最初の支払いが動いている間に、画面がもう一度始めています。これで修正範囲を狭くし、テストできます。'],
      ['CopperBose counted 23 affected customers without bringing names or payment details into the report.', 'CopperBose は名前や支払い情報を報告へ持ち込まず、影響を受けた利用者23人を数えました。'],
      ['IvoryNoether showed the failure before the fix and ten clean runs after it. That is the proof the release gate needs.', 'IvoryNoether は修正前の失敗と、修正後10回の成功を示しました。これがリリースゲートに必要な証拠です。'],
      ['Final report: a slow confirmation repeated one payment; 23 customers had 27 extra charges; repeated attempts now return the first result; monitoring found no new duplicates.', '最終報告: 確認が遅いと支払いが繰り返されていました。利用者23人に余分な請求27件。繰り返された処理は最初の結果を返すようになり、監視では新しい重複はありません。'],

      ['Hold the release until the duplicate charge is reproduced, fixed and counted.', '二重請求を再現し、修正し、影響件数を数えるまでリリースを止めてください。'],
      ['Gate rule: no release on a promise. I need the failing example, a passing repeat test, the affected count and a refund plan.', 'ゲートの規則: 約束だけではリリースしません。失敗例、成功する再発テスト、影響件数、返金計画が必要です。'],
      ['The code proof is good, but people were already charged twice. The impact and refund plan belong in the same decision.', '修正の証拠は十分ですが、すでに二重請求された人がいます。影響と返金計画も同じ判断に含めます。'],
      ['The proof is complete. Human choice needed: release now and watch closely, or wait for the planned release window?', '証拠は揃いました。人の選択が必要です。今すぐ出して注意深く監視するか、予定したリリース時間を待つか。'],
      ['Release now. Watch every new payment for a repeat, and roll back if one appears.', '今すぐリリースしてください。新しい支払いが繰り返されないか監視し、現れたら元へ戻してください。'],
      ['The fix is live and the watch stayed clean. The remaining work is refunds and plain-language customer notices.', '修正は反映され、監視でも問題はありません。残る作業は返金と、分かりやすい利用者への案内です。'],

      ['Reproduce the customer report with one click in the safe checkout.', '安全な支払い画面で1回だけ押し、利用者の報告を再現してください。'],
      ['The two records share the same order and amount. Their four-second gap is the clue the cause trace needs.', '2つの記録は注文番号も金額も同じです。4秒の差が、原因を追う手がかりになります。'],
      ['Reproduced three times: one click, a slow confirmation, then two charges. Exact steps and screenshots sent to AmberKepler.', '3回再現しました。1回のクリック、遅い確認、その後に請求が2つ。正確な手順と画面を AmberKepler に送りました。'],

      ['Find why one payment attempt can be recorded twice. Explain it without internal names.', '1回の支払いが2回記録される理由を探し、内部名を使わず説明してください。'],
      ['The first attempt is still running when the waiting page starts the second. The two attempts never learn they belong together.', '待っている画面が2回目を始めた時、最初の処理はまだ動いています。2つは同じ支払いだと分からないままです。'],
      ['Cause found: after five seconds without confirmation, the page starts the same payment again. Both attempts are saved because they do not share one identity.', '原因を発見。確認がないまま5秒たつと、画面が同じ支払いをもう一度始めます。共通の目印がないため、両方が保存されます。'],

      ['Count affected customers with masked fields only. Ask before reading production records.', '伏せ字の項目だけで影響人数を数えてください。本番の記録を読む前に承認を求めること。'],
      ['The count is ready, but running it crosses the line into real payment records. Stop and make the boundary visible.', '集計は準備できましたが、実行すると本物の支払い記録へ入ります。止まって、その境界を見えるようにします。'],
      ['Approval requested: read four masked fields from production payment records and return totals only.', '承認を求めました。本番の支払い記録から伏せ字の4項目を読み、件数だけを返します。'],
      ['Approved for this incident and date range. Keep no rows and close access after the count.', 'この不具合と対象期間に限って承認します。行は残さず、集計後にアクセスを閉じてください。'],
      ['Impact confirmed: 23 customers and 27 extra charges. Access is closed; the report contains no names or payment details.', '影響を確認しました。利用者23人、余分な請求27件。アクセスは閉じ、報告に名前や支払い情報は含めていません。'],

      ['Stop a repeated payment attempt from creating a second charge, then prove it.', '繰り返された支払い処理で2件目の請求ができないよう直し、証明してください。'],
      ['The retry is useful when a request is lost. The safe fix is not to remove it, but to make both attempts point to the same payment.', '返事が届かない時にはやり直しが役立ちます。安全な修正はやり直しを消すことではなく、2回とも同じ支払いを指すようにすることです。'],
      ['Fixed and proved: the test fails before the change and passes ten times after it. A repeated attempt now returns the first payment result.', '修正し、証明しました。変更前はテストが失敗し、変更後は10回通ります。繰り返された処理は最初の支払い結果を返します。'],
      ['Evidence handed off. The release decision belongs to MossSomerville, so I am stopping here.', '証拠を引き渡しました。リリース判断は MossSomerville の役割なので、ここで終了します。'],

      ['Duplicate-charge incident report', '二重請求の不具合報告'],
      ['Release decision record', 'リリース判断の記録'],
      ['Reproduction steps and screenshots', '再現手順と画面'],
      ['Plain-language cause timeline', '分かりやすい原因の時系列'],
      ['Affected-customer count', '影響を受けた利用者の件数'],
      ['Fix and repeat-test evidence', '修正と再発テストの証拠'],
      ['no transcript on disk for this agent', 'このエージェントの会話ログはディスク上にありません'],
      ['demo mode — nothing was started, stopped or changed', 'デモモード — 何も起動・停止・変更していません'],
    ];
    var table = {};
    pairs.forEach(function (pair) { table[pair[0]] = pair[1]; });
    return table;
  })();

  window.AGENTSTACK_STORIES.bugreport = {
    id: 'bugreport',
    preferred: true,   // the story a first-time visitor lands on

    label: { en: 'A bug report', ja: '不具合の報告から修正まで' },
    loop: 240,
    opensAt: 8,
    cast: CAST,
    past: [],
    script: SCRIPT,
    transcripts: TRANSCRIPTS,
    deliverables: DELIVERABLES,
    beats: BEATS,
    ja: JA,
  };
})();
