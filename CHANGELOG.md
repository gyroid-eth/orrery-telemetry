# Changelog

版は日付で付けます（`YYYY.MM.DD`）。互換性の約束ではなく、**いつの配布物か**を言えるようにするためのものです。

インストール済みの版は `GET /api/version`、または install root の `VERSION` で確認できます。更新手順は [docs/install.md](docs/install.md) の Upgrade を参照してください。

**この記録は 2026.09.16 から始めます。** それ以前は `VERSION` が `0.9.0` のまま更新されておらず、版から中身を知ることができませんでした。過去 6 週間分を遡って記載することはせず、ここを新しい基点とします。以前の版を使っていた場合も、上書き更新の手順は変わりません。

---

## Unreleased

### 正常終了した Codex child を再開できませんでした（#59）

Codex child は正常終了時に owner credential と専用 home を削除していたため、履歴と provenance が残っていても同じ identity を再登録できず、dashboard の resume は `credential_missing` で止まっていました。正常 cleanup では remote retire と reservation release を維持したまま、schema version・`retired_at`・`resume_expires_at` 付き state と canonical credential を既定30日保持するようにしました。専用 home、proxy runtime、旧 MCP config は毎回削除し、resume 時に現在の source home と保存済み `codex_mcp_profile` から新しく作ります。credential 付き再登録と fresh binding expectation が成功した後、Codex exec の直前にだけ unretire します。保持期間は `AGENTSTACK_CHILD_RESUME_RETENTION_DAYS` で変更でき、`0` は従来どおり全削除です。明示 purge と期限切れ maintenance を追加し、doctor は削除せず期限切れ・purge 待ちだけを報告します。resume 後の receipt にも child provenance を引き継ぐため、cleanup を挟んだ2回目以降の resume も可能です。また、provenance gate が従来 resume できた `cx` 起動の top-level Codex まで child 扱いで拒否していたため、製品の top-level launch / receipt には `launch_origin: standalone` を記録し、private owner credential を検証したうえで child 専用 home・cleanup・unretire を使わない従来経路を維持します。実 Codex は resume の `SessionStart` を REPL 起動時ではなく最初の prompt 送信時に発火するため、prompt を送らず終了すると fresh receipt が無いまま旧 receipt も無効になり、次回以降を resume できませんでした。resume expectation は dashboard が選んだ session ID を旧 receipt と rollout header の両方で照合し、hook が未発火の間だけその receipt の nonce pair を fallback として保持します。別 session の hook、競合、startup、または別の fresh receipt が現れれば fail-closed で無効にします。SessionStart hook の1秒 deadline が recorder の途中で切れると、lock file だけ作られて launch transition と receipt が残らず、原因も観測できませんでした。deadline を5秒へ延ばし、lock・header・write・outcome の時間を session ID、path、nonce、credential を含まない runtime log へ記録するようにしました。

upgrade 前に起動した top-level Codex の receipt は origin 不明のため、次に製品 launcher から起動して `standalone` provenance を記録するまでは dashboard から resume できません。

### cleanup 済み Codex child と unmanaged session を区別できませんでした（#59）

正常終了時に child state と専用 home を削除すると、残った履歴だけでは製品が起動した child か、もともと管理外の Codex session かを判定できませんでした。Codex child の launch expectation と bound receipt に、秘密を含まない `launch_origin: child`、`codex_mcp_profile`、数値 agent ID、project、provider を保存し、cleanup 後も dashboard が child provenance を検証できるようにしました。既存の provenance 無し receipt は推測で child に昇格しません。

### resume できない終了済み agent に、resume 操作を案内していました（#59）

DECK と NETWORK は `gone` / `retired` という表示状態だけで resume 操作を出していたため、検証済み transcript、元の cwd、CLI、Codex の launch provenance・credential・設定が無い row も resume 可能に見えていました。backend が row ごとに固定理由コードの `resume_capability` を返すようにし、DECK card、NETWORK の一括選択、詳細 panel、`/api/jump` が同じ判定を使うようにしました。新しい製品 launch は `child` または `standalone` を receipt に記録し、provenance 導入前や製品外の origin 不明 row は推測で `ready` にせず、API から直接呼んでも terminal を開く前に拒否します。終了済み Claude row の表示判定が agent ごとに数千件の transcript を全読みして dashboard を止めていたため、表示では exact index または transcript directory mtime 付きの確定済み cache だけを使い、未検証 row は `verification_required` とするようにしました。この row は一括 resume には含めず、詳細 panel の `VERIFY & RESUME` を1回押すと `/api/jump` が full 検証し、成功時はその呼び出しのまま resume します。

### fresh install と CI が `sqlmodel 0.0.45` 以降で動かなくなっていました（#67）

ORRERY Mail は datetime を naive UTC で書き込んでいますが、依存に上限が無かったため、fresh venv は naive datetime を拒否する新しい `sqlmodel` を解決し、Mail の tool と installer が database write で失敗していました。隔離した同じ fixture は `0.0.44` で通り、`0.0.45` から失敗します。稼働中と同じ挙動へ戻す即応として `sqlmodel<0.0.45` に pin しました。timezone-aware datetime への移行と既存 database の naive 値との互換対応は別の修正で行います。

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
