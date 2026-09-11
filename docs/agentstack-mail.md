# ORRERY Mail extraction

> English version: [agentstack-mail.en.md](agentstack-mail.en.md)

ORRERY Mail は public installer の coordination service です。この
repository の中で論理的に隔離された package として開発されており、
versioned contract と独立した export/test gate が安定するまで、repository
からの extraction（切り出し）は先送りしています。

実装は `packages/agentstack_mail` で保守されています。その provenance
snapshot は runtime dependency ではなく、あくまで audit の入力として残ります。

既定の endpoint は `http://127.0.0.1:18765/mcp` で、data は
`~/.agentstack/mail` 以下に置かれます。legacy と current の service を、
同じ書き込み可能な database や archive に同時に向けることは、どの test や
migration でも許していません。

caller-derived の compatibility surface は
`packages/agentstack_mail/fixtures/compatibility-tools-v1.json` で
version 管理されています。その 25 個の tool は、実行可能な caller と
出荷済みの model-facing contract の union（和集合）に、cutover 以降に
surface が獲得したもの（同じ fixture の `post_cutover_published` を参照）を
足したものです。Permission deny entry、negative instruction、Codex
Bridge-local の operation は source-extraction の root にはなりません。

実装は次の順で進めました。

1. provenance、live tool schema、caller-derived tool contract を freeze する。
2. 隔離された configuration と、exact-schema な database copy/import gate を
   定義する。
3. identity、messaging/contact、receipt、reservation、notification の
   挙動を、live source に対する differential test 付きで port する。
4. machine-specific な notify daemon と tmux daemon を除いた HTTP と
   lifecycle の安定性を port する。
5. installer、doctor、bridge、hooks を、新しい endpoint、authentication、
   `orrery-mail` という MCP key へ一括で更新する。
6. 承認された authority switch の前に、coexistence、migration、rollback、
   fault、実機 soak の証跡を揃える。

最初の 4 つの gate は
[`packages/agentstack_mail/scripts/cutover_gates.py`](../packages/agentstack_mail/scripts/cutover_gates.py)
によって実行可能かつ hermetic です。automated contract 自体もこの gate
script の中にあります。実機 soak の手順と handoff runbook は一度限りの
event の議事メモであり、公開されていません。そこで記録された決定は
decision ledger fixture とその test によって強制され、drift は文書と
食い違うだけでなく test が落ちる形になっています。

provider identity と両方の client registration key は `orrery-mail` です。
install 時の migration は、同一 endpoint の entry を重複した authority を
作らずに置き換えるためだけに、legacy な client key を認識します。
authority は client から見える key に加えて、endpoint、data root、
ownership によって決まります。

## 現在の Core boundary

core の実装は、live の data/archive/tool-body の境界をそのまま renamed
package へ copy し、fail-closed な FastMCP subclass を通して contract が
名指す versioned tool だけを厳密に publish します。MCP resource と
compatibility 対象外の 16 個の tool は publish されません。それらの本体は、
differential な作業で pruning してもマクロや storage の依存を壊さないと
証明できるまで、internal only のままです。

roster resource を publish していないため、tool の description は caller に、
ORRERY Telemetry runtime が割り当てた identity か `register_agent`/
`macro_start_session` が返した identity を使うよう指示します。
`list_contacts` は既知の link を返し、`whois` は既知の identity を検証し、
broadcast の配信は roster の応答を必要としません。Tool filtering で
public surface を減らすことはできません。contract の tool を 1 つでも
取り除く profile は、server の構築が fail closed します。

すべての production 設定は `AGENTSTACK_MAIL_*` の namespace を使います。
新しい設定が何もない状態では、解決される port は `18765` で、database、
archive、signals は `~/.agentstack/mail` 以下です。legacy の unprefixed
変数と CWD の `.env` は無視されます。install された package は今や
`agentstack-mail` を公開し、loopback-only な既定 `http://127.0.0.1:18765/mcp`
で厳密な boundary を配信します。最初の entry point は、authentication を
実施しているふりをするのではなく、non-loopback な bind と bearer/JWT の
設定を拒否します。`agentstack-mail --help` は server を起動せずに exit し、
`--host`、`--port`、`--path` はその process 限りで namespaced な endpoint
設定を上書きします。Identity mode の挙動は frozen source と互換のままです。
既定の `coerce` は非 canonical な明示 request に対して生成した name を
返すことがあり、不正な mode は `coerce` に fallback します。したがって
cutover profile は、固定された runtime identity のために
`AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE=passthrough` を設定する必要が
あり、legacy の unprefixed key は意図的に隔離されたままです。Claude Code の
registration hook は、成功を記録する前に、明示された各 request を返された
name と比較します。不一致や読み取れない応答は非 0 で exit します。これは
transaction の rollback ではなく caller 側の拒否であり、tool call の後に
実行されるため、置き換わった server row が残る場合があり、既存の
`AGENT_NAME` を持つ session は success-flag guard によって一律に止められる
わけではありません。Codex には相当する PostToolUse hook がありません。
reserved な bootstrap と reregister の path はすでに不一致で止まりますが、
direct spawn、raw MCP call、Codex App の再認証は、cutover に必須の設定を
代替するものではなく、今後の課題として残っています。
Service helper/controller と copy/verify/rollback-assess の migration
command は実装済みです。Installer は既定でこの provider を、固定された
`orrery-mail` の client key で provision します。

## Install と lifecycle

通常の installer は、bundled package を immutable な candidate virtual
environment へ provision し、namespaced な service environment を
render して、supervised-background な runner を起動します。runner は
crash した server を 5 秒後に再起動します。
`agentstack-mail-service foreground` は state-root の authority lock を
保持するため、restart が 2 つの writer を作ることはありません。薄い lifecycle controller が、
PID file、rendered-runner identity の厳密な照合、endpoint と database の
health check、短命な operation lock を追加します。

```bash
~/.agentstack/bin/agentstack-mailctl start
~/.agentstack/bin/agentstack-mailctl status
~/.agentstack/bin/agentstack-mailctl stop
~/.agentstack/bin/agentstack-mailctl restart
```

controller は意図的に、自前の 2 段目の supervision layer を追加しません。
command の異なる live な PID、owned PID file のない健全な endpoint、
別の database を報告する endpoint は、止められたり再利用されたりせず、
拒否されます。

### reboot 後の再起動

runner は `nohup` で起動されるため reboot を生き延びません。そのため
installer は *supervising trigger* を登録します——macOS では launchd job
`org.agentstack.mail`、Linux では oneshot service と `.timer` の組で、
その仕事はただ 1 つ、login 時とその後 5 分おきに `agentstack-mailctl
start` を実行することです。これはインストールのたびに、installer が
すでに健全な server を見つけた場合を含めて登録されるため、既存の
setup に対して `install.sh` を再実行するだけで得られます。

unit が持つのは `HOME`、`AGENTSTACK_HOME`、`PATH` だけです。
`agentstack-mailctl` はそれ以外のすべてを `env.sh` から読むため、
trigger は operator が手で打つのと全く同じ command を実行し、
再 render された service env を自動的に取り込みます。これらの path を
unit に固めて焼き込んでしまうと、再インストール後も静かに以前の render を
起動し続けてしまいます。それぞれの起動は意図的に one-shot です
（`KeepAlive` false / `Type=oneshot`）。controller は server を `nohup`
に渡して終了するため、restart-always な unit だと server ではなく
*controller* をループで再起動してしまいます。繰り返しは launchd の
`StartInterval` と systemd の timer から来ます。systemd の unit は
`KillMode=process` も設定します。これがないと、既定の control-group
cleanup が、oneshot controller が終了した瞬間に起動したばかりの server を
kill してしまいます（WSL2 で確認）。`start` は冪等です——owned PID が
生きていて健全なら "already running" と報告して 0 で exit するため、
再実行のコストはなく、何もすることがなければ静かなままです。その出力は
`agentstack-mail-autostart.log`（launchd の `StandardOutPath`、systemd の
`StandardOutput=append:`）に書かれ、server 自体の log とは分かれています。

`agentstack-mailctl stop` は尊重されます。その意図を
`runtime/agentstack-mail.stopped` に記録し、sweep は明示的な `start` や
`restart` がその hold を解除するまで、意図的に止められた server を
そのままにします。この記録がなければ、trigger は次の発火時に operator の
stop を静かに取り消してしまいます——修正前に実測したところ、`stop` は
「ORRERY Mail stopped」と報告し、続く sweep は「ORRERY Mail started」と
報告しました。

launchd も systemd も使えない場合、installer は黙って skip するのでは
なく明示的にそう伝えます。autostart の欠落は、machine が実際に reboot
するまで見えないからです。これは仮定の話ではありません。2026-08-16、
maintainer の Mac で reboot した後、dashboard は動いているのに mail
server はなく、古い legacy service が port 8765 を握ったままでした——
その後に登録されたすべての agent は間違った database に書き込み、何も
error を報告しませんでした。

`agentstack-uninstall` は、install manifest に記録された他の service と
一緒に、この trigger（launchd/systemd の job と unit file の両方）を
削除します。

**この仕組みがカバーする範囲。** render された runner は、crash した
*server* を 5 秒後に再起動します。*runner 自体* が kill された場合は、
trigger が次の sweep で拾います（sweep が存在する前に実測: pidfile は
残り、port は閉じたまま、次の login まで何も再起動しませんでした）。
port が空いている stale な PID は自動的に回復しますが、port を
unhealthy な、または無関係な listener が握っている stale な PID は、
奪い合うのではなくメッセージ付きで拒否されます——sweep は再試行します
が、自分が所有していない listener を追い出すことはしません。即時の
回復は `agentstack-mailctl start` です。

### watcher も 1 つの service

tmux への配信は `hooks/watch_agent_mail_signals.sh` が行い、Mail server
とは別の長時間稼働 process です。2026-09-07 以降、installer はこれを
`org.agentstack.mail-watcher`（launchd、`KeepAlive`）または
`org.agentstack.mail-watcher.service`（systemd user unit、
`Restart=always`）として登録し、`~/.agentstack/runtime/mail-watcher.log`
へ log します。それ以前は `agent-start` と Codex bootstrap だけが、
detached な tmux session としてこれを起動していたため、agent がすべて
dashboard から spawn された host では signal が溜まる一方で何も配信され
ませんでした（`wsl --shutdown` 後の WSL2 で確認）。watcher は
single-instance lock を保持するため、`agent-start` の tmux fallback は
service がすでに動いている場合は手を引くようになりました。installer は
unit を登録する際、残っていた `mail-watcher` tmux session も退役させます。
`/api/mail-watcher-health` は `watcher_mode` として `launchd`、
`systemd-user`、`pidfile` のいずれかを報告します。

## upstream からの手動 migration

Migration は installer の step ではなく、operator が手で行う手続きです。
まず upstream の writer を静止させ、canonical な絶対 path で database、
archive、signals を特定します。移行先はまだ存在してはいけません。
repository checkout から、3 つの projection すべてを copy し、その後
verify します。

```bash
LEGACY_DB=/absolute/path/to/storage.sqlite3
LEGACY_ARCHIVE=/absolute/path/to/git_mailbox_repo
LEGACY_SIGNALS=/absolute/path/to/signals
DESTINATION="$HOME/.agentstack/mail"

uv run --project packages/agentstack_mail agentstack-mail-migrate copy \
  --source-db "$LEGACY_DB" \
  --source-archive "$LEGACY_ARCHIVE" \
  --source-signals "$LEGACY_SIGNALS" \
  --destination-root "$DESTINATION"

uv run --project packages/agentstack_mail agentstack-mail-migrate verify \
  --source-db "$LEGACY_DB" \
  --source-archive "$LEGACY_ARCHIVE" \
  --source-signals "$LEGACY_SIGNALS" \
  --destination-root "$DESTINATION"

./scripts/install.sh
```

この手順は 2026-08-12 の実際の切替で使われました。database と archive、
合計約 6 万件の record が、無事に copy され照合されました。source
snapshot が verifier の下で変化しないよう、copy と verification の間は
upstream の service を止めたままにしてください。

## Rollback

installer は external provider へ自動で切り戻すことはしません。ORRERY
Mail を停止し、migration と configuration の backup を使って、意図的に
手動で rollback してください。

## Notification layout の互換性

ORRERY Mail は、message ごとに 1 つの signal を
`signals/projects/<project>/agents/<agent>/<message-id>.signal` に
書き込みます。bundled の `hooks/watch_agent_mail_signals.sh` はこの
layout を再帰的に発見し、入れ子になった `message` の metadata を抽出し、
notification を注入し、正常に配信できた message 単位の signal だけを
削除します。Repository installer の regression test は、隔離された
signals/runtime root と fake tmux boundary の中で、まさにこの
producer-shaped な path を検証します。live の watcher や port には
一切触れません。

File-reservation の activity probe は upstream #240 の one-pathspec な
Git walk に収束させたうえで、process-global な concurrency limit 8、
probe ごとの 3 秒の deadline、status-pass 全体の 4 秒の budget を
追加しています。timeout した、失敗した、または不完全な filesystem/Git
probe は明示的に unknown な activity として扱われるため、stale な
auto-release を引き起こすことはできません。TTL の失効は変わりません。
package-local な performance gate は、57 個の具体的な tracked path を
5 回繰り返し、6 秒以下の median と、完全に一致・完了した run を最低 3 回
要求し、最大値は別に報告します。Fingerprint は可変な activity
timestamp を除外します。

## Archive commit の latency と startup 時の修復

archive を書く tool は、返る前に SQLite を確実に更新し、audit file を
書き込みます。それら file の Git commit は既定で非同期に queue される
ため、Git history の構築は request の latency には含まれません。
`AGENTSTACK_MAIL_ARCHIVE_COMMIT_ASYNC=false` を設定すると旧来の同期的な
挙動に戻せます。この kill switch は、deployment で queue や commit の
失敗が観測された場合に備えて残してあります。

trade-off は Git の projection に限られます。process や machine の
hard shutdown は、tool が返った後で commit を取り消してしまうことが
あります。database は commit されたままで、audit file は archive の
working tree に uncommitted な file として残ります。次に server が
起動したとき、既存の archive heal pass が stale な lock artifact を
除去し、untracked かつ modified な audit file を発見して、service が
work を受け付け始める前に同期的に commit します。recovery や
maintenance の失敗は log されますが、database や file を破棄することは
ありません。

Startup では `git gc --auto` もチェックしますが、`.git` 以下の marker に
よって最大でも 24 時間に 1 回にレート制限されます。この日次の上限に
より、毎回の restart で object-count のチェックすら払わずに済みます。
`--auto` が第 2 の gate を担うため、repack が走るのは Git 自身の
loose-object や pack のしきい値がそれを必要だと判断したときだけです。
Git が実際に maintenance を始めたときは、`gc.autoDetach=false` により、
完了と失敗が startup の heal pass から観測可能なままになります。

規模感として、2026-08-14 の Tier-1 計測は、開発用 MacBook Pro、Python
3.12.2、ephemeral な loopback server、空の scratch archive、3 回の
warmup 後の 25 回の計測、production 相当の tool log を有効にした状態で
取得しました。`commit_async=true` では、register/send/reservation の
p50 はそれぞれ 32/76/47 ms（p95 は 41/89/58 ms）でした。同じ machine・
同じ scratch-archive 形状で `commit_async=false` の場合は、217/310/259
ms（p95 252/344/271 ms）でした。これはその machine とその profile に
限った比較 data であり、普遍的な latency の約束ではありません。実行
可能な gate と記録済みの全設定は
[`bench/tier1_latency.py`](../bench/tier1_latency.py) と
[`bench/README.md`](../bench/README.md) にあります。

dirty patch は repository だけの audit 入力のままであり、それに付随して
いた Git bundle はもう配布されておらず、wheel と source distribution
からは除外されています。Distribution gate は、両方の artifact 種別が
runtime module、NOTICE、両方の license、versioned な fixture を確かに
含んでいることを検証します。

## Behavior differential gate

承認された Core base の full SHA は
`fixtures/differential-expected-divergences-v2.json` だけが所有し、
prose はそれを反映しません。Artifact verification は、packaged fixture
をその checkout の fixture と byte-match させ、その commit object が
永続的な local branch、remote-tracking branch、または tag から到達可能な
場合に限って base として受け入れます。CI は full history を fetch する
ため、shallow / unfetched な object と、存在はするが到達不能な object は
異なる失敗として区別されます。承認された base は review の anchor で
あって candidate ではありません。Local と push の lane は checkout された
まさにその `HEAD` を使い、pull-request の lane は、その lane が checkout
した synthetic merge の `HEAD` をそのまま使います。同じ full な
candidate SHA を、exact-checkout、`candidate-source-bound`、そして
candidate-bound なすべての evidence verifier に渡す必要があり、この
2 つの SHA が互いに取り違えられることはありません。Behavior test は、
operator が供給する場合、frozen された live baseline を Git bundle と
dirty patch から authenticate・再構築し、live と Core を別々の
subprocess として起動します。Worker environment が継承するのは OS の
bootstrap allowlist だけで、database、archive、signals、home、
temporary file、Git identity、port、import root は明示的に隔離されます。
Test の入出力は private で、symlink escape と source-origin drift は
fail closed し、developer の AgentMail checkout は一切参照されません。

順序付けられた scenario は次のとおりです。

1. identity、contact、messaging、topic/inbox、mark-read、acknowledgement
   replay、reply、full-text search、heuristic な thread summary。
2. Unicode reservation の idempotency/conflict/renew/release と、
   message 単位の signal、BCC の privacy。
3. health、start-session、reservation-cycle、contact-handshake、
   summary fetch、retirement の lifecycle。

これらの union は、contract が名指す versioned tool と正確に一致します。
それぞれの operation は call window を記録するため、300/900/604800 秒の
TTL の挙動を、不安定な wall-clock 推定なしに検証できます。oracle は、
公開される structured/text projection、SQLite の integrity と foreign
key、schema identity、relational ID、Git の fsck と cleanliness、
archive の filename/frontmatter/copy/thread derivation、signal の
recipient、token の非開示、receipt の idempotency を、絶対 clock 値を
正規化する前に検証します。Timestamp の正規化は、すべての timestamp を
1 つの wildcard に置き換えるのではなく、時系列順と等価類を保存します。

versioned な divergence manifest は wheel と sdist に packaging され、
live fixture と Core source に対して検証されます。許容するのは、
live 40/0/21/0 対 Core 25/0/0/0 という exact な tool/concrete
resource/resource template/prompt の publication surface、renamed と
isolation の既定値、provenance と lazy-LLM boundary、3 件の
roster-resource description の書き換えだけです。manifest の単一の
`product_decisions` array が normative な decision ledger です。各
entry は selection、implementation、cutover の各状態を独立に記録する
ため、selected な design が implemented や cutover-approved な挙動と
取り違えられることはありません。この文書は entry の scope を意図的に
重複記載しません。unselected な entry と selected だが unimplemented な
entry は `comparator_disposition: fail` のままです。実装済みの
selection は allowance ではなく、選択された挙動を必ず assert しなければ
なりません。現在の ledger は、承認済みの authority-cutover の選択を
`go` として記録しています。別 scope の post-cutover の follow-up が
`no_go` のままであっても、その承認を覆すことはありません。

## Decision material

- normative な選択は decision manifest fixture にあり、
  `test_decision_manifest.py` によって pin されています。それに付随した
  evidence packet は cutover 当時の資料であり、公開されていません。
- [Claim/enrollment design](agentstack-mail-claim-enrollment-design.md)
  は、credential 発行、legacy な null-token の ownership 証明、
  recovery、macro との統合、migration、rollback への影響を扱います。
  normative な選択は manifest ledger に留まります。
- [Performance gate design](agentstack-mail-performance-gate.md) は、
  timing-normalization の blind spot を埋めるために必要な、独立した
  measurement boundary を規定します。これは design であって、実装済みの
  budget や release gate ではありません。
