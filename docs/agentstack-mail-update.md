# 稼働中の ORRERY Mail service を更新する

> English version: [agentstack-mail-update.en.md](agentstack-mail-update.en.md)

third-party server からの一度限りの authority handoff は過去の出来事であり、その
runbook は公開されていません。この文書が扱うのは、それ以降のすべての deployment
です。つまり、すでに production traffic を捌いている machine へ `agentstack_mail`
の新しい build を出荷することです。

## 原則

- **Candidate は immutable。** 稼働中の job が使う venv へ install・upgrade・edit
  してはいけません。commit を正確な名前で示した新しい venv
  （`final-candidate-<sha>/venv`）を build し、検証してから launchd をそちらへ
  切り替えます。前の candidate は disk 上に手を付けずに残す——それが rollback
  そのものです。
- **deployment ごとに render は 1 つ。** ある deployment の plist、env file、
  ownership manifest は 1 つの render directory にまとめて置き、`start` の後は
  一切編集しません。新しい deployment には新しい render directory が要ります。
- **database は shared state であり、candidate の一部ではない。**
  `--state-root` は deployment をまたいで固定です。`ensure_schema` が schema を
  変えてしまう build には専用の migration plan が要り、ここでは扱いません。
- **切り替える前に検証する。切り替えた後ではない。** production に影響してよい
  step は stop/start の対だけです。それより前はすべて scratch port と scratch
  database に対して実行します。

## 手順

定義: `MAINT=~/.agentstack/cutover-maintenance`、`SHA` = deploy する正確な
commit、`NEW=$MAINT/final-candidate-$SHA`。

1. **正確な commit から candidate を build する。**

   ```bash
   git -C <repo> rev-parse HEAD          # must equal $SHA; a dirty tree disqualifies
   python3 -m venv "$NEW/venv"
   "$NEW/venv/bin/pip" install <repo>/packages/agentstack_mail
   ```

2. **その commit で test suite を gate する**（dev venv。`CONTRIBUTING.md`
   参照）: `PYTHONPATH=. .venv/bin/python -m pytest -q` が、汚れのない単発の
   run で green であること。

3. **candidate を offline で検証する。** 稼働中の env file を copy し、port
   だけを変え、database は scratch copy を指すようにして、candidate を
   foreground で走らせます。production が必要とするものを probe します——
   最低限、canonical path と**各 alias**（`/api/`、`/mcp`）への `health_check`
   `tools/call`、それに代表的な read を 1 つ（`whois`）。

4. **新しい deployment を render する**（新しい directory、稼働中の env 値）:

   ```bash
   "$NEW/venv/bin/agentstack-mail-service" render \
     --output-dir "$MAINT/deploy-$SHA-$(date +%Y%m%d%H%M)/render" \
     --service-executable "$NEW/venv/bin/agentstack-mail-service" \
     --server-executable  "$NEW/venv/bin/agentstack-mail" \
     --env-file  <live env file copied into the new render dir> \
     --state-root ~/.agentstack/mail \
     --label org.orrery.mail
   ```

   `AGENTSTACK_MAIL_LEGACY_LAUNCHD_*` の key は残してください。`service_start`
   の同一 port に対する production guard がまだそれらを要求します。

5. **切り替える。** ここが outage window です（分ではなく秒単位）:

   ```bash
   "$NEW/venv/bin/agentstack-mail-service" stop  --ownership-manifest <OLD render>/org.orrery.mail.ownership.json
   "$NEW/venv/bin/agentstack-mail-service" start --ownership-manifest <NEW render>/org.orrery.mail.ownership.json
   ```

6. **mail を再起動させる仕組みを新しい deployment へ向ける。** login を越えて
   service を稼働させ続ける machine には、たいてい supervisor がいます——ここ
   では、endpoint が応答しないときに 5 分おきに mail を起動する launchd job
   です。それが自分自身の candidate と manifest を持っている場合、
   `stop`/`start` だけでは deployment が終わっていません。次に何らかの理由で
   mail が静かになったとき、その supervisor は*古い* build を呼び戻し、
   endpoint が再び応答するために rollback は誰にも気づかれません。

   これは仮定の話ではありません。`72b76aa` の 2026-08-25 の deployment は、まさに
   このパスによって 2026-08-26 14:37 に undo され、2 日後に無関係な bug report
   が来るまで誰も気づきませんでした。

   supervisor の target は、deployment が更新する 1 か所にまとめておきます——
   script に焼き込んだ path ではなく、supervisor が読む pointer file にします:

   ```bash
   cat > ~/.agentstack/mail/runtime/current-deployment.env <<EOF
   ORRERY_MAIL_SERVICE=$NEW/venv/bin/agentstack-mail-service
   ORRERY_MAIL_MANIFEST=$MAINT/deploy-.../render/org.orrery.mail.ownership.json
   EOF
   ```

   そのうえで、立ち去る前に supervisor が新しい path を解決していることを
   確認します。

7. **production を確かめてから完了と言う。** `launchctl print` の state が
   goal ではありません。step 3 の probe を実際の port に対して繰り返し、
   配信されている database file が production のものであることを確認します
   （`health_check` が `database_url` を報告します）。probe、`$SHA`、両方の
   render path を deployment note に記録します。

## Rollback

前の render の ownership manifest で再び `start` します（その candidate venv
は一度も触られていません）。Rollback は引数を入れ替えて step 5 を繰り返す
だけです——update によって古い render も古い candidate も削除されないのは
そのためです。supervisor も同様に元へ戻します（step 6）。さもないと次の
restart で rollback が取り消されます。

## 実際にどの build が動いているかを確認する

自分が行った deployment と、その port が応答している build は別の主張です。
自分のメモではなく port に聞きます:

```bash
pid=$(lsof -nP -iTCP:<port> -sTCP:LISTEN | awk 'NR>1{print $2}')
ps -o command= -p "$pid"          # which candidate's python is this
```

launchd が報告する pid は wrapper であって server ではない点に注意してください。
server はその子であり、file descriptor を保持して request に答えているのはその
子です。wrapper を測って「修正が反映された」と結論づけるのは、ここですでに
一度起きた間違いです。

## cutover receipt との関係

cutover receipt は 2026-08 の cutover を行った candidate を pin しています。
より新しい candidate を deploy しても、その履歴は書き換わりません——receipt は
出来事を記述するのであって、現在稼働中の build を記述するものではありません。
receipt を orphan にしてしまうのは、pin された candidate をその場で編集する
ことであり、この手順はそれを禁じています。
