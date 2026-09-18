# 稼働中の ORRERY Mail service を更新する

> English version: [agentstack-mail-update.en.md](agentstack-mail-update.en.md)

この文書は、すでに traffic を捌いている machine へ `agentstack_mail` の新しい
build を出荷する手順です。同梱 service が唯一の provider になって以降
`scripts/install.sh` が管理している配置を前提にしています。それ以前に
`cutover-maintenance/` の下で手作業していた配置は過去のものです。installer が
書くものはそれを読みませんが、かつてそれを使った machine には unit や pointer
file が残っていることがあるので、下の事前確認は「消えているはず」と仮定せず
探します。

前進手順（手順 1〜7）は 2026.09.17.1 の build を出荷するときに稼働中の machine で
1 回実行し、その結果を該当箇所に引用しています。Rollback 節は installer の
source から読んだもので、**未実行**です。その旨を明記しています。

## installer がすること・しないこと

`install.sh` は毎回 hooks・skills・dashboard・`bin/`・`env.sh`・autostart unit を
更新します。ORRERY Mail については次のどちらか一方の経路を取ります。

- **設定された endpoint に何かが応答している。** installer はそれに
  `health_check` を送ります。答えに `database_url` があり、それが期待する state
  database に解決されれば listener を採用します。その render を `env.sh` に記録し、
  service には**触りません**。port が占有されているのに ORRERY Mail として答え
  ない、あるいは別の database を配信している場合は error で止まります。この
  採用経路が通常の再実行のすべてで、だから再実行だけでは Mail の build は決して
  切り替わりません。dashboard の `/api/version` は package の版であって、port の
  裏にある build ではありません。
- **何も応答していない。** installer は checkout の正確な commit で candidate を
  用意し（完全なものがあれば再利用）、service env を render し、
  `agentstack-mailctl` で起動し、`env.sh` と autostart unit をその render に向け、
  配信される database が共有のものであることを確かめます。

したがって更新とは「古い service を止めてから installer を走らせる」ことで、
その間に autostart unit が古い build を起こさないよう unit を押さえておきます。

## 配置

これらは installed の `env.sh` から解決します。仮定で決めないでください。state
root の既定は install dir と独立に `~/.agentstack/mail` で、endpoint・label
prefix・各 root はどれも設定可能です。

| `env.sh` の変数 | 意味 |
| --- | --- |
| `AGENTSTACK_MAIL_DIR`（service root） | `candidates/<commit>/venv`: 不変の candidate。正確な commit ごとに 1 つの venv で、`packages/agentstack_mail` から `uv` で build する。`renders/<commit>-<id>/service.env` と `run-agentstack-mail.sh`: (commit, venv, endpoint, state root) の組ごとに 1 つの render。id はちょうどその入力の hash なので、同じ入力は同じ directory を指し、installer は既存の render を書き換えることを拒否する。`runtime/agentstack-mail.pid`（2 行: runner の pid と runner の path）と `runtime/agentstack-mail.log` |
| `AGENTSTACK_MAIL_STATE_ROOT`（state root） | `storage.sqlite3`（WAL mode）、`archive/`、`signals/`。共有 state で、どの candidate にも属さず、deployment をまたいで固定 |
| `AGENTSTACK_MAIL_ENV` | controller と autostart unit が起動する render |
| `AGENTSTACK_MCP_URL` | endpoint。その port を見る |
| `AGENTSTACK_PYTHON` | candidate を build する interpreter |

autostart unit は `<label prefix>.mail`（`AGENTSTACK_LABEL_PREFIX` 未設定なら
`org.agentstack.mail`）です。macOS では 300 秒ごとの launchd job、systemd では同名
の `.timer` で、`agentstack-mailctl start` を実行し、それは `env.sh` を読みます。
installer が管理する範囲に第二の supervisor はありません。旧配置の残骸は事前
確認で探します。

## 原則

- **candidate は不変。** 新しい build は新しい `candidates/<commit>` directory。
  既存の candidate に `uv venv` や `pip install` を再実行しない。前のものは disk に
  手つかずで残り、それが rollback です。
- **database は共有 state。** 起動時に schema を変える build には専用の
  migration plan が要り、ここでは扱いません。始める前に
  `packages/agentstack_mail/src` の tree diff に DDL が無いことを見ます。
- **切り替える前に検証する。後ではない。** 停止より前のことはすべて、隔離した
  process 環境で、scratch port と database の一貫した snapshot に対して行います。
- **どの build が応答しているかは port に聞く。** `launchctl print` でも pidfile
  でも自分のメモでも package の版でもなく。

## 手順

installed の値を一度読みます。`env.sh` は `shlex.quote` で書かれているので、
右辺を `sed` で抜くと quote が残ります。installer 自身と同じく shell に unquote
させます。

```bash
INSTALL=~/.agentstack                      # あるいはあなたの --install-dir
eval "$(
  set +u; . "$INSTALL/env.sh" >/dev/null 2>&1
  for k in AGENTSTACK_PYTHON AGENTSTACK_MCP_URL AGENTSTACK_MAIL_DIR \
           AGENTSTACK_MAIL_STATE_ROOT AGENTSTACK_MAIL_ENV AGENTSTACK_LABEL_PREFIX; do
    eval "v=\${$k:-}"; printf '%s=%q\n' "$k" "$v"
  done
)"
PY=$AGENTSTACK_PYTHON; SVC=$AGENTSTACK_MAIL_DIR; STATE=$AGENTSTACK_MAIL_STATE_ROOT
OLD_ENV=$AGENTSTACK_MAIL_ENV; LABEL="${AGENTSTACK_LABEL_PREFIX:-org.agentstack}.mail"
PORT=$("$PY" -c 'import sys,urllib.parse;print(urllib.parse.urlparse(sys.argv[1]).port)' "$AGENTSTACK_MCP_URL")
REPO=<deploy する commit の汚れのない checkout>; SHA=$(git -C "$REPO" rev-parse HEAD)
```

1. **その commit で test suite を gate する**（dev venv、`CONTRIBUTING.md`
   参照）: `PYTHONPATH=. .venv/bin/python -m pytest -q` が汚れのない単発の run で
   green、かつ `packages/agentstack_mail/tests` も green。

2. **candidate を先に build する。** ただし既に完全なものがあれば触りません。
   これで outage に `pip install` が入りません。installer はこの path に完全な
   candidate があれば再利用し、不完全なものは拒否します。

   ```bash
   V="$SVC/candidates/$SHA/venv"
   if [ -x "$V/bin/agentstack-mail" ] && [ -x "$V/bin/agentstack-mail-service" ] && [ -x "$V/bin/agentstack-mail-migrate" ]; then
     echo "candidate complete; not touching it"
   elif [ -e "$V" ]; then
     echo "candidate exists but is incomplete; stop and investigate" >&2; false
   else
     [ -z "$(git -C "$REPO" status --porcelain -- packages/agentstack_mail)" ] || { echo "dirty package" >&2; false; }
     uv venv --python "$PY" "$V"
     uv pip install --python "$V/bin/python" "$REPO/packages/agentstack_mail"
   fi
   ```

3. **candidate を隔離した環境で offline 検証する。** server は python-decouple で
   設定を読み、これは env file より process 環境を優先します。`env.sh` を
   source 済みの shell（あるいは `AGENTSTACK_MAIL_*` が 1 つでも入った環境）から
   起動すると scratch server が live の database を向きます。空の環境で起動
   します。database の snapshot は `cp` ではなく SQLite の backup API で取ります。
   live file は WAL mode で、main file だけの copy は commit 済みの page を欠く
   ことがあります。snapshot は credential を含むので private に保ち、終わったら
   削除します。

   ```bash
   S=$(mktemp -d); chmod 700 "$S"; mkdir -p "$S/state/archive" "$S/state/signals"
   "$PY" - "$STATE/storage.sqlite3" "$S/state/storage.sqlite3" <<'PY'
   import sqlite3, sys
   src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True); dst = sqlite3.connect(sys.argv[2])
   src.backup(dst); dst.close(); src.close()
   PY
   SPORT=18799; [ -z "$(lsof -nP -iTCP:$SPORT -sTCP:LISTEN)" ] || { echo "scratch port busy" >&2; false; }
   sed -e "s#^AGENTSTACK_MAIL_HTTP_PORT=.*#AGENTSTACK_MAIL_HTTP_PORT=$SPORT#" \
       -e "s#^AGENTSTACK_MAIL_DATABASE_URL=.*#AGENTSTACK_MAIL_DATABASE_URL=sqlite+aiosqlite:///$S/state/storage.sqlite3#" \
       -e "s#^AGENTSTACK_MAIL_STORAGE_ROOT=.*#AGENTSTACK_MAIL_STORAGE_ROOT=$S/state/archive#" \
       -e "s#^AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR=.*#AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR=$S/state/signals#" \
       "$OLD_ENV" > "$S/service.env"
   grep -E '^AGENTSTACK_MAIL_HTTP_HOST=' "$S/service.env"     # loopback address であること
   env -i HOME="$HOME" PATH=/usr/bin:/bin AGENTSTACK_MAIL_ENV_FILE="$S/service.env" \
     "$V/bin/agentstack-mail" > "$S/server.log" 2>&1 &
   SPID=$!
   ```

   probe は下の helper で行います。公開されている各 path（既定は `/mcp` と
   `/api`。確実には render の `AGENTSTACK_MAIL_HTTP_PATH` と `_PATH_ALIASES` を
   読む）に対して scratch endpoint を叩きます。

   ```bash
   probe() {  # probe <url> <tool> '<json arguments>'  → 結果の text を出力
     "$PY" - "$1" "$2" "$3" <<'PY'
   import json, sys, urllib.request
   url, tool, args = sys.argv[1:]
   body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": tool, "arguments": json.loads(args)}}
   req = urllib.request.Request(url, data=json.dumps(body).encode(),
         headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
   print(urllib.request.urlopen(req, timeout=15).read().decode())
   PY
   }
   probe "http://127.0.0.1:$SPORT/mcp" health_check '{}'   # database_url が $S/state/storage.sqlite3 を指すこと
   probe "http://127.0.0.1:$SPORT/api" health_check '{}'   # alias でも同じ答え
   probe "http://127.0.0.1:$SPORT/api" whois '{"project_key": "<live database にある project key>", "agent_name": "<そこにいる agent>"}'
   probe "http://127.0.0.1:$SPORT/api" health_check '{"nonce": "canary-value"}'   # isError true、text は件数だけで "canary-value" を含まない
   grep -c canary-value "$S/server.log"                    # 0
   kill "$SPID"; wait "$SPID" 2>/dev/null; rm -rf "$S"
   ```

   成功とは次のすべてです。両 path が scratch の `database_url` で `health_check`
   に答える。read が record を返す。拒否された呼び出しが、応答にも scratch log
   にも呼び手由来の値を含まない。1 つでも外れたら candidate は準備できておらず、
   手順はここで止まります。

4. **現在の deployment を記録し、installer を dry-run する。** すべて動いている
   うちに行います。dry run は既存の service を再利用すると言わなければなりません。
   それ以外を言うなら live の状態はあなたの認識と違い、切替は待ちです。

   ```bash
   pid=$(lsof -nP -iTCP:$PORT -sTCP:LISTEN | awk 'NR>1{print $2}')
   ps -o command= -p "$pid"                                  # 旧 candidate の python。この行を控える
   echo "$OLD_ENV"                                           # 旧 render。この行を控える
   ( cd "$REPO" && bash scripts/install.sh --scoped --dry-run ) | grep -E "ORRERY Mail"
   ```

5. **autostart unit を押さえる。** unit は `env.sh` が指すものを起動し、installer が
   `env.sh` を書き換えるまでそれは古い render です。あなたの停止と installer の
   起動の間に発火すると古い build が戻り、installer はそれを採用します。

   macOS:

   ```bash
   launchctl bootout "gui/$(id -u)/$LABEL"
   launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 && echo "still loaded" >&2
   ```

   systemd:

   ```bash
   systemctl --user stop "$LABEL.timer"
   systemctl --user is-active "$LABEL.timer" && echo "still active" >&2
   ```

   続ける前に旧配置の残骸も探します。`agentstack-mail-service`、
   `cutover-maintenance`、`current-deployment.env` を名指しする他の user unit や
   cron entry があれば、先に無効化します。

6. **止めて、install する。** ここが outage window です。candidate を先に build した
   状態で 1 回測った値は、停止から ready まで 12 秒でした。`agentstack-mailctl stop`
   は自分が記録した managed runner に signal を送り、endpoint が答えなくなるまで
   待ちます。自分が起動していない process は止めません。

   ```bash
   "$INSTALL/bin/agentstack-mailctl" stop
   lsof -nP -iTCP:$PORT -sTCP:LISTEN                         # 何も出ないこと
   ( cd "$REPO" && bash scripts/install.sh --scoped )        # あなたの install が使う flag を添えて
   ```

   installer の出力に、この順で期待する行:
   `no native listener found`、
   `reuse immutable ORRERY Mail candidate venv .../candidates/$SHA/venv`、
   `render namespaced ORRERY Mail service env .../renders/$SHA-<id>/service.env`、
   `ORRERY Mail started (pid ..., ready after Ns)`、
   `ORRERY Mail will restart at login`。代わりに
   `existing ORRERY Mail listener detected` が出たら、窓の間に何かが古い build を
   起動して installer がそれを採用したということで、deployment は**起きていません**。
   何が起動したかを突き止め、もう一度止めて、この手順をやり直します。

   前の `env.sh` を source 済みの shell で構いません。installer が inherited の
   `AGENTSTACK_MAIL_ENV` を許すのは、installed の `env.sh` の値と等しく、service
   root の `renders/` の下にあり、`AGENTSTACK_MAIL_SERVICE_ENV` が未設定のときだけ
   です。それ以外の値は run を止めます。

7. **production を確かめてから完了と言う。** すべての行が成り立たなければなり
   ません。1 つでも外れたら deployment は完了していません。

   ```bash
   pid=$(lsof -nP -iTCP:$PORT -sTCP:LISTEN | awk 'NR>1{print $2}')
   ps -o command= -p "$pid"                                  # .../candidates/$SHA/venv/bin/python
   ( set +u; . "$INSTALL/env.sh"; echo "$AGENTSTACK_MAIL_ENV" ) # .../renders/$SHA-<id>/service.env
   sed -n 2p "$SVC/runtime/agentstack-mail.pid"              # 同じ render の runner
   launchctl list | grep "$LABEL"                            # macOS: unit が再登録されている
   systemctl --user is-active "$LABEL.timer"                 # systemd
   "$INSTALL/bin/agentstack-mailctl" status
   "$INSTALL/bin/agentstack-selftest"
   probe "$AGENTSTACK_MCP_URL" health_check '{}'             # database_url = $STATE/storage.sqlite3
   ```

   手順 3 の probe を実際の endpoint に対して繰り返します。変更が引数の扱いや
   logging に触れたなら、拒否される呼び出しも含めます。`$SHA`、新旧の render
   path、outage、probe の結果を deployment note に記録します。

## Rollback

**installer の source から読んだ手順で、未実行です。** 頼る前に scratch な install
で検証する plan として扱ってください。

前の candidate と render は disk に残っています。rollback は前の commit で前進
手順を繰り返すことですが、違いが 2 つあります。第一に、
`AGENTSTACK_MAIL_CANDIDATE_ID` は candidate directory の名前を決めるだけです。
その directory が無いか不完全なら、installer は*現在の* checkout の package を
旧い名前の下に build してしまいます。だから止める前に旧 candidate が完全である
ことを確かめ、`AGENTSTACK_MAIL_SERVICE_VENV` で明示的に pin します。こうすると
installer は build する代わりに失敗します。第二に、installer が管理する他の
ものは走らせた checkout から来ます。

```bash
OLD_SHA=<前の commit>; OLD_V="$SVC/candidates/$OLD_SHA/venv"
ls "$OLD_V/bin/agentstack-mail" "$OLD_V/bin/agentstack-mail-service" "$OLD_V/bin/agentstack-mail-migrate"   # 3 つ全部。無ければここで止める
# 手順 5: autostart unit を上と同じように押さえる
"$INSTALL/bin/agentstack-mailctl" stop
lsof -nP -iTCP:$PORT -sTCP:LISTEN                                     # 何も出ない
( cd "$REPO" && AGENTSTACK_MAIL_CANDIDATE_ID="$OLD_SHA" AGENTSTACK_MAIL_SERVICE_VENV="$OLD_V" bash scripts/install.sh --scoped )
# 手順 7 を $SHA の代わりに $OLD_SHA で
```

unit は `env.sh` に従うので、別に更新する pointer はありません。

## 実際にどの build が動いているかを確認する

自分が行った deployment と、その port が応答している build は別の主張です。
port に聞きます。

```bash
pid=$(lsof -nP -iTCP:<port> -sTCP:LISTEN | awk 'NR>1{print $2}')
ps -o command= -p "$pid"          # which candidate's python is this
```

`agentstack-mail.pid` の pid は runner であって server ではありません。server は
その子で、descriptor を持って request に答えているのはその子です。runner を
測って「修正が反映された」と結論づけるのは、ここですでに一度起きた間違い
です。dashboard の `/api/version` を読むのも同じ間違いで、それは package の版
であり、Mail を切り替えたかどうかに関わらず再実行のたびに変わります。

## 経緯

同梱 service より前の deployment は `cutover-maintenance/` の下で
`agentstack-mail-service render/stop/start` を手で走らせ、pointer file を読む
supervisor を置いていました。その supervisor が一度（2026-08-26）古い build を
静かに戻したため、installer の配置は `env.sh` を読む supervisor 1 つになり、
手順 5 は残骸を探します。`cutover-maintenance/` の下の cutover receipt は 2026-08
の cutover を行った candidate を pin しています。receipt は出来事を記述するので
あって現在稼働中の build を記述するものではなく、この手順はそれを編集しません。
