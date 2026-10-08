# project 廃止の契約と移行計画（PR1）

絶対 path の予約は [PR2 の候補契約](absolute-reservations.md)で検証します。PR1 の activation false と既定の旧 mode は保ちます。

[English](project-removal-plan.en.md)

この版は [#213](https://github.com/gyroid-eth/orrery-telemetry/issues/213) の準備です。公開中の 25 tool、project ごとの配送、DB、installer、hook の動作は従来どおりです。新しい契約と計画 API は明示した candidate 呼出にだけ使い、live の切替には接続していません。

## Candidate 契約

`namespace-tools-v2.json` と `NamespaceAdapter` は、旧 `project_key`、macro の `human_key`、contact の `to_project/from_project` を対応する tool で受けて無視します。値の違い・未指定で候補 backend の宛先を変えません。owner token、既存 owner の復旧引数などは保持し、未知の引数を黙って捨てません。candidate の `ensure_project` は no-op です。現在公開している tool は従来どおり project を作ります。

`NamespaceAdapter.dispatch(tool, arguments, backend)` は明示した candidate backend 専用です。本人の確認、操作別の値検証、配送先の contact policy を backend が保ちます。既存の project 必須の関数へそのまま繋ぐ API ではありません。

## Thread を持たない返信

`ReplyCatalog` は message ごとの `reply_to` を扱います。thread の UUID 発行・名前空間・対応表・旧 alias resolver はありません。返信先を省略すれば独立した message です。`CallerEvidence` は trusted な認証/binding 境界が確認した agent ID であり、tool の入力から作ってはいけません。

旧 message の数字 `thread_id` は保存した message ID への `reply_to` に写します。旧値は会話の root を指す場合があり、当時の直接の親を推測しません。名前付き旧値は source の provenance とともに `legacy_thread_label` として読取専用で残します。同じラベル・件名・topic から返信 edge を作りません。欠落、自己参照、循環、不正な ID は移行と restore を拒否します。

返信してよいかは、元 message ごとの sender または to/cc/bcc recipient かで確認します。会話全体の参加者で代用しません。message101 が A/B、message102 が A/C なら、B は101へ返信でき、102へは返信できません。数字の旧入力でも root の非参加者は要更新です。参照できる102への直接の返信を使い、root のチェックを緩めません。

`send_message` の候補 adapter は旧数字 `thread_id` を `reply_to` に変換します。両方を指定する場合は同じ ID のみ受け、違えば `REPLY_INPUT_CONFLICT`。名前付きの書込は `LEGACY_THREAD_READ_ONLY` として更新を求めます。参照先と本人の照合は候補 backend の `ReplyCatalog.resolve()` が行います。

`reply_message(message_id)` の候補 backend は、その ID を直接の親として使う返信 wrapper です。元 thread を継ぐ処理はやめます。宛先既定・件名 prefix・importance/ack/topic 継承の実配線は後続 PR です。

`summarize_thread` は互換入口として、新しい `message_id` または旧 `thread_id` を受けます。数字は message 起点へ変換し、comma 区切りは各起点、名前付きは履歴ラベルの読取として扱います。`legacy_read()` の各 view を集計する backend は重複 message を二重計上しません。旧名の撤去と正規の `summarize_conversation` の公開は client inventory を確認した後です。`fetch_topic` は現在も topic タグ検索なので、その用途を保ちます。

`conversation()` は返信のつながりを辿り、caller が参照できる message ID だけを返します。見えない message の ID・bcc・本文・件数は返さず、一部省略があることだけを示します。読み取りは初期化/更新時の索引を使い、毎ページ全 record を走査しません。`limit`（可視結果、最大1000）と `scan_limit`（node/edge 探索手順、最大1000）を別に設けます。`next_cursor` を同じ caller/起点へ渡すと続きを取得でき、全ページで各可視 message を一度ずつ返します。順序は起点からの決まった探索順で、日時順が必要なら backend が取得後に並べます。探索上限で空のページになる場合も cursor があれば続けます。`scan_exhausted` は探索上限、`has_more` は未探索が残ることを表し、可視結果が必ず残るという意味ではありません。

cursor は内部の frontier/visited/hidden ID を含まない不透明な値で、caller/選択条件に結び付け、単回使用・5分期限・最大128個とします。候補データの変更、期限切れ、不一致は `INVALID_CURSOR` で初めからの再取得を求めます。export/restore に cursor は保存しません。旧 label と旧数字の `legacy_read()` も各 view の cursor を元の入力に渡して継続でき、label は caller が参照できる source だけを返します。探索 budget は各 view ごとです。返された ID を表示/要約へ渡すときも、backend は message ごとの閲覧と bcc の非表示を維持します。実 UI、本文の rendering、LLM 要約はこの PR では実装しません。旧ラベルの読取は source ごとに分け、同名ラベルを会話と断定しません。

`record_delivery()` は owner/contact 検証済みの backend が呼びます。元 message の確認と登録を計画内の lock でまとめ、同じ message の再記録は idempotent、異なる edge への付替えは拒否します。`export()` / `from_export()` は ID、sender/個々の宛先、reply_to、topic、旧 label/source を保ち、参照を再検証します。本文や credential を入力・保存しません。

## 削除する親と残る子

候補の `plan_purge()` / `purge()` は、残る子の祖先を再帰的に保留します。古い親＋新しい子は親を保留し、全て期限切れで残る子がなければ親子とも削除できます。子の cascade、reply_to の null 化、親の付替え、tombstone は行いません。候補数・予定削除/保留と実削除/保留を分けます。

実行時に削除計画を再確認するため、dry-run 後に返信が登録されていれば親は保留します。親の削除が先なら返信は `MESSAGE_UNAVAILABLE`。これは in-memory lock の契約試験です。実 DB の同一 write transaction、RESTRICT/NO ACTION の FK、削除順序と runtime purge への接続は後続 PR3/4 で行います。

## 名前と window

`plan_agent_names()` は退役済みを含む exact/normalized 名前の衝突を全件示します。自動の改名や同名 agent の合体はしません。

`plan_windows(records, {12: "keep", 34: "generate"})` は row ID を指定する入力です。重複する全行を明示し、生成 UUID は receipt に保持します。再開は同じ choices と `prior_targets` を使い、別 UUID を発行しません。期限切れの行と binding も保ち、新しい重複や入力変更は解決を求めます。

## 最終検証と固定した writer 集合

receipt の準備 phase は `planned → prepared → prevalidated → quiesced → final_verified` です。この版では `switched/committed` に遷移できません。

計画 snapshot の非空・重複なしの `expected_writers` を receipt の `writer_roster` に固定します。quiesce/final 検証/current gate は evidence の expected と states の両方をこの roster と照合します。expected と states を同時に縮めても通りません。final snapshot の roster が計画と違えば再計画し、保存/復元後も同じ集合を要求します。trusted collector が対象を列挙する実装は後続 PR です。

最終差分適用後に、名前/window、reply 参照と旧 label、lease、message ID、recipient/read/ack、credential 世代、添付、FTS、配送 state、contact policy、binding、block hash の **13 項目を再検証**します。`FinalValidationEvidence.record(snapshot, candidate, results)` は trusted verifier が実際に検証した snapshot/candidate と結果を結び付けます。古い結果、変化した source/candidate、欠けた検証、未解決、writer 再起動/不明、connection/wake が残る場合は gate を通しません。件数から FTS や所有権を推定しません。

ready でも `activation_enabled` は常に false です。環境変数でも切替を有効にできません。receipt は JSON の明示的な serialize/deserialize だけです。digest は偶発的な変更を検出し、署名や caller 認証を代替しません。保存の権限・atomic な出版、実 collector/停止と authority handoff は後続 PR です。

## 検証と残る範囲

一時 HOME の fixture で message ごとの返信可否、旧数字/ラベル、参照/循環、表示の範囲、1001通以上の全ページ/隠れた中間/旧 label/cursor の拒否、purge の両順序、export/restore、名前/window、writer 集合の同時縮小、stale evidence、無効な activation を検査します。同じ試験は fixture DB を使った実際の v1 MCP 呼出も行い、公開 schema と project 別 inbox が従来どおりであることを確かめます。

実 DB/配送 DB の変換、persistent FK/transaction、filesystem activity/GC、サービス停止/切替、installer/runtime/client/旧 block/UI の変更は含めません。準備 fixture の成功を live の移行成功や raw tool 全体の強制認証と説明しません。通常利用者向けの新しい migration CLI もまだありません。
