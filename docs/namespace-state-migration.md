# Mail と配送の記録を統合する候補

[English](namespace-state-migration.en.md)

PR3 は隔離した snapshot の変換と復旧を準備します。`activation=false` で、公開 tool、installer、旧 DB schema、daemon の配送、hook、dashboard API 12 は変わりません。実運用の DB、環境変数、サービス、設定 pointer は変更しません。[契約と計画](project-removal-plan.md)と[絶対 path 予約](absolute-reservations.md)の候補を永続 store に結びます。

## 入力と解決

`namespace_state_io.SourceBundle` に Mail DB、配送 DB、archive、signals、history、bindings JSON、config JSON の **7つの明示した path** を渡します。探索して実 DB を選ぶ入口はありません。入力は Mail instance ID を持つ現行 Mail schema と旧配送 v1 schema が前提で、未対応の schema は停止します。`namespace_transform.plan_report(source, choices)` は read-only で衝突した全 agent ID、window、曖昧な予約、返信の不整合、未解決の配送を返します。token と message 本文は診断に出しません。未解決のまま `NamespaceMigration` を進めると停止します。

| choices | 必要な決定 |
| --- | --- |
| `rename_map` | 衝突する agent ID ごとの名前。retired も含み、自動で勝者を選ばない |
| `window_map` / `window_agents` | window 行 ID ごとの `keep` / `generate` / 明示 UUID と、旧表示名で一意に決まらない owner の agent ID |
| `window_targets` | 新しい計画へ持ち越す発行済み UUID。通常の再実行では receipt から復元 |
| `lease_anchors` / `as_of` | active な相対予約の行 ID ごとの絶対 cwd と、期限判定の時刻 |
| `reply_map` | 不正・欠損・自己参照・循環する旧数字 thread の message ID ごとの修正先 ID または null |
| `delivery_map` | 旧配送 key が曖昧なときの agent ID。既存の宛先行と照合する |
| `wake_recovery` | 旧 leased 配送について、未完了 wake を確認した key の集合 |

配送 key は `delivery_key(project, name, message_id)` が返す compact JSON 配列の文字列です。policy key は同じ関数の message ID なしです。改行を区切りにせず、名前や path に改行があっても曖昧になりません。config の `delivery_policies` に policy key ごとの `version`、`coalesce_seconds`、`base_backoff_seconds`、`max_backoff_seconds` を明示します。旧 DB に保存されていない backoff の設定を推測しません。

config の `expected_writers` は、Mail、配送、archive、signal、予約 GC、retention など、予定した全 writer の固定集合です。fixture では `instruction_blocks: []` を明示します。未実装の managed block collector を「確認済み」とは扱いません。block を持つ入力の実運用への切替には後続 PR の collector が必要です。

## 保存と照合

Mail の agent / message / recipient / lease / window の数値 ID、Mail instance ID、owner token、credential generation、read / ack、to / cc / bcc、本文、添付 JSON、監査、補助 table を保持します。旧 project は `legacy_projects` と各 `legacy_*project_id` に来歴として残します。新しい global message と予約は来歴列を null にでき、旧 project を選ぶ必要がありません。summary は来歴として保存し `cache_valid=0` にします。

旧数字 thread は直接の `reply_to` として解釈し、名前付き旧値は source と label を読取専用で保持します。親のない値や循環を自動修復しません。返信と purge は同じ SQLite の write transaction で競合を直列化します。残る子の祖先を purge から保留し、全てが削除対象なら子と親を同時に削除します。新返信の親が既に削除されていれば拒否します。新返信は親 message ごとの参加者と配送 policy callback を確認します。

配送の新主キーは `(mail_instance_id, agent_id, message_id)` です。全5 status、attempt、owner、lease 期限、error、作成・更新・完了日時を保持し、policy と次回時刻を追加します。旧 table とその trigger も来歴として残します。終端 status の再配送を DB 制約で拒否し、期限切れ lease の回収と新 owner による再試行を試験します。保存済み日時は元の文字列を保持し、期限比較は UTC の時点で行います。

archive は安定した agent ID / 旧 project ID へ配置し、添付 map は旧 path と新 path の hash を結びます。元の Git 履歴を含む archive 全体は最終 snapshot に保持します。candidate archive は内容を照合したコピーで、新しい Git writer の配線は後続です。history と旧 signal の bytes も保持し、active な新 signal は Mail instance ID / agent ID / message metadata で別に発行します。旧広域 signal は `needs_inbox=true` とし、message を捏造しません。bindings は stable ID と owner token を照合し、window UUID の決定を反映します。

FK、全 table の行・column、FTS の本文と index integrity、添付と全 file の hash、配送の全 status を最初と最後に再検証します。[SQLite の integrity-check](https://www.sqlite.org/fts5.html#the_integrity_check_command) は candidate のみに実行します。source は read-only 接続と SQLite backup API で WAL の確定分を含めて取得します。

## phase と復旧

`NamespaceMigration(source, workspace, choices).resume(fence=collector)` は所有を確認した private workspace のみを変更します。source と重なる path、symlink、所有確認のない既存の出力先は拒否します。receipt と pointer は fsync / atomic replace で保存し、排他的 lock を使います。DB / token を含む出力は秘密として扱い、他の利用者へ配布しないでください。

1. `planned`: 衝突を解決し、UUID と writer 集合を receipt に固定。
2. `prepared`: 初期 source が計画時と同じことを確認して snapshot。
3. `prevalidated`: candidate を変換し、全内容を照合。
4. `quiesced`: 全 writer が停止、同一 authority、supervisor の再起動停止、接続終了、in-flight 0 の `FenceEvidence` を確認。
5. `final_verified`: 停止後の差分を snapshot へ反映し、再変換と全照合。source・candidate・writer generation と evidence を gate に結ぶ。
6. `switched`: workspace 内の `candidate-pointer.json` を更新。
7. `committed`: 同じ gate と pointer を確認して receipt を確定。

`switched` は **workspace の切替練習** で、実サービスを切り替えません。collector は呼出側が提供する証拠の入口で、PR3 自体は OS の writer を止めません。fixture の停止済み証拠を実運用に流用しないでください。毎回 source と候補を確認し、writer 集合の増減や block scope の変更も拒否します。初期 snapshot より前に source が変わった場合は新しい計画が必要です。最終再検証後の変化は gate で止めます。

各 phase の前後と pointer 更新直後で process を強制終了する試験があります。同じ source、choices、workspace で再実行し、receipt の UUID と解決を再利用します。`status()` は phase、検証数、rollback 可否を返します。`rollback()` は candidate が最終確認時から全く変わらない場合だけ workspace pointer を戻します。新しい Mail / 配送 / 予約の書込みや file の変化後は拒否し、forward fix / export が必要です。

## 明示した候補の利用と残る作業

`CandidateMailStore`、`PersistentReservations`、`CandidateDelivery` は candidate path と expected generation を明示して構築します。旧 writer generation は拒否します。配送 policy callback は必須です。Mail の候補書込みは DB のみで、archive / signal の実配送 writer、公開 tool と proxy の結線、サービスの停止・再起動、installed pointer、managed block の移行、CLI の実切替は後続 PR に残ります。既定の25 tool の契約は保ちます。

case-insensitive filesystem の非 ASCII path は #232 の未解決のため予約を拒否します。期限切れの旧予約は履歴として保持しますが、active な未知の予約は移行を止めます。Mac の日本語 path を含む実運用を切り替える前に、この制限を解決する必要があります。

試験は一時 HOME の旧 schema fixture から始め、実 DB を読みません。配布物の wheel / sdist と、checkout を参照しない installed candidate も確認します。

pointer の authority・receipt ID・generation・activation が違えば、完了済みからの resume も停止します。rollback の pointer 更新後に中断した場合は、candidate が変わっていなければ同じ `rollback()` を再実行して receipt を確定し、その後に resume できます。

signal の metadata 省略設定も保持します。per-message file の ID は file 名から照合し、subject・送信者・本文を補いません。旧単一 signal と per-message が同じ ID を持つ場合は、`legacy-<ID>.signal` と `<ID>.signal` に分けて来歴と元 bytes を残します。無効な signal は plan に path と reason を出します。予約の Mail 活動は送信・受信の全時刻を UTC へ変換してから最新を選び、offset の表記差で早期解放しません。
