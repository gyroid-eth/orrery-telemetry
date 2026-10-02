# ORRERY Mail の build 差し替え: 設計メモ

> English version: [agentstack-mail-update-design.en.md](agentstack-mail-update-design.en.md)
> 手順と挙動の正本は [agentstack-mail-update.md](agentstack-mail-update.md)。この文書は、なぜその形にしたかと、まだ決めていないこと（既定にするか）を残すためのものです。

## 起点（2026-09-30 時点）

- 再実行した installer は、endpoint に応答する ORRERY Mail があれば `health_check` で共有 database を確かめて**採用**し、build には触れません（`resolve_native_mail_connection` → `adopt_running_native_mail_render`）。build が切り替わるのは、何も応答していないときの provision 経路だけでした。
- そのため Mail の変更（例: 2026.09.30.4 の `register_agent(existing_agent_id=…)`）は、普通の update ではユーザーに届きません。手で「unit を押さえる → 止める → installer」を行う runbook だけが経路でした。
- 実害: 旧形式の Claude の子が `mail_schema_unsupported` になり、会話だけの resume に落ちる（HumbleLavoisier）。
- 差し替えに使える部品はそろっていました。candidate（`candidates/<commit>/venv`）と render（`renders/<commit>-<hash>/`）は不変で、前のものは disk に残る。`agentstack-mailctl stop` は自分が起動した runner だけを止め、stop marker で autostart の sweep を押さえ、`start` がそれを外す。`server_instance_id` は database の `mail_instances` にある。server は stateless HTTP。

## 設計

`--update-mail` は、採用経路で稼働中の deployment を特定したあとに、この checkout の candidate へ切り替えます。

1. 失敗しうる段階（candidate の build、scratch port での検証、database の backup）は、稼働中の Mail を止めずに済ませる。ここで失敗したら何も止めない（`not-switched`）。
2. 停止から start までが outage。新しい build の health が共有 database を返し、pidfile が新しい runner を指すことを確かめる。
3. 確かめられなければ、前の render をそのまま start する（`rolled-back`）。前の candidate・render・runner は不変なので、戻し先は「さっきまで動いていたもの」そのもの。
4. `env.sh`・autostart unit・`connections/local.json`・`install-state.json` は、切り替えの**後で**、実際に動いている方の deployment から書く。rollback したら前の deployment が記録される。これが「旧 manifest に戻す」の実体で、manifest を巻き戻す処理は持たない（書く前に決着させる）。
5. 中断（Ctrl-C・SIGTERM・HUP）も失敗と同じく決着させてから終わる。検証中なら scratch の server を止めて snapshot を消す（snapshot は全 message と token の複製なので、残すと露出になる）。stop から `env.sh` を書くまでの間なら、`env.sh` が新しい build を指していればそのまま、でなければ前の build に戻す。長い待ちは background に置いて `wait` で待つので、signal はすぐに処理される。

### 失わないもの

| 対象 | 扱い |
| --- | --- |
| 稼働中の接続 | stateless HTTP なので session は無い。outage 中の request は拒否され、再試行で戻る |
| token・credential | database と client 側 file にあり、どちらも触れない |
| enrollment の pin | `server_instance_id` は database にあり不変。`expected_server_instance_id` は保持し、`mail_env` だけ更新 |
| database | 同じ state root。起動時 DDL は追加のみ許可（検証で既存の table・column・index・trigger の消失と再定義、`quick_check` を見る）。切り替え直前に backup |
| archive・signals・管理 socket | 同じ path。検証用 server はすべて scratch を向く（本番 render と同じ関数で env を作る） |
| autostart | stop marker で切り替え中の sweep を押さえ、`start` が外す |

database は自動では戻しません。切り替え後に書かれた message を捨てることになるからです。backup は人が判断して使うためのものです。

## 選択肢: 既定にするか

| 案 | 挙動 | 利点 | 欠点 |
| --- | --- | --- | --- |
| A. 明示 opt-in（実装済み） | `--update-mail` を付けたときだけ差し替える。付けなければ `notice:` で差を示す | outage の時期を人が選ぶ。既存の再実行の意味が変わらない | 付け忘れると Mail の変更は届かないまま（ただし notice で見える） |
| B. 既定で差し替え | build が違えば差し替える。`--keep-mail` で抑止 | 何もしなくても届く | すべての再実行が outage を伴う。agent が多い時間帯でも止まる |
| C. 必要なときだけ既定 | 新しい build が「必要」を宣言した（例: 子が要求する Mail schema を持つ）ときだけ既定で差し替え | 届くべき変更は届き、不要な outage は無い | 「必要」の宣言と判定の仕組みが要る。まだ無い |

**推薦: 今は A。** live machine で `--update-mail` を数回通し、outage と rollback の実測がそろったら C へ進める。B は再実行のたびに全 agent へ outage を課すので勧めません。package の tree が同じ commit 間では、A でも差し替えも notice も起きません。

最終判断は maintainer が行います。


## 決定（2026-10-02）: 3 値を入れ、既定は keep のまま

`--keep-mail`（既定）、`--update-mail`（厳格）、`AGENTSTACK_MAIL_UPDATE=auto`（opt-in）の 3 値にし、結果を最後の `mail-result:` 行と `install.sh --print-mail-plan` の `mail-plan:` 行で機械が読めるようにしました（[agentstack-mail-update.md](agentstack-mail-update.md#--update-mail-による差し替え)）。setup.sh・update.sh はこれを読みます。

`auto` は、`agentstack-mailctl` 管理の配置で、検証が通り、止まる時間の見込み（検証で candidate が snapshot に応答するまでの秒数 + 1.5 秒）が `AGENTSTACK_MAIL_UPDATE_OUTAGE_BUDGET`（既定 8 秒）以内のときだけ差し替え、だめなら今の build を残して exit 0 で終わります。止まる時間の実測（2026-10-02、一時 HOME・別 port）: 停止は 1 秒未満、空の DB での起動は 1〜2 秒。stdio proxy（子）と top level の Codex は次の呼び出しで戻り（Codex は 71 秒でも）、top level の Claude Code（HTTP 直結）は 15 秒までなら自動で戻り、18 秒以上では `/mcp` から reconnect が要りました。

**`auto` を既定にする前に閉じること**（2026-10-02 の設計レビュー）:

1. **止まる時間の上限。** 見込みは成功時の値で、失敗時の長い停止を防ぎません。停止の開始からの deadline を決め、新しい build の起動を諦めて前の build に戻すまでを、その中に収めます（rollback と health の待ちも含める）。
2. **前の build に戻れること。** 新しい build が開いた後の database を前の build が読み書きできることを、snapshot の上で確かめます（既存の owner token・message・reservation）。確かめられない更新は `--update-mail` の明示操作に送ります。DDL が変わらないことは data が変わらないことの証明ではありません。
3. **古い update.sh。** 公開済みの cockpit の update.sh は install.sh を option なしで実行するので、既定を `auto` にした時点でその利用者は差し替えの対象になります。既定を変えるのは、update.sh と setup.sh が毎回明示の値を渡すようになってからにし、それでも古い update.sh では `auto` になることを CHANGELOG に書きます。
