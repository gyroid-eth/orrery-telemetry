# project 廃止の契約と移行計画（PR1）

[English](project-removal-plan.en.md)

この版は [#213](https://github.com/gyroid-eth/orrery-telemetry/issues/213) の最初の準備です。**公開中の tool、project ごとの配送、DB、installer、hook の動作は変わりません。** 新しい契約と計画 API は明示的な fixture/candidate の呼出にだけ使い、live の切替には接続していません。

## この版に入るもの

- `namespace-tools-v2.json`: 25 tool の candidate input schema。既存の v1 fixture/公開契約は維持します。移行期間に受ける旧 scope 引数を tool ごとに列挙し、通常の引数と分けています。
- `NamespaceAdapter`: `project_key`、macro の `human_key`、contact の `to_project/from_project` を、対応する tool で受けて無視します。未指定・異なる値でも candidate dispatcher に渡す内容は同じです。owner token 等はそのまま渡し、backend の操作別の検証を代行しません。未知の引数を黙って捨てません。
- `ThreadCatalog`: server 側で ID を発行する契約、旧 thread 入力、message ID の reply、明示した group/separate の計画。DB は開かず、計画内の排他と一意性を検査します。永続 DB の unique 制約と実 tool の配線は後続 PR です。
- `plan_agent_names` / `plan_windows`: 退役済みを含む exact/normalized 名の衝突と、window row ID による UUID の解決。自動の改名・同名 agent の合体・UUID の勝者選択をしません。
- `MigrationReceipt` / `FinalValidationEvidence`: 計画の replay、最終 snapshot に結び付いた検証結果、writer 停止の evidence と gate。ファイル保存、収集、実 DB の検証、writer の停止自体は後続 PR です。

## Candidate adapter

`NamespaceAdapter(load_namespace_contract()).dispatch(tool, arguments, backend)` は candidate backend を明示した場合だけ使います。現在の project 必須の tool 関数へそのまま繋ぐ API ではありません。`ensure_project` は candidate では no-op ですが、現在公開している tool では従来どおり project を作ります。

scope 値を捨てることと、本人の確認を捨てることは別です。raw/proxy/operator の現在の保護を操作ごとに保ちます。thread の `CallerEvidence` は認証/binding 層が確認した情報であり、tool の引数から作ってはいけません。

## Thread の継続

新しい ID は `th-<UUID>` です。ID なしの送信と明示的な新規作成は新しい ID を返し、ラベルが同じでも会話を合体しません。既存 canonical ID、確認済みの旧 alias、元 message ID の reply を区別します。旧 alias が canonical の形でも対応表を確認し、発行 ID が旧 alias と重ならないようにします。

異なる旧 source の同じ thread は、独立した会話の場合と、既存の橋渡し会話の場合があります。重複する alias は確認済みの `ThreadDecision` が必要です。関連を確認した group だけを結び、separate は個別の履歴を保ちます。関連不明を個別保存すると決めた場合は旧 ID の継続を要更新とし、元 message からの reply を使えます。本文や時刻の類似から自動結合しません。

旧 alias は確認済み caller の旧 binding/参加履歴から候補が一つの場合だけ解決します。`project_key` は resolver に入りません。曖昧なら `LEGACY_THREAD_AMBIGUOUS`、関連不明なら `LEGACY_THREAD_UPDATE_REQUIRED`、未知なら `LEGACY_THREAD_UNKNOWN` を返し、第三の同名 thread を作りません。確認できない caller と本人が参照できない message は拒否します。

`ThreadCatalog.export()` と `from_export()` は確定した ID/message 対応を保ちます。再開時に発行し直さず、壊れた対応を拒否します。新規配送の記録は backend の owner/contact 検証後だけ `record_delivery()` へ渡します。

## Window の入力

`plan_windows(records, {12: "keep", 34: "generate"})` のように row ID を指定します。重複 UUID は全ての行に明示した対応が必要です。`generate` の結果は receipt に保管し、再開時は `prior_targets` と同じ choices を渡します。入力が変われば再計画を求めます。期限切れの行と binding も保持し、新たに同じ UUID にした行は unresolved とします。

## 最終の確かめ直しと gate

receipt の準備 phase は `planned → prepared → prevalidated → quiesced → final_verified` です。`switched/committed` は予約した将来の phase で、この版では遷移できません。

初回の検証後にも source は変わります。最終差分適用後に、agent/window/thread/lease の対応、message/recipient/read/ack、credential 世代、添付/FTS、配送 state、contact policy、binding/block hash の **13 項目を全て再検証**します。後続の trusted verifier が `FinalValidationEvidence.record(snapshot, candidate, results)` で、実際に確かめた source/candidate の digest と結果を記録します。件数から FTS や所有権の正しさを推定せず、初回の結果を別の世代へ流用できません。PR1 の fixture に渡す合成結果は、live の検証成功を意味しません。

`FenceEvidence` は共通 authority、旧 supervisor の抑止、全ての予定 writer の停止、connection の終了、inflight wake が 0 と分かることを要求します。空の inventory、unknown、writer 再起動は gate を通しません。共通 authority の取得と観測は後続 PR で実装します。

`receipt.gate(current_snapshot, current_candidate, current_fence)` は最終検証の世代/照合値と現在の状態を比較します。遅れた変更、unresolved、欠けた検証、停止不明なら ready は false。**ready が true でも `activation_enabled` は常に false** です。環境変数でもこの版の切替を有効にできません。

receipt は明示的な JSON の serialize/deserialize のみを提供します。検査用 digest は偶発的な変更を検出するもので、署名や caller 認証を代替しません。保存と更新の権限・atomic な出版は後続 PR で扱います。snapshot の本文を receipt に保存せず digest にし、確定した非秘密の解決入力を保持します。

## 確認と残る範囲

`packages/agentstack_mail/tests/test_namespace_contract_plan.py` は一時 HOME で v2 adapter、衝突/解決/replay、独立/橋渡し thread、最終差分後の stale evidence、writer 再侵入、receipt と無効な切替を検査します。同じ試験は fixture DB を使った実際の v1 MCP 呼出も行い、公開 schema と project 別の inbox が従来どおりであることを確かめます。

実 DB の統合、Codex App 配送 DB の変換、filesystem activity/GC、service の停止/切替、旧 block の撤去/UI、installer/client の更新はこの PR に入りません。通常利用者が新しい migration helper を実行できる版でもありません。既定の利用手順・README・managed block に変更は不要です。
