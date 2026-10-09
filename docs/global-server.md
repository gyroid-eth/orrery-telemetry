# global server の S1 準備

[English](global-server.en.md)

S1 は #213 の client 対応の前提を、実 HTTP と管理 socket で確かめる隔離 server です。引数なしの `agentstack-mail` は従来の旧 mode・25 tool のままです。global は `agentstack-mail --global-config <設定 JSON> --host 127.0.0.1 --port <空き port>` でだけ選びます。PR3 の pointer と DB の activation は false を保ち、installer・実サービス・実 HOME の切替は行いません。

## 固定した契約

正本は package の `fixtures/global-server-s1.json` と、`global_server.py` の実 signature です。`tools/list` で能力を確認し、未対応の tool に fallback しないでください。

- `health_check` は stable `server_instance_id`、`candidate_generation`、`authority_epoch`、`mutation_revision`、対応 tool と未対応 resources を返します。
- owner の操作は `expected_server_instance_id`・`candidate_generation`・`authority_epoch` と、数値 `agent_id`・`registration_token` を使います。旧 `project_key` は任意で受けて無視します。名前や cwd、旧 tmux env は owner/routing を選びません。
- `register_agent` は既存 ID/token を照合し、改名後の canonical name と credential generation を返します。確定した window の再接続には `window_row_id` と `window_uuid` の両方を指定し、owner を照合します。既存行の owner token が無ければ新 token を付けて乗っ取りません。
- 新規登録は `agent_id` を省略して行います。同名・同じ lookup の競合は拒否します。旧 project 所属は新行に作りません。
- `whois` は自己 identity のみ、`fetch_inbox` は自己 recipient のみです。BCC の別 recipient や token を返しません。inbox は signal を clear せず、read/ack も変更しません。この接続は S2 に残ります。
- `retire_agent` / `unretire_agent` は同じ ID の owner token を要求します。`ensure_project` は何も作りません。

3つの世代を混同しないでください。`candidate_generation` は PR3 の統合候補の fingerprint、`mutation_revision` は DB の `write_generation` という書込みカウンタ、`authority_epoch` は runtime の切替世代です。登録や enrollment のたびにカウンタは増えますが、通常の書込みで authority epoch を変えません。

## 隔離設定と fence

設定は owner の private な JSON（0600）で、`kind=orrery-global-server-s1`、`activation_enabled=false`、`isolation_root`、`runtime_root`、`database`、`mail_instance_id`、`candidate_generation`、`authority_epoch`、`authority`、`authority_lock`、`management_socket` を持ちます。path は絶対 path で、DB と authority/lock/socket は owner の0700の一時 directory の下に置きます。global DB は PR3 が作った schema 2 の候補だけを開き、旧 ORM の schema 初期化を行いません。通常 HOME の DB を global 設定に指定できません。

authority の正本は `kind=orrery-global-authority-v1`、`phase=active`、`root_status=active`、`runtime_root`（隔離 directory 内の対象 state root）、`mail_instance_id`、`candidate_generation`、`authority_epoch` を持ちます。updater は同じ private regular lock file の排他 lock を取って authority を更新する契約です。server は shared lock を通信の操作と SQLite transaction の間保持し、commit 前にも現在の authority を照合します。quiescing、retired root、異なる epoch、config/DB/lock の差し替えは拒否します。

これは対応済みの writer を守る fence です。OS supervisor の停止、全 writer の列挙、旧接続の終了、実 pointer の handoff は PR7 に残ります。直接 DB に書く未知の process を、この lock だけで止めたとは扱いません。

## 管理 socket

owner UID の Unix socket でのみ `version=1` の global 管理契約を使います。MCP の tool に enrollment は公開しません。全 request に HTTP と同じ instance/candidate/epoch の binding を指定します。

- `inspect`: `agent_id` から canonical name、credential generation、非秘密 fingerprint を返します。token は返しません。
- `claim` / `recover`: `request_id`・`agent_id`・`expected_generation`・`new_credential` を指定します。null token の claim と既存 token の recover を分け、CAS と監査を同じ transaction にします。
- 同じ request ID と内容の再実行は同じ非秘密 receipt を返します。内容違いは拒否します。`request_status` は request ID の receipt を返します。

旧 enrollment の履歴 table は保存し、新 global receipt/audit は別 table に記録します。4a の CLI/profile はこの wire を読み、専用 HOME の credential file と stable ID を照合する作業として後続に実装します。

## 未対応と次の作業

S1 が公開するのは health/ensure/register/whois/inbox/retire/unretire の7 tool だけです。残り18 tool（正本 fixture の `unimplemented`）は S2 に残ります。送信・返信・read/ack・search・contact・summary/topic・予約と macro、archive/添付/Git writer、global signal の生成/clear、生成 Git guard の結線は未対応です。resources は公開しません。従来の未公開 resource/tool body を、新しい公開要件として勝手に追加しません。

順番は S1 → 4a → S2 → 4b → 4c です。S1 の成功は global の全機能完成や実切替完了を意味しません。現行 mode の回帰と、この7 tool/管理 socket の実 HTTP 試験を区別して検証します。
