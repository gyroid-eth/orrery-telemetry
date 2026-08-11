# AgentStack Mail working-tree 切替手順（未実行）

> **ここを越えると戻れない:** C5 で最初の consumer の `register_agent` または別の write tool が新 endpoint に成功し、新 root が migration baseline から変わった瞬間。以後は旧 DB へ部分的に戻さず、新 authority 上で fix-forward する。

> **この間は全員が黙る:** C2 で旧 writer を止めてから、C5 の専用 test sender/recipient による1通の send/readとreservation guard実動確認が終わるまで、ProOpus、他の全 Claude/Codex parent・child、bot、watcher/hook、切替 operator は agent-mail を使わない。**2–4分はC3のdata copy/verificationだけの実測**であり、client restart/rebindを含む全静止時間は未測定である。

これは maintainer の Mac で後日、上から順に実行するための手順書である。この変更では切替を実行しない。稼働中の `~/mcp_agent_mail`、MCP 設定、launchd、service、port 8765 には触れていない。

## 切替承認の正本

本番切替の合格線は、この文書のチェック項目数ではなく、`packages/agentstack_mail/fixtures/differential-expected-divergences-v2.json` の `cutover_gate` である。exact manifest bytes、clean な exact candidate commit、condition definition、raw evidence のdigestを結び付け、`packages/agentstack_mail/tests/cutover_readiness.py` が **26条件すべてをexactly onceで再計算して `cutover_state: go` を返した場合だけ** C0へ進める。missing、duplicate、extra、unknown、手書きの`status: pass`、candidate不一致はいずれも`no_go`である。operatorは判定を書き換えず、同じcandidateとmanifestに束縛された出力であることを確認する。

次の表は26個のmachine gateをoperatorの確認順にgroup化したものである。IDと合格線は台帳が正本であり、表の説明は置換しない。現在の台帳ではpre-cutover follow-up taskが`not_implemented`のため、**現時点は明示的にNO-GO**である。

| 段階 | machine gate ID（台帳とexact match） | operatorが確認する意味 |
|---|---|---|
| 決定・candidate固定 | `product-decisions-selected`<br>`pre-cutover-product-decisions-implemented`<br>`initial-cutover-difference-set-exact`<br>`candidate-source-bound`<br>`product-decision-cutover-approval` | D1–D12、初回差異集合、clean candidate、maintainer承認が同じcandidateへ固定されている |
| behavior・build・release | `selected-behavior-release-gate`<br>`distribution-artifact-release-gate`<br>`provenance-regression-sync`<br>`full-repository-release-gate`<br>`installed-wheel-contract-release-gate` | 選択挙動、wheel/sdist、source provenance、exact CI、fresh installed wheelが同じcandidateで通っている |
| 診断・予約・性能 | `d2-d3-worker-progress-diagnostics`<br>`d2-d3-timeout-process-group-cleanup`<br>`d10-diagnostic-liveness-timeout`<br>`reservation-probe-safety-release-gate`<br>`reservation-performance-release-gate`<br>`full-performance-load-soak-matrix` | timeout診断、予約のfail-closed安全性、独立した予約性能とfull load/soakがraw evidenceから再計算されている |
| runtime・deploy・consumer | `http-cli-transport-entrypoints`<br>`service-lifecycle-supervision`<br>`installer-core-integration`<br>`mcp-client-reregistration-cutover`<br>`notification-layout-consumer-compatibility` | HTTP/CLI、単一writer supervision、install、全client切替、通知layoutと実consumerの互換性が通っている |
| authority移行・復旧 | `data-migration-reconciliation`<br>`rollback-revert-procedure`<br>`coexistence-fault-soak-gates` | data照合、段階別rollback、新旧隔離とfault rehearsalが一つのauthority遷移を証明している |
| 証跡・文書 | `cutover-evidence-provenance-gate`<br>`cutover-documentation-consistency` | 証跡のissuer/candidate bindingと、この手順書を含む文書整合が通っている |

C5の専用test sender/recipientによる5項目とreservation guardの4観測は、GO判定後にauthority switchが実際に機能したことを見る**post-switch operational smoke check**である。26条件の代替でも、切替承認を作る第二の合格線でもない。

evaluatorはread-onlyであり、`go`でもservice、config、authorityを自動変更しない。C0/C2/C5のhuman hold pointは実行を止められるが、`no_go`を承認で上書きできない。

## 現在の hard stop

今回の移送方針は **DB + signals + legacy archive の working tree を運び、legacy `.git` は運ばない**で固定する。検証回数は既存設計の6回を維持し、2回へ減らす最適化はしない。

ただし working-tree scope の migration command はまだ未実装である。現在の `agentstack-mail-migrate copy` は legacy `.git` まで複製するため、この手順の代用に使わない。

consumer設定用の `agentstack-mail-consumers` は実装済みである。明示inventoryから全before/after imageを先に作り、外部にpinするmanifest SHA-256、whole-set CAS、同一directoryのatomic replace、write-once terminal receipt、migration baselineを再検査する1操作rollbackを持つ。ただし複数directoryを跨ぐ真のatomic syscallではない。途中状態は `status=committed` にならず、C2でconsumerを止めたまま再実行またはrollbackする契約である。実機inventoryの確定、個人設定のpreview承認、下記のOrrery/dashboard前提条件が揃うまで C2へ進まない。

## 今回運ぶもの、運ばないもの

| 対象 | 扱い |
|---|---|
| SQLite | 約59 MiB。WAL の committed row を含む logical backup を作る |
| signals | 約404 KiB。全 file の content/mode を照合する |
| archive working tree | 約230 MiB、約49k files。現 Markdown、profile、reservation JSON、attachment を運ぶ |
| legacy `.git` | **運ばない。** 約1.3 GiB、43,380 commits、reflog、unreachable/deleted-file recovery を新 authority へ継承しない |
| runtime artifacts | `server.pid` は運ばない。`*.lock`、`*.lock.owner.json`、`.git/index.lock` が一つでもあれば copy を始めず失敗する |

SQLite の `messages.attachments` を read-only の `json_each` で再集計し、`type=file` の33 unique pathsが legacy working tree に **33/33実在**することを確認済みである。inline attachment は DB 内にある。したがって現 attachment read は working tree copy で維持できる。

失う通常挙動は、`whois(include_recent_commits=true)` が legacy commit history を返さなくなることだけである。新規 write 後の commit は新 repo に蓄積される。live 40 toolsのうち24を公開し、suppressed 16に含まれるGit-enriched resource/UI/time-travelは初回boundaryでは公開しない。

legacy DB、signals、working tree、`.git` は移動も削除も変更もしない。成功後も元の場所に cold copy として残し、旧 service を起動しないことで**運用上 read-only**にする。`chmod`、gc、reflog expire、archive cleanup は行わない。

## 裁定済み: baseline-commit-A

maintainerは **A（copied filesを1 baseline commitにする）** と裁定済みである。Bは比較時のrejected alternativeであり、実行時に再選択しない。

| 判定 | 新 repo の開始方法 | `whois` | 利点 / 不利 |
|---|---|---|---|
| **selected** | **A. copied files を1 baseline commitにする** | 切替直後は migration baseline 1件、その後は新規 commit | 約49k filesが全て追跡され、Git tree hashで current file set を説明できる。実測では `git add` + commit 約24秒、新 `.git` 約40 MiB |
| rejected history | B. 空 repo のまま開始する | 最初の新規 write まで空 | legacy filesは全て untrackedとなり、status/auditが恒常的に noisy。`.git` を明示的に先に初期化しないと runtime が `.gitattributes` の init commit を作る |

Aでもlegacy commit SHA、author/time series、reflogは新repoに持ち込まない。baseline-commit-Aの選択、exact tree、candidate、manifest、approver、timestampをmachine evidenceへbindしてからartifactを作る。

## 実測と maintenance window

2026-08-11 00:xx JST の read-only / `/private/tmp` rehearsal は次の通りだった。active writer 下の測定なので本番の quiesced rehearsal は別途必要だが、2–4分枠の根拠にはなる。

| 操作 | 実測 |
|---|---:|
| working tree copy（legacy `.git` と runtime artifacts を除外） | 19.14秒 |
| working tree content fingerprint | source 10.73秒、destination 10.76秒（warm cache） |
| working tree cold walk | 42.71秒 |
| DB + signals logical fingerprint | 1.93秒 |
| SQLite backup + destination logical verification | 2.03秒 |
| copied tree fsync | 3.60秒 |
| baseline `git add` + commit | 24.47秒 |

最初の live copy は動的 lock が走査中に消えて失敗した。これは静止を省略した場合に migration が成功しない、望ましい fail-closed の実証である。既存6 snapshot相当を working-tree scope で維持すると warm側で約100秒になり、cold walkと余裕を含む **2–4分**をC3 data copy/verification枠とする。全体の無通信はC2からC5 test合格まで維持し、短縮のために検証粒度を変えない。

## 実行前に固定する namespace

| 対象 | 旧 authority | 新 authority |
|---|---|---|
| service / port | `mcp_agent_mail` / `127.0.0.1:8765` | `agentstack_mail` / `127.0.0.1:18765` |
| SQLite | `~/mcp_agent_mail/storage.sqlite3` | `~/.agentstack/mail/storage.sqlite3` |
| archive | `~/.mcp_agent_mail_git_mailbox_repo` | `~/.agentstack/mail/archive` |
| signals | `~/.mcp_agent_mail/signals` | `~/.agentstack/mail/signals` |
| MCP key | `mcp-agent-mail` / Codex の `agent-mail` | `agentstack-mail` |
| launchd label | `com.operator.mcp-agent-mail` | `org.agentstack.mail` |

新 service の env は少なくとも次を固定する。

```dotenv
AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE=passthrough
AGENTSTACK_MAIL_HTTP_HOST=127.0.0.1
AGENTSTACK_MAIL_HTTP_PORT=18765
AGENTSTACK_MAIL_HTTP_PATH=/mcp
AGENTSTACK_MAIL_DATABASE_URL=sqlite+aiosqlite:////Users/operator/.agentstack/mail/storage.sqlite3
AGENTSTACK_MAIL_STORAGE_ROOT=/Users/operator/.agentstack/mail/archive
AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR=/Users/operator/.agentstack/mail/signals
```

旧 port/root/env key への fallback は持たせない。`ProOpus`、`AirSonnet`、`BiomatterBot`、`SeminarBot` は隔離環境で request name と response `name` の完全一致を確認してから使う。

## 切替前に完了させる consumer compatibility

Orrery と dashboard は C5 helper が検出して後回しにする対象ではない。maintainer が日常的に使う表示・操作面なので、次を別変更として実装・検証してから C2へ入る。この手順書では列挙だけを行い、実装もlive file変更もしていない。

| consumer | 切替前に直すこと | 直さず切り替えた場合 |
|---|---|---|
| Orrery | `~/.orrery/config.json` にabsoluteな`mail_db=/Users/operator/.agentstack/mail/storage.sqlite3`を必須にする実装と切替操作を用意し、Finder起動でも実画面が新DBのmessage/agent更新を読むことを確認する。selectorの欠落・relative path・旧値・誤値では旧DBへfallbackせず起動前にfail-closedする | 現在のconfigには`mail_db`がなく、Finder起動はshell envを継承しないため、Orreryのmail roster/railだけ旧DBの静止snapshotを読み続ける |
| dashboard DB | 実際にport 8770で動く`~/.claude/tools/agent-dashboard/server.py`と`graph_data.py`の固定`~/mcp_agent_mail/storage.sqlite3`を、`AGENTSTACK_MAIL_DB` / `AGENTSTACK_MCP_URL` / `AGENTSTACK_SIGNALS_DIR`の明示selectorへ置換する。欠落・旧値・誤値・新旧混在をsealed inventory照合で拒否し、欠落時はdashboard自身も起動しない | 一覧・graph・timeline・telemetryが旧authorityの静止snapshotを表示し続ける |
| dashboard retire | 旧 `POST /mail/api/retire-agent` を24-tool MCPの `retire_agent` 呼出しへ置換する前にcredential経路を決め、token付きagentを含むrequest/responseと失敗表示を検証する。現toolはtoken付きagentの`registration_token`を要求し、dashboardは現在`agent_id`しか持たない | 新serviceには旧REST routeがない。単純なMCP置換もtoken付きagentで失敗し、現在の後続tmux killだけが進むと「paneは消えたがagentはactive」の不整合になる |
| dashboard unretire | repo版の旧 `POST /mail/api/unretire-agent` を初回24-tool境界では無効化し、「未対応」を明示する。`unretire_agent`を無断で公開しない | 存在しないroute/toolを呼び、成功したように見えるUIまたは操作失敗になる |
| dashboard import | live dashboardの `~/mcp_agent_mail/src` 挿入と `from mcp_agent_mail import utils` を除き、repo版が既に使う`bin/lib/agentstack-scientists.sh`等のAgentStack-owned語彙へ統一してpickerを検証する | 旧source撤去時に即死はせずfrozen語彙へsilent fallbackするが、語彙正本との同期を失い、upstream依存ゼロも満たさない |
| dashboard auth | `HTTP_BEARER_TOKEN` 必須読出しと常時`Authorization: Bearer ...`送信を除き、認証未実装の新loopback MCP入口に合わせる | token fileが無ければregister/sendの呼出し前にhard failする。一方、新serviceはbearer/JWT設定があると起動自体を拒否するため、旧auth前提との両立経路はない |
| dashboard signals | live固定 `~/.mcp_agent_mail/signals` とrepo版旧defaultを `AGENTSTACK_SIGNALS_DIR=~/.agentstack/mail/signals` へ揃える | 通知・offline表示が旧signal treeを監視し続ける |

加えて、`~/.zshrc` の旧 `MCP_AGENT_MAIL_TOKEN` export、旧health URLを直接叩くhook/watcher、dashboard/Codex Appの旧fallbackを、新旧envの両方で動くrepo-managed artifactへ先に置換する。ここは任意文字列置換をせず個別testを持つ。旧source自身の `.mcp.json` / `.codex/config.toml`、frozen differential fixture、backup/historyは通常consumer inventoryへ入れない。

### live hooks / watcher の変更inventory（適用は未実行）

repo版と`~/.claude/hooks/`のlive版は同一物と仮定しない。C0でexact pathとdigestを数え上げ、次の変更をrepo-managed artifactとして個別test後にdeployする。live fileへの適用はmaintainerの承認対象であり、この変更では行わない。

| live artifact | 切替前の必要変更と検証 |
|---|---|
| `check-file-reservation.sh` | 新endpoint selector、`Accept: application/json, text/event-stream`、global bearer optional、agent `registration_token`非送信へ揃える。初回のtransport unreachableだけfail-open、HTTP 406・JSON-RPC error・MCP `isError`・malformed response・definitive zero後の失敗はfail-closed。予約なしはexit 2、既存予約はexit 0を両方向で実証する |
| `spawn_child.sh` | 新MCP key/endpointを選び、global bearerを必須にしない。requested identityと`register_agent` response nameが不一致なら警告継続ではなくfail-closedにし、作りかけのchild/session/configをcleanupする |
| `mark-agent-registered.sh` | responseのexact nameを検証してからsuccess markerを更新する。失敗responseや名前すり替えで登録済みにしない |
| `get-mcp-agent-mail-token.sh` | 新loopback endpointではglobal bearer取得を起動条件にしない。legacy endpointだけに必要な互換経路として隔離する |
| reservation release worker / release-all / child cleanup | old URL、常時bearer、legacy response shapeへの固定を除き、新endpointのexact MCP responseをfail-closedで扱う |
| `retire-agent-by-name.sh` / session-end retire | 初回24-tool境界では`retire_agent`だけを使う。`deregister_agent` optionのlive削除は別途maintainer承認後とし、旧DB import・old URL・bearer前提を残さない |
| `session-start-reminder.sh` / readiness check | 旧REST liveness URLをbounded MCP `health_check`へ置換し、HTTP rejectionをunreachable扱いにしない |
| `watch_agent_mail_signals.sh` / stale-signal cleanup | signal rootを`~/.agentstack/mail/signals`へ切り替え、per-messageとlegacy layoutの移行testを通す。旧DB/signal rootのsilent fallbackを残さない |
| warm-pool / launchd helper | 旧`/mail` route、8765、legacy labelを新18765 `/mcp`とexact ownershipへ置換し、旧jobを起動・停止しないことをtestする |
| `check-agent-registered.sh`ほかraw HTTP caller | 新MCP key/endpointとexact identity responseへ語彙を揃え、全raw HTTP callに必要な`Accept`を付ける |

hook/watcherのrepo実装とtestはC2前の前提条件にする。liveへのdeployも原則C2前だが、`check-file-reservation.sh`のstrict identity版だけは既存sessionを途中で止めないため、C5の全client restart/rebindとexact identity確認の**後**にdeployする。repo版だけ直してlive版を未更新のまま実動確認へ進めない。

## 上から順に実行する操作

### C0–C1: 旧 authority を動かしたまま準備する

1. clean checkoutでfull candidate commit、exact manifest、digest-verified evidenceを`cutover_readiness.py`へ渡す。`evaluation_state: valid`、`cutover_state: go`、`condition_count: 26`、`missing_conditions: []`と上表26 IDのexact unionをmaintenance記録へsealする。一つでも違えば後続のhuman確認やsmoke testで上書きせず、C0で止まる。
2. baseline開始方法はmaintainer裁定済みの **A（copied filesを1 baseline commitにする）** をversioned migration inputとmaintenance記録へ固定する。選択のcandidate/evidence bindingがmachine gateに含まれることも確認する。
3. working-tree scope migration、bounded MCP readiness probe、上のOrrery/dashboard互換変更とlive hooks変更が実装・検証済みであることを確認する。`agentstack-mail-consumers` は確定artifact上でcopy-only rehearsalが通ることを確認する。一つでも未実装ならここで止まる。
4. 確定 wheel を専用 venv へ入れ、live `~/Library/LaunchAgents` ではない staging directory に `agentstack-mail-service render` で plist と ownership manifest を作る。render は launchctl を呼ばない。
5. 旧 DB/archive/signals の read-only fingerprintと旧 launchd plistを保存する。全consumerをtyped inventoryへ列挙し、`agentstack-mail-consumers prepare`で0600/0400のbefore/after bundleを作る。標準出力のmanifest SHA-256はbundle外のmaintenance記録へpinする。
6. `agentstack-mail-consumers preview`のcontent-redactedなfile pathとbefore/after line rangeをmaintainerへ提示する。特にlive inventoryでtool permission/hookを確認した15個の `.claude/settings.local.json` は、対象fileと変更行を一件ずつ事前承認されるまでapplyしない。helperは**列挙したfile内**の旧alias、old/new併存、未知endpointをfailさせる。inventory外のfileは見えないため、別のlive inventory reviewで漏れ0を承認する。旧source tree自身の`09_MCP/mcp-agent-mail/.mcp.json`、`.codex/config.toml`、`.claude/settings.local.json`（最後のfileは旧sourceの`enabledMcpjsonServers=["mcp-agent-mail"]`だけを選ぶ開発用設定）はcutover consumerではないためexact pathで明示excludeし、理由をmaintenance記録へ残す。
7. 全 sender に開始時刻、C3の2–4分見込み、C5 test合格まで無通信が続くことを事前通知する。ProOpus 自身も、停止後は agent-mail を送受信せず同じ maintenance shell だけを使う。

### C2: 全 sender と旧 writer を静止する

tmux server 全体を kill しない。Claude/Codex parent・childは agent-mail call を止めて idle、`BiomatterBot`、`SeminarBot`、watcher/hook は停止または送信不能状態にする。全員の停止確認を agent-mail 停止前に済ませる。

最後の agent-mail 通知を送った後、operator は通常 shell へ移り、旧 job を止める。

```sh
launchctl bootout "gui/$(id -u)/com.operator.mcp-agent-mail"
```

次を全て確認する。一つでも hit したら copy せず、writer を特定する。

```sh
if launchctl print "gui/$(id -u)/com.operator.mcp-agent-mail"; then
  echo "legacy job is still loaded" >&2
  exit 1
fi

if lsof -nP -iTCP:8765 -sTCP:LISTEN; then
  echo "legacy listener is still present" >&2
  exit 1
fi

for path in \
  /Users/operator/mcp_agent_mail/storage.sqlite3 \
  /Users/operator/mcp_agent_mail/storage.sqlite3-wal \
  /Users/operator/mcp_agent_mail/storage.sqlite3-shm; do
  if [ -e "$path" ] && lsof "$path"; then
    echo "legacy database still has an open holder: $path" >&2
    exit 1
  fi
done

find /Users/operator/.mcp_agent_mail_git_mailbox_repo \
  \( -name '*.lock' -o -name '*.lock.owner.json' \) -print
```

最後の `find` は0件が合格である。さらに `.git/index.lock` が無いことを確認する。これらは既知 writer の停止を示すが、unknown direct filesystem writer の不在を証明しない。そのため migration 自身の6回の source照合も維持する。

### C3: DB + signals + working tree を一単位として複製・検証する

working-tree scope migration は次を一つの staging generation 内で行う。

1. SQLite read-only connection の `backup()` で committed WAL を含む copy を作る。
2. signals と archive working tree を copyする。legacy `.git` と `server.pid` は対象外。lock artifact、symlink、special file、権限不足、容量不足は fail-closedにする。
3. maintainer の A/B 選択どおり、新 archive に legacy と無関係な新 Git repo を作る。
4. SQLite `integrity_check`、`foreign_key_check`、schema、全 table digestに加え、agent→project、message→project/sender/thread、message→recipient/read/ack、reservation→project/agent、thread membershipを比較する。
5. `source_before`、`staged_state`、`source_after`、`source_final`、finalizer の `source_now` と `destination_now` という既存6回の照合を working-tree scope で維持する。検証回数・粒度を最適化しない。
6. working treeの全 path/content/mode、signals、33 file attachments、選んだGit開始状態を確認する。
7. fsync後、同一 filesystem上の一回のdirectory renameで `~/.agentstack/mail` を公開する。失敗時は部分treeを canonical path に残さない。

**この操作の実行コマンドはまだ存在しない。** 現在の `agentstack-mail-migrate copy` / `verify` は legacy `.git` を含む契約なので使わない。working-tree scope が実装されるまで、この節は仕様であって実行可能手順ではない。

### C4: 新 service を起動し、read-only readiness を確認する

```sh
agentstack-mail-service start \
  --ownership-manifest /path/to/cutover-staging/launchd/org.agentstack.mail.ownership.json

agentstack-mail-service status \
  --ownership-manifest /path/to/cutover-staging/launchd/org.agentstack.mail.ownership.json
```

`status: job_loaded` は exact plist/program/arguments が loaded という意味だけで、MCP readiness ではない。bounded probe で新 port 18765 の `health_check`と、既存 identity の read-only `whois(include_recent_commits=false)`を確認する。この段階では `fetch_inbox`も呼ばない。notification有効時の`fetch_inbox`はsignal fileをclearし、migration baselineそのものを変え得るためである。`register_agent`、send、receipt変更、reservation変更も行わない。

新 root が migration baseline と同一で、旧 job/8765が停止、新 job/18765だけがreadyであることを確認する。readinessが期限内に通らなければC4 rollbackへ進む。

### C5: consumer を一括切替し、最初の1通で実動確認する

個別手編集はしない。C0でsealしたbundleと外部pinしたdigestだけを使い、次の1操作で構造化configを切り替える。

```sh
agentstack-mail-consumers apply \
  --bundle /path/to/private-consumer-bundle \
  --expected-manifest-sha256 "$PINNED_MANIFEST_SHA256"

agentstack-mail-consumers status \
  --bundle /path/to/private-consumer-bundle \
  --expected-manifest-sha256 "$PINNED_MANIFEST_SHA256"
```

`status=committed`以外ではconsumerを再開しない。対象は明示inventoryに入れたClaude/Codex direct config、tool permissions、AgentStack/Codex App envとinstall receipt、停止時に存在したchild resume configである。Bridge自身の client key `agentstack` は変えない。repo-managed launcher/watcher/skillsとOrrery/dashboardはC2より前に新旧env両対応artifactとしてdeploy済みであることを前提とし、C5でsourceを文字列置換しない。例外はstrict identity版のreservation hookだけで、下記restart/rebind後にexact repo artifactをdeployする。

helperは列挙済みfile内の未知aliasを拒否するが、**inventoryから漏れたfileを発見するscannerではない**。C0のlive inventory reviewが別のhard gateである。inventory schema v1は次の全fieldを明示し、pathは全てabsoluteにする（値は本番用maintenance artifactにのみ書き、repoへcommitしない）。

live residual Codex child config 4件は、read-only inventory時点で全てper-tool policy tableが0件だった。helperは存在するproxy 8-tool policyだけを保存し、欠落policyを製造しない。C0で各artifactを「resume対象」または「staleなのでexcludeし、更新済みspawn経路から再生成」に分類し、未分類のchild configはinventoryへ入れない。

```json
{
  "schema_version": 1,
  "desired": {
    "legacy_mcp_url": "http://127.0.0.1:8765/mcp",
    "new_mcp_url": "http://127.0.0.1:18765/mcp",
    "legacy_mail_db": "/absolute/legacy/storage.sqlite3",
    "new_mail_db": "/absolute/new/mail/storage.sqlite3",
    "legacy_mail_env": "/absolute/legacy/.env",
    "new_mail_env": "/absolute/new/agentstack-mail.env",
    "legacy_mail_home": "/absolute/legacy/mail-home",
    "new_mail_home": "/absolute/new/mail",
    "legacy_signals_dir": "/absolute/legacy/signals",
    "new_signals_dir": "/absolute/new/mail/signals"
  },
  "consumers": [
    {"kind": "claude_mcp", "path": "/absolute/copied/.claude.json"},
    {"kind": "claude_settings", "path": "/absolute/copied/.claude/settings.local.json"},
    {"kind": "codex_mcp", "path": "/absolute/copied/.codex/config.toml"}
  ]
}
```

全config置換後、**既に起動していたclientは設定file変更だけでは新endpointへ移らない**。各Claude/Codex parent、Codex App、停止時に存在したchildを`agent-start`等のmanaged launcherで明示的にrestart/rebindする。raw non-tmux Claudeは対応せず、同じmanaged経路で再起動する。各sessionの`AGENT_NAME`、または`TMUX_PANE`で明示したtargeted tmux sessionがcanonical identityと一致し、stale pane metadataとの不一致が無いことを先に確認する。続いてloaded MCP keyが`agentstack-mail`、endpointが127.0.0.1:18765、新keyのtool surfaceが期待値、旧key/8765のconnectionが無いことをread-onlyに確認する。確認前はtest pairを含め誰もcallしない。

restart/rebindとidentity確認が全件終わった後にだけ、strict版`check-file-reservation.sh`と`resolve-agent-name.sh`をrepoのexact digestからliveへdeployする。untargeted tmux fallbackは無く、unresolved/placeholder identityとmetadata-session不一致はHTTPを送る前にexit 2であることを負方向testで確認する。deploy前に予約guardの実動確認へ進まない。

最初の clientが `register_agent` またはwriteを成功させる直前に、maintainerが冒頭の不可逆境界を再確認する。成功した瞬間から旧 authorityへのrollbackは禁止である。

その後、専用test sender/recipientだけを許可して1通だけ送る。他の全senderはoperational smoke確認まで黙ったままにする。観測項目は次の全てである。

- request nameとresponse `name` が完全一致する。
- `send_message` が返したmessage IDをrecipientの `fetch_inbox` が返す。
- sender、recipient、subject、本文が完全一致する。
- DBのmessage/recipient edgeと新 working treeのcanonical message fileが一つ増える。
- legacy DB/archive/signalsのfingerprintがC0から変わっていない。

続けて、専用test agentとprotectedなthrowaway pathでreservation guardを実動確認する。

- `file_reservation_paths`で予約を取得し、Write/Edit相当のhook payloadを渡すとexit 0になる。
- 予約をreleaseして同じpayloadを渡すとexit 2になり、対象fileは変更されない。
- production endpointではなく隔離stubでHTTP 406、JSON-RPC `error`、MCP `isError` true/型違反を返すと全てexit 2になる。
- 隔離stubの初回connection refusalだけは既定どおりexit 0になる。definitive zero回答後のtransport failureはexit 2になる。

単なる `isError: false` はoperational smoke成功にしない。全観測後に全 consumerを再開し、C6として新 authorityだけがwriterであることを再確認する。

## 失敗時の戻し方

| 失敗した段階 | 戻し方 |
|---|---|
| C0–C1 | 新 artifactを使わない。旧 authorityは動いたままなので変更なし |
| C2、destination未公開 | 新 serviceを起動せず、旧 source fingerprint不変を確認し、旧jobだけをbootstrapする |
| C3、copy検証済み | 新 copyは診断用に保持する。両service停止下でbaselineを確認し、旧jobだけをbootstrapする |
| C4、新service ready・consumer未切替 | exact ownershipで新jobをstopし、新rootがbaselineと同一なら旧jobだけをbootstrapする |
| C5、config切替済み・新rootがまだbaseline | 新jobをstopし、`agentstack-mail-consumers rollback --bundle ... --expected-manifest-sha256 ... --migration-manifest ~/.agentstack/mail/migration-manifest.json --cutover-stage C5_CLIENT_SWITCHING` の1操作でserviceのauthority lockを取得し、data baselineを再検査してからexact before-imageへ戻す。`status=rolled_back`を確認して旧jobをbootstrapする。新jobがlockを保持中、外部編集、post-baseline writeのいずれかを検出した場合は一つも上書きせずincidentにする |
| **C5/C6、最初のdurable write後** | **旧jobを起動しない。configを戻さない。** 全consumerをquiesceし、exact new jobを再起動してbounded readiness後に新authority上でfix-forwardする |

旧 job の再開が許される段階だけ、同じ maintenance shell から次を実行する。

```sh
launchctl bootstrap \
  "gui/$(id -u)" \
  /Users/operator/Library/LaunchAgents/com.operator.mcp-agent-mail.plist
```

その後、旧8765のbounded health、旧DB/archive/signalsのfingerprint、実clientの同名read handshakeを確認してからsenderを再開する。

durable write後にnew jobがreadyにならない場合は、旧を起動して二つのauthorityを作らない。新dataを保持したままincident/no-writerとし、repair後にexact new jobだけを起動する。検証済みreverse transformが無いため、新規recordだけを旧DBへbest-effort mergeしない。

## 現在の blocker digest（non-normative）

これは進捗を読むための要約であり、別の完了条件ではない。canonicalな残件は冒頭のevaluatorが返す`missing_conditions`である。現在は少なくとも次が未完了なので、本番切替は未承認である。

- working-tree scope migration commandと、その6回照合・中断・source mutation・destination occupation・corruptionの負方向テスト
- 実機consumerとlive hooksのexact inventory、maintainerによる個人settings preview承認、Orrery/dashboardの切替前compatibility
- bounded MCP readiness probe
- clean candidateのwheel/sdistとfresh installed wheel verification
- 台帳で`not_implemented`のpre-cutover follow-up taskと、それぞれのdigest-bound raw evidence

`README.md`、`claude/CLAUDE.md`、`codex/AGENTS.md`は今回の未実行runbookと矛盾するinstalled behaviorを記述していないため変更しない。実装が入るPRで同時に更新する。
