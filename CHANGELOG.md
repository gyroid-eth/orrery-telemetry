# Changelog

版は日付で付けます（`YYYY.MM.DD`）。互換性の約束ではなく、**いつの配布物か**を言えるようにするためのものです。

インストール済みの版は `GET /api/version`、または install root の `VERSION` で確認できます。同じ応答の `api` は日付とは別の、利用者（ORRERY cockpit など）との互換の世代で、利用者が頼る API を足す・意味を変えるときだけ上げ、その版の節に書きます（[docs/api.md](docs/api.md#get-apiversion)）。更新手順は [docs/install.md](docs/install.md) の Upgrade を参照してください。

**この記録は 2026.09.16 から始めます。** それ以前は `VERSION` が `0.9.0` のまま更新されておらず、版から中身を知ることができませんでした。過去 6 週間分を遡って記載することはせず、ここを新しい基点とします。以前の版を使っていた場合も、上書き更新の手順は変わりません。

---

## Unreleased

### Codex plugin の導入処理を共通化

- Codex の専用 installer を再利用できる処理へ分け、共有 HOME・cache の所有情報と対象 hook の承認結果を記録します。
- 承認済みの対象定義だけを公式 API で信頼し、未確認なら `/hooks` へ案内します。Mac/Linux/WSL の隔離確認 script を追加します。本体 install への接続は後続です。
- 解除前に現在の registry と service の所有を照合します。別の root や再利用された PID、確認不能な状態では削除・停止を拒否し、途中の解除を再実行できる情報を保持します。
- Linux/WSL の service は boot ID と kernel の起動 tick で照合し、表示時刻の揺れを比較から除きます。起動途中の command は記録せず、runner の一致を再取得してから所有情報を保存します。
- macOS の終了途中に `ps` が `(bash)` を返す場合は、所有確認を済ませて signal を送った後の待機だけ、同じ UID・起動時刻と実行名の一致を確認して終了を待ちます。全 signal 直前の厳密な所有照合と、未確認時の記録保持は維持します。

### project 廃止に向けた Mail と配送の記録の移行候補を準備

明示した隔離 snapshot から、複数 project の Mail と配送の DB、archive・添付・signal・履歴・window・binding を統合する候補を追加しました。ID・token・read / ack・監査・旧 thread の reply_to・配送の全 status と試行、policy と backoff を保存し、最終差分の反映後にも全内容を照合します。同名 agent・window UUID・曖昧な予約は利用者の決定を要求します。全 phase と pointer 更新直後の強制中断から再実行でき、新しい書込みの後の rollback は拒否します。

metadata を省略した旧 per-message signal も file 名の ID と宛先を照合して移行し、同 ID の旧単一 signal との共存は別の出力で保持します。無効な signal は計画に表示します。予約の Mail 活動は日時を UTC の時点に変換してから最新を選び、offset の表記差で早期解放しません。

永続 candidate で返信と purge、予約、配送の世代確認を試験します。既定の公開 tool・実 DB・installer・hook・daemon は旧方式のままで、activation は false です。切替は private workspace 内の練習のみです。実サービスの writer 制御・managed block・archive と signal の配送配線・非 ASCII path の制限解決は後続に残ります。詳細は[記録の移行と復旧](docs/namespace-state-migration.md)を参照してください。

### project 廃止に向けた絶対 path 予約の候補を準備

候補の Unicode 比較は対象 filesystem の規則を read-only で確かめ、Mac の未作成 NFC/NFD 別名と glob の活動を見落とさないようにしました。Linux の別 file は区別し、規則が不明なら取得と早期回収を止めます。候補 release hook も失敗・blocked の結果では予約を保ちます。case-insensitive な filesystem の非 ASCII path は case table を保証できないため取得を拒否し、glob の探査も unknown として早期回収を止めます。

共通の path 正規化、認証と owner を確かめる候補 server、bound proxy・hook・CLI の adapter、予約ごとの filesystem/Git 活動と GC を追加しました。別 cwd の同じ相対名を分け、同一 file と glob 内の symlink の先の新規 file は競合させます。未知の探査結果では早期解放せず、TTL と owner release は維持します。候補は明示した隔離 store で試験し、既定の公開 tool・DB・installer・既存 hook は旧 mode のままです。永続化と切替は後続 PR で行います。詳細は[絶対 path の予約候補](docs/absolute-reservations.md)を参照してください。

### 常駐 daemon が Mail の識別子を確認し、共通の通知待機を使えます

同梱 Mail の `health_check` に、管理 socket と同じ永続的な `server_instance_id` を追加します。秘密ではない UUID で、別の Mail への誤接続を拒否できます。利用する側が必要な field を判定できるよう、互換世代を11から12に上げました。API 11の watcher health と古い signal の扱いは保ちます。

`agentstack-await-reply` の認証済み fetch を渡せる待機処理を共通化し、取消しに対応しました。常駐 daemon 用の厳密な binding profile と operator 専用 CLI は、同じ owner・数値ID・project・固定送信先を照合します。待機は本文取得や ACK を行いません。CLI は固定 source から実行する対象で、一般の installer には追加しません。identity 作成と実際の配備は operator が別途確認します。

### project 廃止に向けた契約と移行計画を準備

旧 scope 引数を受けて無視する candidate adapter、message ごとの reply_to と旧数字 thread の変換、名前付き旧値の読取、agent 名と window UUID の衝突・解決を追加しました。返信は元 message ごとの送信者・宛先で照合し、残る子が参照する親は purge から保留します。候補の会話・旧ラベル読取は、可視結果数と探索量の上限を分け、不透明な cursor で続きを取得できます。最終 snapshot の再検証と固定した予定 writer 集合に結び付く receipt/gate も fixture で確かめます。

これは準備の PR で、公開中の tool・project ごとの配送・DB・installer・hook は従来どおりです。新 API は明示した candidate だけで使い、gate が ready でも切替は有効になりません。詳細は[契約と移行計画](docs/project-removal-plan.md)を参照してください。

### session の無い宛先の古い Mail signal で health が red になる誤警報を修正

watcher は `session_not_found` の signal を再試行用に残していました。health は24時間以上古く、tmux session が無いと確認できた signal を残留数として別に報告します。生きている宛先・直近24時間・headless runtime の未配送は従来どおり数え、50件超なら red にします。測定に失敗した場合も除外しません。doctor は残留数と確認・削除のコマンドを warn で示しますが、この残留だけでは終了コードを1にしません。

片付け用の `dashboard/mail_signals.py` は既定で候補を表示し、`--prune` で削除前にも session 不在を確認して、signals 配下の古い通常ファイルだけを削除します。Mail の DB と inbox は変更しません。全数・判定対象数・残留数・session 確認可否を API に追加したため互換世代を11に上げました。

## 2026.10.06

### Codex の既定モデルを、そのアカウントで使えるものに下げます

モデルを省略した Codex の子と NEW AGENT の既定は `gpt-6.1-sol` でした。無料の ChatGPT の plan など、このモデルを使えないアカウントでは、起動の直後に 400 で止まっていました。選択された `CODEX_HOME` の CLI ローカル cache にその ID が無ければ、`gpt-6.1-sol` → `gpt-6-sol` → `gpt-6-luna` → `gpt-5.6-terra` → `gpt-5.6-luna` の順で、掲載されている次の候補へ下げます。既定を下げる判断には期限切れの cache も使い、変更があれば provider の `model_note` に変更前後と理由を返します（API 10）。

正式 ID の明示要求は置き換えません。cache の `identity` は現在のログインと照合しておらず、fallback は利用権限の証明ではありません。2026-10-06、無料の ChatGPT の Codex（`gpt-6-luna` medium）で、Full tour のしりとりが最後まで通ることを確かめました。詳しくは [設定](docs/configuration.md#codex-model-catalog) を参照してください。

### Codex でも `$delegate` と `$log` を使えます

installer は `delegate` と `log` の skill を、`~/.claude/skills` に加えて Codex の skills（`$CODEX_HOME/skills`、既定は `~/.codex/skills`）にもリンクし、manifest に記録します。Codex の入力欄では `/` から始まる語が Codex 自身のコマンドとして扱われ、skill が無いと `/delegate` は `Unrecognized command` になっていました。Codex では `$delegate` / `$log` と書きます。`~/.codex/AGENTS.md` の managed block もこの書き方に合わせました。

同じ名前の skill がすでにあれば上書きせず、installer が warn を出します。uninstall は ORRERY が作ったリンクだけを取り除きます。

### Codex の子が、選んだ MCP の起動を待つようにしました

ブラウザなどの MCP server が起動中のまま最初の道具一覧から省略され、子が道具を持たずに作業を始める場合がありました。`--tools` と installer の既定で選んだ browser・computer use・`mcp:<name>` の server は、子の生成設定だけで `required = true` とし、起動待ちを最低60秒にします。既存の長い timeout は保ち、初期化に失敗すれば Codex の起動・resume を止めます。

選択しない継承 server と ORRERY Mail の待機設定は変えず、利用者の設定も変更しません。installer の既定で設定を解決できない項目は従来どおり外しますが、採用された server の実行時の初期化失敗は停止として扱います。Chrome 側の接続許可は別で、人が答える必要があります。Codex 0.159.2 の途中の道具追加が呼び出しに反映されない制限は、この変更では解決しません。

### Claude の browser 子の tool を、起動ごとに承認します

Claude in Chrome を付けた子は、既知の22 tool を1本の `--allowed-tools` にまとめ、Claude Code 側で承認します。`javascript_tool`・`computer`・`file_upload`・`upload_image`・`form_input` も含みます。`--tools browser`、installer の browser 既定、旧 `claude_chrome` 指定とその resume に適用し、利用者の設定は変えません。新しい名前の tool は自動承認しません。

Claude in Chrome 自身の操作確認は別です。タブを開く等の操作では **人が Chrome 側の Allow を押す**必要があり、headless の子はその確認で止まります。拡張の接続・アクセス許可も人の操作のままです。2026-10-06、WSL2 の Ubuntu、Claude Code 2.1.290 でも、22 tool の承認後にこの確認で停止することを実測しました。

### Codex の子に chrome-devtools の browser を渡せるようにしました

Codex の `--tools browser` は、利用者の設定にある chrome-devtools MCP server を選び、既知の全30 tool を公開して個別に承認します。Codex と既知固定版の Claude の `screen:operate:<server>` でも、chrome-devtools の同じ30 tool を承認します。任意 JavaScript 実行、ホストへのファイル保存、ローカルファイルの upload を含む許可です。未知の新しい tool は公開・承認しません。

読み取りに絞る `screen:read:<server>` は8 tool を公開し、保存引数を持つ tool や任意 JavaScript・upload は含めません。Claude の chrome-devtools 選択は既知固定版が必要です。browser と別の computer use server は、それぞれの承認範囲で併用できます。詳しくは [delegation](docs/delegation.md#子に渡す道具を選ぶ--base----tools) を参照してください。

### 子に渡す道具の既定を installer で選べるようにしました

`--child-default-tools` / `AGENTSTACK_CHILD_DEFAULT_TOOLS` は `--tools` と同じ文法で、既定は空です。明示の `--base` / `--tools` / `--codex-mcp` が優先します。使えない既定の項目だけを外し、起動の出力と子の最初の指示に残します。子の記録には実際の選択を保存し、resume 時に現在の既定を追加しません。

API 8 では `/api/spawn` の `tools: {}` または `base: "default"` がこの既定を抑止します。CLI で base・tools・MCP profile を省略した新規起動に既定を適用します。dashboard/API ではフォームの選択を正とし、installer の env を黙って追加しません。

### 停止した子の道具を変更して同じ会話を再開できます

API 9 の `/api/jump` に `base` / `tools` を追加し、`agentstack-resume` と `/delegate --resume` の手順を用意しました。作業の区切りで EXIT した子を、同じ名前・会話・作業ディレクトリで再開します。`tools` は全置換、`{}` は追加の道具の解除、base 省略は現在の base を維持します。両方省略した resume と UI は従来のままです。動作中・状態不明・道具の記録が無い子などは変更を拒否します。

変更は子ごとに直列化し、端末が起動を受け付ける前の失敗には、その変更の道具記録と生成設定を戻します。API の成功は端末の起動受理を意味し、その後の認証や MCP 呼び出しの成功を保証しません。起動受理後の失敗には新しい選択が残るため、再開した子から同じ会話と実際の道具の呼び出しを報告してもらってください。

Claude Code 2.1.289 では、同じ会話の native resume で新しい chrome-devtools MCP を呼び出せることを実測しました。**Codex 0.159.2 は、途中で追加した道具が呼び出しに反映されない既知の制限が未解決です。** この版の API 世代は、2026.10.05 の 7 から、既定の道具で 8、道具変更 resume で 9、Codex の既定モデルの `model_note` で 10 に上がります。

### ブラウザと computer use の届き方、人が行う許可をまとめました

日英 [delegation](docs/delegation.md#ブラウザと-computer-use-何が届くか人がやること) に Claude / Codex × Mac / Windows（WSL2）の8通りと、子・NEW AGENT・resume ごとの届き方をまとめ、README から案内します。「画面全体」「画面操作」の表記は computer use に揃え、WSL 側の道具は Windows-MCP と明記しました。指定名の `screen` は変わりません。

Chrome の接続許可は接続のたびに人が答え、Mac の computer use のアプリ単位の許可も人が出します。Claude の組み込み computer use は Mac 全体で同時に1セッションだけです。ロックは computer use を一度使ったセッションが終わるまで外れず、その間に起動した子は `Computer use is in use by another Claude session` で止まります。Windows-MCP は RDP などの対話セッションで動かす必要があります。英語 README 冒頭の agent の数を限定する表現も直しました。

### Gemini の dry-run テストの不安定な判定を直しました

試験用の Git repository に自動 maintenance の lock が生成されると、dry-run の副作用と誤判定していました。試験用 repository の自動 maintenance と gc を止め、Git のメタデータも含めた副作用の確認を保ちます。製品の Gemini の起動や dry-run の動作は変わりません。

## 2026.10.05

### 新しい環境で、ワークショップのしりとりが「作業フォルダの外を読むか」の確認で止まっていました

しりとりの prompt は、親に installer が置いた `/delegate` skill（`~/.agentstack/skills/delegate/SKILL.md`）を読ませます。この path は作業フォルダの外なので、Claude Code が初回に「外を読んでよいか」を確認し、答えるまで親が止まっていました（Full tour の録画で約 10 分止まった）。

installer は、ORRERY が置いた skill の場所だけを `permissions.additionalDirectories` に足します。対象は `~/.agentstack/skills` と、そこを指す `~/.claude/skills/<name>` の symlink だけで、利用者が置いた同名の skill は入れません。同じ場所への `Edit` は deny にして、読み取りだけに絞っています。uninstall では、manifest にある分だけを外します。

- この deny が止めるのは file の道具（Edit・Write など）と、それと分かる Bash の書き込みです。sandbox なしの Python や Node を通した間接的な書き込みまでは覆いません（[docs/install.md](docs/install.md)）。
- 既知の制限: 再 install の後に uninstall すると、installer が足した設定が残ります（allow・deny・hooks と同じ既存の構造。gyroid-eth/orrery-telemetry#205 で直します）。

### NEW AGENT の既定の作業フォルダを、project folder にしました

1 行の install（fresh install）は `AGENTSTACK_SPAWN_DIRS` を空で書き、dashboard は空のとき NEW AGENT の候補を `~` だけにしていました。そのため NEW AGENT で起動した agent が home で動き、project folder の `CLAUDE.md`（ORRERY の指示）を読まず、`/delegate` を使わないので親子の線も出ませんでした（2 人目の Mac 利用者の試走で見つかった）。

- `AGENTSTACK_SPAWN_DIRS` が空なら、候補は project folder、`~` の順になります。明示した `--spawn-dirs` は変わりません。

### README の入口を作り直しました

冒頭に 1 行の install を置き、入るもの・置き場所の表、作業フォルダの変え方（Obsidian の vault を project key にする）、完全に消す手順を足しました。Telemetry 単独の install と、cockpit との関係、両方を入れる 1 行の順に並べ、cockpit と Telemetry の関係図を入れました。

### 開いたままの詳細パネルのボタンが、agent の状態に合わせて変わるようにしました

詳細パネルの OPEN IN COCKPIT／RESUME IN COCKPIT と Exit のボタンは、パネルを開いた時点の状態のまま変わりませんでした。パネルを開いたまま子を Resume すると、ボタンは RESUME IN COCKPIT のまま残り、cockpit の Full tour の「Return to your agent」の輪が、パネルの Close に付いていました。ボタンは、Deck の一覧か Network の graph のどちらかを受け取るたびに、新しい方の状態で描き直します（送信中の Exit はそのまま）。どちらにも agent が無くなったときは、ボタンを押せない「NO LONGER LISTED」にします。また、Full tour の「Return」の段は Close を指さなくなりました。running の agent の OPEN IN COCKPIT が無いときは輪を出さず、tour の欄の文で案内します。

### mail watcher の lock を利用者ごとにし、doctor が watcher の停止を検出するようにしました

1 台の Mac に 2 人目の利用者が fresh install すると、既定の lock dir（`/tmp/orrery-mail-watcher.lock`）が全利用者で共通だったため、先にいる利用者の watcher が持つ lock を 2 人目が取れず、`mkdir: ... Permission denied` → 「Stale watcher lock detected; taking ownership」を 5 秒おきに繰り返して一度も動きませんでした。入力待ちの agent に Mail が届いても起こされず、`agentstack-doctor` は全項目 ok のまま watcher の停止を報告しませんでした（dashboard の `/api/mail-watcher-health` は `watcher_running: false`・status red を返していました）。

- watcher の lock の既定を利用者ごと（`$AGENTSTACK_RUNTIME_DIR/mail-watcher.lock`）にしました。既定が利用者ごとになったので、他人の lock に当たるのは `AGENTSTACK_MAIL_WATCHER_LOCK_DIR`・`_PIDFILE`・`_HEARTBEAT` を明示で他人の物に向けたときだけです
- 所有者が自分でない path（lock dir・pidfile・heartbeat の親 dir とそれぞれの既存 file、symlink ならその参照先も）には、何も書かず（別の場所への切り替えもせず）、そのことを一度だけ log に出して止まります。黙って別の場所へ切り替えると、同じ override を見ている dashboard や agent-start が元の（他人の）path を読み続け、watcher は動いているのに「止まっている」と誤って報告するため
- 2026-10-05 より前の既定（`/tmp/orrery-mail-watcher.lock`）に自分の watcher がまだ生きていれば（pid の process が自分の UID で command line が `watch_agent_mail_signals.sh` のときだけ。pid が再利用された無関係の process は stale 扱い）、新しい path では起動せず、旧 watcher を止める案内を出して止まります（二重配送の防止）。update 時、同じ旧既定に自分の物で使われていない lock が残っていれば片付けます。どちらも他人の物には触れません
- 所有者 UID の取得は GNU（Linux/WSL）と BSD（macOS）の両方で UID だけを安全に取れる順に直しました（GNU の `stat -f` は「ファイルシステム情報」を意味し、未知の directive でも stdout を汚してから失敗するため、所有者比較が常に不一致になるおそれがありました）
- `agentstack-doctor` が、dashboard の `/api/mail-watcher-health` と同じ判定で watcher の停止・status の red/yellow・heartbeat の停滞を warn するようになりました（`watcher_running` だけでは、signal が溜まっていたり直近の配送が無かったりする red/yellow な watcher を ok と報告していました）

### EXIT を確定した直後、隣のカードを誤って EXIT しないようにしました

EXIT した agent のカードが LIVE から消えたり finished の区画へ移ったりすると、後ろのカードが詰まり、直前に押した位置へ次のカードの EXIT が来ます。そこをもう一度押すと、別の agent の EXIT を構えてしまいました（Full tour の録画で、子を EXIT した直後に親の EXIT がカーソルの下に来た）。EXIT を確定してから 2 秒間、またその後 30 秒の間にそのカードが動いたり区画が変わったり消えたりしたときはその時点から 2 秒間、EXIT を押しても受け付けません。その間は EXIT ボタンを薄く表示し、カーソルも「押せない」形にして、効かない理由が見えるようにしました。

### cockpit の Full tour の後半で、Telemetry の中の押すボタンを示せるようにしました

cockpit の tour は、次に押す control をシアンの脈打つ輪と HERE の札で示します。Full tour の後半の 7 段（EXIT、Network の edge、Select、Replay、Resume、Network の Settings、Open in cockpit）は、cockpit に埋め込んだ Telemetry の中のボタンを押す段です。cockpit はその中に手を出せないので、これまでは Telemetry の画面全体を指すだけでした。

Telemetry は、cockpit から段の名前（`orrery-tour-cue`）を受け取り、自分の画面の該当するボタンに同じ輪と札を描くようになりました（`dashboard/tour_cue.js`）。

- 段ごとに候補を順に試し、画面に出ていて覆われていない最初のものを指します。ボタンがまだ出ていなければ、そこへ行く手前の操作（Deck／Network の切り替え、card）を指します。
- 受け取るのは埋め込みのときだけで、送り主が親の cockpit・同じ origin の場合に限ります。cockpit の tour の欄がかぶる範囲を受け取れば、札はそこを避けます。
- 指すのは「その種類のボタン」です。どの agent のボタンかまでは分かりません。EXIT なら画面で最初に見つかった EXIT を指すので、段の文が言う「しりとりの子」のものとは限りません。

cockpit 側から段の名前を送る変更は、gyroid-eth/orrery に入っています。

### Full tour の録画で見つかった、Telemetry の中の輪と札の置き場所を直しました

- **Network の edge の段**は、cockpit がしりとりの親と子の名前（`pair`）を送ったときだけ、その 2 人の edge を指します。名前が来ない、またはその edge が画面にないときは、edge は指さずに Deck／Network の切り替えを指します。これまでは画面で最初に見つかった edge を指したので、関係ない 2 人の edge に輪が付くことがありました。
- **HERE の札**は、ほかのボタンや入力欄に重なる側を避けます。どの側も空いていなければ、輪だけを出します。
- **通知（toast）**は、選択の操作バー（selection bar）が出ている間はその上に出ます。Replay の件数の通知が、操作バーの RESUME を隠していました。
- tour の段が進んでいる間は、右側に開いている edge の drawer・agent の詳細・Settings の範囲を cockpit に知らせます（`orrery-tour-cover`）。cockpit の tour の欄は、それに重なる間は自動で畳まれます（cockpit 側は gyroid-eth/orrery に入っています）。

## 2026.10.04

### agent の詳細パネルを開いている間、Help map を押せませんでした

詳細パネルの背景は header より上に重なるため、header の「Help map」は見えていても押せませんでした。help map には、パネルを開いているときにパネルの 3 か所（History と Output、Exit・Open・Close、Role）を注記する機能がありますが、その入口が塞がれていました。

パネルの操作の列に「HELP MAP」を置きました。header の Help map と同じ map を開き、パネルの注記が出ます。閉じると、開いたボタンに focus が戻ります。

### Deck で EXIT した後も、カードが LIVE に数秒残り、もう一度押すと 400 になっていました

EXIT が届いた後も、tmux の session が終わるまでの数秒間、カードは「↩ EXIT」に戻ったまま LIVE に残っていました。もう一度押すと `/api/exit` に再び届き、すでに終わりかけの agent に対して 400 が返っていました。

EXIT が届いたカードは「exiting…」の表示にし、押せないようにしました。この表示は再描画しても続きます。agent が LIVE から外れた時点で状態を外し、30 秒経っても残っている場合は、その旨を知らせて EXIT を押せる状態に戻します。終わった agent への EXIT が 400 になった場合は、サーバーの文面をそのまま出すのではなく「<名前> has already exited」と知らせます。

### 詳細パネルの Exit と、選択の一括 EXIT で、Full tour の「Exit」の段が進みませんでした

cockpit に埋め込んだ Telemetry の Full tour は、EXIT が届いた知らせで「Exit」の段を完了にします。この知らせを出していたのは Deck のカードの EXIT だけで、詳細パネルの「↩ Exit」と、選択した agent の一括 EXIT からは出ていませんでした。

EXIT が届いた時の知らせを 1 つの関数にまとめ、3 つの経路すべてから呼びます。知らせを出す条件は 3 つとも同じで、次のすべてを満たすときです。

- 応答が成功している（`ok`）
- 実際に送られた（`exit-sent`）か、終わった agent が shell に残っていて `exit` を打ち込んだ（`shell-exit-sent`）

### Claude Code の初回設定が終わっていないと、子が理由を告げずに消えていました

初回設定（text style・ログイン・Security notes・フォルダの信頼）を途中で閉じると、`~/.claude.json` に `hasCompletedOnboarding` が付きません。そのまま NEW AGENT で子を起動すると、子は最初の text style の画面で止まっていました。launcher はこの画面を選択画面と見分けられず 60 秒待ってから子を片付け、cockpit には「closed」としか出ませんでした。

- launcher は初回設定の画面（text style・login method・Login successful・Security notes）を見分け、キーを送らずにすぐ止めます。答えは利用者の設定なので、ORRERY は代わりに答えません。
- 止めた理由は `[spawn_child] reason:` の 1 行で出します。dashboard はそれを spawn の error の先頭に入れるので、cockpit の失敗の知らせに「端末で `claude` を一度起動し、入力欄が出るまで進めてから /exit」と表示されます。Claude in Chrome の一度きりの質問も同じ扱いです。
- doctor は `hasCompletedOnboarding` が無いと warn します（値そのものは表示しません）。

あわせて直したこと:

- 子が最初の prompt に返答した後に一度きりの質問（「Try the new fullscreen renderer?」）が出ると、10 秒後に「unrecognised choice screen」として、動いていた子を片付けていました。prompt の echo の下に Claude の返答（`⏺`、WSL では `●`）があれば起動は成功として扱い、質問は子の pane で利用者が答えるものとして残します。返答の前にこの質問が出た場合は、キーを送らずに止め、理由を返します。
- 親のいない（standalone の）子についても最初の turn を見張り、普通に返事をしただけで「declined; could not tell the parent by Mail: no parent to tell」と記録していました。知らせる相手がいないので、standalone では見張りません。

### 「子を作って話して」と頼むと、Claude Code の組み込み Agent と SendMessage で済ませていました

managed block が context に無いセッションでは（block は `--project-key` の dir の CLAUDE.md に入るため、それ以外の dir で起動した場合）、「Start one child agent and play shiritori with it over 3 turns」に対して 3 回とも組み込みの Agent が子を作り、SendMessage でしりとりを最後まで進めていました。ORRERY Mail には何も残らず、dashboard にも出ません。Claude Code 2.1.224 以降の組み込み `SendMessage` / `ListAgents` は、tmux の ORRERY child にも直接届きます。そのため、`/delegate` で作った子と話すときにも、同じ回避の経路がありました。`/delegate` skill の description の先頭に、子を作るのも子と話すのも組み込みの道具ではなく `/delegate` と `send_message` で、と書きました。block の禁止文にも `SendMessage`・`ListAgents`・agent teams を足しました。skill の description は block の外でも context に入ることが多く、その場合は道具を選ぶ手掛かりになります。ただし skill の一覧が予算を超えると description は削られるので、どのセッションでも入るとは限りません。入っているかは `/context` や `/doctor` で確かめられます。確実に通すには、明示の `/delegate` を使います。修正後は同じ条件で、3 回とも `/delegate` から始めました（stub の Mail を使った少数回の比較で、全セッションでの保証ではありません）。tmux の子への送信も 3 回とも `send_message` でした。組み込みの経路を設定で断る deny rule は、組み込み subagent への送信も消すので既定では入れず、[docs/launchers.md](docs/launchers.md) に opt-in として書きました。

### Telemetry にも help map を付け、README と docs の索引を画面のガイド中心に作り直しました

- Telemetry の header に「HELP MAP」を置きました。cockpit の help map と同じ見た目（金の角括弧と引き出し線）で、Deck・Network・詳細パネルの主な操作に注記します。置き場所が足りない狭い窓では凡例にします。
- README（日英）を、何ができるか → 1 行の install → 画面のガイド → 退出した agent を再開する、の順に作り直しました。冒頭に Mail が流れる NETWORK の GIF、「退出した agent を再開する」に RESUME の GIF を置きました。
- docs の索引（日英）を目的別に作り、全文書を載せました。索引の漏れと存在しないリンクは検査で止まります。
- 詳細パネルの「HELP MAP」の案内を README と索引に足しました。

## 2026.10.03.1

### Mail を更新した後、前から開いていた端末からの update が止まっていました

`--mail update` で Mail を差し替えると、`env.sh` の `AGENTSTACK_MAIL_ENV` は新しい render を指します。差し替えの前に開いた端末（`env.sh` を読み込む shell）は古い render を export したままで、その端末から update（`./scripts/install.sh`、cockpit の update・setup）を実行すると `AGENTSTACK_MAIL_ENV must equal the native service env` で止まっていました。この installer が `env.sh` に書いた値と等しいときだけ許していたためです。`env.sh` の値も引き継いだ値も、この installation の render の置き場所にある render なら、自分の出力として扱って許します。`AGENTSTACK_MAIL_SERVICE_ENV` で固定した場合や、置き場所の外の path は従来どおり止まります。

## 2026.10.03

### cockpit に埋め込んだ dashboard の RESUME が何もしていませんでした

ORRERY アプリの cockpit に埋め込まれた dashboard では、RESUME（と VERIFY & RESUME）が cockpit に名前を渡すだけで、この server に再開を頼んでいませんでした。cockpit は tmux に session が現れるのを待つだけなので、retire 済み・gone・husk の agent は何も起きずに戻っていました。埋め込みでは、先に `/api/jump` を `open: false`（端末を開かない）で呼び、server が自分の状態で決めます（動いている session はそのまま、動いていなければ再開、husk なら置き換え）。成功したら cockpit に渡し、断られたら理由を表示して渡しません。画面の DECK や NETWORK の古い表示で判断しません。 `/api/jump` に `open: false` を渡したときは、Codex App の agent でも App を前面に出さず `already_running` を返します（埋め込みの cockpit は従来どおり「Codex App で動いている」と表示します。単独の dashboard の OPEN は変わりません）。埋め込みでの bulk の RESUME も `open: false` で頼みます。単独の dashboard の挙動は変わりません。

### 起動時の回復が、子の state でない file にも tmux を呼んでいました

PC の再起動の後に子を再開できる state に戻す処理（前の項目）は、`child-agents/` の JSON を 1 つずつ見て、`*.mcp.json`（proxy の設定）や正常に retire した子にも「session が生きているか」を tmux に尋ねていました（数百回）。動いていた子・再開の途中の子だけに尋ねるようにしました。state を書き換える条件は変わりません。試験の process でこの処理が本物の runtime を読み、後の試験の stub を呼んでいた点も直しました。

### Codex の hook が大きな payload で「Hook failed（exit 141）」になっていました

ORRERY の Codex App plugin の hook（`run-hook.sh`、子の Codex にも入る）は、payload を pipe で Python に渡します。Python 側が 64 KiB の上限を超えた payload や使わない payload を読み切らずに終わると、書いている側が SIGPIPE で死に、`set -o pipefail` によって hook が exit 141 で終わっていました（162nd のセミナーで、digest-paper の実行中にレビュー役の Codex に複数回）。道具の出力を含む PostToolUse の payload は数百 KB〜数 MB になります。`hook_entry.py` と session の索引の recorder は、使わない payload も最後まで読んでから終わります。recorder が読まずに終わったときに出ていた誤った `recorder_process_failed` も出なくなります。

### PC の再起動・強制終了の後に、子の Resume が拒否されていました

`/delegate` の子の state は、正常に終わったときに retire されます。PC が落ちると retire されないまま残り、dashboard の RESUME も端末の `claude --resume` も `retired_at is missing` で拒否していました（162nd のセミナー: 再起動のたびに手で retire してから戻していた）。dashboard は起動時（と 1 時間ごと）に、この machine が起動する前に書かれたまま、session も生きた lease も無い「動作中」の state と、終わらなかった再開を、再開できる state に戻します。拒否の文も「正常に終わらなかった（PC の強制終了など）」と言い、どうすれば戻るかを示します。起動の前に書かれた live-session の lease は、PID が再利用されうるので生きている印に数えません（[troubleshooting](docs/troubleshooting.md)）。

### agentstack-doctor: 動いている ORRERY Mail に足りない機能を知らせます

Mail が install より古いと、health は通り、ほかの検査もすべて通るのに、dashboard や launcher が頼る機能だけが欠けていました（2026-10-02: `register_agent` に `existing_agent_id` が無く、Claude の再開が Mail の無い会話だけになった）。doctor は動いている Mail の `tools/list` を 1 回読み、`scripts/lib/mail_required_features.json` の機能のうち欠けているものごとに `warn:` と直し方（`--update-mail`）を出します。Mail そのものは動いているので exit status は変えません。最後に `mail-features: status=<ok|missing|unknown> missing=<…> running=<commit>` を 1 行出し、setup.sh はこれを読めます。

install.sh も、この run で差し替えなかった Mail がこの checkout の build より古いと分かるとき（動いている build の commit が checkout の祖先）、結果行の前に同じ案内を出します。動いている Mail の方が新しい・どちらが新しいか分からないときは、そう出すだけで、この checkout からの更新は勧めません。欠けている機能はどの場合も知らせますが、それだけでは「この checkout から更新すれば直る」根拠にしません。案内は、足りない機能、更新の仕方（`--mail update`）、更新のリスク（止まるのは通常は数秒だが、新しい build が起動しないと数分かかりうる。子と Codex は自分で戻るが、top level の Claude Code は戻らないことがあり、Claude Code 2.1.287 の試験では 18 秒と 19 秒の停止で戻らなかった）、戻らなかったときの操作（その session で `/mcp` → orrery-mail → Reconnect）です。文は `scripts/lib/mail_update_notice.py` の 1 か所にあり、差し替えの後の案内もここから出します。cockpit の setup.sh は `install.sh --print-mail-update-advice`（読むだけ）で同じ文を出せます。 更新を承認してもらう前の画面には、リスクと戻し方だけを `scripts/lib/mail_update_notice.py risk`（引数なし・読むだけ・常に exit 0）で出せます。
### install.sh: ORRERY Mail の扱いを 3 値にし、結果を 1 行で出します

再実行で、稼働中の ORRERY Mail をどうするかを選べるようにしました。既定は従来どおり差し替えません。

- `--keep-mail`（既定）: 差し替えず、build が違えば `notice:` で示す
- `--update-mail`: 従来どおり差し替える。できなければ exit 1
- `AGENTSTACK_MAIL_UPDATE=auto`（opt-in）: `agentstack-mailctl` 管理の配置で、検証が通り、止まる時間の見込みが `AGENTSTACK_MAIL_UPDATE_OUTAGE_BUDGET`（既定 8 秒）以内のときだけ差し替える。だめなら今の build を残して exit 0

選び方は `--mail auto|update|keep` で、`--update-mail` / `--keep-mail` はその別名です。option は `AGENTSTACK_MAIL_UPDATE` より優先し、`env.sh` には記録しません。どの場合も最後に `mail-result: <installed|switched|kept|unchanged|refused|rolled-back> mode=… from=… to=… running=… outage_s=… reason=<code>` を 1 行出します（`--dry-run` では `mail-plan:`）。`install.sh --print-mail-plan` は、何をするかを `mail-plan:` の 1 行で出して終わります（読むだけ・project key 不要・常に exit 0）。差し替えや rollback の後には、止まっていた秒数と、動いている Claude Code の session で `/mcp` の接続と小さい呼び出しを確かめる手順、止まっている間に失敗した操作は繰り返す前に実行済みかを確かめること、を出します。`install-state.json` の `agent_mail.update` には mode・reason の code・止まっていた秒数も残ります。setup.sh・update.sh は、文の grep ではなくこの行を読めます。

`auto` を既定にするのは、rollback まで含めた停止時間の上限と、更新後の database で前の build が動くことの確認を入れてからです（[設計メモ](docs/agentstack-mail-update-design.md)）。

### `sonnet` が Sonnet 5.5 を起動するようになりました。別名の行き先を固定しません

「Sonnet 5.5 の子を」と頼まれた agent が `--model sonnet` を渡すと、`claude-sonnet-5` が起動していました。launcher が `opus` / `sonnet` / `haiku` / `fable` の行き先を固定値で持っていたためです（dashboard の一覧は #90 でローカルの catalog に追従していましたが、launcher の別名と warm pool の照合は固定のままでした）。いまは、この 4 つの別名と省略時の既定を、Claude Code のローカル model catalog（`~/.claude/cache/model-catalog/`）がその系統の現行として示すモデル（系統ごとに 1 つの `main` 行）に解決します。手元の Claude Code 2.1.287 が `--model sonnet` などを解決した結果とは一致しました（provider の設定や model の上書きがあると、CLI の解決は変わり得ます）。catalog が無い・読めない・系統の行が 1 つに決まらないとき、子が使う Claude Code がそのモデルに必要な版より古いときは、`dashboard/claude_models.py` の同梱の表を使います。固定の値はこの表 1 か所だけで、warm pool の照合と NEW AGENT の既定も同じ解決の結果を使います。

- どこから解決したか（catalog か同梱の表か、catalog が古いか）を、launcher は起動時に、`agentstack-doctor` は 1 行で出します
- 版つきの別名は世代を固定するためのものなので、追従しません。`sonnet-5-5` / `sonnet55` / `sonnet5.5` / `Sonnet 5.5` を足しました（`claude-sonnet-5-5`）。`sonnet-5` は引き続き `claude-sonnet-5` です。`opus-5-5`・`haiku-4-5`・`fable-5-1` も同じ形で受け付けます
- 省略時の既定は catalog の並び順の先頭ではなく、catalog が現行の Opus として指定したモデルに追従します。`AGENTSTACK_CLAUDE_MODELS` の許可リストの扱いは変わりません
- Claude Code は catalog を取得から 1 時間で古いと印を付けます。新しい catalog だけを使うと、ほとんどの起動が同梱の表に戻ってしまうので、別名の解決には古い catalog も使います。ただし古い catalog が同梱の表より古い世代を示すときは、同梱の表を使います。古い catalog は NEW AGENT の候補を増やすのには使いませんが、別名が今起動するモデルは候補に入れるので、画面の既定と launcher は一致します
- 版の確認と起動は同じ binary で行います。`hooks/claude-child-bin.sh` が子の login shell で `~/.local/bin` を先頭にして `claude` を探し、launcher はその版で別名を決め、子はその path を実行します。dashboard も起動時とモデル省略の起動の前に同じ script を実行するので、dashboard と子で PATH が違っても、既定は子の `claude` が動かせるモデルになります
- warm pool は、status がその種類の行に要求モデルの正式 ID を括弧で示し（例 `opus ready (claude-opus-5-5)`）、`claim-model <種類> <子> <正式 ID>` がそのモデルの session を不可分に claim できたときだけ使います。`claim-model` の無い pool・別のモデルで事前起動した pool は cold start、別のモデルを報告した claim は起動を中止します
- catalog は profile の中で最も新しいものを使い、今ログインしている account のものかは確かめません

### Mail なしの再開で、理由と直し方が見えるようにしました

古い（`existing_agent_id` を受け付けない）ORRERY Mail のもとでは、旧形式の state を持つ Claude は、親の有無にかかわらず会話だけで再開されます（MCP なし・hook 無効・Mail なし）。この判定は変えていません。変えたのは見え方です。これまでは理由（`mail_schema_unsupported`）が出ても、直し方は書かれていませんでした。また再開後は、Mail 側の行が retired で `program` が空になるため、走っている行に MAIL UNAVAILABLE が出ていませんでした。いまは、

- `mail_message` と新しい `mail_remedy` に、`./scripts/install.sh --update-mail --dry-run`、続けて `--update-mail` を実行し、会話だけのセッションを終えてから再開し直す手順が入ります。これはこの installer が入れた Mail に当てはまる手順で、dry-run が拒否したら止まって [Mail の更新手順](docs/agentstack-mail-update.md) に従うこと、更新しなければ会話だけの再開のままであることも書きます。再開前の要約、再開の結果、端末に出る notice のどれにも出ます。会話の中の agent には、その更新を自分で実行しないよう伝えます
- 再開の button は、会話だけになる場合 `RESUME WITHOUT MAIL`（確認が要る場合は `VERIFY & RESUME WITHOUT MAIL`）と表示し、title に理由と直し方を出します
- 走っている行は、`program` が空でも、tmux の目印から MAIL UNAVAILABLE を出します。codex など、ほかの program の行は従来どおりです

credential が無い・保持期限切れの場合は dashboard から直せないので、`mail_remedy` は付きません。

### 登録のたびに、contact policy の設定が 1 回失敗していました（#51）

登録の helper は `set_contact_policy` を owner token 付きで先に呼んでいました。同梱の ORRERY Mail の `set_contact_policy` は `registration_token` を受け付けないので、この呼び出しは毎回失敗し、token なしの呼び直しで設定されていました。token なしを先に呼び、失敗したときだけ token 付きで呼び直すようにしました。owner token を求める古い Mail にも、2 回目で設定されます。

### worktree の Codex の子が「hooks need review」で止まっていました

`--worktree` で起動した Codex の子は、利用者が元の checkout で信頼した project の hook を、別の path なので信頼済みと見なされず、Codex の review の画面で止まっていました（2026-10-01 の通しで 4.5 分）。その間は hook も動いていませんでした。launcher は子の `CODEX_HOME` の設定にだけ、元の checkout の hook の信頼を worktree の path でも書き足します。Codex は hook の中身の hash で照合するので、中身が同じ hook だけが信頼されます。利用者の `~/.codex/config.toml` は変えません。dashboard からの再開でも同じです。

## 2026.10.01.1

### 計算の待ちに上限を付け、失敗した計算が重ならないようにしました（#161 のレビュー、API 世代6）

`/api/agents` と `/api/graph` で、ほかの request の計算を待つ時間が 30 秒を超えると `503`（`Retry-After: 1`、`{"error":"busy","retry":true}`）を返します。利用側が扱う新しい応答なので、`/api/version` の `api` を6にしました。共有の計算が失敗したときは、待っていた request がそれぞれ計算し直すのではなく、次の 1 回の計算をまとめて待ちます。失敗し続ける場合も、再試行の途中に新しい request が来た場合も、同時に走る計算は 1 本です。表示の鮮度は変わりません。

### codex の検査が、すぐ終わった失敗も時間切れのように表示していました（#121）

installer・`agentstack-doctor`・子の launcher が `codex --version` を確かめるとき、失敗はすべて「did not succeed within 10s」と表示していました。0.04 秒でエラー終了した場合（PATH の node が Codex を入れた版と違うなど）も、時間切れのように読めました。いまは、時間切れなら `did not finish within Ns and was stopped`、エラー終了なら `exited with status N after Ns:` に続けて stderr の先頭 1 行を表示します。

### vault の外の Claude の子が、渡したタスクを断っていました

launcher はタスクを入力欄に貼り付けていたため、Claude Code はそれを `<pasted_content>` として扱っていました。vault の外の Sonnet 5 の子は、文面によらず断りました（2026-10-01 の測定で 6 回中 6 回）。Claude の子には、タスクを `claude [prompt]` の引数（利用者の最初の発言）で渡し、同じ起動コマンドの `--append-system-prompt` で、誰がこの session を起動したかを運用者の設定として伝えるようにしました。同じ条件で 3 回中 3 回実行しました。

launcher は、子の最初の turn が親への報告で終わったかを transcript で確かめます。文章だけで終わった、tool を呼んだが報告せずに終わった、既定 180 秒（`AGENTSTACK_CHILD_START_WAIT_SECONDS`）の間に応答が無い、のいずれかなら、親に `[launcher]` で始まる Mail を送ります。180 秒の時点でまだ考えている子は分けて知らせ、後で始めたらもう 1 通送ります。子が黙って止まることはなくなりました。1 引数に収まらない長いタスクは、子だけが読める file 経由で渡します。

報告の道具は ORRERY Mail の `send_message` と名前で書くようにしました（Claude Code の SendMessage で報告する子がいたため）。モデルが変わったときに回す `scripts/canary-embed-task.sh` を足しました。

### 製品が起動する Codex で、起動時の更新案内を出さないようにしました（#60）

Codex の起動時の更新案内は、既定の選択が `npm install -g @openai/codex` です。無人の child では断る人がいません。以前の launcher は案内の Enter を sign-in と取り違えて押しており、更新の途中で child を止めると、機体の `codex` が旧版も新版も使えない状態で残りました。Enter の取り違えはすでに直っています（trust 画面だけを全体の配置で見分ける）。今回、child・dashboard の再開・`agent-start-codex`・Windows の launcher の全部で `-c check_for_update_on_startup=false` を付け、案内そのものが出ないようにしました。Codex の更新は利用者が行ってください。また、起動に失敗した child を片付けるとき（launcher の 2 つの経路と dashboard の spawn）、画面に `Updating Codex via` が出ていれば session を止めずに残し、そのことを知らせます。

### 子に渡す道具を起動時に選べるようにしました（API 世代7）

`/delegate` と `spawn_child.sh` に `--base default|mail-only` と `--tools`（`browser[:<deviceId>]`・`screen[:read|:operate]`・`screen:<read|operate>:<server>`・`mcp:<server>`）を、`POST /api/spawn` に `base` と `tools` を足しました。利用者が頼る field を足したため `/api/version` の `api` を7にしました。`--base` も `--tools` も指定しない（道具の選択がない）起動は、コマンドも引数も従来と同じです。

- `mail-only` の child には ORRERY Mail と選んだものだけを渡します。Claude はブラウザを選ばなければ `--no-chrome` も付け、Codex は `orrery-only` と同じ無効化の後で選んだ server だけを有効にします
- 選んだ server は Claude では利用者の定義を strict の設定に写し、Codex では子の `config.toml` で有効にします。承認は child の起動の中だけで、分類表で分かる tool ごとに渡し、server 全体は承認しません。利用者の設定ファイルと全体の承認方針は変えません
- 分類表（今は Windows-MCP 0.8.6）は tool を「読む」「画面を操作する」「画面ではない」に分けます。`screen:read` は読む tool だけを、`screen:operate` は画面の tool だけを公開・承認し、PowerShell・Registry・FileSystem などは `mcp:<server>:all` を別に指定したときだけ承認します。表に無い版では tool を承認しないので、resume の前に server の版が上がっても承認は広がりません
- dashboard の resume の見込み（`resume_capability`）も、道具の選択を戻せるかを見ます。戻せなければ `config_unrestorable` です
- 選択のある child は、指定どおりにできなければ起動しません。Mail の proxy が無い、server を写せない（見つからない・`env` や `headers` を持つ・コマンドが絶対パスでない）、Mac で computer use が有効な project での `mail-only`、などです。選択の無い child の fallback は従来どおりです
- WSL で画面の server を選ぶと、Windows のセッション0（ssh から起動した WSL。画面を取れない）を検出して警告します
- 選択は Claude では会話に結び付いた起動の記録（version 3）に、Codex では `child-agents/<name>.tools.json` に残し、resume は同じ設定を作り直すか、できなければ止めます。Claude child の MCP 設定は、launcher と dashboard の resume が同じ `hooks/child_tools.py` で作るようになりました

これは child への方針であって、技術的な隔離ではありません。動いている child への追加、resume での変更、roster・DECK の表示、NEW AGENT の欄はまだありません（[子に渡す道具を選ぶ](docs/delegation.md#子に渡す道具を選ぶ--base----tools)）。

### 並行の hook と bridge の起動で、時間しだいで重複・拒否が起きていました（#55・#122）

Mail に届かないときの警告は、session ごとに 10 分に 1 回だけ出す設計でしたが、10 分の枠を `date +%s` から決めて `mkdir` で取っていたため、並行に走る hook の時計が枠の境目をまたぐと 2 つの枠を取り、同じ警告が 2 回出ました。session と状態ごとに 1 つの記録を `flock` の下で読み書きし、前回の報告から 600 秒経つまで出さないようにしました（#55）。

Codex App の bridge は、Unix socket を正式なパスに bind してから権限を変え、listen していました。その間に接続した hook は拒否され、socket が既定の権限のまま見える瞬間もありました。短い一時の名前で bind・権限の設定・listen を済ませてから、正式なパスに rename して公開します（#122）。同じ issue のもう 1 つ（lock を待つ時間の検査）はテストの前提の誤りで、子の起動からではなく、子が記録を始めてから数えるように直しました。

### テストがこの Mac の launchd に job を残していました

installer を走らせるテストの後片付けは dashboard の job だけを外していたため、同じ install が読み込んだ Mail の autostart と watcher が launchd に残り、消えた一時ディレクトリを指して再起動を繰り返しました（exit 78/127）。後片付けは、test 用の prefix の下にあり、plist か program がそのテストの HOME の中にある job を全部外します。それ以外の prefix（本物の `org.agentstack` を含む）は launchctl を呼ぶ前に拒否し、同じ prefix で並行して走る他のテストの job には触れません。

## 2026.10.01

### Claude の access token が更新されると、USAGE が dashboard を再起動するまで止まっていました（#53）

Claude Code が定期的に access token を更新すると、同じ account なのに Claude の quota が `account_identity_changed` になり、cockpit の USAGE は最後の値のまま数時間止まることがありました。account が変わったかどうかは、token ではなく Claude Code が記録した account（`~/.claude.json` の `oauthAccount` の account と organization）で判断するようにしました。記録が読めない場合（`CLAUDE_CODE_OAUTH_TOKEN` を使う場合を含む）は従来どおり token で判断します。記録が読めず token だけで判断した場合（`credential_changed_unverified`。多くは token の更新）は、前の値は消したうえで、その後に status line が観測した値を表示します。記録で account が変わったと分かった場合は、新しい account の値が届くまで status line の値も出しません（前の account で動いている session が書いた値が混ざるため）。`~/.claude.json` は版（mtime と大きさ）ごとに 1 回だけ読み、16 MB を超えるときは読まずに token で判断します。account の取得に失敗するたびに、理由・失敗の種類・次に取りに行くまでの秒数を dashboard の log に 1 行出します（token は出しません）。

### cockpit を開いていると、dashboard が CPU を数コア使っていました

ORRERY cockpit は `/api/graph?all=1&spawn_only=1` を 6 秒ごとに呼びます。dashboard はそのたびに全 agent（約 2,000 行）の resume の可否を計算して捨て、行ごとに SQLite を開き直していました（1 回あたり約 3,000 回）。複数の poll が重なると 1 本が 10 秒を超え、cockpit の timeout で取り直しが積み上がっていました。

- `spawn_only` は親子の線だけを返し、行ごとの計算をしません。返す内容は以前と同じです
- graph と agents の 1 回の計算では、Mail の登録を 1 回の問い合わせで読みます
- 同じ graph / agents の計算は同時に 1 本だけにします。計算中に来た問い合わせは、その計算が終わった直後に始まる次の 1 回の結果を、ほかの問い合わせとまとめて受け取ります。どの問い合わせも自分が来た後に始まった計算の結果を受け取り、結果は時間で使い回さないので、表示が以前より古くなることはありません

2,000 行での計測: `spawn_only` 1 回 0.94 秒 → 0.01 秒、`all=1` 0.83 秒 → 0.21 秒、`/api/agents?days=all` 1.08 秒 → 0.20 秒。4 本同時の `spawn_only` は最長 3.65 秒・CPU 7.9 秒 → 0.01 秒未満。API と表示の内容は変わりません。

### 入れ直しで、前回変えた設定が既定値に戻っていました（#137）

`git pull && ./scripts/install.sh` を環境変数のない端末で実行すると、port・label prefix・terminal・MCP URL・service の `PATH`・Python・ORRERY Mail の state root（DB の場所）・`LANG` / `MURMUR` / `DELIVERABLE_ROOTS`・`AGENTSTACK_VAULT` などが既定値に戻り、dashboard が別の port・別の launchd label で登録し直されていました。installer はすべての設定を「明示した値（option・環境変数）> 前回の `env.sh` > 既定値」の順で決めるようにしました。`AGENTSTACK_VAULT` は明示しても env.sh・service に空で書かれていたので、明示した値も届くようにしました。`AGENTSTACK_MANAGED_AGENTS_FILE`・dashboard の log と再起動の設定・Mail の management socket も引き継ぎます。引き継ぐのは選んだ値だけです（`env.sh` に `AGENTSTACK_CHOSEN_SETTINGS` として記録します）。書き出された既定値は固定されず、次の版で既定値が変われば新しい既定値になります。1 つだけ既定値に戻すには空の値を明示し（`AGENTSTACK_VAULT=`、`--codex-add-dirs ""`）、まとめて戻すには `--reset-settings`（`AGENTSTACK_RESET_SETTINGS=1`）を使います。project key・protected roots・Mail の state / service root と management socket・label prefix・Mail の launchd label・MCP URL は、データの置き場所とそこで動く service の名前なので reset でも引き継ぎます（reset で戻すと、前の Mail が残ったまま同じ DB に 2 つ目の Mail が立ちえました）。`env.sh` と同じ値の環境変数（`env.sh` を読み込んだ shell や cockpit の更新 script から来たもの）は新しい選択として扱いません。`AGENTSTACK_CLAUDE_JSON` は試験用の差し替え口なので引き継ぎません。dashboard の plist に入れる値はすべて XML として escape し、`&` や `"` を含む path（vault など）でも plist が壊れないようにしました。前回記録した Python が無くなっていた場合は通知して探し直します。dry-run の冒頭に port・label prefix・terminal・MCP URL を表示します。

### launcher が、明示した project key を install 時の値で上書きしていました（#33）

`agent-start`・`agent-start-codex`・`agent-start-gemini` が起動の途中で `env.sh` を source し、起動前に設定した `AGENTSTACK_PROJECT_KEY` などを install 時の値に戻していました。起動前に設定された値は `env.sh` より優先し、明示した project key に合わせて protected roots も決めます。明示しなければ従来どおり `env.sh` の値を使います。

この順序は `hooks/project-context.sh`（`AGENTSTACK_INHERITED_SETTINGS`）の 1 か所で定義し、installer と launcher の両方が使います。`env.sh` を shell の起動時に読み込んでいる場合、その shell の値は明示した値として扱われるため、入れ直した後は新しい shell を開いてください。

### installer が稼働中の ORRERY Mail を新しい build に差し替えられるようになりました

これまで `install.sh` の再実行は稼働中の Mail を採用し、build には触れませんでした。そのため Mail 側の変更（2026.09.30.4 の `register_agent(existing_agent_id=…)` など）は普通の update では届かず、以前の版の Claude の子は会話だけの resume になっていました。

`./scripts/install.sh --update-mail` は次の順で差し替えます。まず稼働中の Mail を止めずに、新しい build の用意、scratch port で database の snapshot に対する検証、database の backup を済ませます。そのうえで切り替え、新しい build が共有 database で応答しなければ前の build を起動し直します。`env.sh`・autostart・`install-state.json` は、実際に動いている方の build で書きます。差し替えなかったときと戻したときは、終了 status 1 で知らせます。途中で Ctrl-C などで止めた場合も、検証用の snapshot と server を残さず、Mail は前の build か新しい build のどちらかで `env.sh` と一致した状態で終わります。稼働中の接続・token・enrollment の pin・database は失いません。systemd の timer（WSL2 など）でも同じです。`--update-mail` を付けない再実行は、これまでどおり稼働中の build を使い続けます。build が違うときは `notice:` でそれを示します。手順と影響は [docs/agentstack-mail-update.md](docs/agentstack-mail-update.md#--update-mail-による差し替え) を参照してください。

### resume すると whois の model が claude-code に変わっていました（#144）

resume した Claude の SessionStart hook は shell から再登録しますが、model を受け取っていなかったため、登録済みの model（例: `opus-5.5`）を `claude-code`、または tmux server の環境に残った別の agent の model で上書きしていました。resume は登録済みの model（無ければ `claude-code`）をその session の `CLAUDE_CHILD_MODEL` として渡し、SessionStart の再登録はこれを `AGENTSTACK_CLAUDE_MODEL` より優先します。

### resume の前に、Mail まで戻るのか会話だけなのかが分かるようにしました（#145、API 世代5）

`resume_capability: ready` は会話だけの resume でも出るため、resume した体が Mail を使えるかは `/api/jump` を実行した後の応答でしか分かりませんでした。`/api/agents` / `/api/graph` の resume できる Claude row（`ready` / `verification_required`）に `resume_mode` を追加し、`mail`（owner を認証して unretire する。できなければ起動しない）か `conversation_only`（Mail に触れない。`mail_reason` 付き）かを resume の前に出します。`/api/jump` も Mail まで戻した場合に `resume_mode: mail` を返します。`resume_capability` の code と意味は変えていません。利用側が頼る field を足したため `/api/version` の `api` を5にしました。

### token だけの Claude を resume して exit すると、Mail の row が active のまま残っていました（#143）

子の state が無く owner token だけを持つ Claude を resume すると、resume は Mail の row を unretire しますが、exit の後に retired へ戻す処理がありませんでした（子の state を持つ体だけが cleanup で戻っていました）。resume の前に retired だった体は、CLI が正常に終了した後に同じ token で再 retire します。reservation の解放は SessionEnd hook に任せ、同じ identity が別の tmux pane や端末の session で生きている間は retire もしません。token は削除せず次の resume に残します。元から active だった体は retire しません。

対になる側として、端末から `claude --resume` で開き直した体も（`/clear` や compaction では、動いている間に意図して retire した状態を戻しません）、SessionStart が保存した owner credential で本人を確かめ、retained（保持期間内）の子か token だけの体なら unretire します（これまでは dashboard の resume だけが unretire していました）。旧形式・期限切れ・purge 済みは retired のままです。

### session binding の receipt が無い環境で、長い応答の Codex の子の起動が約 90 秒待っていました（#118）

receipt が無い環境（WSL など）では、launcher は Codex の画面を見て、最初の task が始まったことが分かった時点で待つのをやめます。手掛かりは実行中の表示と、task の末尾の後に続く応答です。ところが長い応答では、実行中の表示は約 2 秒、task の末尾は数秒しか画面に出ません。3 秒ごとの確認が両方を逃すと、上限の 90 秒まで待っていました。Codex 0.158 は、task が画面の外に出たあと、その 1 行目を `…` で切って画面の 1 行目に固定表示します。この行がこの task の 1 行目（16 文字以上）と一致し、入力欄が空になっていれば、応答が出ていると判断します。task の末尾が折り返しで 2 行に分かれていても見つけます。WSL で 1 秒ごとに採った実画面で、どの位相から確認を始めても 3〜6 秒で終わることを確かめました（以前は最長 90 秒）。この判定は待ちを終えるためだけに使い、キーは送りません。task が始まった証拠にもしません。

### 以前の版の Claude の子を、同じ名前で起動し直せるようになりました（WSL2 の報告の問題 3）

`spawn_child.sh --pre-registered` は、以前の版の 3 項目の state を「child state belongs to another registration」で拒否し、preregister のやり直しを勧めていました。やり直すと別の名前が登録され、同じ役割の体が二重になりました。旧 state を見つけたら、whois で得た ID で `register_agent(existing_agent_id=…)` を呼び、保存済みの credential を照合します。照合できたときだけ 5 項目の state に移して起動します。照合できなければ state と token を変えずに止め、dashboard の再開を案内します。

### 子の lineage に親を示すようになりました（WSL2 の報告の問題 4）

launcher が子に渡す proxy（direct binding）は親を知らず、`runtime_status` の lineage は常に root でした。`PARENT_AGENT` 付きの Codex の子は、正本の親と食い違うとして起動処理を止めていました。proxy は `AGENTSTACK_PROXY_PARENT_AGENT` を受け、lineage を `kind: child`・`parent_agent: <親>` にします。親を反映できない組み合わせは、CLI の起動前に止めます。

### 子の proxy が、schema に無い引数で落ちていました（WSL2 の報告の問題 2）

direct binding の proxy は、モデルが付けた `sender_name` などで `TypeError` になっていました。binding と一致する値は捨て、食い違う値は理由を返して拒否します。モデルが渡した token は Mail へ送りません。schema に無い引数は、受け付ける引数の一覧つきで拒否します。direct binding の `runtime_status` は `bound` を返します。

### retire された子を起動し直すと、retired のまま動いていました（#153）

`--pre-registered` で以前に動いた子を起動するとき、起動前に Mail の `retired_at` を確かめます。retired なら子自身の token で `unretire_agent` を呼び、active に戻ったことを確かめてから起動します。戻せなければ起動しません。起動に失敗したら retire し直します。

## 2026.09.30.4

### 以前の版で作った Claude child が resume できなくなっていました（#140）

2026.09.30.3 が旧3項目形式の state を `identity_mismatch` と拒否していた回帰を修正しました。既存の正式 Claude 登録・owner と private token を検証し、明示 resume の token 認証が成功した後だけ同じ identity を新形式へ移行します。表示 GET は移行しません。保持期限は認証移行時点から設定日数（既定30日）、設定0では移行を拒否します。認証・起動の失敗では旧 material と元の Mail / husk を保持し、世代付き undo で古い rollback が新しい試行を消すのを防ぎます。会話だけの再開と Mail 不可の表示 field を追加したため API 世代は4です。

bundled Mail の `register_agent` に既存 owner だけを認証する optional `existing_agent_id` を追加しました。旧形式の移行はこのガードを必須とし、古い Mail では登録を代用せず、会話だけ再開します。Mail まで復帰するには dashboard と bundled Mail を一緒に更新・再起動してください。通常の登録と名前生成の動作は変えません。

旧形式の子を exit した際に cleanup が state と token を削除する回帰も修正しました。private な旧3項目 state と一致する token を元のまま保持し、明示 resume で認証して移行します。旧 material の検証エラーでは削除に進まず、保持設定0だけは従来の opt-out に従います。

通常の更新が稼働中の古い Mail を採用する環境でも、以前できていた Claude の会話 resume を戻しました。実 token 欠落・検証済み期限切れ・schema で確認した旧 Mail では、Mail を遮断して会話だけ再開し、応答・DECK・NETWORK・端末に Mail 不可と固定 reason を表示します。既存 material の安全性と identity/pending を先に検査し、認証拒否・不一致・unsafe・破損・明示 purge・schema 取得失敗は拒否します。対応した Mail では認証付きの完全な復帰を維持します。追加 field のため API 世代を4へ上げ、既存 `ok` / `action` / `detail` / `resume_capability` は保持します。

## 2026.09.30.3

### resume も子の窓の自動表示設定に従います（#138、API 世代3）

`AGENTSTACK_AUTO_OPEN_CHILD=0` の Claude / Codex resume は detached tmux で実行し、OS の窓を開きません。未設定 / `1` は従来どおりです。`POST /api/jump` に boolean `open` を明示すると設定より優先します。後から Open tmux や cockpit で開けます。この利用側 field を追加したため `/api/version` の `api` を3にしました。

### Claude の resume 後に Mail を送受信できるようにしました（#135・#136）

正常終了した Claude の子も、Codex と同じく再開用 state と owner credential を既定30日、0600で保持します。dashboard の Claude resume は保存済みの正式 ID と credential を検証し、子専用 Mail proxy config を再生成、同じ identity の再登録・unretire を完了してから起動します。top-level も既存 token で復帰します。credential 欠落、期限切れ、purge、認証や unretire の失敗では起動しません。incoming receipt の provider 不一致や metadata 不備を保存前に拒否し、起動失敗でも既存 credential を保持します。undo は起動ごとの nonce で照合し、purge 後の再試行を旧 launcher の cleanup が消しません。Claude の起動準備が失敗した場合は元の husk と Mail の状態を戻し、元から active だった identity を retire しません。Codex の top-level resume で unretire を飛ばしていた箇所も直しました。履歴は保持・purge の対象外です。

## 2026.09.30.2

### Codex を子としてしか使わない環境で、正常終了した子の会話の記録が消えていました（#133）

子の Codex home は、利用者の `~/.codex` にある項目だけを link で映します。Codex を直接起動したことが無い環境（新しく入れた WSL など）では `sessions/`・`history.jsonl`・`session_index.jsonl` が無く、子は会話の記録を自分の home の中に書き、正常終了の片付けで home ごと消していました。そのため、きちんと終わった子ほど resume できませんでした。子の home を作る前に、この 3 つを利用者の Codex home に用意してから link します。既にあるものは開かずにそのまま残し、欠けたものだけを作ります（読み取り専用の履歴でも子を起動できます）。片付けで実体の記録を消す前には警告を出します。

### `/api/spawn` の `dry_run` を実装し、知らない指定を拒否するようにしました（#132）

これまで `/api/spawn` は `dry_run` を処理せず、知らない指定を黙って捨てて、本当に agent を登録・起動していました。`"dry_run": true` を付けると、登録・予約・Mail・tmux・起動・一時ファイルの作成を一切行わず、組み立てた起動の内容（argv と解決した provider・model・effort・dir）だけを返します。Codex の版を確かめる読み取り専用の `--version` だけは走ります。知らない指定は 400 で拒否します。cockpit の NEW AGENT が送る指定はすべて受け付けます。

### `/api/version` の `api` を 2 にし、利用者との互換の世代として運用し始めました（#134）

ORRERY cockpit は、orrery-telemetry が必要な版かどうかを、日付ではなく `api` で判定します。`api` は、利用者が頼る endpoint や field を足す・意味を変えるときだけ上げます（[docs/api.md](docs/api.md#get-apiversion)）。これより前の版は、中身にかかわらずどれも 1 を返していたので、この版で 2 にしました。`agentstack-doctor` と Windows の確認は、`api` が 1 以上であれば ORRERY Telemetry と認めます（世代は問いません）。

## 2026.09.30.1

### Codex の既定と「sol」を GPT-6.1 Sol にしました（#128）

NEW AGENT と `/delegate` の Codex の既定、別名 `sol` を `gpt-6.1-sol` にしました。GPT-6.1 Sol は Codex CLI 0.159.0 以上でしか使えないので、使えるかどうかは、実際に子を起動する Codex CLI（`AGENTSTACK_CODEX_BIN`、子と同じ login shell と PATH で解決）の版で決めます。`~/.codex/models_cache.json` は最後に一覧を取った Codex の版で上書きされ、古い Codex の session が残っていると数分ごとに 6.1 の有無が入れ替わるので、この一覧には頼りません。CLI が古い環境では既定を `gpt-6-sol` に戻し、`agentstack-doctor` が Codex CLI の更新を促します。dashboard の API と `/delegate` は、選んだ CLI と正式なモデル ID を組で固定して起動に渡します。Gemini や Claude の起動では Codex CLI を起動しません。`python3 ~/.agentstack/dashboard/codex_models.py resolve sol` で、選ばれるモデル・CLI・版・判断の理由を確かめられます。

### Codex の子の resume が必ず失敗し、失敗すると resume できなくなっていました（#127）

telemetry の deck から Codex の子を resume すると、tmux の session が 1 秒ほどで消え、cockpit にも出ませんでした。resume の起動処理が program を `codex` と書いて照合し、子の記録（receipt）の `codex-cli` と一致しなかったためです。さらに、拒否の前に登録をやり直していたので、一度失敗するとその子は二度と resume できなくなっていました。照合は、照らし合わせた元の receipt の表記を基準にし、書き込まずに照合してから再登録するようにしました。resume して何も入力せずに閉じても、次の resume はできるままです。

### docs

- 「Obsidian と一緒に使う」のページを足しました（`docs/obsidian.md`、#119）
- README 冒頭の GIF を今の画面で撮り直しました（#124）。紹介動画「Orrery」への案内を足し、英語の README は英語版を指します（#126・#130）

## 2026.09.30

### WSL で、Codex の子の起動の確定に毎回約90秒かかっていました（#117・#120）

Codex の子は、task が始まったことを Codex の記録（receipt）で確かめてから起動を確定します。WSL では Codex の連携（lifecycle hooks）が入っていないことが多く、receipt が無いので、launcher が上限の90秒まで見張ってから「確認できない」と確定していました。子は動いていましたが、spawn の完了と次の操作がその間待たされました。

- **連携を入れれば約7秒になります（根本の対処）。** `scripts/install-codex-app-integration.sh` が WSL でも動くようにしました。これまでは PATH の先頭にある Windows 側の `codex`（`/mnt/c/...`）を拾っていたので、core の installer と同じ規則（WSL では `/mnt` 配下を除き、`--version` に答えるものだけ）で選びます。macOS 以外では launchd の service を作らない `--no-service` が既定です。入れた後に Codex で一度 `/hooks` を開き、AgentStack の hooks を承認してください。手順と画面は [docs/codex-app.md](docs/codex-app.md) にあります。承認の後、長い task でも起動の確定は約7秒でした（実機で 7.1〜7.3 秒）
- **連携が無い環境でも、短い task は早く確定します。** receipt が無いとき、子の画面に Codex の実行中の表示か応答の表示が出たら、そこで確定します（93秒 → 約10秒）。長い応答の途中で task の文面が画面の外に出ると、これまでどおり90秒待つことがあります（#118 で追跡中）

### WSL で窓を閉じたときの説明を、実測に合わせました（#116）

docs は「Windows Terminal を閉じると WSL が止まり、agent も止まる」と説明していましたが、これは ssh から起こした場合だけでした。デスクトップ（Windows Terminal など）から起こした tmux や dashboard は、窓を閉じても動き続けます。止めるには `wsl --shutdown` を使いますが、これはすべての distro を止めるので、その範囲も書き添えました。

## 2026.09.29

### Claude の子が、利用者向けの質問画面に答えずに60秒待って失敗していました（#110）

Claude の子の起動直後に、1回限りの質問（「Claude in Chrome extension detected」など）が出ると、準備完了の判定がこの画面を認識できず、何も押さないまま60秒待って失敗していました。この答えは利用者の今後の既定になりうるので、launcher は答えません。質問画面を検出したらキーを送らずにすぐ止め、「通常の端末で `claude` を一度開いて答えてから、もう一度起動してください」と案内し、失敗の時点の画面を記録に残します。trust の画面は、いま選ばれている選択肢を画面の構造から読み、trust のときだけキーを送ります。

### bound proxy の親が /delegate を断っていました。古い managed block も検出します（#113）

dashboard から起動した Codex の親が、自分の接続（bound proxy）に `register_agent` などの道具が無いことを理由に、`/delegate` を断っていました。子の登録は shell の helper（`agentstack-preregister-child`）が別の identity として行うので、親の接続にそれらが無いのは正常です。delegate の skill、managed block、起動の案内をそのように書き直しました。あわせて、`~/.codex/AGENTS.md` や project の `CLAUDE.md` に古い managed block が残っていると、doctor と installer が知らせ、更新のコマンドを示すようにしました（これまでは block があるだけで `ok` でした）。

### dashboard の肖像の来歴を、実際のとおりに記録しました（#114）

dashboard の肖像（`portraits_64`・`portraits_pixel`）は、作者が画像生成で文章だけから作ったドット絵です。Wikimedia Commons の写真ではありません。これまでの記録は Commons の写真だと説明していたので、`docs/third-party.md` と各 manifest、公開デモの説明を実際のとおりに直しました。画像そのものは変えていません。ドット絵は repo と同じ条件で配布します。

### WSL で、agent が起動した Codex の子が Windows の codex を使って即終了していました

dashboard から起動した agent が `/delegate` で Codex の子を作ると、spawn_child.sh が PATH の先頭にある Windows の `codex`（`/mnt/c/.../npm/codex`）を選び、`Missing optional dependency @openai/codex-linux-x64` で数秒で終わっていました。agent の shell には `AGENTSTACK_CODEX_BIN` が無く、PATH の検索に頼っていたためです。spawn_child.sh は、環境の `AGENTSTACK_CODEX_BIN`、次に installer が `~/.agentstack/env.sh` に保存した値（1行だけ読み、実行はしません）、最後に検索の順で探し、installer と同じ規則（WSL では `/mnt` 配下を除く、`--version` に答えるものだけ）で使えるものを選びます。使えない候補は理由を表示して飛ばし、見つからなければ試したものと理由を示して止まります。

### Linux（WSL）で ORRERY Mail の通知が最大30秒遅れていました

Linux の fswatch（inotify）は、新しくできたフォルダの watch を後から足すため、その前に書かれた signal を通知しません。受信者のフォルダは配送のたびに消えて次の1通で作り直されるので、WSL ではほとんどの通知が30秒ごとの回復 scan を待っていました。Linux では回復 scan を2秒にしました（macOS は30秒のまま。`AGENTSTACK_MAIL_WATCHER_SCAN_INTERVAL` で変えられます）。同じ message の再試行間隔は30秒のままです。あわせて、配送の lease を取った直後にもう一度配送済みかを確かめ、前の配送と入れ違いで同じ通知が二重に送られる隙間をふさぎました。また、書きかけで JSON として読めない signal を、件名なしの通知として送って消していたのをやめ、次の scan で読み直すようにしました。

### Codex の子の最初の task を、起動引数で渡すようにしました

Codex の子の最初の task を、画面に貼り付けて送信する代わりに、Codex の公式の `[PROMPT]` 引数（`codex -- "<task>"`）で渡します。WSL で、Codex 0.158 の子が8回に1回、貼り付けた task で turn を始めないまま待っていました。Codex 0.158 は起動途中にも入力欄を描き、確認画面の前に入力を捨てる処理を持つため、貼り付けと画面の切り替わりの競争が最も有力な仮説です（原因は断定できていません）。そこで task を端末経由で渡すこと自体をやめました。task は 0600 のファイルを通して子の shell が読み、1つの引数として渡すので、shell のコマンドとして解釈されることはありません。task が始まったかは、画面の文字ではなく、この起動の Codex の記録（rollout）で確かめます。これは Codex の history binding（任意）が receipt を残した場合だけで、無い環境では最大90秒見張ったあと「確認できない」と残して子はそのままにします（失敗ではなく、再送もしません。その間 spawn の確定は待たされます）。見張りのあいだ launcher がキーを送るのは、実画面で確かめた形の trust 画面だけで、会話の中に出てきた同じ文言や、形を確かめていない model・sign-in の画面には応じません。Claude の子と resume は変わりません。

### WSL で Codex の子が起動直後に終わっていました

WSL の PATH には Windows の PATH（`/mnt/c/...`）が混ざるため、installer が Windows 側の npm の `codex` を選び、`AGENTSTACK_CODEX_BIN` に保存していました。これは Ubuntu の node では `Missing optional dependency @openai/codex-linux-x64` で即座に終わり、NEW AGENT の Codex の子がすべて、指示が届く前に終わっていました。installer は、WSL では `/mnt/<drive>/` 配下の `codex` を候補から外し、どの候補も `--version` に短い時間で答えるものだけを使うようにしました。PATH に無い `~/.npm-global/bin` なども探します。以前の install で保存された値が使えなければ選び直し、`--codex-bin` で明示した値が使えなければ理由を示して止まります。`agentstack-doctor` も、使われる `codex` が動かないときは `ok` ではなく `warn` と直し方を出し、0.157 より古い Codex CLI には更新を促す note を出します（0.153.4 では ChatGPT アカウントで GPT-6 系のモデルが拒否されました）。`--version` の確認は、応答しない候補を TERM のあと KILL してでも時間内に打ち切ります。

### Codex の新しい trust 画面で、指示が送られずに止まっていました

Codex 0.153〜0.157 の trust 画面（「Trust this folder?」「1. Trust and continue / 2. Quit」）を trust 画面と見分けられず、入力できる状態と誤認して、指示を貼ったまま送らずに止まっていました。新旧どちらの文言も trust 画面として扱い、新しい画面では「Trust and continue」が選ばれているときだけ Enter で受けます。「Quit」が選ばれていれば一度上に移して確かめ、移らなければ Enter を押しません。ラベルが折り返された画面や、読み取れなかった画面でも Enter を押さず、Enter を無条件に押すのは旧い画面と確かめられたときだけです。

## 2026.09.27

### Claude の子が、利用者向けの質問画面に答えずに止まるようにしました

Claude Code が「Claude in Chrome extension detected」とブラウザ操作の既定を尋ねる画面では、launcher はキーを押さずにすぐ止まり、「通常の `claude` で自分で答えるか `/chrome` で決めてから、もう一度起動する」よう案内します。答えが利用者の今後の既定になる可能性があるためです。これまでは何も押さずに 60 秒待ってから、理由を示さずに失敗していました。未知の選択画面は 10 秒で止めます。失敗時の画面（空行を除いた最大 40 行）を `spawn_incidents.log` にも残し、待機中は 10 秒ごとに経過を出します。pre-registered と legacy の両方の起動経路が、同じ待機処理を使うようにしました。

### bound proxy の親でも /delegate できるように説明を直し、古い managed block を doctor が見つけるようにしました

WSL で、bound proxy の Codex の親が「proxy に register_agent などが無い」ことを理由に /delegate を止めていました。子の登録は shell の `agentstack-preregister-child` が別の identity として行うので、親の proxy にそれらのツールが無いのは正常です。delegate skill と managed block（Codex / Claude）で、「自分の再登録の禁止」と「子の事前登録」を分けて書きました。standalone の起動 prompt にも「登録済み。自分を再登録せず、起動の儀式として inbox を読まない。子を起動するのは可」と明記しました。

`agentstack-codex-setup --check` / `agentstack-claude-setup --check` を追加しました。marker 間の文面を、インストール済みの template と比べます。doctor と installer の最後が同じ検査を使います。これまでの doctor は開始 marker があるだけで `ok` を出していたため、WSL に残っていた古い block（proxy の経路を導入する前の版）も `ok` と表示していました。一致しない場合は、更新コマンドを表示します。

### Claude の子に Claude in Chrome（ブラウザ操作）を明示して渡せるようにしました

`spawn_child.sh --claude-chrome` / `--claude-chrome-device <deviceId>`、`/delegate` の同名フラグ、NEW AGENT の「Explicitly enable Claude in Chrome」で、Claude の子を `--chrome` 付きで起動します。指定しない子の起動コマンドは従来と同じ（inherit）で、Chrome を使えるかは利用者の Claude 設定で決まります。deviceId は子への選択ポリシーで、技術的な隔離ではありません。子は `list_connected_browsers` で接続を確かめて `select_browser` が成功してから自分のタブで操作し、見つからないときは止まって報告します。指定した子は warm pool を使わず cold start します。保証する範囲は新規の cold 起動です。dashboard からの resume は補助機能で、その会話（session ID）の記録があれば `--chrome` とブラウザの選び方を復元し、記録が壊れていれば止めます。記録が無ければ従来どおり再開します（指定したブラウザの復元は保証しません）。公式 docs では WSL は非対応です。Claude Code 2.1.283 のアカウント接続で WSL から Windows のブラウザを操作できたことを 1 台で確認しています。詳細は [docs/delegation.md](docs/delegation.md#claude-child-とブラウザ操作claude-in-chrome)。

### NEW AGENT の Codex でも、旧世代のモデルを「more models」に畳みます

Claude と同じく、Codex の `gpt-5.6-sol`・`gpt-5.6-luna`・`gpt-5.5` を NEW AGENT の「more models」の中に移しました。`gpt-6-astra`・`gpt-6-sol`・`gpt-6-luna`・`gpt-5.6-terra` は前面のままです。どれを畳むかは同梱の表で決め、番号の大小では決めません。既定のモデルは畳みません。期限内のローカル catalog が隠しているモデル（`visibility` が `list` 以外）は、同梱の候補に入っていても一覧から外し、畳む対象にも入れません（launcher の既定 `gpt-6-sol` だけは残します）。`AGENTSTACK_CODEX_MODELS` で許可リストを明示している場合は畳みません。`/api/spawn-names` の Codex provider は `overflow_models` を返します。

### Codex の子が起動した Codex の子（孫）でも、ローカルのモデル一覧を読めるようにしました

孫の `CODEX_HOME` の `models_cache.json` は「孫→子→元」と symlink を2段たどる必要があり、1段しかたどらなかったため、孫では同梱の候補に戻っていました。launcher が作る子の home の中だけを通る連鎖を、段数の上限（8）と循環の検査付きでたどるようにしました。途中の home 自体が symlink の場合や、実体が `child-agents` の直下に無い場合はたどりません。

### NEW AGENT の Codex 既定を GPT-6 Sol に更新しました（#97）

Codex のローカル model catalog 追従に合わせ、短縮名 `sol` とモデル無指定時の既定をどちらも `gpt-6-sol` に揃えました。以前の世代を固定して使う場合は `gpt-5.6-sol` のように正式 ID を指定してください。`AGENTSTACK_CODEX_MODELS` で許可モデルを明示している環境では、新しい既定の `gpt-6-sol` が許可リストに無ければ、これまで無指定で通っていた起動が拒否されます。短縮名も同じで、`--model sol` は `gpt-6-sol` に展開されてから許可リストと照合されるため、`gpt-6-sol` が許可されていなければ拒否されます（`luna`・`astra`・`terra` も展開先の正式 ID で照合されます）。

ローカル cache は候補表示と無指定 effort の補助に使い、期限切れや候補情報だけを理由に明示 effort を拒否しません。launcher が child の `CODEX_HOME` に作る正規の `models_cache.json` symlink も読み取れるようにし、Gemini provider では `gpt-*` 名前空間を予約して provider 間のモデル名衝突を防ぎます。

### Codex の子を何体か起動すると、全員の履歴の紐付けがぶつかっていました（#95）

子を登録したとき、ORRERY Mail の応答から子の番号を読み取る処理が、応答の外側にある JSON-RPC の id（通信の受付番号）を子の番号と取り違えていました。受付番号を数値で送る経路では全員が `1` になり、同じ `codex_launches/1.json` を共有して、dashboard で UNBOUND になっていました。JSON-RPC の応答では外側の id を使わず、応答の中身から子の番号を読むようにしました。外側の包みが無い応答は、これまでどおり読めます。調査と修正は kame447 さんによるものです。

### NEW AGENT で最初に選ばれる Claude のモデルを、launcher の既定に揃えました

dashboard の NEW AGENT は Claude のモデルとして `claude-sonnet-5` を最初から選んでいましたが、`spawn_child.sh` など launcher の既定は `claude-opus-5-5` で、起動経路によって既定が違っていました。NEW AGENT と `/api/spawn` でモデルを省略したときも `claude-opus-5-5` を使うようにし、両者がずれたらテストで分かるようにしました。選択肢の一覧は変わりません。

## 2026.09.26.1

### Codex の子の履歴が UNBOUND のとき、何を確かめればよいかが分かるようにしました（#86）

Codex の子を dashboard の履歴と結び付けるには、任意の plugin `agentstack-codex-app` を入れて有効にするだけでなく、Codex の `/hooks` でその hook を承認する必要があります。これがセットアップからも診断からも分からず、子は正常に動いているのに履歴だけが UNBOUND のまま、という状態になっていました。

- `install-codex-app-integration.sh` は、plugin を入れた後と `--refresh-plugin-only` の後に、`/hooks` での承認と新しい Codex process での起動を案内します
- `agentstack-doctor` は、子の起動に実際に使う `codex` と `CODEX_HOME` を表示し、plugin が未導入・無効・有効のどれかを見分けます。承認状態は Codex の内部ファイルからは推測せず、「不明」として `/hooks` での確認を案内します。Claude だけで使う環境は故障として扱いません
- self-test は、Codex の hook と履歴の対応を検査していないことを結果に書きます
- dashboard は UNBOUND の判定を変えず、理由の文に確認すべき点を添えます

hook の自動承認はしません。調査と実装は kame447 さんによるものです。

### installer の完了表示に、子の窓の自動表示だけを止める設定の案内を足しました（#87）

`automatically open child terminals: 1` の後ろに、再インストール時に `AGENTSTACK_AUTO_OPEN_CHILD=0` にすれば Open tmux を残して自動表示だけ止められる、という案内を出します。表示だけの変更です。

## 2026.09.26

### 子のターミナルの自動表示だけを止められるようにしました（#87）

子を起動すると OS のターミナルが自動で開きますが、これを止める手段は `AGENTSTACK_TERMINAL=none` しかなく、dashboard の Open tmux まで使えなくなっていました。新しい設定 `AGENTSTACK_AUTO_OPEN_CHILD` を足し、`0` にすると自動表示だけが止まります。tmux session、Open tmux、Mail には影響しません。既定は `1` で、これまでと挙動は変わりません。dashboard から子を見ている場合は `AGENTSTACK_AUTO_OPEN_CHILD=0 ./scripts/install.sh ...` で切り替えられ、再インストールしても保持されます。実装は kame447 さんによるものです。

### Codex の子が、親への完了報告を送れないことがありました（#85）

事前登録して起動した Codex の子が、`bootstrap` に自分の名前を `agent_id` として渡すと、`MCP process is already bound to another runtime` で拒否されていました。子は誤った送信者で送るのを避けて送信そのものをやめるため、親は timeout まで待たされていました。`bootstrap` をこの形で呼ぶかはモデル次第なので、失敗は間欠的に見えていました。直接束縛の子では、自分の名前の `agent_id` を取り除いて続けるようにしました。別の名前はこれまでどおり拒否します。

### Mail パッケージのテストが、稼働中の Mail に触れることがありました（#80）

稼働中の Mail の設定を読み込んだ shell から `packages/agentstack_mail/tests` を流すと、テスト内の Mail が本物の管理 socket を開こうとして失敗していました。`tests/` と同じく、Mail パッケージのテストでも継承した `AGENTSTACK_*` を消すようにしました。

### Mail 更新手順の検証と確認が、環境によって失敗していました（#79・#84）

[docs/agentstack-mail-update.md](docs/agentstack-mail-update.md) の手順3で、隔離した検証用の Mail が稼働中の Mail と同じ管理 socket を開こうとして起動直後に落ちていました。管理 socket も検証用の場所に書き換え、稼働中の場所を指したままの設定が残っていれば起動前に止まるようにしました。手順7の稼働確認も、Homebrew の Python から作った venv では process が `Python.app` として見えて失敗していたので、判定を `candidates/<sha>/venv/bin/` に変えました。

## 2026.09.25

### Claude Code 2.1.282 で、Claude の子が全部起動に失敗していました（#81）

Claude Code 2.1.282 は、起動直後の入力欄にプレースホルダ（`❯ Try "fix lint errors"`）を出し、`? for shortcuts` を出さなくなりました。`spawn_child.sh` は「空の `❯` 行」か「`for shortcuts`」でしか起動完了を判定していなかったため、60 秒待って打ち切り、`Claude readiness timeout (60s)` で Claude の子を1体も起動できませんでした。`❯` の直後が `Try "` の行も入力待ちとして扱うようにしました。安全確認ダイアログの `❯ No, exit`・`❯ Yes, I trust` は、従来どおり入力待ちとして扱いません。

## 2026.09.24

### 子の事前登録が、ハイフン付きの名前で既存の agent と衝突していました（#72）

ORRERY Mail は `register_agent` で名前の英数字以外を取り除いて保存する一方、`whois` などの参照は受け取った名前をそのまま探していました。そのため `agentstack-preregister-child` が生成した `Hardy-Somerville` のような名前は、空きの確認では見つからないのに、登録すると既存の `HardySomerville` と同じ名前になり、事前登録が失敗していました。参照を「完全一致を先に探し、無ければ同じ規則で正規化して探す」に揃え、`agentstack-register.sh` も同じ正規化を使うようにしました。旧形式の名前での参照は、そのまま既存の agent に解決されます。

### install 系のテストが、本物の `~/.codex/AGENTS.md` を書き換えることがありました（#73）

Codex の子の中でテストを流すと、子から受け継いだ `CODEX_HOME` が本物の `~/.codex` を指したまま install 系のテストが走り、managed block の project key を pytest の一時ディレクトリに書き換えていました。テストの前に `CODEX_HOME` と `CLAUDE_CONFIG_DIR` を消し、実ホームを書き換えようとしたテストを止める検査を足しました。

### managed block と docs の記述が実態とずれていました（#74）

Codex 向けの managed block が「Codex には PostToolUse hook が無い」「skill registry が無い」と説明し、docs には存在しない見出し `#credential-unavailable` へのリンクがありました。記述を実態に合わせ、見出しを追加しました。docs 内のアンカーが実在するかを検査するテストも足しました。

### `agentstack-selftest` が、正常なのに「dashboard が別の database を読んでいる」と失敗することがありました

dashboard はグラフを 8 秒 cache します。selftest は agent を登録した直後に1回だけグラフを読むため、直前に cockpit などがグラフを読んでいると、登録前の古いグラフを受け取って失敗していました。2つの agent とそのリンクが揃うまで最大 12 秒読み直し、それでも無いときだけ失敗とするようにしました。

## 2026.09.23

### Claude Opus 5.5 を child の current model として選べませんでした

child launcher の無指定 `opus` と warm pool は Claude Opus 5 のままで、2026-09-22 に公開された Opus 5.5 を選べませんでした。既定を `claude-opus-5-5` に更新し、current 1M alias を `claude-opus-5-5[1m]` に向けました。generic な `opus[1m]` は既存どおり legacy Opus 4.8 1M のままにし、`claude-opus-5`、`opus-5`、`opus-5[1m]` は旧世代を明示指定する互換形として維持しています。dashboard の Claude model allow-list にも Opus 5.5 を追加しました。

### 正常終了した Codex child を再開できませんでした（#59）

Codex child は正常終了時に owner credential と専用 home を削除していたため、履歴と provenance が残っていても同じ identity を再登録できず、dashboard の resume は `credential_missing` で止まっていました。正常 cleanup では remote retire と reservation release を維持したまま、schema version・`retired_at`・`resume_expires_at` 付き state と canonical credential を既定30日保持するようにしました。専用 home、proxy runtime、旧 MCP config は毎回削除し、resume 時に現在の source home と保存済み `codex_mcp_profile` から新しく作ります。credential 付き再登録と fresh binding expectation が成功した後、Codex exec の直前にだけ unretire します。保持期間は `AGENTSTACK_CHILD_RESUME_RETENTION_DAYS` で変更でき、`0` は従来どおり全削除です。明示 purge と期限切れ maintenance を追加し、doctor は削除せず期限切れ・purge 待ちだけを報告します。resume 後の receipt にも child provenance を引き継ぐため、cleanup を挟んだ2回目以降の resume も可能です。また、provenance gate が従来 resume できた `cx` 起動の top-level Codex まで child 扱いで拒否していたため、製品の top-level launch / receipt には `launch_origin: standalone` を記録し、private owner credential を検証したうえで child 専用 home・cleanup・unretire を使わない従来経路を維持します。実 Codex は resume の `SessionStart` を REPL 起動時ではなく最初の prompt 送信時に発火するため、prompt を送らず終了すると fresh receipt が無いまま旧 receipt も無効になり、次回以降を resume できませんでした。resume expectation は dashboard が選んだ session ID を旧 receipt と rollout header の両方で照合し、hook が未発火の間だけその receipt の nonce pair を fallback として保持します。別 session の hook、競合、startup、または別の fresh receipt が現れれば fail-closed で無効にします。SessionStart hook の1秒 deadline が recorder の途中で切れると、lock file だけ作られて launch transition と receipt が残らず、原因も観測できませんでした。deadline を5秒へ延ばし、lock・header・write・outcome の時間を session ID、path、nonce、credential を含まない runtime log へ記録するようにしました。

upgrade 前に起動した top-level Codex の receipt は origin 不明のため、次に製品 launcher から起動して `standalone` provenance を記録するまでは dashboard から resume できません。

### cleanup 済み Codex child と unmanaged session を区別できませんでした（#59）

正常終了時に child state と専用 home を削除すると、残った履歴だけでは製品が起動した child か、もともと管理外の Codex session かを判定できませんでした。Codex child の launch expectation と bound receipt に、秘密を含まない `launch_origin: child`、`codex_mcp_profile`、数値 agent ID、project、provider を保存し、cleanup 後も dashboard が child provenance を検証できるようにしました。既存の provenance 無し receipt は推測で child に昇格しません。

### resume できない終了済み agent に、resume 操作を案内していました（#59）

DECK と NETWORK は `gone` / `retired` という表示状態だけで resume 操作を出していたため、検証済み transcript、元の cwd、CLI、Codex の launch provenance・credential・設定が無い row も resume 可能に見えていました。backend が row ごとに固定理由コードの `resume_capability` を返すようにし、DECK card、NETWORK の一括選択、詳細 panel、`/api/jump` が同じ判定を使うようにしました。新しい製品 launch は `child` または `standalone` を receipt に記録し、provenance 導入前や製品外の origin 不明 row は推測で `ready` にせず、API から直接呼んでも terminal を開く前に拒否します。終了済み Claude row の表示判定が agent ごとに数千件の transcript を全読みして dashboard を止めていたため、表示では exact index または transcript directory mtime 付きの確定済み cache だけを使い、未検証 row は `verification_required` とするようにしました。この row は一括 resume には含めず、詳細 panel の `VERIFY & RESUME` を1回押すと `/api/jump` が full 検証し、成功時はその呼び出しのまま resume します。

### fresh install と CI が `sqlmodel 0.0.45` 以降で動かなくなっていました（#67）

ORRERY Mail は datetime を naive UTC で書き込んでいますが、依存に上限が無かったため、fresh venv は naive datetime を拒否する新しい `sqlmodel` を解決し、Mail の tool と installer が database write で失敗していました。隔離した同じ fixture は `0.0.44` で通り、`0.0.45` から失敗します。まず稼働中と同じ挙動へ戻す即応として `sqlmodel<0.0.45` に pin しました。

続く本修正では database への書き込みと検索条件を timezone-aware UTC に統一しました。SQLModel の版に依存しない型で、既存 database に保存済みの naive 値は UTC として読み出し、新規の naive 値は拒否します。API・signal・通知の timestamp 文字列は従来形式を保ったまま、`sqlmodel` の上限 pin を外しました。

## 2026.09.19

### 正常終了した Codex child の履歴が `receipt_missing` になっていました（#58）

Codex child の履歴 receipt は child 専用 `CODEX_HOME` 経由の rollout path を記録していました。正常終了時の cleanup がその home と `sessions` symlink を削除するため、共有 Codex home に rollout の実体が残っていても dashboard は履歴との対応を確認できず、resume の手前で拒否していました。recorder は symlink を解決した実体 path を記録するようにしました。既存 receipt は、消えた path が同じ child の runtime 内 `codex-home/sessions` 配下にある場合だけ共有 Codex home の同じ相対 path へ引き直し、receipt と rollout header の session id が一致するときだけ採用します。

### `--worktree` の child が、再起動後に作業 directory を失っていました（#57）

isolated worktree は `/tmp/cc-worktrees` に固定されていたため、再起動や OS の一時 file 掃除で cwd や tracked file が消え、残っている child state と rollout から resume できませんでした。新規 worktree の既定を install root 配下の永続な `worktrees/` に変更し、`AGENTSTACK_WORKTREE_ROOT` で上書きできるようにしました。上書き先は Codex child の writable scope と dashboard resume にも渡します。`agentstack-doctor` は live でも active registration でもない worktree を報告しますが、削除しません。既存の `/tmp/cc-worktrees` は移動・削除しません。

## 2026.09.18

### launcher を通らずに起動した常駐 bot が、local credential を持てませんでした（#56）

tmux に常駐する親なしの bot（Claude Channels bot など）は、launcher の外で素の `claude --channels ...` として起動されてきました。そのため local credential が一度も保存されず、SessionStart の案内が誘導する `agentstack-reregister` は `stage=local-token reason=credential-unavailable` で必ず失敗していました。credential が消えたのではなく、保存される経路が無かったためです。こうした bot を製品の経路に乗せる部品を 3 つ加えました。手順は [docs/persistent-agents.md](docs/persistent-agents.md) にあります。

- **credential の enroll**（`agentstack-enroll inspect | claim | recover`）。ORRERY Mail がローカルの Unix 管理 socket（0600、peer UID 検査、稼働中の server instance に pin）を公開します。CLI は数値 agent id・project・期待する `credential_generation` を固定し、既存 row を compare-and-swap で更新して、server が受理した後にだけ 0600 の local credential を有効にします。alias も新しい row も作らず、結果・audit・log に secret は出ません。MCP / proxy の catalog には存在せず、operator が local terminal で実行するものです
- **常駐 profile と起動 wrapper**（`agentstack-persistent inspect | run --profile`）。wrapper は instance lock を取り、保存済み credential を同じ row と照合し、観測された standalone の Mail alias を同名の bound proxy に置き換える MCP overlay を書いてから、設定された command を `exec` します。該当する alias が無ければ `orrery-mail` を 1 本生成します。interactive の Claude は通常の `--channels plugin:...` をそのまま使います（`--strict-mcp-config` は Channels を無効化するため使いません）。exec 前の検査は Claude の実効設定源を有限に列挙し、固定の reason と path で fail-closed します
- **installer と入口**。`install.sh` は自分が動かす Mail deployment を記録し、再 install では稼働中の健全な Mail を adopt して、enroll CLI を未 build の candidate ではなくその deployment のものに向けます。install される `agentstack-persistent` は、installer が選んだ絶対パスの interpreter で実装を `exec` します。ambient `PATH` の `python3` へは fallback しません
- DB に列 `agents.credential_generation` を加えました（default 0）。旧 server は読まないので、この版が claim した DB を前の版で開いても row・name・credential は同じままです
- dashboard は headless profile にだけ `BRIDGE · HEADLESS` を表示します

macOS 1 台で、既存の Channels bot 1 体を `recover` で enroll して確認しました。launchd から起動した wrapper、本番の headless provider 配送、2 台目の機体は未検証です。

enroll の socket は新しい Mail build が公開します。稼働中の Mail は再 install で adopt されるだけで入れ替わらないので、enroll を使うには [docs/agentstack-mail-update.md](docs/agentstack-mail-update.md) の手順で Mail を切り替えてください。

### docs: ORRERY Mail の更新手順を installer の配置に合わせました

`docs/agentstack-mail-update.md`（英語版も）は 8 月の手作業配置（`cutover-maintenance/` と pointer file）を前提にしていました。現在の配置では `install.sh` が「健康な listener があれば再利用、無ければ candidate を用意して起動」の二択で、更新は「`agentstack-mailctl stop` → installer」です。候補の事前 build、scratch port での offline 検証、autostart unit の退避、切替後の確認、`AGENTSTACK_MAIL_CANDIDATE_ID` による rollback を、実機で通した手順として書き直しました。dashboard の `/api/version` は Mail の切替の証拠にならないことも明記しています。

## 2026.09.17.1

### tool 引数の validation error が、引数の値ごと server log に出ていました（#49）

FastMCP は tool の引数を pydantic で検証し、失敗すると ValidationError の全文（`input_value='…'` を含む）を `fastmcp.tools.tool_manager` の logger に記録します。これは ORRERY Mail 側の引数の伏せ字処理より前の層です。登録 helper（`agentstack-reregister` と SessionStart hook）は `set_contact_policy` を **まず `registration_token` 付きで**呼び、この tool がその引数を受けなかったため、毎回この経路で失敗し、owner token が server log に書かれうる状態でした。helper は token なしで再試行して exit 0 で終わるので、気づきません。隔離 fixture で canary token が log に出ることを確認しました。稼働中の 1 台では同じ失敗が記録されている一方で値の形は出ておらず、その差の原因は未確定です。

- tool 呼び出しの境界（middleware）で、tool が受けない引数があれば **件数だけ**を挙げて拒否し、名前も値も出しません（名前は呼び手が自由に置ける文字列です）。残った ValidationError も、公開 schema で確認できた tool 名と field 名、件数、error type だけを持つ error に置き換えて client に返します
- `fastmcp.tools.tool_manager` の logger に filter を入れ、ValidationError を伴う記録を「固定の placeholder・件数・error type」だけに書き換え、例外本体を落とします（logger の側では schema を参照できないので、tool 名も field 名も繰り返しません）。他の tool error の診断は変えていません
- `set_contact_policy` の schema は変えていません（公開 tool の schema は upstream の捕捉 fixture と一致させる契約があります）。helper の token 付きの最初の呼び出しは引き続き失敗しますが、その error と log に値は含まれず、helper はこれまでどおり token なしで再試行して成功します
- 回帰テスト: canary を値・引数名・dict の key のそれぞれに置き、未知の引数・型不正・入れ子で client 応答と server log の両方に現れないこと、helper と同じ順（token 付き → token なし）の `set_contact_policy` 呼び出しで token が漏れず policy が更新されること

過去の log に値が残っているかは、この修正では判定も削除もしません。

## 2026.09.17

### macOS の autostart trigger が起動した Mail server を、launchd が直後に kill していました（#46）

launchd は job が終了すると、job と同じ process group に残っているプロセスを終了処理の対象にします。`agentstack-mailctl start` は runner を `nohup` で起動して終了しますが、`nohup` は process group を変えません。そのため trigger 自身が spawn した runner と server は、log に「ORRERY Mail started」と書かれたあと job の終了直後に消え（reboot 直後の Mac での観測では、次の 2 秒刻みの観測までに消失）、5 分後の sweep でも同じことが繰り返されていました。別の process group で既に動いている server（手で `start` したものなど）には及ばないので、そういう server が動いているあいだは気づきません。key の有無だけを変えて、消失と生存を確認しました。

これは #44 とは別の不具合です。#44 は health 待ちが切れたときに controller 自身が runner を kill するもので、#46 は health が通ったあとでも launchd が process group を片付けるものです。上記の Mac では #46 を観測し、#44 は再現しませんでした。それ以前の別の Mac で起きた失敗の原因は、この観測だけでは確定しません。

- installer が書く launchd plist に `AbandonProcessGroup = true` を加えました（systemd 側の `KillMode=process` と同じ目的の設定）。既存の install は `install.sh` の再実行で plist が再生成されます

### `start` が、起動に時間のかかる Mail server を殺していました（#44）

controller が自分で runner を spawn する経路では、`agentstack-mailctl start` は health を **probe 150 回**待ち、切れると**自分が起動したばかりの runner を kill** していました。probe ごとに Python を起動するため、この窓は計測した Mac で port が閉じたまま約 48 秒です。起動にそれ以上かかる server は listen する直前に殺され、Mail は次の sweep（5 分後）まで存在しません。その間に起動した agent は Mail 不在のまま登録に失敗し、失敗メッセージが指す `agentstack-mail.log` には殺された runner は何も書いていません。遅い runner の fixture でこの kill は再現します。2026-09-16 の reboot 直後に login 時の `start` がこの失敗で終わった事象がありますが、その runner が何秒目で殺されたかは記録が無く、cold start が原因かどうかは未確定です。

- 待ちを 2 段に分け、どちらも**壁時計の期限**にしました（probe 回数は時間の上限にならない）。port が開くまで `AGENTSTACK_MAIL_START_GRACE`（既定 180 秒）、開いてから health が返るまで `AGENTSTACK_MAIL_HEALTH_GRACE`（既定 30 秒）。0 は probe 1 回
- grace を使い切っても**生きている runner は kill しません**。pidfile を残し、次の `start`（timer または operator）が同じ runner を見つけます。その `start` は port が閉じていれば待たずに報告して lock を手放し、port が開いていれば health grace だけ待ちます
- 失敗メッセージに**経過秒と port の状態**を出し、「起動中か stuck」と言います。runner が exit した場合はそう言います
- `status` も「port が閉じていて起動中か stuck」と「port は開いているが health が失敗」を区別します
- launchd が server を直接 supervise する経路は、controller が runner を kill しないので対象外です（health の待ちは同じ期限を使います）

### spawner 側の `codex` が `--help` に答えられないと、child の承認ポリシーが落ちていました（#45）

`spawn_child.sh` は `codex --help` の出力を見て `--ask-for-approval` か `--full-auto` を選び、どちらも無ければ何も付けません。help が**取れなかった**場合（npm wrapper が platform package の欠落で落ちる、別 shell で古い node が先に解決される、など）も同じ「何も付けない」に落ちていました。child 自身は login shell で正常に起動するので、設定（`AGENTSTACK_CODEX_CHILD_APPROVAL=never`）だけが抜けた child ができ、policy で切ったはずの承認要求が戻ってきます。

- `--help` が非 0 で終わるか出力が空なら probe 失敗として扱い、binary が無い場合と同じく `--ask-for-approval <policy>` を固定します。stderr に警告を出します
- help が正常に返り、どちらのフラグも無い場合だけ、従来どおり何も付けません

## 2026.09.16.3

### Claude 側の案内にも、同じ分岐を入れました

前の版（`2026.09.16.2`）で分岐させたのは Codex 向けの案内だけでした。**Claude 向けのテンプレートは手つかずのままで、proxy 経由の Claude エージェントは同じ壁に当たり続けます。** `file_reservation_paths` で予約し `renew_file_reservations` で更新せよ、という案内を読みますが、bound な schema にその名前はありません。

Claude 側のテンプレートにも同じ分岐を入れました。あわせて、**通常の委任 child が task をどこから受け取るのか**を明記しています。すでに持っている接続でその child 自身の inbox から取る、というのが既定です。起動時に完全な task を渡された場合（embedded / standalone）はそちらが優先で、その場合に起動儀式としての取得は命じません。

予約 hook 自体の契約は変えていません。**変えたのは hook の周りの案内であって、実際の強制ではありません。**

### 再登録の失敗を、秘密を出さずに分類します

`agentstack-reregister` の失敗が 1 つの不透明なメッセージにまとまっており、**サーバーに届いていないのか、token を拒否されたのか、応答が壊れているのかを区別できませんでした。** 区別できないと、次に取る手はたいてい「送った内容を出力してみる」になります。秘密が画面に出る経路を、こちらが用意していたことになります。

失敗に**段階と数値コード**が付きました。成功したときは静かなままです。

- ensure の成功判定を、応答が空でないことではなく **project id が数値であること**に変えました
- policy が有効なときに不正な応答を受けても、**成功した実行の stderr に traceback が出ません**
- 出力へ向かう値は allowlist を通ります

## 2026.09.16.2

### 認証済みのセッションが、存在しない道具を要求されて止まっていた

ローカルの MCP proxy 経由で起動したエージェントは、接続の時点で認証が済んでいます。ところが起動時の案内文は、接続方式に関係なく「まず `agentstack-reregister` を実行しろ」と書いていました。必要のない helper と token ファイルを探させ、承認を 1 回よぶだけの手順です。

**予約の案内はさらに実害がありました。** 案内文は `macro_file_reservation_cycle` や `renew_file_reservations` を無条件で指定しますが、proxy が見せる schema にその名前はありません（`reserve_files` / `renew_reservations` / `release_reservations` で、呼び手と project は proxy が持つため引数に取りません）。案内どおりに動いたエージェントは、**ファイルを編集する直前に存在しない道具へ手を伸ばし、そこで止まります**。調整は fail-closed に設計してあるので、何も壊れていないのに黙って進まない、という形になります。

起動 prompt・managed の予約節・task 依頼の本文が、**接続方式で分岐する**ようになりました。

| 接続 | inbox | 予約 |
|---|---|---|
| proxy 経由 | 自分の inbox を直接読む。helper も token も不要 | `reserve_files` / `renew_reservations` / `release_reservations` |
| 直接 | 従来どおり | `macro_file_reservation_cycle` / `file_reservation_paths` / `renew_file_reservations` |

**proxy の不調は、直接接続へ切り替える理由にはしません。** それは認証を迂回することになるためです。inbox が task の正本であること、embedded / standalone の案内、登録・credential・予約の実処理は変わっていません。

直接接続の復旧手順にも 1 つ誤りがありました。「通知やセッション名から既存の名前を確定せよ」と書いた直後に、`AGENT_NAME` が空のままコマンドを実行していました。先に設定する、と明記しています。**新しい名前を生成することはありません。**

この版への更新に特別な手順は要りません。

```bash
git pull
./scripts/install.sh
```

## 2026.09.16.1

### 更新手順が、installer 自身の出力で止まっていた

`AGENTSTACK_MAIL_ENV` は installer が `env.sh` に書き出す値で、shell 起動時に読まれます。render のパスは source id と venv / endpoint / state の hash から決まるため、**前回の install が書いた値は次の upgrade の期待値と一致しません**。

その結果、docs に書いてある手順が、稼働中のサービスを引き継ぐ前に停止していました。

```
error: AGENTSTACK_MAIL_ENV must equal the native service env '…/renders/<id>/service.env'
```

**回避するには変数を手で外すしかなく、そのことはどこにも書かれていませんでした。**

継承した値が「**`env.sh` から読んだ literal の値と一致し、かつこの install の管理 render 配置にある**」ときだけ、set されていなかったものとして扱うようにしました。値が等しいことは誰が設定したかの証明にならないので、**upgrade をまたいで native path を固定したい場合は `AGENTSTACK_MAIL_SERVICE_ENV` を明示**してください。そちらが優先され、免除の対象になりません。管理 render 配置の外にあるパスは、従来どおり停止します。

値は**読むだけで、source しません**。source すると `env.sh` の中の他のコードが走り、途中で失敗したファイルでも値を返してしまうためです。

**`2026.09.16` を使っている場合は、この版へ更新してください。** 前の版は、この不具合により上書き更新そのものが通りません。変数を外して 1 度だけ実行すれば、この版に移れます。

```bash
git pull
env -u AGENTSTACK_MAIL_ENV ./scripts/install.sh
```

## 2026.09.16

> **この版は上書き更新に失敗します。** `AGENTSTACK_MAIL_ENV` の扱いに不具合があり、`2026.09.16.1` で修正しました。新規に入れる場合も新しい版を使ってください。

### Codex の会話を、推測ではなく記録で対応付ける

これまで telemetry は、Codex エージェントがどの会話ファイルを使っているかを**推測で特定**していました。時刻や作業ディレクトリが近いものを選ぶ方式で、複数のエージェントが同時に動いていると取り違えます。

正式な登録から作った起動の期待値と、Codex 自身が渡してくる session の情報を照合し、**一致したときだけ記録する**ようになりました。一致しない、または確認できない場合は、推測で埋めずに未確認のままにします。

**RESUME した直後に `? UNBOUND` と表示されるのは仕様です。** Codex CLI 0.154.0 では、会話を開いただけの状態では本人確認の情報が届かず、**最初に何か入力した時点で確定**します。それまでは「まだ確認できていない」を正しく表示しています。古い記録を今回の起動の結果として流用することはしません。

- DECK の NEW AGENT、`/delegate`、DECK の RESUME のいずれから起動しても同じ経路を通ります
- launcher を経由しない素の `codex` / `codex resume` は対象外です。対応付けが必要な復帰には RESUME を使ってください

### RESUME で子の設定が失われなくなった

DECK から RESUME した Codex の子が、起動時に選ばれた専用の設定を引き継がず、既定の設定で復帰していました。認証済みの ORRERY Mail の接続設定もそこにあるため、**復帰した子は親に報告できず、追加の指示も受け取れない状態**になっていました。画面に警告が 1 行出るだけで、外からは分かりません。

確認できる証拠（登録情報、専用の状態ファイル、所有者の資格情報）が揃ったときだけ、その設定を復元します。壊れている、一致しない、所有者が違う場合は、端末を開く前に停止します。

専用の設定を持たない子（以前の版で起動したものなど）は、これまでどおり既存の設定で復帰します。**その場合はそうと分かるよう、RESUME の結果とログに残します。**

### 子に渡す MCP を絞れる（`--codex-mcp`）

親が多くの MCP を持っていると、それが子にそのまま複製されます。文献を読むだけの子にもブラウザ操作の MCP が付いてくるため、待機しているだけの子が実メモリを使います。

`--codex-mcp orrery-only` を指定すると、shell とファイル操作、認証済みの ORRERY Mail、session の記録に必要なものだけを残し、他を無効にします。**既定は `inherit` で、従来の動作から変わりません。**

手元の macOS での実測（Codex CLI 0.154.0、待機のみの子、プロセス木全体の `phys_footprint` 合計）では、10 プロセス 658 MB が 5 プロセス 318 MB になりました。**環境と構成に依存する値**で、物理メモリがその分解放されることを保証するものではありません。

絞った子には、初回のプロンプトで「持っていないツール」と「必要なら親に相談すること」を伝えます。**測った範囲では、後から設定を有効に戻しても、その会話では呼び出せませんでした**（設定変更後、`/mcp` の実行後、同じ会話を新しいプロセスで再開した後、いずれも呼び出しを確認できていません）。詳細と限界は [docs/delegation.md](docs/delegation.md) にあります。

### エージェントの使用量表示

ヘッダーの使用量表示で、同じ観測時刻が複数箇所に重複していたのを 1 箇所にまとめました。provider ごとに違っていた表記も揃えています。
