# 保存元を維持する B1（未公開）

B1 は #213 の S′ を隔離して検証する内部の核です。本番は従来のままで、installer・通常 server・hook・proxy・休眠 global mode には接続しません。利用者向け切替や #213 完了を意味しません。

## 実装範囲

- 既存 `Agent` / `Message` / `MessageRecipient` / `MailInstance` を使い、保存元・数値 ID・名前・旧未読を維持します。
- 接続先 I、主体 A、資格 K と世代 g、対象 T を区別します。本人確認と read/ack は同じ `BEGIN IMMEDIATE` 内です。`OwnerContext` は同じ task/session/transaction 内だけ有効です。
- target の名前は全保存元・retired 込み、現在名の exact lowercase と sanitized lowercase の ID 集合で数えます。一意かつ active なら受け付け、曖昧なら候補つきエラー。ID selector でも選べます。最新行や起動 folder で選びません。
- 現版は改名・Agent 行削除・ID/名前の再利用を許しません。retired 行を残します。alias/tombstone は追加しません。
- 資格には完全な私有 fingerprint と所有履歴を使います。共有歴は永久 burn、rotation 後は revoke。部分回復・再起動・再準備で復活させません。破損した私有履歴を再準備で正当化しません。
- peek/whois/候補一覧は domain state を変えません。read は本人 recipient の read 初回時刻だけ、ack は本人 read/ack の初回時刻を atomic に保存します。
- 成功と、認証後の terminal refusal は request/intent に固定して保持します。同内容再送は同結果、別内容は拒否。mapping または result reference 欠損は停止し、新規要求として再実行しません。新規 GC はありません。
- cursor は I・ruleset・検索 key 集合・revision・位置に固定します。ページング前に全候補数を判定します。

`storage_origin_directory.metadata` は `SQLModel.metadata` と別です。明示的な candidate preparation 以外では DDL/修復を行いません。要求ごとの整合性検証は全 Agent・資格履歴・request mapping を走査するため、履歴件数に比例する費用があります。大規模性能は本番切替前に別途確認します。

private updater の順序は、全 writer 停止下の原本/資格履歴 census → 旧 binding capture → 私有 index backfill/全検証 → `resolve_v1` → v2 候補検証/publication です。index は binding 解決に依存せず構築でき、共有・失効履歴の validator を一つに保ちます。未準備は `NOT_PREPARED`、整合性破損は `DIRECTORY_STALE` / `INTENT_STORE_STALE` で移行 phase を止め、各 owner の `NEEDS_RESOLUTION` に分類しません。prepare を暗黙に呼ばず、準備済みでも照合不能な資格等だけを要解決にします。B1 の合成 index と、B2 が既知の旧履歴を反映して作る本番 index は区別します。

## 実 handler と試験

`storage_origin_candidate.build_candidate_server(core)` は、明示した session factory の core を使う in-process backend factory です。CLI/HTTP 起動経路や activation flag はありません。model-facing bound proxy の公開は B4 です。backend の auth を model へ公開する指示ではありません。

wire は coercion/defaults 前に省略と NULL、ID/g の型、未知欄を検査します。資格失敗から匿名 peek へ降格しません。合成 secret が応答/捕捉 log に出ないことも検査します。契約 fixture は `packages/agentstack_mail/fixtures/storage-origin-b1.json`。

開発環境は [CONTRIBUTING.md](../CONTRIBUTING.md) のものを使います。

```sh
PYTHONPATH=. .venv/bin/python -m pytest -q packages/agentstack_mail/tests/test_storage_origin_b1.py
```

試験は専用 SQLite と実 ORM/handler を使います。live DB・資格・Mail/dashboard の port は使いません。競争試験には正常登録による原本/索引/digest 更新を使い、lock 前の解決を挟む mutant では登録 commit を先に通します。正常版の lock 保持と mutant の割込みを別々に記録します。

## 後続へ残す範囲

|範囲|B1 の境界／後続|
|---|---|
|send/reply|I/revision・A と宛先 ID/kind・保存元・親/thread の **decision のみ**。I/revision は解決 snapshot の記述で、後の transaction を認証する ticket ではありません。`decision_only=true` は送信成功ではなく、message/配送/成功 receipt を作りません。実 writer と contact の全 policy は B4|
|予約|絶対 path、lease、Mac/WSL Windows側 folder・日本語 path は B3|
|旧 binding|updater 専用 O/name/K 照合の同 ID projection まで。通常の raw actor 探索にはしません。実 v2 store、publication、crash/resume は B4/B5|
|更新不能資格|旧 ID を保存した要解決が既定。利用者が明示回復。移行 census・古い audit の診断用短 fingerprint からの要解決判定と本番 backfill は B2|
|通知|本人/recipient を確認して backend 未接続の NOT_READY。D 注入確定・C 本人消費の永続化、実 shell/App consumer と restart は B5|
|全 query/UI/caller|最小 inbox/whois 以外の query、添付の実取得、proxy の model actor 入力拒否、window の ID 固定は B4|
|移行/復旧|全 writer/旧版 fence、before-image/receipt 保持、live 切替は B2/B5。B1 は code revert のみで戻せる|

既定の利用者向け挙動を変えないため、README・install/hooks・利用者向け指示・dashboard API generation は変更しません。この先行 PR の合格を全 caller 対応、実配送、実 FS、正常更新の end-to-end 合格へ拡張しません。
