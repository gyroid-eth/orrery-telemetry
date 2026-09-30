# Changelog

版は日付で付けます（`YYYY.MM.DD`）。互換性の約束ではなく、**いつの配布物か**を言えるようにするためのものです。

インストール済みの版は `GET /api/version`、または install root の `VERSION` で確認できます。同じ応答の `api` は日付とは別の、利用者（ORRERY cockpit など）との互換の世代で、利用者が頼る API を足す・意味を変えるときだけ上げ、その版の節に書きます（[docs/api.md](docs/api.md#get-apiversion)）。更新手順は [docs/install.md](docs/install.md) の Upgrade を参照してください。

**この記録は 2026.09.16 から始めます。** それ以前は `VERSION` が `0.9.0` のまま更新されておらず、版から中身を知ることができませんでした。過去 6 週間分を遡って記載することはせず、ここを新しい基点とします。以前の版を使っていた場合も、上書き更新の手順は変わりません。

---

## Unreleased

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
