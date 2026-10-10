# 隔離 global 予約・contact welcome（S2c）

[English](global-reservations.en.md)

## 増える公開契約

既定は旧 mode、`activation_enabled=false` のままです。S1/S2a/S2b の既存 profile は変えません。完成した schema5 の設定を明示した隔離 server だけが次の能力を公開します。正本は [global-server-s2c.json](../packages/agentstack_mail/fixtures/global-server-s2c.json)。

| tool | 動作 |
| --- | --- |
| file_reservation_paths | write |
| renew_file_reservations | write |
| release_file_reservations | write |
| list_file_reservations | read |
| check_file_reservations | read |
| macro_file_reservation_cycle | write |
| macro_start_session | write |
| macro_contact_handshake | 既存名の welcome 拡張 |

既存 global 24 tool に新規7つを加え、計31です。`macro_prepare_thread` は公開せず、`summarize_thread` の cursor と `fetch_inbox` をそれぞれ使います。管理 socket の `force_release_reservation`・`purge_messages` は同 UID の operator 専用で、公開 MCP tool に増やしません。

## 唯一の予約状態

`file_reservations` が唯一の lease 正本です。ACTIVE は `released_ts IS NULL AND expires_ts > :now_utc`。期限到達時は判定が変わるだけで、row・revision・receipt を書き換えません。PR2 の in-memory engine、copy/flush adapter、activity probe と collector は削除しました。旧 mode の既存 app の activity 判定は維持します。

server は絶対 path または `tool://`・`resource://`・`service://` を受け取ります。観測した cwd と明示した WSL mapping での相対名の変換は client の役割です。`project_key` を anchor にしません。共通 normalizer/overlap は symlink・新規 file・glob・Unicode・空白を扱い、不明な filesystem 規則を楽観的に許可しません。case-insensitive filesystem の非 ASCII case table が不明なら取得を拒否します。

別 owner の予約と重なり、いずれかが exclusive なら batch 全件を拒否します。共有同士は共存します。自己 ACTIVE の同一 pattern/exclusive は同じ ID を再利用して期限を最大値へ延ばし、reason は保ちます。期限切れの自己履歴は変更せず、新規 ID を割り当てます。

更新・解放は ID と path の片方を選び、両方指定は `LEASE_SELECTOR_CONFLICT`、明示した空配列は `LEASE_SELECTOR_EMPTY`。省略時は自己 ACTIVE だけです。path は正規化後の同一 pattern の自己 ACTIVE だけを選び、更新は未一致 path があれば LEASE_NOT_FOUND。解放は未一致 path を無視し、省略/path で自己 ACTIVE が0件でも released/already_released_ids が空配列の成功を返します。新 UUID は新しい0件 receipt、同 UUID は元の receipt の replay です。他 owner・自己の履歴は選択しません。明示 ID の不存在・他 owner は全件拒否。更新は現在期限へ延長秒数を足し、期限切れの更新は `LEASE_EXPIRED`。明示 ID の解放は期限切れ行も解放できます。TTL/延長は60–86400秒、取得100 path・選択100行・新しい reason500文字。旧長 reason/期限は読取・更新・解放で維持します。共通 preimage の16KiB・応答の1MiB上限も適用し、macro の出力超過は全件 rollback します。

`list_file_reservations` は ID keyset で100行ずつ。cursor の owner/tool/query が違えば拒否します。最初の MAX を固定して新規 ID を続きへ混ぜず、既存行の期限・解放は各 page の現在状態を返します。check/list は活動・quota・通知を更新しません。check は concrete path の最小 ID の witness 一つを返します。

`macro_file_reservation_cycle(auto_release=true)` は今回作った行だけを解放し、自己再利用行は残します。`macro_start_session` は既存 owner の活動更新・取得・純粋な inbox 読取を同じ transaction で行います。登録・program/model/window 更新・read/ack はしません。

coverage は既知の witness があれば別行の規則が不明でも成功します。witness がなく不明な行が残る場合は ACTIVE_LEASE_RULES_UNKNOWN とし、false と断定しません。session macro は fetch_inbox と選択処理を共有して body_md=null の metadata を返し、read/ack 状態を変えません。

## 旧 mode との差

| 項目 | 旧 mode | 隔離 schema5 |
| --- | --- | --- |
| 予約競合 | 競合以外を部分付与しconflicts/holdersを返す | 全件atomic RESERVATION_CONFLICT、conflicts配列無し |
| 期限切れrenew | max(old expiry,now)から延長し復活する | 明示選択はLEASE_EXPIRED、省略選択はactiveだけ |
| cycle reason既定 | macro-file_reservation | 空文字。start_sessionのmacro-sessionは維持 |
| auto_release | 今回指定した既存自己pathも解放 | 今回新規作成したleaseだけ解放 |
| 期限・活動・回収 | 既存appのactivity/grace/stale sweep、expiry cleanup | 固定幅UTCの期限導出のみ。collectorも活動probeも無し |
| selector / batch | 旧toolのselector/返却形。PR2候補はIDとpathの和集合も許す | ID XOR path、省略/pathはown ACTIVEだけ。100件超は拒否（expired履歴を数えない） |
| 解放の再実行 / 不存在ID | 解放済み・不存在の予約はno-op | 省略/pathの自己ACTIVE 0件は成功0件。明示IDの不存在/他ownerは全件拒否。renew path不一致はLEASE_NOT_FOUNDを維持 |
| owner / scope / paths | project/nameと相対path | stable ID/token/binding、projectは無視、abs/virtual scope |
| macro_start_session | project/登録と予約/inboxのmacro | 既存ownerだけ、touch/reserve/inboxの同TX |
| thread macro | raw catalogにmacro_prepare_thread（namespace互換対象外） | 追加しない |
| force / purge | raw agent catalogの強制解放/期限削除 | same-UID管理socketのみ |
| welcome | 旧handshakeの申請/送信の振舞い | 未承認は申請だけ、相手承認後新UUID。blocked優先 |
| 入力と旧実値 | 長reason/offset日時/旧ID | 新入力paths100/reason500/TTL60..86400。旧reason/IDは保存、日時instantだけ正規化 |
| 通知・既定運用 | 既存project modeは現状どおり | signalはS2b1本、default legacy/activation=false |

## 時刻・退役・管理復旧

lease の3時刻は一つの helper で固定幅 UTC `YYYY-MM-DDTHH:MM:SS.ffffff+00:00` へ変換し、CHECK と起動時 roundtrip で確認します。この列だけ SQL TEXT 比較を許し、他の時刻は従来の UTC instant 比較です。S2a fixture の文章を1行だけ追記し、schema と確定拒否の分類は変えません。

退役時は ACTIVE だけを `release_cause=retired` にします。既に期限切れの未解放履歴は NULL のままで inactive。unretire で復活しません。退役中は owner 認証で拒否し、同じ資格情報で unretire した後の同 UUID は当時の receipt を返します。現在の lease は別の read で確認します。

force の前に既存 `action=inspect` へ `agent_id` と任意の `reservation_id` を指定し、同 snapshot の lease/revision を読みます。force は owner と expected lease revision と明示 confirm/note を要求します。応答喪失や `STALE_LEASE_REVISION` の後もこの inspect で現在状態を確認し、expected revision を自動更新して再 force しません。照合には revision・状態を使い、release_note は使いません。retired owner の管理 inspect も可能です。

purge は cutoff・after_id・limit・dry_run を明示します。祖先保持・FTS・添付 blob・通知 dirty は既存の同じ transaction で処理します。held-only page でも next_after_id は走査した最後の ID へ進みます。more=false で終了。既存 provenance の archive/配送 DB 等は不変の保管物です。

## Welcome と中断

相手 owner の承認が無い contact は request-only。welcome は保留保存しません。承認後に新しい UUID で明示した welcome を送信します。古い UUID は元の request-only receipt の再送です。要求者だけの自動承認は拒否し、明示 blocked/block_all を優先します。welcome は通常 message と FTS・recipient・signal dirty・receipt を同じ transaction で確定します。

commit 前の例外は全件 rollback、commit 後の応答喪失は同 UUID/同 intent で replay します。receipt は返却 schema・canonical preimage/hash・binding を照合します。signal の書込み中断は S2b の reconcile で回収します。予約側に外部出力・活動 GC・新 journal はありません。readiness は top-level status、配送の健康は signal.status を使います。

確定拒否は S2b 表を優先し、未掲載だけ S2a、最後に S2c delta を合成します。共通 classifier 一つで処理し、UUID 衝突・transport/output 不明は保持します。

| reason | operation | phase | existing_pending | 処理 |
| --- | --- | --- | --- | --- |
| LEASE_SELECTOR_CONFLICT | write | before_receipt_lookup | False | clear_exact_own_planned |
| LEASE_SELECTOR_CONFLICT | write | before_receipt_lookup | True | preserve_exact_bytes |
| LEASE_SELECTOR_EMPTY | write | before_receipt_lookup | False | clear_exact_own_planned |
| LEASE_SELECTOR_EMPTY | write | before_receipt_lookup | True | preserve_exact_bytes |
| ABSOLUTE_PATH_REQUIRED | write | before_receipt_lookup | False | clear_exact_own_planned |
| ABSOLUTE_PATH_REQUIRED | write | before_receipt_lookup | True | preserve_exact_bytes |
| ABSOLUTE_PATH_REQUIRED | read | read_only | False | unchanged |
| ABSOLUTE_PATH_REQUIRED | read | read_only | True | unchanged |
| TTL_OUT_OF_RANGE | write | before_receipt_lookup | False | clear_exact_own_planned |
| TTL_OUT_OF_RANGE | write | before_receipt_lookup | True | preserve_exact_bytes |
| WELCOME_INPUT_INCOMPLETE | write | before_receipt_lookup | False | clear_exact_own_planned |
| WELCOME_INPUT_INCOMPLETE | write | before_receipt_lookup | True | preserve_exact_bytes |
| RESERVATION_CONFLICT | write | after_receipt_lookup | False | clear_exact_own_planned |
| RESERVATION_CONFLICT | write | after_receipt_lookup | True | clear_exact_own_planned |
| LEASE_OWNER_REQUIRED | write | after_receipt_lookup | False | clear_exact_own_planned |
| LEASE_OWNER_REQUIRED | write | after_receipt_lookup | True | clear_exact_own_planned |
| LEASE_NOT_FOUND | write | after_receipt_lookup | False | clear_exact_own_planned |
| LEASE_NOT_FOUND | write | after_receipt_lookup | True | clear_exact_own_planned |
| LEASE_EXPIRED | write | after_receipt_lookup | False | clear_exact_own_planned |
| LEASE_EXPIRED | write | after_receipt_lookup | True | clear_exact_own_planned |
| LEASE_SELECTION_TOO_LARGE | write | after_receipt_lookup | False | clear_exact_own_planned |
| LEASE_SELECTION_TOO_LARGE | write | after_receipt_lookup | True | clear_exact_own_planned |
| ACTIVE_LEASE_RULES_UNKNOWN | write | after_receipt_lookup | False | clear_exact_own_planned |
| ACTIVE_LEASE_RULES_UNKNOWN | write | after_receipt_lookup | True | clear_exact_own_planned |
| ACTIVE_LEASE_RULES_UNKNOWN | read | read_only | False | unchanged |
| ACTIVE_LEASE_RULES_UNKNOWN | read | read_only | True | unchanged |
| LEASE_ID_CAPACITY_REACHED | write | after_receipt_lookup | False | clear_exact_own_planned |
| LEASE_ID_CAPACITY_REACHED | write | after_receipt_lookup | True | clear_exact_own_planned |
| CHECK_REQUIRES_CONCRETE_PATH | read | read_only | False | unchanged |
| CHECK_REQUIRES_CONCRETE_PATH | read | read_only | True | unchanged |
| STALE_LEASE_REVISION | management | management_only | either | unchanged |
| OWNER_REQUIRED | write | before_receipt_lookup | False | clear_exact_own_planned |
| OWNER_REQUIRED | write | before_receipt_lookup | True | preserve_exact_bytes |
| REQUEST_ID_CONFLICT | write | preserve_pending | False | preserve_exact_bytes |
| REQUEST_ID_CONFLICT | write | preserve_pending | True | preserve_exact_bytes |
| TRANSPORT_FAILED | write | preserve_pending | False | preserve_exact_bytes |
| TRANSPORT_FAILED | write | preserve_pending | True | preserve_exact_bytes |
| RESPONSE_INVALID | write | preserve_pending | False | preserve_exact_bytes |
| RESPONSE_INVALID | write | preserve_pending | True | preserve_exact_bytes |
| UNLISTED_SYNTHETIC_REASON | write | preserve_pending | False | preserve_exact_bytes |
| UNLISTED_SYNTHETIC_REASON | write | preserve_pending | True | preserve_exact_bytes |
| TIMESTAMP_INVALID | write | after_receipt_lookup | False | clear_exact_own_planned |
| TIMESTAMP_INVALID | write | after_receipt_lookup | True | clear_exact_own_planned |
| WRITER_FENCED | write | preserve_pending | False | preserve_exact_bytes |
| WRITER_FENCED | write | preserve_pending | True | preserve_exact_bytes |

### 期限切れ履歴の期待

| 操作 | 期限切れ件数 | ACTIVE | 選択数 | 結果 | 期限切れの行 |
| --- | --- | --- | --- | --- | --- |
| release omitted | 10000 | 0 | 0 | ok | unchanged |
| release omitted | 10000 | 2 | 2 | ok | unchanged |
| renew omitted | 10000 | 0 | 0 | ok | unchanged |
| renew omitted | 10000 | 2 | 2 | ok | unchanged |
| retire hook | 10000 | 0 | 0 | ok | unchanged |
| retire hook | 10000 | 2 | 2 | ok | unchanged |
| fresh retired owner as_of | 10000 | 0 | 0 | ok | unchanged |
| fresh retired owner as_of | 10000 | 2 | 2 | ok | unchanged |
| release omitted | 10000 | 101 | 101 | LEASE_SELECTION_TOO_LARGE | unchanged |
| retire hook | 10000 | 101 | 101 | ok | unchanged |
| release explicit expired ID | 10000 | 0 | 1 | ok | only explicit ID changes |

## Fresh preparation と確認

完成した preparation receipt を持つ schema5 config だけを受け入れます。receipt の無い root は DB を開かず `CANDIDATE_SCHEMA_REQUIRES_REPREPARE`。旧 schema2/3/4 root を上書きせず、元 source から別 workspace/name に新規準備します。未 release の master API で作った開発用 candidate も同じ扱いです。

[S2a の plan と source/fence](global-server.md#s2a-schema3-の新規準備)を使い、plan kind を `orrery-s2c-preparation-plan-v1`、実行を `agentstack-mail-global-prepare-s2c --plan plan.json` にします。出力は `isolation_root/s2c-candidates/name/candidate/` 固定。PR3→S2a→S2b の保存証明の後、非公開 staging 内だけで schema5 を生成します。旧時刻の instant を保ち、choices.as_of で退役 owner の ACTIVE 行だけを退役変換し、reservation-proof.json で全行を照合します。最終 manifest/gate→ready→rename→complete は既存の一つの state machine です。どの保存境界で止まっても同 plan/UUID で再開し、foreign slot は上書きしません。

完成後は新 config を明示して空き port・専用 HOME・passthrough の条件で server を起動し、HTTP health の profile=s2c-v1/status=ok まで待ちます。実運用の env/wrapper/hook/profile の client_config を向け直すのは operator の作業です。4b/4c の reservation/message/contact 呼出の実結線と PR7 の切替はこの PR に含めません。

確認例（repo checkout と専用 venv で実行）:

```sh
fixture=$(mktemp -d)
HOME="$fixture" TMUX_TMPDIR="$fixture" .venv/bin/python -m pytest -q -o addopts= packages/agentstack_mail/tests/test_global_server_s2c.py
```

| 確認 | 回帰試験 |
| --- | --- |
| HTTP health/catalog/同 ID lifecycle | test_real_http_catalog_lifecycle |
| commit 前後の同 UUID | test_mutation_interruption_replays_once |
| prepare の各保存境界 | test_fresh_publication_interruption_is_recoverable |
| 期限切れ1万行・部分索引 | test_expired_history_is_not_selected_or_rewritten |
| force 応答喪失/STALE の読取出口 | test_force_release_inspect_and_stale_recovery |
| macro atomic/再利用行保持 | test_cycle_releases_only_new_ids_and_rolls_back_conflicts |
| 退役前から期限切れの NULL 保持 | test_retirement_changes_only_active_rows_and_unretire_preserves_receipt |
| welcome 承認/blocked/同 UUID | test_contact_welcome_has_no_deferred_queue / test_welcome_block_priority_rolls_back_all |
| receipt 前 DB 接続なし | test_receipt_required_before_sqlite_open |

## 旧 engine の試験の移設

下表は承認した72 test 名の対応です。normalizer・旧 app の活動/liveness・Mail/配送・既定 proxy は維持し、engine 専用の probe/collect は削除します。legacy の57-path performance gate は旧 app を直接計測するため保持します。adapter の fixture transport は正規 S2c runtime を呼び、別 engine/state を持ちません。

| source / test | 処遇 |
| --- | --- |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_overlap_fixture | 維持: normalizer/CLI/hookの単体検査。削除engine/probe importsとmodule fixtureを整理 |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_two_cwds_and_ignored_scope | SQLへ: 唯一S2c runtimeのfixture/実HTTPにbackend差替え、normalizer/owner/guard/releaseのassert保持 |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_symlink_dotdot_new_file_and_lifecycle | SQLへ: 唯一S2c runtimeのfixture/実HTTPにbackend差替え、normalizer/owner/guard/releaseのassert保持 |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_glob_symlink_directory_conflicts_for_future_file | SQLへ: 唯一S2c runtimeのfixture/実HTTPにbackend差替え、normalizer/owner/guard/releaseのassert保持 |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_glob_symlink_file_and_bounded_unknown | 維持: normalizer/CLI/hookの単体検査。削除engine/probe importsとmodule fixtureを整理 |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_ambiguous_paths_rejected | 維持: normalizer/CLI/hookの単体検査。削除engine/probe importsとmodule fixtureを整理 |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_unicode_preserved_when_filesystem_distinguishes_it | 維持: normalizer/CLI/hookの単体検査。削除engine/probe importsとmodule fixtureを整理 |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_linux_native_directory_flags_fail_closed | 維持: normalizer/CLI/hookの単体検査。削除engine/probe importsとmodule fixtureを整理 |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_missing_unicode_leaves_follow_actual_filesystem | SQLへ: 唯一S2c runtimeのfixture/実HTTPにbackend差替え、normalizer/owner/guard/releaseのassert保持 |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_unicode_glob_overlap_and_recent_activity | 分割: glob/concrete alias重なりをSQLへ、probe/collect recent枝は削除 |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_distinguishing_unicode_rules_preserve_missing_names | 維持: normalizer/CLI/hookの単体検査。削除engine/probe importsとmodule fixtureを整理 |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_unknown_unicode_rules_reject_acquire_and_prevent_stale | 分割: unknown追加grant拒否・SQL row不変とTTL前保持へ、probe/stale枝は削除 |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_unknown_unicode_directory_cannot_prove_empty_glob | 削除: 廃止probe/collector専用。正規化/overlapの独立assertがあれば同named caseへ残す |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_supported_unicode_rules_match_canonical_variants | 維持: normalizer/CLI/hookの単体検査。削除engine/probe importsとmodule fixtureを整理 |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_hfs_exclusions_and_unknown_apfs_unicode_version | 維持: normalizer/CLI/hookの単体検査。削除engine/probe importsとmodule fixtureを整理 |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_case_alias_follows_filesystem | 維持: normalizer/CLI/hookの単体検査。削除engine/probe importsとmodule fixtureを整理 |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_missing_nonascii_case_alias_never_grants_twice | SQLへ: 唯一S2c runtimeのfixture/実HTTPにbackend差替え、normalizer/owner/guard/releaseのassert保持 |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_nonascii_case_table_unknown_rejects_initial_acquire | SQLへ: 非ASCII4paramの初回grant拒否、DB/revision/receipt不変 |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_nonascii_case_glob_activity_unknown_prevents_collection | 削除: 廃止probe/collector専用。正規化/overlapの独立assertがあれば同named caseへ残す |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_case_sensitive_unicode_case_names_stay_distinct | SQLへ: 唯一S2c runtimeのfixture/実HTTPにbackend差替え、normalizer/owner/guard/releaseのassert保持 |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_future_file_under_case_alias_and_case_variant | SQLへ: 唯一S2c runtimeのfixture/実HTTPにbackend差替え、normalizer/owner/guard/releaseのassert保持 |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_explicit_wsl_mapping_and_virtual_type | 維持: normalizer/CLI/hookの単体検査。削除engine/probe importsとmodule fixtureを整理 |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_shared_and_atomic_conflicts | SQLへ: 唯一S2c runtimeのfixture/実HTTPにbackend差替え、normalizer/owner/guard/releaseのassert保持 |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_owner_and_token_enforced | SQLへ: 唯一S2c runtimeのfixture/実HTTPにbackend差替え、normalizer/owner/guard/releaseのassert保持 |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_ttl_validation | SQLへ: 唯一S2c runtimeのfixture/実HTTPにbackend差替え、normalizer/owner/guard/releaseのassert保持 |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_unknown_no_early_release_but_ttl_and_owner_work | SQLへ: unknownでもTTL前はrow不変、期限到達は導出のみ、明示owner release可 |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_renew_extends_current_expiry_and_noop_is_not_activity | SQLへ: old expiry+extend、noop row不変。旧activity機構assertは削除 |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_expired_gc_does_not_need_filesystem_evidence | SQLへ: expired行のread後bytes/revision不変、collector無し |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_raw_candidate_tool_names_share_lifecycle | SQL/実HTTPへ: public取得→renew→release同ID、削除engine専用alias assertは削除 |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_unmatched_initial_grace_then_stale | 削除: 廃止probe/collector専用。正規化/overlapの独立assertがあれば同named caseへ残す |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_probe_permission_timeout_cap_and_unknown_anchor | 廃止 probe/collector 枝だけを削除し、dangling-anchor の独立 normalizer assertion を保持 |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_each_lease_actual_repo_git_activity_and_symlink | 削除: 廃止probe/collector専用。正規化/overlapの独立assertがあれば同named caseへ残す |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_broad_glob_multiple_repos_and_git_failure | 削除: 廃止probe/collector専用。正規化/overlapの独立assertがあれば同named caseへ残す |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_recent_deletion_commit_keeps_empty_scope_active | 削除: 廃止probe/collector専用。正規化/overlapの独立assertがあれば同named caseへ残す |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_gc_rechecks_activity_after_probe | 削除: 廃止probe/collector専用。正規化/overlapの独立assertがあれば同named caseへ残す |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_candidate_hook_any_folder_unmanaged_and_bound_outage | SQLへ: 唯一S2c runtimeのfixture/実HTTPにbackend差替え、normalizer/owner/guard/releaseのassert保持 |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_failed_candidate_edit_keeps_lease_without_dispatch | SQLへ: 唯一S2c runtimeのfixture/実HTTPにbackend差替え、normalizer/owner/guard/releaseのassert保持 |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_successful_candidate_edit_releases_lease | SQLへ: 唯一S2c runtimeのfixture/実HTTPにbackend差替え、normalizer/owner/guard/releaseのassert保持 |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_cli_and_bash32_hook_same_normalizer | 維持: normalizer/CLI/hookの単体検査。削除engine/probe importsとmodule fixtureを整理 |
| packages/agentstack_mail/tests/test_absolute_reservations.py / test_candidate_not_published_or_activated | 更新して維持: default legacy/activation=false、唯一SQL runtimeの未配線を確認 |
| packages/agentstack_mail/tests/test_namespace_state_runtime.py / test_window_reconnect_preserves_id_token_and_distinguishes_resolved_uuid | 維持: 削除engineに依存しない既存試験 |
| packages/agentstack_mail/tests/test_namespace_state_runtime.py / test_reply_permission_is_per_message_and_new_edge_is_direct | 維持: 削除engineに依存しない既存試験 |
| packages/agentstack_mail/tests/test_namespace_state_runtime.py / test_named_legacy_thread_and_conflicting_input_remain_rejected | 維持: 削除engineに依存しない既存試験 |
| packages/agentstack_mail/tests/test_namespace_state_runtime.py / test_retention_holds_ancestors_and_removes_complete_expired_sets | 維持: 削除engineに依存しない既存試験 |
| packages/agentstack_mail/tests/test_namespace_state_runtime.py / test_reply_first_serializes_purge_in_the_same_write_transaction | 維持: 削除engineに依存しない既存試験 |
| packages/agentstack_mail/tests/test_namespace_state_runtime.py / test_read_ack_and_bcc_are_retained_without_cross_recipient_leak | 維持: 削除engineに依存しない既存試験 |
| packages/agentstack_mail/tests/test_namespace_state_runtime.py / test_persistent_reservations_preserve_imported_ids_and_owner_on_restart | SQL移設/分割: engine_test_migrationの対応でlease assert保持、同じ試験の返信/配送/default proxy assertは維持 |
| packages/agentstack_mail/tests/test_namespace_state_runtime.py / test_persistent_lease_unknown_does_not_release_early_but_ttl_still_expires | SQL移設/分割: engine_test_migrationの対応でlease assert保持、同じ試験の返信/配送/default proxy assertは維持 |
| packages/agentstack_mail/tests/test_namespace_state_runtime.py / test_delivery_terminal_observe_preserves_status_attempts_and_backoff | 維持: 削除engineに依存しない既存試験 |
| packages/agentstack_mail/tests/test_namespace_state_runtime.py / test_delivery_expired_lease_recovers_without_restarting_old_wake | 維持: 削除engineに依存しない既存試験 |
| packages/agentstack_mail/tests/test_namespace_state_runtime.py / test_same_message_id_is_independent_in_another_mail_instance | 維持: 削除engineに依存しない既存試験 |
| packages/agentstack_mail/tests/test_namespace_state_runtime.py / test_old_writer_generation_cannot_update_mail_leases_or_delivery | SQL移設/分割: engine_test_migrationの対応でlease assert保持、同じ試験の返信/配送/default proxy assertは維持 |
| packages/agentstack_mail/tests/test_namespace_state_runtime.py / test_delivery_legacy_expiry_compares_instants_at_boundary | 維持: 削除engineに依存しない既存試験 |
| packages/agentstack_mail/tests/test_namespace_state_runtime.py / test_delivery_terminal_transition_is_enforced_by_database | 維持: 削除engineに依存しない既存試験 |
| packages/agentstack_mail/tests/test_namespace_state_runtime.py / test_reservation_latest_mail_is_selected_by_utc_instant | SQL移設/分割: engine_test_migrationの対応でlease assert保持、同じ試験の返信/配送/default proxy assertは維持 |
| integrations/codex_app/tests/test_mcp_server.py / test_absolute_candidate_keeps_default_proxy_contract | SQL移設/分割: engine_test_migrationの対応でlease assert保持、同じ試験の返信/配送/default proxy assertは維持 |
| integrations/codex_app/tests/test_mcp_server.py / test_bootstrap_binds_process_and_runtime_status_never_exposes_token | 維持: 削除engineに依存しない既存試験 |
| integrations/codex_app/tests/test_mcp_server.py / test_bridge_binding_wins_over_misleading_shell_identity | 維持: 削除engineに依存しない既存試験 |
| integrations/codex_app/tests/test_mcp_server.py / test_missing_bridge_binding_stops_without_guessing_a_name | 維持: 削除engineに依存しない既存試験 |
| integrations/codex_app/tests/test_mcp_server.py / test_subagent_bootstrap_waits_for_bridge_observed_pair_and_parent | 維持: 削除engineに依存しない既存試験 |
| integrations/codex_app/tests/test_mcp_server.py / test_subagent_bootstrap_rejects_unobserved_agent_id | 維持: 削除engineに依存しない既存試験 |
| integrations/codex_app/tests/test_mcp_server.py / test_allowlisted_tools_inject_bound_identity_and_owner_token | 維持: 削除engineに依存しない既存試験 |
| integrations/codex_app/tests/test_mcp_server.py / test_session_bound_surface_needs_no_caller_identity_after_bootstrap | 維持: 削除engineに依存しない既存試験 |
| integrations/codex_app/tests/test_mcp_server.py / test_unbound_status_fails_closed_without_identity_candidates | 維持: 削除engineに依存しない既存試験 |
| integrations/codex_app/tests/test_mcp_server.py / test_bridge_tools_reject_caller_supplied_identity_after_bootstrap | 維持: 削除engineに依存しない既存試験 |
| integrations/codex_app/tests/test_mcp_server.py / test_proxy_rejects_cross_binding_and_unsafe_reservation_paths | 維持: 削除engineに依存しない既存試験 |
| integrations/codex_app/tests/test_mcp_server.py / test_stdio_server_lists_only_allowlisted_tools_and_rejects_passthrough | 維持: 削除engineに依存しない既存試験 |
| integrations/codex_app/tests/test_mcp_server.py / test_operator_enrollment_is_absent_from_proxy_catalog_and_dispatch | 維持: 削除engineに依存しない既存試験 |
| integrations/codex_app/tests/test_mcp_server.py / test_only_bootstrap_accepts_caller_supplied_runtime_identity | 維持: 削除engineに依存しない既存試験 |
| integrations/codex_app/tests/test_mcp_server.py / test_mcp_server_script_runs_directly_without_plugin_root_env | 維持: 削除engineに依存しない既存試験 |
| integrations/codex_app/tests/test_mcp_server.py / test_whois_lets_a_child_confirm_a_recipient_name | 維持: 削除engineに依存しない既存試験 |
| integrations/codex_app/tests/test_mcp_server.py / test_server_error_text_reaches_the_model_with_secrets_redacted | 維持: 削除engineに依存しない既存試験 |
