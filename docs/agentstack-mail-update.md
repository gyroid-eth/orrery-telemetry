# 稼働中の ORRERY Mail service を更新する

> English version: [agentstack-mail-update.en.md](agentstack-mail-update.en.md)

この文書は、すでに traffic を捌いている machine へ `agentstack_mail` の新しい
build を出荷する手順です。同梱 service が唯一の provider になって以降
`scripts/install.sh` が管理している配置を前提にしています。それ以前に
`cutover-maintenance/` の下で手作業していた配置は過去のもので、installer が
書くものはどれもそれを参照しません。

## installer がすること・しないこと

`install.sh` は毎回 hooks・skills・dashboard・`bin/`・`env.sh`・autostart unit を
更新します。ORRERY Mail については次のどちらか一方だけを行います。

- **設定された endpoint に健康な listener が応答している** → その listener が
  使っている render を採用して `env.sh` に記録し、service には**触りません**。
  通常の再実行はすべてこちらで、つまり再実行だけでは Mail の build は決して
  切り替わりません。dashboard の `/api/version` は package の版であって、Mail の
  port の裏にある build ではありません。
- **何も応答していない** → checkout の正確な commit で candidate を用意し、新しい
  service env を render し、`agentstack-mailctl` で起動し、`env.sh` と autostart
  unit を新しい render に向け、配信される database が共有のものであることを
  確かめます。

したがって更新とは「古い service を止めてから installer を走らせる」ことです。

## 配置

すべて install dir（既定 `~/.agentstack`）の下です。

| Path | 意味 |
| --- | --- |
| `mail-service/candidates/<commit>/venv` | 不変の candidate。正確な commit ごとに 1 つの venv で、`packages/agentstack_mail` から `uv` で build する。作成後に install も編集もしない |
| `mail-service/renders/<commit>-<hash>/service.env`, `run-agentstack-mail.sh` | deployment ごとに 1 つの render。hash は venv・endpoint・state root から決まるので、新しい deployment は必ず新しい directory になる |
| `mail-service/runtime/agentstack-mail.pid` | 2 行。runner の pid と runner の path |
| `mail-service/runtime/agentstack-mail.log` | 稼働中 build の server log |
| `mail/storage.sqlite3`, `mail/archive`, `mail/signals` | 共有 state。どの candidate にも属さず、deployment をまたいで固定 |
| `env.sh` → `AGENTSTACK_MAIL_ENV` | controller と autostart unit がどの render を起動するか |

autostart unit（launchd では `org.agentstack.mail` が 300 秒ごと、systemd では
同じ label の `.timer`）は `agentstack-mailctl start` を実行し、それは `env.sh`
を読みます。自前の candidate を持つ第二の supervisor はありません。

## 原則

- **candidate は不変。** 新しい build は新しい `candidates/<commit>` directory。
  前のものは disk に手つかずで残り、それが rollback です。
- **database は共有 state。** 起動時に schema を変える build には専用の
  migration plan が要り、ここでは扱いません。始める前に
  `packages/agentstack_mail/src` の tree diff に DDL が無いことを見ます。
- **切り替える前に検証する。後ではない。** 停止より前のことはすべて scratch
  port と database の scratch copy に対して行います。
- **どの build が応答しているかは port に聞く。** `launchctl print` でも自分の
  メモでも package の版でもなく。

## 手順

`REPO` は deploy する正確な commit の汚れのない checkout、`SHA` はその commit、
`INSTALL` は install dir です。

1. **その commit で test suite を gate する**（dev venv、`CONTRIBUTING.md`
   参照）: `PYTHONPATH=. .venv/bin/python -m pytest -q` が汚れのない単発の run で
   green、かつ package 自身の suite `packages/agentstack_mail/tests` も green。

2. **candidate を先に build する。** outage に `pip install` を含めないためです。
   installer が行うのと同じ操作で、installer はこの path に完全な candidate が
   あればそれを再利用します。

   ```bash
   PY=$(sed -n 's/^export AGENTSTACK_PYTHON=//p' "$INSTALL/env.sh" | tr -d "'\"")
   V="$INSTALL/mail-service/candidates/$SHA/venv"
   git -C "$REPO" status --porcelain -- packages/agentstack_mail   # 空であること
   uv venv --python "$PY" "$V"
   uv pip install --python "$V/bin/python" "$REPO/packages/agentstack_mail"
   ls "$V/bin/agentstack-mail" "$V/bin/agentstack-mail-service" "$V/bin/agentstack-mail-migrate"
   ```

3. **candidate を offline で検証する。** 稼働中の `service.env` を copy し、port を
   変え、database・archive・signals を scratch path に向けて、candidate を
   foreground で走らせ、production が必要とするものを probe します。canonical
   path **と各 alias**（`/mcp`、`/api`）への `health_check` `tools/call`、代表的な
   read を 1 つ（`whois`）。変更が引数の扱いや logging に触れるなら、tool が
   拒否すべき request も送り、その拒否が応答にも scratch log にも呼び手由来の
   値を含まないことを確かめます。

   ```bash
   S=$(mktemp -d); mkdir -p "$S/state/archive" "$S/state/signals"
   cp "$INSTALL/mail/storage.sqlite3" "$S/state/"
   LIVE=$(sed -n 's/^export AGENTSTACK_MAIL_ENV=//p' "$INSTALL/env.sh")
   sed -e 's#^AGENTSTACK_MAIL_HTTP_PORT=.*#AGENTSTACK_MAIL_HTTP_PORT=18799#' \
       -e "s#^AGENTSTACK_MAIL_DATABASE_URL=.*#AGENTSTACK_MAIL_DATABASE_URL=sqlite+aiosqlite:///$S/state/storage.sqlite3#" \
       -e "s#^AGENTSTACK_MAIL_STORAGE_ROOT=.*#AGENTSTACK_MAIL_STORAGE_ROOT=$S/state/archive#" \
       -e "s#^AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR=.*#AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR=$S/state/signals#" \
       "$LIVE" > "$S/service.env"
   AGENTSTACK_MAIL_ENV_FILE="$S/service.env" "$V/bin/agentstack-mail" > "$S/server.log" 2>&1 &
   # ... http://127.0.0.1:18799/mcp と /api を probe してから kill %1
   ```

4. **autostart unit をどかす。** unit は `env.sh` が指すものを起動し、この窓の
   間それはまだ古い render です。あなたの停止と installer の起動の間に unit が
   発火すると古い build が戻り、installer はそれを採用してしまいます。

   ```bash
   launchctl bootout "gui/$(id -u)/org.agentstack.mail"      # macOS
   systemctl --user stop org.agentstack.mail.timer              # systemd
   ```

   installer は run の最後で unit を登録し直します。

5. **止めて、install する。** ここが outage window です。candidate を先に build
   した状態で、1 台で測った値は停止から ready まで 12 秒でした。

   ```bash
   "$INSTALL/bin/agentstack-mailctl" stop
   cd "$REPO" && bash scripts/install.sh --scoped     # あなたの install が使う flag を添えて
   ```

   installer の出力に期待する行: `no native listener found`、
   `reuse immutable ORRERY Mail candidate venv .../candidates/$SHA/venv`、
   `render namespaced ORRERY Mail service env .../renders/$SHA-<hash>/service.env`、
   `ORRERY Mail started (pid ..., ready after Ns)`、
   `ORRERY Mail will restart at login`。前の `env.sh` を source 済みの shell で
   構いません。installer は自分が書いた管理下の render path を認識し、新しい
   render を解決します。それ**以外**の `AGENTSTACK_MAIL_ENV` の値は意図的に run
   を止めます。

6. **production を確かめてから完了と言う。**

   ```bash
   pid=$(lsof -nP -iTCP:8765 -sTCP:LISTEN | awk 'NR>1{print $2}')
   ps -o command= -p "$pid"                      # .../candidates/$SHA/venv/bin/python
   sed -n 's/^export AGENTSTACK_MAIL_ENV=//p' "$INSTALL/env.sh"   # .../renders/$SHA-<hash>/service.env
   launchctl list | grep org.agentstack.mail     # unit が再登録されている
   "$INSTALL/bin/agentstack-mailctl" status
   "$INSTALL/bin/agentstack-selftest"
   ```

   手順 3 の probe を実際の port に対して繰り返します。`health_check` は
   `database_url` を報告し、それは共有 database でなければなりません。`$SHA`、
   新旧の render path、測った outage を deployment note に記録します。

## Rollback

前の candidate と render は disk に残っています。service を止め、前の commit を
candidate として pin して installer をもう一度走らせます。

```bash
"$INSTALL/bin/agentstack-mailctl" stop
cd "$REPO" && AGENTSTACK_MAIL_CANDIDATE_ID=<前の commit> bash scripts/install.sh --scoped
```

installer は `candidates/<前の commit>/venv` をそのまま再利用し、それ用の service
env を新しく render して起動し、`env.sh` と autostart unit を戻します。installer が
管理する他のもの（hooks・dashboard・`bin/`）は走らせた checkout から来るので、
Mail だけでなく install 全体を戻したいなら対応する commit を checkout してから
走らせます。手順 6 のコマンドで確認します。unit は `env.sh` に従うので、別に
更新する pointer はありません。

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
静かに戻したため、現在の配置は supervisor をちょうど 1 つにし、それが `env.sh`
を読む形になっています。`cutover-maintenance/` の下の cutover receipt は 2026-08
の cutover を行った candidate を pin しています。receipt は出来事を記述するので
あって現在稼働中の build を記述するものではなく、この手順はそれを編集しません。
