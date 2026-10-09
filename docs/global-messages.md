# 隔離 global server の message と通知（S2b）

[English](global-messages.en.md)

S2b は完成した schema4 candidate を明示した場合だけ使う server の能力です。既定の旧 mode・25 tool、S1/S2a の既存設定、activation=false は変わりません。live の Mail・利用者の HOME・dashboard の切替は行いません。

## 公開する能力

| 新しい tool | capability | 動作 |
|---|---|---|
| `send_message` / `reply_message` | `message_send_v1` | stable ID の宛先、DB 内の本文・添付、同 transaction の監査 receipt |
| `mark_message_read` / `acknowledge_message` | `message_receipt_v1` | 受信者自身の初回時刻を保存。ack は read も設定 |
| `search_messages` / `fetch_topic` | `message_query_v1` | 可視集合の ID 降順 keyset。添付の取得は search の明示指定だけ |
| `fetch_summary` / `summarize_thread` | `message_digest_v1` | 見える message から読取時に digest を計算。保存 summary・LLM は持たない |

添付の能力は `message_attachments_inline_v1`、DB の永続確定は `durable_message_outputs_v1` です。

正本は [wire fixture](../packages/agentstack_mail/fixtures/global-server-s2b.json) です。tool の入力・出力、管理 socket の request/response、固定 reason、状態遷移を同じ fixture に置きます。`fetch_inbox` は list の純粋な読取を維持し、S2b に限り `before_id` を受けます。取得だけでは signal を消しません。

## 新規の準備と起動

[S2a の準備](global-server.md#s2a-schema3-の新規準備)と同じ source snapshot・writer fence・最終 gate を使い、別 workspace の `s2b-candidates/<name>/candidate/` に schema4 を新規作成します。公開前に既存 row と ID・token の保存を検証し、その後の添付 blob 化と通知 import の差分を別の proof で照合します。旧 archive・Git・signal は immutable な保管物で、新しい active signal と分離します。archive md・添付ファイル・Git の継続 writer は作りません。必要な export は将来の一回限りの operator 操作に残します。

明示 plan の `kind` は `orrery-s2b-preparation-plan-v1`、`activation_enabled` は false です。source_paths / isolation_root / candidate_name / choices / request_id / fence_evidence は既存準備と同じです。

```sh
.venv/bin/python -m agentstack_mail.global_prepare_s2b --plan "$plan"
env AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE=passthrough \
  AGENTSTACK_MAIL_ENV_FILE="$fixture/missing.env" \
  .venv/bin/python -m agentstack_mail.cli --host 127.0.0.1 --port "$port" \
  --global-config "$fixture/s2b-candidates/one/candidate/server-config.json"
```

`fixture` は0700の専用一時 root、HOME と TMUX_TMPDIR もその下、`port` は空き loopback port を使います。実 DB を source にしません。receipt のない既存 schema2/3 root は DB を開く前に `CANDIDATE_SCHEMA_REQUIRES_REPREPARE` で拒否し、ファイル一覧・bytes を変えません。未 release の master API で作った開発用 candidate も、旧 root を編集せず合成 source から別 workspace に作り直します。schema4 の complete receipt と config が一致してから起動できます。準備の各保存境界は同じ plan/request ID で再実行します。

client を向け直す際は、新しい instance/candidate/epoch と現在の enrollment を使う**新しい client 名**を用意し、`AGENTSTACK_CLIENT_CONFIG`、wrapper の既定、hook の明示設定、profile の `client_config` を同じ新 context へ揃えます。旧 pending を新 binding で再送しません。旧 root の明示解消・退避と呼出し側の向け直しは [既存手順](global-server.md#schema2-を拒否されたとき)に従います。S2b の local intent の実 client 接続は4b/4cです。

## DB commit と通知の意味

本文・recipient・添付 blob と relation・FTS・操作 receipt・通知の再照合印は1つの DB transaction で確定します。同 owner の UUID は tool をまたいで一意です。応答を失っても同じ UUID/intent の replay は元の receipt を返します。別 intent への UUID 再使用は `REQUEST_ID_CONFLICT` で止めます。

成功 receipt の `durability=db-committed` と `outputs_as_of_commit=pending` は immutable な commit 時点の結果です。配送済みとは意味が違います。現在の signal 状態は health/inspect の `signal` を使います。 top-level `status` は server が要求を受けられるかだけを示します。配送・再照合の健康は `signal.status`（degraded/red）と counts/age に一元化し、signal が red でも4a client の読取を止めません。

通知の消費は `notification_fact` **1列**だけで表します。NULL は未消費、`imported-consumed` は旧 mode で消費済み（時刻を創作しない）、`delivered:<UTC>` は consumer が成功を申告した実時刻です。read/ack と agent の active 状態を合わせた共通 SQL predicate を worker・health・inspect が使います。管理 `delivery_completed` も同じ fact を読み、import 済みを already-completed と返します。

旧通知状態の正本は残留 hint の inventory と Codex App の配送 row です。read/ack、delivered、明示した unknown の解決、pending/failed、残留 hint の順に評価します。hint も配送 row もない過去の未読 message は imported-consumed とし、起床や backlog を増やしません。消費済みの旧 parent への新しい reply は別 row なので普通に通知します。leased/dead_letter や message ID 不明の hint は、`choices.notification_resolutions` に対象を指定した map/discard/delivered/retry_after が必要です。choice は `source_delivery_digest` と `source_signals_digest` へ束縛し、不一致や未解決を ready にしません。

worker は `signals/agents/` directory の flock → authority SH → BEGIN IMMEDIATE の順で lock し、**その write transaction 内で**最新 fact から desired signal を導出します。固定 `signals/agents/<recipient-id>/<message-id>.signal` の私有ファイルだけを fsync・rename・unlink し、同じ transaction で再照合印を消します。途中で停止したら同 pair を再照合します。blocked pair は自動で繰返さず、他 pair の処理を続けます。未来の backoff は保持します。

同じ UID の非協調 process が検査と名前 open/rename の間に path を変更する競合は残ります。既存 S1 と同じく、この検査は共通 lock を守る writer の境界です。別形式・symlink・hardlink・owner/mode 不一致を見つけた slot は上書きせず blocked にします。管理 `reconcile_outputs` が明示した再試行入口です。

consumer は注入成功後だけ same-UID Unix socket の `delivery_completed` を呼びます。応答を失った再申告は同じ初回時刻を返します。unknown message は no-op と孤立 hint の再照合、known 非recipient を含む batch は全拒否です。live watcher の直接 unlink は流用しません。consumer 接続と activation gate は4b/4c・PR7に残ります。

## 上限・可視性・削除

新しい添付は base64、最大8個・各256KiB・合計512KiB。owner の distinct blob 上限は既定256MiB（明示 config で増量）。同じ blob の再送は二重加算しません。旧巨大添付は保存したまま metadata/content_ref で読めます。bytes は `search_messages(attachment_sha256=..., limit=1, include_bodies=false)` だけです。最終 MCP の text＋structuredContent を合わせた1MiB予算を超えた row/blob は `MESSAGE_RESPONSE_TOO_LARGE` で拒否し、保存内容を切り詰めません。inbox は limit を下げるか metadata 読取へ、巨大 blob は将来の operator export へ案内します。

cursor の上限は同じ read transaction の**可視集合 MAX**（空は0）です。不可視 message の ID・件数を公開せず、worker/read/ack/touch で失効しません。cursor に binding の複製は持たず、呼出し context だけを照合します。新しい message は継続 page に混ぜず、purge 済み row は飛ばします。返信グラフは可視 vertex だけの読取 SQL で辿り、保存 namespace や cursor 用 server 状態を増やしません。

内部の purge は既存の祖先保留計画を使い、存続する子の parent を消しません。削除する recipient pair を同 transaction で dirty にし、relation と最後の参照を失った blob を削除します。receipt は歴史の hash/binding を保持し、replay で本文を復活させません。message ID の high-water は purge 後も保持します。書込み tracker がずれた場合の inspect/quarantine は schema4 にも共通の記録付き入口を使い、未知の変更を自動採用しません。

| 旧 mode との違い | S2b の明示 global mode |
|---|---|
| 名前・project による送信 | owner と宛先の stable ID。project 引数は無視 |
| BCC の wake | to/cc と同じ signal を生成。別の受信者には BCC を隠す |
| contact の例外 | S2a の blocked / block_all 優先。予約の重なり等による上書きを認めない |
| 添付 path / image conversion / broadcast / 自動 contact | 固定 reason で拒否 |
| thread 書込み / LLM summary | 返信は reply_to、旧 label は読取だけ。summary は読取 digest |
| archive / Git | immutable 旧保管物だけ。新しい同期出力を作らない |

## 自分で確かめる

開発 venv を用意し、次を serial（`-n` なし）で実行します。fixture は合成 source、一時 HOME、専用 TMUX_TMPDIR、空き port を用意し、管理 socket だけでなく HTTP health の profile を確認してから使います。

```sh
PYTHONPATH=.:packages/agentstack_mail/src .venv/bin/python -m pytest -q \
  packages/agentstack_mail/tests/test_global_server_s2b.py
```

| 確認 | node / 期待 |
|---|---|
| 実 HTTP・管理 socket | `test_actual_http_eight_tools_inbox_and_management` / 8 schema、全 tool、通知完了・再申告 |
| commit の中断 | `test_write_interruption_same_uuid` / 保存境界ごとに同 UUID・message 1個 |
| signal の中断 | `test_signal_interruption_same_pair` と `test_clear_signal_interruption_does_not_reemit` / 再照合・既読を再通知しない |
| 過去の通知 | `test_imported_consumed_not_backlog_new_reply` / backlog 0、新 reply だけ通知 |
| 添付・quota | `test_attachment_blob_dedup_metadata_download_quota` / distinct 使用量と replay |
| 可視性・継続 | `test_visible_keyset_ignores_other_owner_and_survives_mutations` と `test_1001_chain_keyset_never_loses_tail` |
| 準備の中断 | `test_fresh_schema4_preparation_interruption_restarts` / complete 前拒否・同 plan 再実行 |
| 旧 root の不変 | `test_old_schema_root_without_receipt_is_unchanged_before_db_open` / DB open 0、全 bytes 不変 |

S2c は予約と contact welcome、4b/4c は送信 client・hook/proxy/consumer の実接続、PR7 は停止・移行・activation を扱います。既存 dashboard の画面と watcher はまだ旧 mode です。将来の global signal health は同じ health の `signal` を使い、別の未配送判定を作りません。
