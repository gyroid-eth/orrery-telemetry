# AgentStack Mail working-tree 切替手順（未実行）

> **ここを越えると戻れない:** C5 で最初の consumer の `register_agent` または別の write tool が新 endpoint に成功し、新 root が migration baseline から変わった瞬間。以後は旧 DB へ部分的に戻さず、新 authority 上で fix-forward する。

> **この間は全員が黙る:** C2 で旧 writer を止めてから、C4 の新 endpoint readiness が確認できるまでの **2–4分**、ProOpus、全 Claude/Codex parent・child、bot、watcher/hook、切替 operator を含む全 sender は agent-mail を使わない。

これは maintainer の Mac で後日、上から順に実行するための手順書である。この変更では切替を実行しない。稼働中の `~/mcp_agent_mail`、MCP 設定、launchd、service、port 8765 には触れていない。

## 現在の hard stop

今回の移送方針は **DB + signals + legacy archive の working tree を運び、legacy `.git` は運ばない**で固定する。検証回数は既存設計の6回を維持し、2回へ減らす最適化はしない。

ただし working-tree scope の migration command と、consumer を before-image/CAS 付きで一括切替する helper はまだ未実装である。現在の `agentstack-mail-migrate copy` は legacy `.git` まで複製するため、この手順の代用に使わない。両方が実装・負方向テスト済みになるまで C2 へ進まない。

## 今回運ぶもの、運ばないもの

| 対象 | 扱い |
|---|---|
| SQLite | 約59 MiB。WAL の committed row を含む logical backup を作る |
| signals | 約404 KiB。全 file の content/mode を照合する |
| archive working tree | 約230 MiB、約49k files。現 Markdown、profile、reservation JSON、attachment を運ぶ |
| legacy `.git` | **運ばない。** 約1.3 GiB、43,380 commits、reflog、unreachable/deleted-file recovery を新 authority へ継承しない |
| runtime artifacts | `server.pid` は運ばない。`*.lock`、`*.lock.owner.json`、`.git/index.lock` が一つでもあれば copy を始めず失敗する |

SQLite の `messages.attachments` を read-only の `json_each` で再集計し、`type=file` の33 unique pathsが legacy working tree に **33/33実在**することを確認済みである。inline attachment は DB 内にある。したがって現 attachment read は working tree copy で維持できる。

失う通常挙動は、`whois(include_recent_commits=true)` が legacy commit history を返さなくなることだけである。新規 write 後の commit は新 repo に蓄積される。Git-enriched resource/UI/time-travel は新しい22-tool boundaryでは公開しない。

legacy DB、signals、working tree、`.git` は移動も削除も変更もしない。成功後も元の場所に cold copy として残し、旧 service を起動しないことで**運用上 read-only**にする。`chmod`、gc、reflog expire、archive cleanup は行わない。

## maintainer が実行前に選ぶ1点

| 新 repo の開始方法 | `whois` | 利点 / 不利 |
|---|---|---|
| **A. copied files を1 baseline commitにする（推奨）** | 切替直後は migration baseline 1件、その後は新規 commit | 約49k filesが全て追跡され、Git tree hashで current file set を説明できる。実測では `git add` + commit 約24秒、新 `.git` 約40 MiB |
| B. 空 repo のまま開始する | 最初の新規 write まで空 | legacy filesは全て untrackedとなり、status/auditが恒常的に noisy。`.git` を明示的に先に初期化しないと runtime が `.gitattributes` の init commit を作る |

どちらも legacy commit SHA、author/time series、reflog は新 repo に持ち込まない。Aは履歴の起点を明示でき、検証と保守が単純なので推奨する。maintainer の選択を記録してから artifact を作る。

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

最初の live copy は動的 lock が走査中に消えて失敗した。これは静止を省略した場合に migration が成功しない、望ましい fail-closed の実証である。既存6 snapshot相当を working-tree scope で維持すると warm側で約100秒になり、cold walkと余裕を含む **2–4分**を正式な無通信枠とする。短縮のために検証粒度を変えない。

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

## 上から順に実行する操作

### C0–C1: 旧 authority を動かしたまま準備する

1. maintainer が baseline commit の A/B を選び、記録する。
2. working-tree scope migration、consumer一括切替 helper、bounded MCP readiness probe が実装・検証済みであることを確認する。一つでも未実装ならここで止まる。
3. 確定 wheel を専用 venv へ入れ、live `~/Library/LaunchAgents` ではない staging directory に `agentstack-mail-service render` で plist と ownership manifest を作る。render は launchctl を呼ばない。
4. 旧 DB/archive/signals の read-only fingerprint、旧 launchd plist、全 consumer config の whole-file before-image と対象 entry hash を保存する。
5. 全 sender に開始時刻と2–4分の無通信を事前通知する。ProOpus 自身も、停止後は agent-mail を送受信せず同じ maintenance shell だけを使う。

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

`status: job_loaded` は exact plist/program/arguments が loaded という意味だけで、MCP readiness ではない。bounded probe で新 port 18765 の `health_check`、既存 identity の read-only `whois(include_recent_commits=false)`、read-only inbox fetch を確認する。この段階では `register_agent`、send、receipt変更、reservation変更を行わない。

新 root が migration baseline と同一で、旧 job/8765が停止、新 job/18765だけがreadyであることを確認する。readinessが期限内に通らなければC4 rollbackへ進む。

### C5: consumer を一括切替し、最初の1通で実動確認する

個別手編集はしない。before-image/CAS helperで Claude、Codex、bridge、launcher、hooks、watcher、skills、child proxyを新 key/endpoint/signal rootへ一括変更する。Bridge自身の client key `agentstack` は変えない。

最初の clientが `register_agent` またはwriteを成功させる直前に、maintainerが冒頭の不可逆境界を再確認する。成功した瞬間から旧 authorityへのrollbackは禁止である。

専用test sender/recipientで1通だけ送る。合格条件は次の全てである。

- request nameとresponse `name` が完全一致する。
- `send_message` が返したmessage IDをrecipientの `fetch_inbox` が返す。
- sender、recipient、subject、本文が完全一致する。
- DBのmessage/recipient edgeと新 working treeのcanonical message fileが一つ増える。
- legacy DB/archive/signalsのfingerprintがC0から変わっていない。

単なる `isError: false` は合格にしない。合格後に全 consumerを再開し、C6として新 authorityだけがwriterであることを再確認する。

## 失敗時の戻し方

| 失敗した段階 | 戻し方 |
|---|---|
| C0–C1 | 新 artifactを使わない。旧 authorityは動いたままなので変更なし |
| C2、destination未公開 | 新 serviceを起動せず、旧 source fingerprint不変を確認し、旧jobだけをbootstrapする |
| C3、copy検証済み | 新 copyは診断用に保持する。両service停止下でbaselineを確認し、旧jobだけをbootstrapする |
| C4、新service ready・consumer未切替 | exact ownershipで新jobをstopし、新rootがbaselineと同一なら旧jobだけをbootstrapする |
| C5、config切替済み・新rootがまだbaseline | 新jobをstopし、CASでconfig before-imageを戻し、旧jobをbootstrapする。外部編集を検出したconfigは上書きせずincidentにする |
| **C5/C6、最初のdurable write後** | **旧jobを起動しない。configを戻さない。** 全consumerをquiesceし、exact new jobを再起動してbounded readiness後に新authority上でfix-forwardする |

旧 job の再開が許される段階だけ、同じ maintenance shell から次を実行する。

```sh
launchctl bootstrap \
  "gui/$(id -u)" \
  /Users/operator/Library/LaunchAgents/com.operator.mcp-agent-mail.plist
```

その後、旧8765のbounded health、旧DB/archive/signalsのfingerprint、実clientの同名read handshakeを確認してからsenderを再開する。

durable write後にnew jobがreadyにならない場合は、旧を起動して二つのauthorityを作らない。新dataを保持したままincident/no-writerとし、repair後にexact new jobだけを起動する。検証済みreverse transformが無いため、新規recordだけを旧DBへbest-effort mergeしない。

## 完了条件と未実装条件

この手順の文書化をもってデータ移送・戻し方の設計③は終了するが、本番切替は未承認である。次が揃うまで実行しない。

- working-tree scope migration commandと、その6回照合・中断・source mutation・destination occupation・corruptionの負方向テスト
- baseline commit A/Bのmaintainer裁定
- transactional consumer cutover helperとbefore-image/CAS manifest
- bounded MCP readiness probe
- 確定wheelでのinstalled-artifact verification

`README.md`、`claude/CLAUDE.md`、`codex/AGENTS.md`は今回の未実行runbookと矛盾するinstalled behaviorを記述していないため変更しない。実装が入るPRで同時に更新する。
