# AgentStack Mail working-tree 切替手順（未実行）

> **ここを越えると初回切替の手順では戻れない:** C5 で最初の consumer の `register_agent` または別の write tool が新 endpoint に成功し、新 root が migration baseline から変わった瞬間。以後は旧 DB へ部分的に戻さず、新 authority 上で fix-forward する。将来の `post-authority-reverse-transform` は台帳に残すが、実装・rehearsal・別承認が済むまでは初回切替のrollback根拠にしない。

> **この間は全員が黙る:** C2 で旧 writer を止めてから、C5 の専用 test sender/recipient による1通の send/readとreservation guard実動確認が終わるまで、ProOpus、他の全 Claude/Codex parent・child、bot、watcher/hook、切替 operator は agent-mail を使わない。**2–4分はC3のdata copy/verificationだけの実測**であり、client restart/rebindを含む全静止時間は未測定である。

これは maintainer の Mac で後日、上から順に実行するための手順書であり、本番authority切替自体はまだ実行していない。切替前実測として、明示許可に束縛した試験専用label 1つのisolated launchd rehearsalと、maintainer承認下のlegacy production job 1回のstop/startだけを実行した。後者は同じplist・DB・endpointで再起動し、MCP設定・data・port 8765を変更していない。

## 2026-08-11 簡素化後の切替前scope

maintainer承認下の本番legacy server 1回再起動で、listener消滅はstopから5.41秒、同じendpointの自動復帰は15.72秒だった。既存clientは停止中の`fetch_inbox`が1回transport errorとなった後、session再起動も`/mcp`手動再接続もなく最初の再試行で成功した。PIDは28395から77623へ変わり、復帰後の`health_check`、DB `integrity_check`、件数は正常だった。したがって同じkey・同じURL/portでのclient再接続は実測済みとし、追加の隔離client rehearsalは行わない。

このlocalhost・単独利用の初回切替で、切替前に残す作業は次の4点だけである。従来のhash-lock済み依存閉包、atomic install receipt、残りの証跡handler、consumer orchestrationは切替後backlogへ移す。4点が揃った後にだけ、authorityの4遷移を固定するcommitted testを追加する。

1. production backupを、本番とcanonical path・symlink・inodeまで異なる故意破損済みの隔離targetへ非no-op復元すること。receiptの`target.kind=rehearsal-copy` / `production_source=false`、停止直前の最新行watermark、exact candidate PIDによるtarget DB familyのopen、起動後full logical snapshot一致、本番main/WAL/SHM fingerprintと8765 PIDの前後不変をすべて確認する。
2. 旧`com.operator.mcp-agent-mail`のbootoutから新`org.agentstack.mail`のbootstrapまでを、一つの不可分なwriter handoffとして実行すること。
3. 逆順のnew bootoutからlegacy bootstrapまでを、一つの不可分なrollbackとして実行できること。new labelのenabled overrideは正常な残留として記録すること。
4. 利用側が新endpointで70個のpermission/hook selectorに承認prompt 0を確認すること。この確認は利用側担当が行い、PluckyEinsteinは重複実装しない。

### production backupの復元実演（完了）

ProOpusが2026-08-11 21:57 JSTに作成した`/private/tmp/agent-mail-backup-20260811-215721.sqlite3`は67,293,184 bytes、SHA-256 `c80bdf9ddb59ab712c0ef23a60be08fbe8ec78f4fa523f02918fb1bae35eea02`、`integrity_check: ok`だった。最初にfresh DBへSQLite `.restore`して件数を比較した実演は、故意破損もPIDのopen-file確認も無い予備観測であり、合格証跡には使わない。

受理実演は`/private/tmp/plucky-restore-accept.fAPn0f`で、既存`agentstack-mail-migrate`のcold backup、raw family、組み込みdamage、`cold-restore`だけを使った。元backupとisolated seedはbyte-exactで、sealed cold-backup receiptは`de6c1f0010ca40d4b8b0f05ee6e8d8d0f0823cf233b607345db109467feca2ea`、baseline full logical SHA-256は`afb50ad0a331b233c865db8d0e9512248c9ef5d75aa129c859198d9002317818`である。復元前targetのmainを67,293,184 bytesから3,904 bytesへtruncateして別内容にし、backupではABSENTだったWAL/SHMへ各29 bytesの偽sidecarを作った。物理generationが変わり、logical validatorも`file is not a database`で失敗したため、復元はno-opではない。

既存`cold-restore`はこの故意破損targetを置換し、receipt `/private/tmp/plucky-restore-accept.fAPn0f/receipt/cold-restore-receipt.json`（SHA-256 `a3bcdbb27128b7f3ce08238322ec386c043b062337d0ba4d6fd91abc6479d1a0`）へ`target.kind=rehearsal-copy`、`production_source=false`、fault divergence observed、schema・全table rows・relations・PRAGMAのlogical matchを記録した。target `/private/tmp/plucky-restore-accept.fAPn0f/target-parent/state/storage.sqlite3`はsymlinkではなく、本番mainの`dev/inode=16777232/12149182`に対して`16777232/95663068`で別物だった。

停止直前の最新rowはmessages ID 8829、created `2026-08-11 12:57:05.816025`、全11 columnのcanonical content SHA-256 `2c31643956f5bc603269fea9383321819aea101850a648f1f1f70091eb90358b`である。復元直後とexact product code `48ad386908191ce8d9a9c60a704f91b04065644c`の起動後に同じwatermarkを確認した。wheel SHA-256 `69d47462bfedfe99c3dd9ab9e513f956c913f63f396decfe48668ca5b688d1a5`のinstalled entrypointはisolated port 28768で`health_check`と24-tool listに成功し、PID 56101が復元先main/WAL/SHMを実際にopenしていた。起動中のfull logical SHA-256も`afb50ad0...`のまま一致し、SIGTERM終了はrc 0だった。

同じ実演窓の前後で、本番`/Users/operator/mcp_agent_mail/storage.sqlite3`のmain/WAL/SHMはcanonical path、symlink状態、dev/inode、mode、size、mtime/ctime、SHA-256が完全一致し、8765 listenerもPID 77623のまま同じraw fingerprintだった。本番8765へnetwork requestや停止起動は行っていない。これにより上記1を完了とする。

同日の本番legacy stop/start後に、採取済み`legacy-launchd-definition-v1.json`のdefinitionと再起動後loaded definitionをread-onlyで比較し、plist bytes/path、program/arguments、working directory、RunAtLoad、KeepAliveを含むdefinition全体が一致した。legacy listenerはPID 77623/port 8765、wrapperはPID 77599で復帰した。これはrollback tailで採取済みlegacy定義から同じjobが戻ることの実機確認である。

## 旧14条件 evaluator（切替後hardeningへ延期）

`packages/agentstack_mail/fixtures/differential-expected-divergences-v2.json`と`packages/agentstack_mail/tests/cutover_readiness.py`の14条件evaluatorは、配布製品相当のfull evidence gateとしてfail-closedのまま保持する。ただし2026-08-11の簡素化裁定後は、この単独localhost切替のpre-cutover hard stopには使わず、未実装handlerを埋める作業も切替後へ延期する。以下の表と旧C0 producerは履歴・将来hardening用であり、上の4点へ作業範囲を再拡張しない。

次の表は旧14個のmachine gateをoperatorの確認順にgroup化したものである。IDと合格線は旧台帳のまま保持し、表の説明は置換しない。

| 段階 | machine gate ID（台帳とexact match） | operatorが確認する意味 |
|---|---|---|
| 決定・candidate固定 | `product-decisions-selected`<br>`pre-cutover-product-decisions-implemented`<br>`initial-cutover-difference-set-exact`<br>`candidate-source-bound`<br>`product-decision-cutover-approval` | D1–D12、初回差異集合、clean candidate、maintainer承認が同じcandidateへ固定されている |
| behavior・build | `selected-behavior-release-gate`<br>`distribution-artifact-release-gate` | 選択挙動と、実際に使うwheel/sdistが同じcandidateへ束縛されている |
| 予約安全 | `reservation-probe-safety-release-gate` | timeout/error/filesystem-incompleteで予約を誤解放せず、TTL expiryだけがreleaseされる |
| runtime・deploy・consumer | `http-cli-transport-entrypoints`<br>`service-lifecycle-supervision`<br>`mcp-client-reregistration-cutover`<br>`notification-layout-consumer-compatibility` | exact wheelからの24-tool起動、単一writer lifecycle、全clientの可逆切替、通知layoutと実consumerの互換性が通っている |
| authority移行・復旧 | `data-migration-reconciliation`<br>`rollback-revert-procedure` | data照合とfirst durable write前までのrollbackが、一つのauthority遷移を証明している |

C5の専用test sender/recipientによる5項目とreservation guardの4観測は、GO判定後にauthority switchが実際に機能したことを見る**post-switch operational smoke check**である。14条件の代替でも、切替承認を作る第二の合格線でもない。

evaluatorはread-onlyであり、`go`でもservice、config、authorityを自動変更しない。C0/C2/C5のhuman hold pointは実行を止められるが、`no_go`を承認で上書きできない。

## 旧full-evidence hard stop（切替後backlog）

今回の移送方針は **DB + signals + legacy archive の working tree を運び、legacy `.git` は運ばない**で固定する。検証回数は既存設計の6回を維持し、2回へ減らす最適化はしない。

working-tree scope の `agentstack-mail-migrate copy` / `verify` / `rollback-assess` と、production-shaped rehearsal / candidate-bound raw evidence runner は実装済みである。台帳のdata-migration-reconciliation evidence handlerは未実装だが、簡素化裁定によりhandler自体は切替後backlogである。以下の旧C0 command例はfull-evidence pathの参照として残し、今回の4点を満たすために実行しない。

consumer設定用の `agentstack-mail-consumers` は実装済みである。明示inventoryから全before/after imageを先に作り、外部にpinするmanifest SHA-256、whole-set CAS、同一directoryのatomic replace、write-once terminal receipt、migration baselineを再検査する1操作rollbackを持つ。ただし複数directoryを跨ぐ真のatomic syscallではない。途中状態は `status=committed` にならず、C2でconsumerを止めたまま再実行またはrollbackする契約である。実機inventoryの確定、個人設定のpreview承認、下記のOrrery/dashboard前提条件が揃うまで C2へ進まない。

## 2026-08-11 isolated launchd rehearsal の明示許可

maintainer は **2026-08-11**、foregroundではlaunchdが送る停止signal、KeepAliveによるcrash recovery、bootstrap/bootoutのjob状態遷移を代替できず、このままでは切替当日がlaunchd管理下の初回になるため、**隔離した試験専用service 1つでのlaunchd rehearsal**を許可した。

許可は次の範囲に限定する。

- 対象labelは `org.agentstack.mail.rehearsal.<candidate8>.<nonce>` 形式の **1つだけ**とする。
- launchdへの読み取り専用操作は許可する。`print`、`print-disabled`、`list`等を含むが、receiptへ保存するのは試験専用exact labelに関係する値だけとする。
- 許可された状態変更は、そのexact labelへの `bootstrap` / `enable` / `kickstart` / `bootout` の4操作だけである。これ以外の状態変更が必要になった場合は実行前にProOpusへ確認する。
- `enable` は最初の3操作の列挙後に追加された。実際の `agentstack-mail-service start` が `bootstrap → enable → kickstart` を呼ぶためであり、rehearsalだけ省略すると本番と異なるcontroller経路を試すことになる。
- 稼働中のdashboardとlegacy agent-mail job `com.operator.mcp-agent-mail`、新candidateのproduction label `org.agentstack.mail`、MCP設定・data、port 8765は対象外であり、起動・停止・変更しない。両labelと8765が不変であることを確認するread-only観測だけを例外とし、8765はnetwork requestを送らず `lsof` listener fingerprintだけをbefore/afterで比較する。
- plist、ownership、env、state root、DB、archive、signals、log、wheel、venv、receiptは0700の隔離temp配下に置き、隔離portを使う。
- 実行前にlabelの明示引数化、production既定値の維持、ownership/CLI label不一致のfail-closed、fake launchctl検証を完了する。`launchctl print gui/$UID/<rehearsal_label>` が113以外、production labelと一致、予約prefix外、またはlabelが既存なら、process起動前に中止する。
- `finally` は試験専用exact labelだけをbootoutする。最後に同labelがprint=113、隔離portのlistenerが0、production labelと8765のfingerprintがbefore/after一致であることを確認する。
- cleanup経路ではprocessへSIGTERM/SIGKILLを送らない。bootout後もlistenerが残ること自体を失敗証跡として保持する。
- cleanupに失敗した場合はterminal receiptを出さない。想定外のprocessまたはjobが残った場合は、exact label・port・PID・隔離pathを **ProOpusへurgent報告**し、ProOpusが素性を照合してcleanupする。

試験receiptには、foreground receiptとの差の有無に加え、`enable` が隔離temp外に作るlabel単位の永続overrideをbefore/afterで実測し、残ったものとcleanupしなかった理由を記録する。exact nonce labelのoverrideは永続残留として受け入れ、許可外の`disable`やdomain全体へ作用する`reset-disabled`では消さない。推測で「何も残らない」とは扱わない。

現在の状態: **exact candidate `48ad386908191ce8d9a9c60a704f91b04065644c` のforeground rehearsalと実launchd rehearsalは完了し、terminal receiptを独立検算してacceptした。** sandbox外でinstalled whole CLIを1回だけ実行し、17秒でrc 0となった。sequenceは`start → stop → start → SIGKILL → KeepAlive別PID復帰 → stop`、2回のstopはrc 113まで7 poll / 743.921 msと8 poll / 784.018 msで収束した。終了時は試験labelがrc 113、port 28766がlistener 0、in-progress markerなし、legacy wrapper 28189・listener 28395/8765と新production label不在がbefore/after一致である。legacy定義receiptは`58ad959fb65748a42ada0825c066711cbc4575c83efa9e9935e8f129465008ef`、launchd terminal receiptは`6431ec9ed0ccc4b32949cbcdceaec4a6cf95ff48769b8dc1871eac6deb2e248b`で、どちらもmode 0400である。このacceptはlaunchd rehearsalだけに対するもので、cutover GOではない。

その前のcandidate `fdb9839` では、`org.agentstack.mail.rehearsal.fdb98391.once-204052` の直前controller `status`が`stopped`（内部的にexact `print` rc 113）だった後、最初の`bootstrap`がrc 5/EIOを返した。EIO直後のjob stateは未記録なので、load成功/失敗を推測しない。`finally`後はexact labelがrc 113、隔離port 28765がlistener 0、runtime logなし、8765のPID/fingerprint不変、terminal receiptなし、in-progress marker残留である。

別途ProOpusが許可した`/bin/sleep 30`だけの最小diagnostic plistは、同一内容・新規label・事前print rc 113を揃えた比較で **Codex sandbox内からはEIO、sandbox外のProOpus shellからはrc 0**となった。temp配置、0700 parent、plist payloadではなく、sandbox内からのstate-changing launchctl呼出しが原因である。`48ad386`の成功runでは、PluckyEinsteinがexact candidateのplist、whole CLI command、受け入れ条件を作り、ProOpusがsandbox外で **`agentstack-mail-evidence launchd-rehearsal` 全体**を実行した。CLI自身がcontroller実行とwrite-once receipt生成を一体で行い、ProOpusは生のrc/stdout/stderr/timeとartifact pathを返し、PluckyEinsteinがreceiptを別のread-only検算で判定した。今後も状態変更部分だけを切り出したり、結果からreceiptを手書きしない。

controller側でもEIOを「未load」とみなさず、bootstrap前のrc 113とEIO直後のexact loaded/absent再照合を別fieldでreceiptへ残す。`bootout`は非同期なので、cleanupはexact ownershipを毎回再確認しながらrc 113までbounded pollする。foreign化は即failとする。全試験labelのjobとlistenerはbootout済みだが、成功runの`enable`が書いた`org.agentstack.mail.rehearsal.48ad3869.once-215000 => enabled` overrideは永続している。`disable`はentryを消さず`disabled=true`へ悪化させ、`reset-disabled`はdomain全体へ作用するため、どちらもcleanupに使わない。

**production overrideの扱い:** C4で本番`org.agentstack.mail`を`enable`すると、同じlabel単位の`enabled` overrideが永続的に書かれる。後でnew jobをbootoutしてlegacyへ戻してもこのentryは消えない。これは意図したcontroller挙動であり、`launchctl print`のjob不在とは別の状態である。rollback後の保守記録にはoverrideが残ることを記し、「戻したのに痕跡がある」または「enabled entryがあるからjobも動いている」と誤読しない。entryを消す目的の`disable` / `reset-disabled`は実行しない。

## 今回運ぶもの、運ばないもの

| 対象 | 扱い |
|---|---|
| SQLite | 約59 MiB。WAL の committed row を含む logical backupを作り、main DBのschema・全row・関係・PRAGMAを比較する。`-wal`/`-shm`はruntime sidecarとして比較対象外 |
| signals | 約404 KiB。全 file の content/mode を照合する |
| archive working tree | 約230 MiB、約49k files。現 Markdown、profile、reservation JSON、attachment を運ぶ |
| legacy `.git` | **運ばない。** 約1.3 GiB、43,380 commits、reflog、unreachable/deleted-file recovery を新 authority へ継承しない |
| runtime artifacts | `server.pid` は運ばない。`*.lock`、`*.lock.owner.json`、`.git/index.lock` が一つでもあれば copy を始めず失敗する |

SQLite の `messages.attachments` を read-only の `json_each` で再集計し、`type=file` の33 unique pathsが legacy working tree に **33/33実在**することを確認済みである。inline attachment は DB 内にある。したがって現 attachment read は working tree copy で維持できる。

失う通常挙動は、`whois(include_recent_commits=true)` が legacy commit history を返さなくなることだけである。新規 write 後の commit は新 repo に蓄積される。live 40 toolsのうち24を公開し、suppressed 16に含まれるGit-enriched resource/UI/time-travelは初回boundaryでは公開しない。

legacy main DBの論理record、signals、working tree、`.git`は移動も削除も変更もしない。成功後も元の場所にcold copyとして残し、旧serviceを起動しないことで**運用上 read-only**にする。`mode=ro`のWAL readでもlegacy DB directoryへ`-wal`/`-shm`を生成し得る。一方、既存sidecarをmain DBへcheckpointして除去し、main-file bytesを変え得る直接原因は、copy全体のquiesceを強制するために選んだ`mode=rw` writer guardのcloseである。外部process確認だけでは確認直後のwriter発生を防げないためguardは残し、その代償をSQLiteを開く前のcold byte backupで吸収する。したがって**source byte-for-byte untouchedやsidecar不変とは言わない**。migration/rollbackの受け入れ不変条件はmain DBのschema・全row・関係・PRAGMAであり、sidecarの存在とbytesはfile一覧比較から明示除外する。migration自身のsidecar cleanupは行わない。`immutable=1`はcommitted WALを無視し得るため使わない。`chmod`、gc、reflog expire、archive cleanupは行わない。

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
| provider identity / client key | provider `mcp-agent-mail`; Claude `mcp-agent-mail`; Codex `agent-mail` | provider `agentstack-mail`; Claude `mcp-agent-mail`; Codex `agent-mail`（初回切替では維持） |
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
| dashboard retire | 旧 `POST /mail/api/retire-agent` を24-tool MCPの `retire_agent` 呼出しへ置換し、token付きagentを含むrequest/responseと失敗表示を検証する。初回cutoverで採用したloopback local-process境界ではlocal principalがtargetの`registration_token`なしでretireでき、schema fieldは保持し、credential-freeな`retire_agent.loopback_authorized` auditを残す | 新serviceには旧REST routeがない。MCP置換と失敗判定が無いまま後続tmux killだけが進むと「paneは消えたがagentはactive」の不整合になる |
| dashboard unretire | repo版の旧 `POST /mail/api/unretire-agent` を初回24-tool境界では無効化し、「未対応」を明示する。`unretire_agent`を無断で公開しない | 存在しないroute/toolを呼び、成功したように見えるUIまたは操作失敗になる |
| dashboard import | live dashboardの `~/mcp_agent_mail/src` 挿入と `from mcp_agent_mail import utils` を除き、repo版が既に使う`bin/lib/agentstack-scientists.sh`等のAgentStack-owned語彙へ統一してpickerを検証する | 旧source撤去時に即死はせずfrozen語彙へsilent fallbackするが、語彙正本との同期を失い、upstream依存ゼロも満たさない |
| dashboard auth | `HTTP_BEARER_TOKEN` 必須読出しと常時`Authorization: Bearer ...`送信を除き、認証未実装の新loopback MCP入口に合わせる | token fileが無ければregister/sendの呼出し前にhard failする。一方、新serviceはbearer/JWT設定があると起動自体を拒否するため、旧auth前提との両立経路はない |
| dashboard signals | live固定 `~/.mcp_agent_mail/signals` とrepo版旧defaultを `AGENTSTACK_SIGNALS_DIR=~/.agentstack/mail/signals` へ揃える | 通知・offline表示が旧signal treeを監視し続ける |

準備済みpatchはsource用4本（`0001 → 0002 → 0003 → 0004a`）とlive LaunchAgent専用`0002b`に分ける。0003はexact 0002-after baseから再生成し、0004aはplain unified diffへ正規化してtrailing-garbage warningを除いた。liveの `~/Library/LaunchAgents/com.operator.agentdashboard.plist` は手編集やrepo plistの直接copyで置換せず、sealed before-image digestが一致したときだけ0002bのstrict dry-run後に適用する。rollbackは`0004a → 0003 → 0002b → 0002 → 0001`の逆順で全patchをstrict dry-runしてから戻し、旧jobをreloadする。

当日のliteral invocationは次へ固定する。`--fuzz=0`だけではoffsetを拒否しないため、dry-run/applyの出力に`offset`、`fuzz`、`Ignoring`、`malformed`、`misordered`のどれかがあればrc 0でも停止する。`.orig` / `.rej` が一つでも生成された場合も停止する。

```sh
PATCH_ROOT=${PATCH_ROOT:-/Users/operator}
strict_patch() {
  direction=$1
  phase=$2
  patch_file=$3
  dry_arg=
  test "$phase" = apply || dry_arg=--dry-run
  output=$(patch --batch "$direction" --fuzz=0 --no-backup-if-mismatch \
    $dry_arg -d "$PATCH_ROOT" -p1 < "$patch_file" 2>&1) || {
      printf '%s\n' "$output" >&2
      return 1
    }
  printf '%s\n' "$output"
  if printf '%s\n' "$output" | grep -Eiq 'offset|fuzz|Ignoring|malformed|misordered'; then
    return 1
  fi
  test -z "$(find \
    "$PATCH_ROOT/.orrery" \
    "$PATCH_ROOT/OSS/orrery/bridge" \
    "$PATCH_ROOT/.claude/tools/agent-dashboard" \
    "$PATCH_ROOT/Library/LaunchAgents" \
    -type f \( -name '*.orig' -o -name '*.rej' \) -print -quit)"
}

verify_display_patch_bundle() {
  (
    cd "$PATCH_DIR"
    shasum -a 256 -c <<'EOF'
bc4b7d9d379c4770408bb45a09d8778307f1038ed5e679d1b71a3ad5c57506d1  0001-orrery-mail-db-selector.patch
fb57b50157931255c9a9efb4dd1b7d1a93c3008374a10fd566d73d95883bb658  0002-dashboard-mail-cutover-selectors.patch
5df21b01757d5829b038ed785a72f248613f54be6d2ec12e4444feabcde9a470  0002b-dashboard-live-launchagent-selectors.patch
42b95c21d5b71163bff7be842b5183b2ff4897d6598f7f60b27a939dc9485748  0003-dashboard-agentstack-mail-no-bearer.patch
f0c62d81f383951eb5daa4d6af3c9581fe8f5f9d4dbc37cb4420b9c1d3dd55c9  0004a-dashboard-loopback-retire-exit.patch
EOF
  )
}

apply_display_patches() {
  verify_display_patch_bundle || return 1
  for patch_name in \
    0001-orrery-mail-db-selector.patch \
    0002-dashboard-mail-cutover-selectors.patch \
    0002b-dashboard-live-launchagent-selectors.patch \
    0003-dashboard-agentstack-mail-no-bearer.patch \
    0004a-dashboard-loopback-retire-exit.patch
  do
    strict_patch --forward dry "$PATCH_DIR/$patch_name" || return 1
    strict_patch --forward apply "$PATCH_DIR/$patch_name" || return 1
  done
}

rollback_display_patches() {
  verify_display_patch_bundle || return 1
  # rollback only; never call after the first durable write on new authority
  for patch_name in \
    0004a-dashboard-loopback-retire-exit.patch \
    0003-dashboard-agentstack-mail-no-bearer.patch \
    0002b-dashboard-live-launchagent-selectors.patch \
    0002-dashboard-mail-cutover-selectors.patch \
    0001-orrery-mail-db-selector.patch
  do
    strict_patch --reverse dry "$PATCH_DIR/$patch_name" || return 1
    strict_patch --reverse apply "$PATCH_DIR/$patch_name" || return 1
  done
}
```

再生成後の隔離自己検証は`/private/tmp/agentstack-display-chain-rebuilt.AwNxke`で2026-08-11T15:48 JSTに実施した。source 5 fileとlive plistの複製だけへ`0001 → 0002 → 0002b → 0003 → 0004a`を上記flagsで各dry-run→applyし、JSON、plist 2 file、Python 3 fileを検査した後、逆順の各dry-run→reverseでbaselineとのbyte-exact diff 0、`.orig/.rej` 0を確認した。これは使い捨て複製の証跡であり、liveへの適用証跡ではない。

加えて、`~/.zshrc` の旧 `MCP_AGENT_MAIL_TOKEN` export、旧health URLを直接叩くhook/watcher、dashboard/Codex Appの旧fallbackを、新旧envの両方で動くrepo-managed artifactへ先に置換する。ここは任意文字列置換をせず個別testを持つ。旧source自身の `.mcp.json` / `.codex/config.toml`、frozen differential fixture、backup/historyは通常consumer inventoryへ入れない。

### live hooks / watcher の変更inventory（適用は未実行）

repo版と`~/.claude/hooks/`のlive版は同一物と仮定しない。C0でexact pathとdigestを数え上げ、次の変更をrepo-managed artifactとして個別test後にdeployする。live fileへの適用はmaintainerの承認対象であり、この変更では行わない。

| live artifact | 切替前の必要変更と検証 |
|---|---|
| `check-file-reservation.sh` | 新endpoint selector、`Accept: application/json, text/event-stream`、global bearer optional、agent `registration_token`非送信へ揃える。初回のtransport unreachableだけfail-open、HTTP 406・JSON-RPC error・MCP `isError`・malformed response・definitive zero後の失敗はfail-closed。予約なしはexit 2、既存予約はexit 0を両方向で実証する |
| `spawn_child.sh` | 親が使う既存client keyを維持したまま新endpointを選び、key名ではなくendpoint/root/ownershipでauthorityを判定し、global bearerを必須にしない。requested identityと`register_agent` response nameが不一致なら警告継続ではなくfail-closedにし、作りかけのchild/session/configをcleanupする |
| `mark-agent-registered.sh` | responseのexact nameを検証してからsuccess markerを更新する。失敗responseや名前すり替えで登録済みにしない |
| `get-mcp-agent-mail-token.sh` | 新loopback endpointではglobal bearer取得を起動条件にしない。legacy endpointだけに必要な互換経路として隔離する |
| reservation release worker / release-all / child cleanup | old URL、常時bearer、legacy response shapeへの固定を除き、新endpointのexact MCP responseをfail-closedで扱う |
| `retire-agent-by-name.sh` / session-end retire | 初回24-tool境界では`retire_agent`だけを使う。`deregister_agent` optionのlive削除は別途maintainer承認後とし、旧DB import・old URL・bearer前提を残さない |
| `session-start-reminder.sh` / readiness check | 旧REST liveness URLをbounded MCP `health_check`へ置換し、HTTP rejectionをunreachable扱いにしない |
| `watch_agent_mail_signals.sh` / stale-signal cleanup | signal rootを`~/.agentstack/mail/signals`へ切り替え、per-messageとlegacy layoutの移行testを通す。旧DB/signal rootのsilent fallbackを残さない |
| warm-pool / launchd helper | 旧`/mail` route、8765、legacy labelを新18765 `/mcp`とexact ownershipへ置換し、旧jobを起動・停止しないことをtestする |
| `check-agent-registered.sh`ほかraw HTTP caller | 既存client keyを維持して新endpointとexact identity responseへ語彙を揃え、全raw HTTP callに必要な`Accept`を付ける |

hook/watcherのrepo実装とtestはC2前の前提条件にする。liveへのdeployも原則C2前だが、`check-file-reservation.sh`のstrict identity版だけは既存sessionを途中で止めないため、C5の全client restart/rebindとexact identity確認の**後**にdeployする。repo版だけ直してlive版を未更新のまま実動確認へ進めない。

### client key と stale selector の切替後整理

初回切替ではClaude `mcp-agent-mail`、Codex `agent-mail`を互換ABIとして維持し、permission/hook selectorを変更しない。**projectの`.mcp.json`へ新しいMCP server entryを追加しない。** 既存key `mcp-agent-mail`を据え置き、そのentryのURLと認証だけを切り替える。2026-08-11 22:04:49 JSTの隔離N=1では新しいproject serverとして置くとtool call前にMCP trust promptが1件出た一方、strict configと`--setting-sources user,project`で既存keyを使った22:06:24–22:07:20 JSTの9 toolはpermission/trust prompt 0、error 0だった。証跡は`/private/tmp/agentstack-mail-rehearsal-52594f6.UP4e5q/receipts/selector-probe/n1-user-selector-key-stability.json`、SHA-256 `338a8fc8574146fc0ef974ae3d92c06ba50f4826d5b88740e07f57e309cbc423`である。

任意のkey改名、24-tool境界に無いstale permission 21 occurrence（10 tool名）、Lifeの旧8765 raw curl selector 4件、dormantな`deregister_agent` optionは別のnon-blocking post-cutover taskで扱う。これらは未コミットmachine-local観測 `2026-08-11T15:18:49 JST` の値であり、実行時の正本ではない。将来toolが再公開されるとstale permissionが自動的に有効化され得るため、post作業でも新しいone-run sealed inventory、maintainerへのexact file/line preview、明示承認を必須にする。`retire-agent-by-name.sh`を再有効化する場合は、dormant optionだけでなく旧DB・8765・bearer前提も同時に置換して検証する。

## 上から順に実行する操作

### C0–C1: 旧 authority を動かしたまま準備する

最初に次の固定pathを同じmaintenance shellへ設定する。isolated rehearsal evidenceの生成・検算だけはfinal readinessより先に行う。本番pathへのcopy、service/config/authority操作は、readinessが`go`でなければ開始しない。

```sh
set -eu
REPO='/Users/operator/Syncthing/<vault-directory>/21_Coding Projects/claude-agent-stack'
MAINT='/Users/operator/.agentstack/cutover-maintenance'
PATCH_DIR="$REPO/docs/agentstack-mail-cutover-patches"
READINESS="$REPO/packages/agentstack_mail/tests/cutover_readiness.py"
CUTOVER_MANIFEST="$REPO/packages/agentstack_mail/fixtures/differential-expected-divergences-v2.json"
EVIDENCE_INDEX="$MAINT/evidence-index.json"
EVIDENCE_ROOT="$MAINT/evidence"
LEGACY_PLIST='/Users/operator/Library/LaunchAgents/com.operator.mcp-agent-mail.plist'
LEGACY_LAUNCHD_RECEIPT="$MAINT/legacy-launchd-definition-v1.json"
NEW_OWNERSHIP="$MAINT/render/org.agentstack.mail.ownership.json"
NEW_ENV="$MAINT/render/agentstack-mail.env"
NEW_STATE_ROOT='/Users/operator/.agentstack/mail'
CANDIDATE_VENV="$MAINT/candidate-venv"
MIGRATE_BIN="$CANDIDATE_VENV/bin/agentstack-mail-migrate"
SERVICE_BIN="$CANDIDATE_VENV/bin/agentstack-mail-service"
CONSUMERS_BIN="$CANDIDATE_VENV/bin/agentstack-mail-consumers"
SERVER_BIN="$CANDIDATE_VENV/bin/agentstack-mail"
EVIDENCE_BIN="$CANDIDATE_VENV/bin/agentstack-mail-evidence"
MIGRATION_MANIFEST='/Users/operator/.agentstack/mail/migration-manifest.json'
COLD_BACKUP_DIR="$MAINT/cold-backup"
CONSUMER_BUNDLE="$MAINT/consumer-bundle"
REHEARSAL_SEED_ROOT="$MAINT/rehearsal-seed"
REHEARSAL_SEED_DB="$REHEARSAL_SEED_ROOT/legacy/storage.sqlite3"
REHEARSAL_SEED_ARCHIVE="$MAINT/rehearsal-seed/legacy/archive"
REHEARSAL_SEED_SIGNALS="$MAINT/rehearsal-seed/legacy/signals"
REHEARSAL_COPY_ROOT="$MAINT/rehearsal-seed/migration-copy"
REHEARSAL_MANIFEST="$MAINT/rehearsal-seed/migration-copy/migration-manifest.json"
REHEARSAL_PROVENANCE="$MAINT/rehearsal-seed/seed-provenance.json"
REHEARSAL_EVIDENCE_DIR="$EVIDENCE_ROOT/data-migration-reconciliation"
REHEARSAL_RUN="$REHEARSAL_EVIDENCE_DIR/restore-rehearsal"
REHEARSAL_PINS="$REHEARSAL_EVIDENCE_DIR/restore-rehearsal-pins.json"
install -d -m 700 "$MAINT" "$EVIDENCE_ROOT" "$REHEARSAL_EVIDENCE_DIR"
```

この時点で、後続のC4/C5とR1–R6が使うassertionを一度だけ定義する。関数定義より前に利用しない。

```sh
bounded_mail_probe() {
  python3 - "$1" "$2" "$3" 'PluckyEinstein' <<'PY'
import json, sys, time, urllib.request
url, expected_port, expected_db, agent = sys.argv[1:]

def call(name, arguments):
    payload = json.dumps({
        "jsonrpc": "2.0", "id": name, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }).encode()
    request = urllib.request.Request(url, data=payload, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    })
    with urllib.request.urlopen(request, timeout=2) as response:
        raw = response.read().decode("utf-8")
    for line in raw.splitlines():
        if line.startswith("data:"):
            raw = line[5:].strip()
            break
    envelope = json.loads(raw)
    if "error" in envelope:
        raise RuntimeError(envelope["error"])
    result = envelope.get("result") or {}
    if result.get("isError") is True:
        raise RuntimeError(result)
    value = result.get("structuredContent")
    if value is None:
        for block in result.get("content") or []:
            if block.get("type") == "text":
                value = json.loads(block.get("text") or "{}")
                break
    if not isinstance(value, dict):
        raise RuntimeError("missing structured MCP result")
    return value

deadline = time.monotonic() + 20
last = None
while time.monotonic() < deadline:
    try:
        health = call("health_check", {})
        assert health["status"] == "ok"
        assert health["http_host"] == "127.0.0.1"
        assert health["http_port"] == int(expected_port)
        assert health["database_url"] == expected_db
        who = call("whois", {
            "project_key": "/Users/operator/Syncthing/<vault-directory>",
            "agent_name": agent,
            "include_recent_commits": False,
        })
        assert who["name"] == agent
        assert who.get("recent_commits", []) == []
        raise SystemExit(0)
    except Exception as exc:
        last = exc
        time.sleep(0.5)
raise SystemExit(f"bounded MCP read probe failed: {last}")
PY
}

assert_legacy_writer_absent() {
  phase="$1"
  : > "$MAINT/${phase}-legacy-retire-poll.tsv"
  legacy_retire_deadline=$(( $(date +%s) + 60 ))
  while :; do
    set +e
    launchctl print "gui/$(id -u)/com.operator.mcp-agent-mail" \
      > "$MAINT/${phase}-legacy-launchctl-print.txt" 2>&1
    legacy_print_rc=$?
    set -e
    printf '%s\t%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$legacy_print_rc" \
      >> "$MAINT/${phase}-legacy-retire-poll.tsv"
    if [ "$legacy_print_rc" -eq 113 ]; then
      break
    fi
    if [ "$legacy_print_rc" -ne 0 ]; then
      echo "legacy launchd state is unknown: rc=$legacy_print_rc" >&2
      return 1
    fi
    "$CANDIDATE_VENV/bin/python" - \
      "$MAINT/${phase}-legacy-launchctl-print.txt" \
      "$LEGACY_LAUNCHD_RECEIPT" <<'PY' || return 1
import json, pathlib, sys
from agentstack_mail.service import _parse_launchd_record
record = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
receipt = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
path, program, arguments = _parse_launchd_record(record)
definition = receipt["definition"]
assert path == definition["plist_path"]
assert program == definition["program"]
assert arguments == definition["program_arguments"]
PY
    if [ "$(date +%s)" -ge "$legacy_retire_deadline" ]; then
      echo "legacy job did not retire within 60 seconds" >&2
      return 1
    fi
    sleep 0.1
  done

  set +e
  lsof -nP -iTCP:8765 -sTCP:LISTEN \
    > "$MAINT/${phase}-listener-lsof.txt" \
    2> "$MAINT/${phase}-listener-lsof.err"
  listener_lsof_rc=$?
  set -e
  if [ "$listener_lsof_rc" -eq 0 ]; then
    echo "legacy listener is still present" >&2
    return 1
  fi
  if [ "$listener_lsof_rc" -ne 1 ] || \
     [ -s "$MAINT/${phase}-listener-lsof.err" ]; then
    echo "cannot prove legacy listener absence" >&2
    return 1
  fi

  : > "$MAINT/${phase}-db-lsof.txt"
  : > "$MAINT/${phase}-db-lsof.err"
  for path in \
    /Users/operator/mcp_agent_mail/storage.sqlite3 \
    /Users/operator/mcp_agent_mail/storage.sqlite3-wal \
    /Users/operator/mcp_agent_mail/storage.sqlite3-shm; do
    if [ -e "$path" ]; then
      set +e
      lsof -- "$path" \
        >> "$MAINT/${phase}-db-lsof.txt" \
        2>> "$MAINT/${phase}-db-lsof.err"
      db_lsof_rc=$?
      set -e
      if [ "$db_lsof_rc" -eq 0 ]; then
        echo "legacy database still has an open holder: $path" >&2
        return 1
      fi
      if [ "$db_lsof_rc" -ne 1 ]; then
        echo "cannot prove holder absence for: $path" >&2
        return 1
      fi
    fi
  done
  if [ -s "$MAINT/${phase}-db-lsof.err" ]; then
    echo "legacy database holder probe wrote stderr" >&2
    return 1
  fi
}

assert_new_writer_absent() {
  phase="$1"
  set +e
  launchctl print "gui/$(id -u)/org.agentstack.mail" \
    > "$MAINT/${phase}-new-launchctl-print.txt" 2>&1
  new_print_rc=$?
  set -e
  test "$new_print_rc" -eq 113 || return 1
  set +e
  lsof -nP -iTCP:18765 -sTCP:LISTEN \
    > "$MAINT/${phase}-new-listener-lsof.txt" \
    2> "$MAINT/${phase}-new-listener-lsof.err"
  new_listener_rc=$?
  set -e
  test "$new_listener_rc" -eq 1 || return 1
  test ! -s "$MAINT/${phase}-new-listener-lsof.err" || return 1
  : > "$MAINT/${phase}-new-db-lsof.txt"
  : > "$MAINT/${phase}-new-db-lsof.err"
  for path in \
    /Users/operator/.agentstack/mail/storage.sqlite3 \
    /Users/operator/.agentstack/mail/storage.sqlite3-wal \
    /Users/operator/.agentstack/mail/storage.sqlite3-shm; do
    if [ -e "$path" ]; then
      set +e
      lsof -- "$path" \
        >> "$MAINT/${phase}-new-db-lsof.txt" \
        2>> "$MAINT/${phase}-new-db-lsof.err"
      new_db_lsof_rc=$?
      set -e
      test "$new_db_lsof_rc" -eq 1 || return 1
    fi
  done
  test ! -s "$MAINT/${phase}-new-db-lsof.err" || return 1
}

capture_legacy_state_snapshot() {
  output="$1"
  test ! -e "$output" || return 1
  "$CANDIDATE_VENV/bin/python" - "$output" <<'PY'
import os, pathlib, sys
from agentstack_mail.migration import (
    ARCHIVE_EXCLUDED_ROOT_NAMES,
    _canonical_json,
    snapshot_database,
    snapshot_tree,
)
output = pathlib.Path(os.path.abspath(os.path.expanduser(sys.argv[1])))
state = {
    "kind": "legacy-authority-state",
    "database": snapshot_database(
        pathlib.Path("/Users/operator/mcp_agent_mail/storage.sqlite3")
    ),
    "archive": snapshot_tree(
        pathlib.Path("/Users/operator/.mcp_agent_mail_git_mailbox_repo"),
        required=True,
        excluded_root_names=ARCHIVE_EXCLUDED_ROOT_NAMES,
    ),
    "signals": snapshot_tree(
        pathlib.Path("/Users/operator/.mcp_agent_mail/signals"),
        required=False,
    ),
}
payload = _canonical_json(state)
descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
with os.fdopen(descriptor, "wb") as stream:
    stream.write(payload)
    stream.flush()
    os.fsync(stream.fileno())
directory = os.open(output.parent, os.O_RDONLY)
try:
    os.fsync(directory)
finally:
    os.close(directory)
PY
}

assert_service_state() {
  python3 - "$1" "$2" "$3" <<'PY'
import json, pathlib, sys
p = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert p["status"] == sys.argv[2]
assert p["owned"] is True
assert p["label"] == "org.agentstack.mail"
if "environment_drift" in p:
    assert p["environment_drift"] is False
if sys.argv[3] != "-":
    assert p["action"] == sys.argv[3]
if sys.argv[3] == "started":
    preflight = p["bootstrap_preflight"]
    assert preflight["status"] == "stopped"
    assert preflight["owned"] is True
    assert preflight["launchctl_print_returncode"] == 113
    assert preflight["launchctl_print_state"] == "absent"
    if p["bootstrap_outcome"] == "loaded":
        assert p["bootstrap_eio_recheck"] is None
    elif p["bootstrap_outcome"] == "exact_job_already_loaded_after_eio":
        recheck = p["bootstrap_eio_recheck"]
        assert recheck["status"] == "job_loaded"
        assert recheck["owned"] is True
        assert recheck["launchctl_print_returncode"] == 0
        assert recheck["launchctl_print_state"] == "loaded"
    else:
        raise AssertionError("unsupported bootstrap outcome")
elif sys.argv[3] == "stopped":
    wait = p["stop_wait"]
    assert wait["poll_count"] >= 1
    assert wait["bounded_stopped_ms"] >= 0
    assert wait["deadline_seconds"] == 30.0
PY
}

assert_rollback_state() {
  python3 - "$1" "$2" <<'PY'
import json, pathlib, sys
p = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert p["status"] == "reversible"
assert p["cutover_stage"] == sys.argv[2]
assert p["cutover_stage_provenance"] == "caller_asserted_unverified"
assert p["source_matches_baseline"] is True
assert p["destination_matches_baseline"] is True
assert p["data_reversible"] is True
assert p["source_verification_error"] is None
assert p["destination_verification_error"] is None
assert p["service_and_client_state_requires_external_verification"] is True
PY
}

assert_rollback_no_go() {
  python3 - "$1" "$2" "$3" <<'PY'
import json, pathlib, sys
p = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert p["status"] == "no_go"
assert p["cutover_stage"] == sys.argv[2]
assert p["cutover_stage_provenance"] == "caller_asserted_unverified"
if sys.argv[3] == "fresh":
    assert p["source_matches_baseline"] is True
    assert p["destination_matches_baseline"] is True
elif sys.argv[3] != "any":
    raise AssertionError("unsupported baseline expectation")
assert p["data_reversible"] is False
assert p["service_and_client_state_requires_external_verification"] is True
assert p["reason"] == (
    "caller asserted C6, which is at or beyond the first durable new-authority "
    "write boundary; rollback is fix-forward-only even if both snapshots still "
    "equal the migration baseline"
)
assert p["actions"] == [
    "keep all consumers quiesced and keep the legacy service stopped",
    "stop and inspect only the exact owned new job",
    "repair the new authority in place and start only that exact owned new job",
    "require bounded MCP readiness before resuming consumers",
    "if the new job cannot become ready, start neither authority and enter incident/no-writer state",
]
PY
}

assert_consumer_state() {
  python3 - "$1" "$2" <<'PY'
import json, pathlib, sys
p = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert p["status"] == sys.argv[2]
assert p["receipt_invalid"] is False
assert p["third"] == 0
if sys.argv[2] == "committed":
    assert p["committed_receipt"] is True
    assert p["before"] == 0
elif sys.argv[2] == "rolled_back":
    assert p["rolled_back_receipt"] is True
    assert p["after"] == 0
else:
    raise AssertionError("unsupported expected consumer state")
PY
}
```

#### C0A: isolated restore rehearsal evidenceを先に作る

これは本番sourceを開かず、service/config/authorityを変えない唯一のpre-readiness操作である。candidate checkoutはcleanなexact HEADで、実行中の`migration.py` bytesもそのcommit blobと一致しなければrunnerが失敗する。schema v1のprovenanceは現在`production-shaped-synthetic`だけを許可する。caller-authored JSONだけで`production-read-only-clone`を名乗ることは禁止し、clone captureは専用capture receiptが実装されるまで未対応である。

versionedな`packages/agentstack_mail/scripts/build_rehearsal_seed.py`はseed DB、少なくとも1 fileを持つarchive、signals、generator receipt、次のexact 7-field provenance JSONを一つのsibling staging generationで作り、fsync後にdirectory renameで公開する。実行scriptと`migration.py`のbytesをclean candidateのblobへ束縛し、本番source pathは記録するだけでopenしない。`created_at`はUTC、両DB pathはcanonical absolute、`source_reference`にはgeneratorのcandidate SHAとinvocation receiptを入れる。free-form provenanceは由来の証明にならないため、syntheticであることと、runner/verifierがraw artifactから再計算する規模だけを主張する。合格floorはdatabase family 50 MiB、agents 700、messages 8,000、message_recipients 8,000である。

```json
{
  "schema_version": 1,
  "kind": "production-shaped-synthetic",
  "created_at": "2026-08-11T00:00:00+00:00",
  "seed_database": "/Users/operator/.agentstack/cutover-maintenance/rehearsal-seed/legacy/storage.sqlite3",
  "production_source_database": "/Users/operator/mcp_agent_mail/storage.sqlite3",
  "acquisition_method": "deterministic candidate-bound synthetic generator",
  "source_reference": "candidate SHA + generator invocation receipt"
}
```

seed生成後、次の順序でseed自身のC3 manifest、4状態raw evidence、write-once verifier receiptを作る。`REHEARSAL_RUN`は開始前に存在してはならない。runner stdoutから得たreceipt SHA/run ID/candidateをrun directory外へpinしてから、別processのverifierを起動する。

```sh
CANDIDATE_COMMIT=$(git -C "$REPO" rev-parse --verify 'HEAD^{commit}')
test -z "$(git -C "$REPO" status --porcelain)"
candidate_migrate() {
  PYTHONPATH="$REPO/packages/agentstack_mail/src" \
    python3 -m agentstack_mail.migration "$@"
}
test ! -e "$REHEARSAL_SEED_ROOT" && test ! -L "$REHEARSAL_SEED_ROOT"
PYTHONPATH="$REPO/packages/agentstack_mail/src" \
python3 "$REPO/packages/agentstack_mail/scripts/build_rehearsal_seed.py" \
  --output-root "$REHEARSAL_SEED_ROOT" \
  --production-source-db /Users/operator/mcp_agent_mail/storage.sqlite3 \
  --candidate-repo "$REPO" \
  --candidate-commit "$CANDIDATE_COMMIT" \
  > "$MAINT/c0-rehearsal-seed-generate.json" || exit 1
python3 - "$MAINT/c0-rehearsal-seed-generate.json" \
  "$REHEARSAL_SEED_ROOT/generator-receipt.json" "$CANDIDATE_COMMIT" <<'PY'
import hashlib, json, pathlib, re, sys
command, receipt_path, candidate = sys.argv[1:]
p = json.loads(pathlib.Path(command).read_text(encoding="utf-8"))
raw = pathlib.Path(receipt_path).read_bytes()
receipt = json.loads(raw)
assert p["status"] == "generated"
assert p["candidate_commit"] == candidate == receipt["candidate_commit"]
assert p["generator_receipt"] == receipt_path
assert p["generator_receipt_sha256"] == hashlib.sha256(raw).hexdigest()
assert p["seed_database_size"] >= 50 * 1024 * 1024
assert p["major_table_rows"]["agents"] >= 700
assert p["major_table_rows"]["messages"] >= 8_000
assert p["major_table_rows"]["message_recipients"] >= 8_000
assert receipt["production_source_opened"] is False
assert re.fullmatch(r"[0-9a-f]{64}", receipt["seed_database_sha256"])
PY
shasum -a 256 "$REHEARSAL_SEED_ROOT/generator-receipt.json" \
  > "$MAINT/rehearsal-seed-generator-receipt.sha256"
REHEARSAL_GENERATOR_SHA256=$(python3 -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["generator_receipt_sha256"])' \
  "$MAINT/c0-rehearsal-seed-generate.json")
test -f "$REHEARSAL_SEED_DB" && test ! -L "$REHEARSAL_SEED_DB"
test -d "$REHEARSAL_SEED_ARCHIVE" && test ! -L "$REHEARSAL_SEED_ARCHIVE"
test -d "$REHEARSAL_SEED_SIGNALS" && test ! -L "$REHEARSAL_SEED_SIGNALS"
test -f "$REHEARSAL_PROVENANCE" && test ! -L "$REHEARSAL_PROVENANCE"
test ! -e "$REHEARSAL_COPY_ROOT" && test ! -L "$REHEARSAL_COPY_ROOT"
test ! -e "$REHEARSAL_RUN" && test ! -L "$REHEARSAL_RUN"

python3 - "$REHEARSAL_PROVENANCE" "$REHEARSAL_SEED_DB" <<'PY'
from datetime import datetime, timezone
import json, pathlib, sys
path, seed = map(pathlib.Path, sys.argv[1:])
p = json.loads(path.read_text(encoding="utf-8"))
assert set(p) == {
    "schema_version", "kind", "created_at", "seed_database",
    "production_source_database", "acquisition_method", "source_reference",
}
assert p["schema_version"] == 1
assert p["kind"] == "production-shaped-synthetic"
assert p["seed_database"] == str(seed)
assert p["production_source_database"] == "/Users/operator/mcp_agent_mail/storage.sqlite3"
assert p["acquisition_method"] == "deterministic candidate-bound synthetic generator"
assert p["source_reference"]
created = datetime.fromisoformat(p["created_at"])
assert created.utcoffset() == timezone.utc.utcoffset(created)
PY

candidate_migrate copy \
  --source-db "$REHEARSAL_SEED_DB" \
  --source-archive "$REHEARSAL_SEED_ARCHIVE" \
  --source-signals "$REHEARSAL_SEED_SIGNALS" \
  --destination-root "$REHEARSAL_COPY_ROOT" \
  > "$MAINT/c0-rehearsal-seed-copy.json" || exit 1
candidate_migrate verify \
  --source-db "$REHEARSAL_SEED_DB" \
  --source-archive "$REHEARSAL_SEED_ARCHIVE" \
  --source-signals "$REHEARSAL_SEED_SIGNALS" \
  --destination-root "$REHEARSAL_COPY_ROOT" \
  > "$MAINT/c0-rehearsal-seed-verify.json" || exit 1

candidate_migrate rollback-assess \
  --manifest "$REHEARSAL_MANIFEST" \
  --cutover-stage C5_CLIENT_SWITCHING \
  > "$MAINT/c0-boundary-c5.json" || exit 1
assert_rollback_state "$MAINT/c0-boundary-c5.json" C5_CLIENT_SWITCHING
set +e
candidate_migrate rollback-assess \
  --manifest "$REHEARSAL_MANIFEST" \
  --cutover-stage C6_NEW_AUTHORITY_VERIFIED \
  > "$MAINT/c0-boundary-C6_NEW_AUTHORITY_VERIFIED.json" \
  2> "$MAINT/c0-boundary-C6_NEW_AUTHORITY_VERIFIED.err"
BOUNDARY_RC=$?
set -e
test "$BOUNDARY_RC" -eq 1
test ! -s "$MAINT/c0-boundary-C6_NEW_AUTHORITY_VERIFIED.err"
assert_rollback_no_go \
  "$MAINT/c0-boundary-C6_NEW_AUTHORITY_VERIFIED.json" \
  C6_NEW_AUTHORITY_VERIFIED fresh
set +e
candidate_migrate rollback-assess \
  --manifest "$REHEARSAL_MANIFEST" --cutover-stage C5_TO_C6 \
  > "$MAINT/c0-boundary-unknown.out" 2> "$MAINT/c0-boundary-unknown.err"
UNKNOWN_RC=$?
candidate_migrate rollback-assess \
  --manifest "$REHEARSAL_MANIFEST" \
  > "$MAINT/c0-boundary-omitted.out" 2> "$MAINT/c0-boundary-omitted.err"
OMITTED_RC=$?
set -e
test "$UNKNOWN_RC" -eq 2 && test ! -s "$MAINT/c0-boundary-unknown.out"
grep -q 'invalid choice' "$MAINT/c0-boundary-unknown.err"
test "$OMITTED_RC" -eq 2 && test ! -s "$MAINT/c0-boundary-omitted.out"
grep -q 'required' "$MAINT/c0-boundary-omitted.err"

candidate_migrate cold-restore-rehearse \
  --seed-db "$REHEARSAL_SEED_DB" \
  --production-source-db /Users/operator/mcp_agent_mail/storage.sqlite3 \
  --run-dir "$REHEARSAL_RUN" \
  --migration-manifest "$REHEARSAL_MANIFEST" \
  --candidate-repo "$REPO" \
  --candidate-commit "$CANDIDATE_COMMIT" \
  --seed-provenance "$REHEARSAL_PROVENANCE" \
  --generator-receipt "$REHEARSAL_SEED_ROOT/generator-receipt.json" \
  --expected-generator-receipt-sha256 "$REHEARSAL_GENERATOR_SHA256" \
  > "$MAINT/c0-restore-rehearsal-command.json" || exit 1

REHEARSAL_RUN_ID=$(python3 -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["run_id"])' \
  "$MAINT/c0-restore-rehearsal-command.json")
REHEARSAL_RECEIPT_SHA256=$(python3 -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["rehearsal_receipt_sha256"])' \
  "$MAINT/c0-restore-rehearsal-command.json")
python3 - "$MAINT/c0-restore-rehearsal-command.json" \
  "$MAINT/c0-rehearsal-seed-generate.json" "$CANDIDATE_COMMIT" \
  "$REHEARSAL_RUN" "$REHEARSAL_PINS.runner" <<'PY'
import json, pathlib, re, sys
command, generator_command, candidate, run_dir, output = sys.argv[1:]
p = json.loads(pathlib.Path(command).read_text(encoding="utf-8"))
generator = json.loads(pathlib.Path(generator_command).read_text(encoding="utf-8"))
assert p["status"] == "completed"
assert p["candidate_commit"] == candidate
assert p["rehearsal_receipt"] == f"{run_dir}/cold-restore-rehearsal-receipt.json"
assert re.fullmatch(r"[0-9a-f]{64}", p["rehearsal_receipt_sha256"])
pathlib.Path(output).write_text(json.dumps({
    "run_id": p["run_id"],
    "candidate_commit": candidate,
    "generator_receipt_sha256": generator["generator_receipt_sha256"],
    "rehearsal_receipt_sha256": p["rehearsal_receipt_sha256"],
}, sort_keys=True) + "\n", encoding="utf-8")
PY

candidate_migrate cold-restore-rehearsal-verify \
  --receipt "$REHEARSAL_RUN/cold-restore-rehearsal-receipt.json" \
  --verification-receipt "$REHEARSAL_RUN/cold-restore-rehearsal-verification.json" \
  --expected-receipt-sha256 "$REHEARSAL_RECEIPT_SHA256" \
  --expected-run-id "$REHEARSAL_RUN_ID" \
  --expected-candidate-commit "$CANDIDATE_COMMIT" \
  > "$MAINT/c0-restore-rehearsal-verify.json" || exit 1
REHEARSAL_VERIFICATION_SHA256=$(python3 -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["verification_receipt_sha256"])' \
  "$MAINT/c0-restore-rehearsal-verify.json")

python3 - "$MAINT/c0-restore-rehearsal-verify.json" \
  "$REHEARSAL_PINS.runner" "$REHEARSAL_PINS" <<'PY'
import json, pathlib, re, sys
p = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
runner = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
assert p["status"] == "verified"
assert p["raw_artifact_count"] == 4
assert p["damage_control"] == "physical_and_logical_non_noop"
assert re.fullmatch(r"[0-9a-f]{64}", p["verification_receipt_sha256"])
assert runner["run_id"] == p["run_id"]
assert runner["candidate_commit"] == p["candidate_commit"]
assert runner["rehearsal_receipt_sha256"] == p["rehearsal_receipt_sha256"]
pathlib.Path(sys.argv[3]).write_text(json.dumps({
    "run_id": p["run_id"],
    "candidate_commit": p["candidate_commit"],
    "generator_receipt_sha256": runner["generator_receipt_sha256"],
    "rehearsal_receipt_sha256": p["rehearsal_receipt_sha256"],
    "verification_receipt_sha256": p["verification_receipt_sha256"],
}, sort_keys=True) + "\n", encoding="utf-8")
PY

candidate_migrate cold-restore-rehearsal-verify \
  --receipt "$REHEARSAL_RUN/cold-restore-rehearsal-receipt.json" \
  --verification-receipt "$REHEARSAL_RUN/cold-restore-rehearsal-verification.json" \
  --expected-receipt-sha256 "$REHEARSAL_RECEIPT_SHA256" \
  --expected-verification-receipt-sha256 "$REHEARSAL_VERIFICATION_SHA256" \
  --expected-run-id "$REHEARSAL_RUN_ID" \
  --expected-candidate-commit "$CANDIDATE_COMMIT" \
  --check-only \
  > "$MAINT/c0-restore-rehearsal-check-only.json" || exit 1
python3 - "$MAINT/c0-restore-rehearsal-check-only.json" <<'PY'
import json, pathlib, sys
p = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert p["status"] == "verified_check_only"
assert p["raw_artifact_count"] == 4
assert p["damage_control"] == "physical_and_logical_non_noop"
PY
```

runnerはsource/backup/damaged/restoredの4 raw familyを同一run IDへ束縛し、built-in damageがmainを実際に変え、backup時ABSENTだったsidecarを作って除去branchを通したことを要求する。no-op damage、restore skip、PRESENT replace skip、ABSENT unlink skipは各mutation testで赤くなる。raw artifact、terminal receipt、separate verifier receipt、run directory外の3 SHA pinは、将来の data-migration-reconciliation handlerへの入力として保持する。rollback-revert-procedure は同じartifactを別名で再登録せず、C3–C5/R1–R5のfirst durable write前だけを再計算する独立handlerと独立recordを必要とする。post-authority reverse transformは別のnon-blocking post-cutover taskであり、このconditionへ混ぜない。最終booleanだけはevidenceに数えない。

canonical rehearsal receiptが無い、command rc0を観測できない、`.prepared`/`.unconfirmed`/ownership markerだけが残る、または初回verifier/check-onlyのどちらかが失敗した場合は未完了である。prepared/unconfirmedを手動renameしない。receipt内の`fsync`/`atomic_replace`という文字列は自己証明ではなく、それらはcode pathとEIO fault testでのみ照合する。producerが実際に走ったことの暗号学的証明ではなく、保持raw artifactsから独立再計算できるところが保証の上限である。

### 現行v1の停止点（normative）

現在のversioned manifestでは data-migration-reconciliation と rollback-revert-procedure がともに`unimplemented_v1`である。この状態ではartifactやindexへ何も書かず、非0で停止する。両conditionのversioned handlerと別々のevidence recordが実装され、readiness evaluatorが両recordを再計算して受理するまで次へ進まない。

### handler実装後のfuture skeleton（現行では実行禁止）

次のproducerは、将来 data-migration-reconciliation 用のversioned handlerが実装された後に、rehearsalの3つの外部pinとcanonical raw evidenceから同条件の1 recordだけを作るためのskeletonである。現在の`unimplemented_v1`では先頭で意図的に非0となる。これは rollback-revert-procedure のproducer/handlerを実装せず、未知の将来`evidence_kind`も固定していないため、現行の実行可能契約ではない。handler実装後もcondition IDを増やさず、review済みのversioned `evidence_kind`と同じ値だけを受け入れる。

```sh
python3 - "$CUTOVER_MANIFEST" "$CANDIDATE_COMMIT" "$REHEARSAL_RUN" \
  "$REHEARSAL_PINS" "$EVIDENCE_ROOT" "$EVIDENCE_INDEX" <<'PY'
import hashlib, json, os, pathlib, sys, tempfile
manifest_path, candidate, run_arg, pins_arg, root_arg, index_arg = sys.argv[1:]
manifest_path = pathlib.Path(manifest_path)
run = pathlib.Path(run_arg)
pins_path = pathlib.Path(pins_arg)
root = pathlib.Path(root_arg)
index_path = pathlib.Path(index_arg)
manifest_bytes = manifest_path.read_bytes()
manifest = json.loads(manifest_bytes)
condition = next(c for c in manifest["cutover_gate"]["conditions"]
                 if c["id"] == "data-migration-reconciliation")
kind = condition["evidence_kind"]
if kind in {"none", "unimplemented_v1"}:
    raise SystemExit("data-migration-reconciliation evidence handler is not implemented")
pins = json.loads(pins_path.read_text(encoding="utf-8"))
receipt = run / "cold-restore-rehearsal-receipt.json"
verification = run / "cold-restore-rehearsal-verification.json"
sealed_generator = run / "identities" / "generator-receipt.json"
for value in pins.values():
    if not isinstance(value, str):
        raise SystemExit("rehearsal pin envelope is malformed")
assert pins["candidate_commit"] == candidate
assert hashlib.sha256(receipt.read_bytes()).hexdigest() == pins["rehearsal_receipt_sha256"]
assert hashlib.sha256(verification.read_bytes()).hexdigest() == pins["verification_receipt_sha256"]
assert hashlib.sha256(sealed_generator.read_bytes()).hexdigest() == pins["generator_receipt_sha256"]
relative = pathlib.Path("data-migration-reconciliation/rehearsal-binding.json")
artifact = root / relative
payload = {
    "schema_version": 1,
    "candidate_commit": candidate,
    "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    "run_id": pins["run_id"],
    "generator_receipt": str(sealed_generator.relative_to(root)),
    "generator_receipt_sha256": pins["generator_receipt_sha256"],
    "rehearsal_receipt": str(receipt.relative_to(root)),
    "rehearsal_receipt_sha256": pins["rehearsal_receipt_sha256"],
    "verification_receipt": str(verification.relative_to(root)),
    "verification_receipt_sha256": pins["verification_receipt_sha256"],
}
artifact.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
artifact_sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
definition_sha = hashlib.sha256(json.dumps(
    condition, sort_keys=True, separators=(",", ":"), ensure_ascii=False
).encode()).hexdigest()
if index_path.exists():
    index = json.loads(index_path.read_text(encoding="utf-8"))
else:
    index = {
        "schema_version": 1,
        "candidate_commit": candidate,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "artifacts": [],
    }
assert index["candidate_commit"] == candidate
assert index["manifest_sha256"] == hashlib.sha256(manifest_bytes).hexdigest()
record = {
    "condition_id": condition["id"],
    "definition_sha256": definition_sha,
    "kind": kind,
    "path": relative.as_posix(),
    "sha256": artifact_sha,
}
prior = [r for r in index["artifacts"] if r["condition_id"] == condition["id"]]
if prior and prior != [record]:
    raise SystemExit("conflicting data-migration-reconciliation evidence already indexed")
if not prior:
    index["artifacts"].append(record)
fd, temporary = tempfile.mkstemp(prefix=".evidence-index-", dir=index_path.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(index, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, index_path)
    parent = os.open(index_path.parent, os.O_RDONLY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)
finally:
    pathlib.Path(temporary).unlink(missing_ok=True)
PY
```

1. 将来版で data-migration-reconciliation と rollback-revert-procedure のversioned handlerおよび別々のevidence recordが実装され、両recordを含むindexをreadiness evaluatorが再計算して受理できた場合にだけ、clean checkoutでfull candidate commit、exact manifest、digest-verified evidenceを次のexact commandへ渡す。現行v1ではここへ進まない。一つでも違えば後続のhuman確認やsmoke testで上書きせず、C0で止まる。

```sh
CANDIDATE_COMMIT=$(git -C "$REPO" rev-parse --verify 'HEAD^{commit}')
python3 "$READINESS" \
  --candidate-commit "$CANDIDATE_COMMIT" \
  --manifest "$CUTOVER_MANIFEST" \
  --evidence "$EVIDENCE_INDEX" \
  --evidence-root "$EVIDENCE_ROOT" \
  > "$MAINT/c0-readiness.json" || exit 1
python3 - "$MAINT/c0-readiness.json" <<'PY'
import json, pathlib, sys
p = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert p["evaluation_state"] == "valid"
assert p["cutover_state"] == "go"
assert p["condition_count"] == 14
assert len(p["passed_condition_ids"]) == 14
assert len(set(p["passed_condition_ids"])) == 14
assert p["missing_conditions"] == []
assert p["invalid_reasons"] == []
PY
test -f "$LEGACY_PLIST" && test ! -L "$LEGACY_PLIST"
test "$(stat -f '%l' "$LEGACY_PLIST")" -eq 1
plutil -lint "$LEGACY_PLIST"
python3 - "$LEGACY_PLIST" <<'PY'
import pathlib, plistlib, sys
with pathlib.Path(sys.argv[1]).open("rb") as handle:
    plist = plistlib.load(handle)
assert plist["Label"] == "com.operator.mcp-agent-mail"
assert plist["WorkingDirectory"] == "/Users/operator/mcp_agent_mail"
assert plist["ProgramArguments"] == [
    "/bin/bash",
    "/Users/operator/mcp_agent_mail/scripts/run_server_with_token.sh",
]
PY
shasum -a 256 "$LEGACY_PLIST" > "$MAINT/legacy-plist.sha256"
```

`passed_condition_ids`の集合と順序は台帳の`cutover_gate.required_condition_ids`とexact一致することもmaintenance記録へsealする。旧jobを戻す可能性があるR1–R5では、bootstrap直前に必ず`shasum -a 256 -c "$MAINT/legacy-plist.sha256"`を再実行する。
2. baseline開始方法はmaintainer裁定済みの **A（copied filesを1 baseline commitにする）** をversioned migration inputとmaintenance記録へ固定する。選択のcandidate/evidence bindingがmachine gateに含まれることも確認する。
3. working-tree scope migration、bounded MCP readiness probe、上のOrrery/dashboard互換変更とlive hooks変更が実装・検証済みであることを確認する。`agentstack-mail-consumers` は確定artifact上でcopy-only rehearsalが通ることを確認する。一つでも未実装ならここで止まる。
4. distribution-artifact-release-gate がindexへ束縛するのは現在wheel/sdistまでで、transitive dependency closureとinterpreter identityはまだ束縛しない。したがって現行v1は専用venvを作る前でNO-GOであり、以下はfuture-only skeletonとしてもそのまま実行しない。将来のdistribution/install evidenceは、interpreter identity、hash-lockされたrequirements、全dependency wheelのname/SHAを持つsealed wheelhouse manifest、install receiptを同じcandidateへ束縛する。installerは`--no-index --find-links <sealed-wheelhouse> --require-hashes -r <sealed-lock>`でcandidate wheelをlock内から導入し、`pip check`を通してからrenderへ進む。live `~/Library/LaunchAgents`ではないstaging directoryだけを使い、bare PATH entrypointやlaunchctlは使わない。

```sh
CANDIDATE_WHEEL=$(python3 - "$EVIDENCE_INDEX" "$EVIDENCE_ROOT" <<'PY'
import hashlib, json, pathlib, sys
index = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
root = pathlib.Path(sys.argv[2]).resolve()
record = next(r for r in index["artifacts"]
              if r["condition_id"] == "distribution-artifact-release-gate")
report_path = (root / record["path"]).resolve()
assert report_path == root or root in report_path.parents
assert hashlib.sha256(report_path.read_bytes()).hexdigest() == record["sha256"]
report = json.loads(report_path.read_text(encoding="utf-8"))
wheel = (root / report["wheel"]["path"]).resolve()
assert wheel == root or root in wheel.parents
assert hashlib.sha256(wheel.read_bytes()).hexdigest() == report["wheel"]["sha256"]
print(wheel)
PY
)
# FUTURE ONLY: sealed lock/wheelhouse evidence is not implemented in v1.
test -n "${SEALED_WHEELHOUSE:-}" && test -d "$SEALED_WHEELHOUSE" || exit 1
test -n "${SEALED_LOCK:-}" && test -f "$SEALED_LOCK" || exit 1
test ! -e "$CANDIDATE_VENV"
python3 -m venv "$CANDIDATE_VENV"
"$CANDIDATE_VENV/bin/python" -m pip install --disable-pip-version-check \
  --no-index --find-links "$SEALED_WHEELHOUSE" --require-hashes \
  -r "$SEALED_LOCK" > "$MAINT/c1-pip-install.txt" || exit 1
"$CANDIDATE_VENV/bin/python" -m pip check \
  > "$MAINT/c1-pip-check.txt" || exit 1
for executable in \
  "$MIGRATE_BIN" "$SERVICE_BIN" "$CONSUMERS_BIN" "$SERVER_BIN" "$EVIDENCE_BIN"; do
  test -x "$executable"
done
install -d -m 700 "$MAINT/render"
umask 077
cat > "$NEW_ENV" <<'EOF'
AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE=passthrough
AGENTSTACK_MAIL_HTTP_HOST=127.0.0.1
AGENTSTACK_MAIL_HTTP_PORT=18765
AGENTSTACK_MAIL_HTTP_PATH=/mcp
AGENTSTACK_MAIL_DATABASE_URL=sqlite+aiosqlite:////Users/operator/.agentstack/mail/storage.sqlite3
AGENTSTACK_MAIL_STORAGE_ROOT=/Users/operator/.agentstack/mail/archive
AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR=/Users/operator/.agentstack/mail/signals
EOF
"$SERVICE_BIN" render \
  --output-dir "$MAINT/render" \
  --service-executable "$SERVICE_BIN" \
  --server-executable "$SERVER_BIN" \
  --env-file "$NEW_ENV" \
  --state-root "$NEW_STATE_ROOT" \
  > "$MAINT/c1-service-render.json" || exit 1
python3 - "$MAINT/c1-service-render.json" "$NEW_OWNERSHIP" <<'PY'
import json, pathlib, sys
result = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert result["status"] in {"rendered", "noop"}
assert result["ownership_manifest"] == sys.argv[2]
assert pathlib.Path(sys.argv[2]).is_file()
PY
```

旧jobがまだliveで、新candidate labelが未loadのこの時点に、戻し先のloaded定義とplist bytesをexact candidateへ束縛して1回だけsealする。producerは`com.operator.mcp-agent-mail`のloaded path/ProgramArgumentsをplistとread-onlyで照合し、plistの完全bytes、KeepAlive、RunAtLoad、WorkingDirectoryを保存し、wrapper PIDと8765 listener PIDの親子関係を証明する。8765へnetwork requestは送らない。出力はwrite-once 0400で、旧job不在、listenerがexact 1でない、plist/loaded record不一致、新labelが既にload、candidate/wheel不一致なら公開しない。

```sh
test ! -e "$LEGACY_LAUNCHD_RECEIPT"
"$EVIDENCE_BIN" legacy-launchd-snapshot \
  --output "$LEGACY_LAUNCHD_RECEIPT" \
  --wheel "$CANDIDATE_WHEEL" \
  --candidate-repo "$REPO" \
  --candidate-commit "$CANDIDATE_COMMIT" \
  > "$MAINT/c1-legacy-launchd-snapshot.json" || exit 1
test "$(stat -f '%Lp' "$LEGACY_LAUNCHD_RECEIPT")" = 400
shasum -a 256 "$LEGACY_LAUNCHD_RECEIPT" \
  > "$MAINT/legacy-launchd-definition-v1.sha256"
python3 - "$LEGACY_LAUNCHD_RECEIPT" "$CANDIDATE_COMMIT" "$LEGACY_PLIST" <<'PY'
import base64, json, pathlib, sys
receipt = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert receipt["kind"] == "legacy-launchd-definition"
assert receipt["cutover_eligible"] is True
assert receipt["candidate_commit"] == sys.argv[2]
assert receipt["definition"]["label"] == "com.operator.mcp-agent-mail"
assert receipt["definition"]["state"] == "loaded"
assert receipt["definition"]["plist_path"] == sys.argv[3]
assert receipt["definition"]["loaded_path_program_arguments_match_plist"] is True
assert base64.b64decode(receipt["definition"]["plist_bytes_base64"]) == pathlib.Path(
    sys.argv[3]
).read_bytes()
assert receipt["runtime"]["listener_port"] == 8765
assert receipt["runtime"]["listener_is_wrapper_child"] is True
assert receipt["runtime"]["network_requests_sent"] == 0
assert receipt["new_candidate_label"]["state"] == "absent"
PY
```

5. 旧 launchd plistとrollback定義は上で保存済みである。旧 DB/archive/signals のauthoritative fingerprintはactive writer下のC0値を基準にせず、**C2で旧job、8765、DB holderを止めた後に一度だけseal**する。C5とR1はそのquiesced bytesを再生成してexact比較する。ここでは全consumerをtyped inventoryとして列挙する。**file採取は`agentstack-mail-consumer-inventory`の一つのbounded runで一度だけ行い、開始/終了時刻、path、count、digestを同じsealへ入れる。CLIのliteral `--hidden --no-ignore` を両方必須とし、(a)既知のselectorがhitする検索liveness対照と、(b)既定でignoreされる既知pathが列挙されるcompleteness対照を別々に通す。** collectorはGit ignoreを参照せず、片方のflag/control欠落、曖昧rule、途中変化、上限超過ではbundleを公開しない。ただし現行v1が封じるのはfile consumerだけで、同じ瞬間のtmux session・稼働/停止child・reservation状態を一つの正本へ結合するorchestrationと、rule集合のoperator承認は未実装である。したがって手書き一覧やfile seal単独で代用せず、overall gateはNO-GOのままにする。実装済みfile collectorのexact commandは次であり、0600/0400のbefore/after bundleにはその出力inventoryだけを渡す。

```sh
INVENTORY_BIN="$CANDIDATE_VENV/bin/agentstack-mail-consumer-inventory"
INVENTORY_SNAPSHOT="$MAINT/consumer-inventory-snapshot"
"$INVENTORY_BIN" \
  --spec "$MAINT/consumer-inventory-spec.json" \
  --bundle "$INVENTORY_SNAPSHOT" \
  --hidden \
  --no-ignore \
  --max-files 100000 \
  --deadline-seconds 60 \
  > "$MAINT/c0-consumer-inventory-collect.json" || exit 1
CONSUMER_INVENTORY="$INVENTORY_SNAPSHOT/inventory.json"
test -r "$INVENTORY_SNAPSHOT/seal.json"

"$CONSUMERS_BIN" prepare \
  --inventory "$CONSUMER_INVENTORY" \
  --bundle "$CONSUMER_BUNDLE" \
  > "$MAINT/c0-consumer-prepare.json" || exit 1
PINNED_MANIFEST_SHA256=$(python3 - "$MAINT/c0-consumer-prepare.json" <<'PY'
import json, pathlib, re, sys
p = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert p["status"] == "prepared"
assert p["bundle"] == "/Users/operator/.agentstack/cutover-maintenance/consumer-bundle"
assert re.fullmatch(r"[0-9a-f]{64}", p["manifest_sha256"])
print(p["manifest_sha256"])
PY
)
printf '%s\n' "$PINNED_MANIFEST_SHA256" > "$MAINT/consumer-manifest.sha256"
test "$(wc -l < "$MAINT/consumer-manifest.sha256")" -eq 1
grep -Eq '^[0-9a-f]{64}$' "$MAINT/consumer-manifest.sha256"
```

maintenance shellを再開した場合は`PINNED_MANIFEST_SHA256=$(cat "$MAINT/consumer-manifest.sha256")`で同じexternal pinを復元し、正規表現を再検査する。
6. `agentstack-mail-consumers preview`のcontent-redactedなfile pathとbefore/after line rangeをmaintainerへ提示する。初回切替ではglobal 1件とlocal 15件のClaude settingsにあるpermission/hook selectorを一つも変えないため、これら16 fileはすべて`changed=false`でなければ止まる。未コミットmachine-local観測 `2026-08-11T15:18:49 JST` はallow 68 + hook matcher 2 = 70 selectorであり、採取器の正本値ではない。helperは**列挙したfile内**の複数recognized key、未知endpoint、endpoint/root混在をfailさせるが、key名だけでauthorityを判定しない。inventory外のfileは見えないため、上のhidden/ignored completeness対照を含むsealed inventoryで漏れ0を承認する。旧source tree自身の`09_MCP/mcp-agent-mail/.mcp.json`、`.codex/config.toml`、`.claude/settings.local.json`（最後のfileは旧sourceの`enabledMcpjsonServers=["mcp-agent-mail"]`だけを選ぶ開発用設定）はcutover consumerではないためexact pathで明示excludeし、理由をmaintenance記録へ残す。
7. readinessが`go`でmaintainerがpreviewを承認した後だけ、versioned display patch chainを適用する。これは本番pathへの最初の変更なので、markerが作られる前の失敗では続行せず、現物を検査して旧before-imageへ戻す。全5 patch適用後にJSON、Python AST、repo/live plistを検査し、after digestを保存してからmarkerを作る。

```sh
test ! -e "$MAINT/display-patches.applied"
apply_display_patches || exit 1
python3 - <<'PY'
import ast, json, pathlib
root = pathlib.Path("/Users/operator")
json.loads((root / ".orrery/config.json").read_text(encoding="utf-8"))
for relative in (
    "OSS/orrery/bridge/orrery_backend.py",
    ".claude/tools/agent-dashboard/server.py",
    ".claude/tools/agent-dashboard/graph_data.py",
):
    path = root / relative
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
PY
plutil -lint \
  /Users/operator/.claude/tools/agent-dashboard/com.operator.agentdashboard.plist \
  /Users/operator/Library/LaunchAgents/com.operator.agentdashboard.plist
shasum -a 256 \
  /Users/operator/.orrery/config.json \
  /Users/operator/OSS/orrery/bridge/orrery_backend.py \
  /Users/operator/.claude/tools/agent-dashboard/server.py \
  /Users/operator/.claude/tools/agent-dashboard/graph_data.py \
  /Users/operator/.claude/tools/agent-dashboard/com.operator.agentdashboard.plist \
  /Users/operator/Library/LaunchAgents/com.operator.agentdashboard.plist \
  > "$MAINT/display-patches-after.sha256"
touch "$MAINT/display-patches.applied"
```

8. 全 sender に開始時刻、C3の2–4分見込み、C5 test合格まで無通信が続くことを事前通知する。ProOpus 自身も、停止後は agent-mail を送受信せず同じ maintenance shell だけを使う。
9. 停止時間を計測するT0–T1の全区間は、MacをAC電源へ接続し、蓋を開けたまま実行する。`caffeinate` processの生存だけを物理条件の証明にしない。T0直前・各phase終端・T1直後に、記録したPIDのsleep assertion所有、AC接続、clamshell stateを検査し、別のhash-chain guardでAC/clamshellを毎秒記録する。AC切断、蓋閉じ、guard停止または許容上限を超えるsampling gap、事後power-state logのsleep/wake/clamshell eventが一つでもあれば、切替の成否とは分けて**停止時間の計測を無効**とし、25分予測の検証値には使わない。この環境では蓋閉じsleepがblackbox記録に17,342秒の空白を作り、sleepを除外しなかった監視の緊急警報6通が全て誤報になった実例がある。またlocalの`caffeinate(8)`では`-s` assertionはAC電源時だけ有効である。

### C2A: cold backup前に全 sender と旧 writer を静止する

tmux server 全体を kill しない。Claude/Codex parent・childは agent-mail call を止めて idle、`BiomatterBot`、`SeminarBot`、watcher/hook は停止または送信不能状態にする。全員の停止確認を agent-mail 停止前に済ませる。

**ここからC4のnew readiness完了までは、一つの不可分なwriter handoffである。** 操作上必要なcopy/verifyを間に挟むが、旧bootoutと新bootstrapを別ticket・別operator・別再開点へ分離しない。旧bootout後は全consumerを停止したまま、旧job/8765/旧DB holderの不在を維持し、C4直前に再検査してからnewだけをbootstrapする。途中で通常運用へ戻したり、旧newどちらかを独立に起動したりしない。どこかで失敗した場合は新を起動せず、該当stageのR1–R5へ一続きで移る。

最後の agent-mail 通知を送った後、operator は通常 shell へ移る。**旧labelをbootoutする前に**、新writerが不在であることと、旧loaded definition/runtime topologyがC1でsealしたreceiptから変わっていないことをread-onlyで再確認する。同名foreign jobや再起動でPID/definitionが変わっていれば停止しない。

```sh
assert_new_writer_absent c2-pre || exit 1
shasum -a 256 -c "$MAINT/legacy-launchd-definition-v1.sha256" || exit 1
C2_LEGACY_LAUNCHD_RECEIPT="$MAINT/c2-legacy-prebootout.json"
test ! -e "$C2_LEGACY_LAUNCHD_RECEIPT"
"$EVIDENCE_BIN" legacy-launchd-snapshot \
  --output "$C2_LEGACY_LAUNCHD_RECEIPT" \
  --wheel "$CANDIDATE_WHEEL" \
  --candidate-repo "$REPO" \
  --candidate-commit "$CANDIDATE_COMMIT" \
  > "$MAINT/c2-legacy-prebootout-command.json" || exit 1
python3 - "$LEGACY_LAUNCHD_RECEIPT" "$C2_LEGACY_LAUNCHD_RECEIPT" <<'PY'
import json, pathlib, sys
before, current = (
    json.loads(pathlib.Path(path).read_text(encoding="utf-8")) for path in sys.argv[1:]
)
assert current["candidate_commit"] == before["candidate_commit"]
assert current["wheel"] == before["wheel"]
assert current["definition"] == before["definition"]
assert current["runtime"] == before["runtime"]
assert current["new_candidate_label"]["state"] == "absent"
PY
launchctl bootout "gui/$(id -u)/com.operator.mcp-agent-mail"
```

直後に共通assertionを1回呼ぶ。一つでも失敗したらcopyせず、writerを特定する。同じassertionをC4の新job bootstrap直前にも再実行し、旧job停止から新job開始までを一つのwriter handoffとして閉じる。

```sh
assert_legacy_writer_absent c2-post || exit 1
assert_new_writer_absent c2-post || exit 1

LOCK_HIT=$(find /Users/operator/.mcp_agent_mail_git_mailbox_repo \
  \( -name '*.lock' -o -name '*.lock.owner.json' \) -print -quit) || exit 1
if [ -n "$LOCK_HIT" ]; then
  echo "legacy archive contains a lock artifact" >&2
  exit 1
fi
if [ -e /Users/operator/.mcp_agent_mail_git_mailbox_repo/.git/index.lock ] || \
   [ -L /Users/operator/.mcp_agent_mail_git_mailbox_repo/.git/index.lock ]; then
  echo "legacy archive Git index is locked" >&2
  exit 1
fi
capture_legacy_state_snapshot "$MAINT/legacy-state-quiesced.json" || exit 1
shasum -a 256 "$MAINT/legacy-state-quiesced.json" \
  > "$MAINT/legacy-state-quiesced.sha256"
```

`launchctl print`はexact rc 113だけをstoppedと扱い、権限・query failureなど別のnonzeroを成功に畳まない。これらは既知 writer の停止を示すが、unknown direct filesystem writer の不在を証明しない。そのため migration 自身の6回の source照合も維持する。

### C2B: cold backupを公開し、receiptをsealする

この時点でもmigrationはまだSQLiteを開かない。`cold-backup`はmain / `-wal` / `-shm`の存在集合を前後で固定し、mainを必須、sidecarを`PRESENT`または`ABSENT`として記録する。各`PRESENT` fileを`O_NOFOLLOW`かつsingle-linkでsibling stagingへcopyし、SHA-256、file fsync、sealed raw bundleの別scratch cloneによるschema・全row・関係・PRAGMA検証、receipt fsync、directory fsync、atomic directory publish、parent fsyncまで成功した場合だけ`status=backed_up`を返す。sealed原本そのものはSQLiteで開かない。

```sh
"$MIGRATE_BIN" cold-backup \
  --source-db /Users/operator/mcp_agent_mail/storage.sqlite3 \
  --backup-dir "$COLD_BACKUP_DIR" \
  --services-stopped \
  > "$MAINT/c2-cold-backup.json" || exit 1
python3 - "$MAINT/c2-cold-backup.json" "$COLD_BACKUP_DIR/cold-backup-receipt.json" <<'PY'
import hashlib, json, pathlib, sys
result = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
receipt_bytes = pathlib.Path(sys.argv[2]).read_bytes()
receipt = json.loads(receipt_bytes)
assert result["status"] == "backed_up"
assert result["receipt"] == sys.argv[2]
assert result["receipt_sha256"] == hashlib.sha256(receipt_bytes).hexdigest()
assert result["operation_id"] == receipt["operation_id"]
assert result["logical_sha256"] == receipt["logical_sha256"]
assert receipt["kind"] == "cold-backup"
assert receipt["services_stopped"] == {
    "asserted": True, "provenance": "caller_asserted_unverified"
}
assert receipt["files"]["main"]["state"] == "PRESENT"
assert {receipt["files"][k]["state"] for k in ("wal", "shm")} <= {"PRESENT", "ABSENT"}
PY
shasum -a 256 "$COLD_BACKUP_DIR/cold-backup-receipt.json" \
  > "$MAINT/cold-backup-receipt.sha256"
```

`$MAINT/cold-backup-receipt.sha256`はbundle外のpinであり、R2のrestoreとHappyTeslaの照合が同じbackup identityを見るために保持する。ここで中止する場合はR2へ進み、C3を一度もinvokeしていないことをmaintenance記録へ残す。

### C3: DB + signals + working tree を一単位として複製・検証する

C2Bのmachine-readable cold backup receiptとbundle外SHA-256 pinが揃った後だけ開始する。`cold-backup`の取得成功はrestore可能性の証明ではないため、本番C0の data-migration-reconciliation evidenceには、後述の非production rehearsal receiptも必要である。

working-tree scope migration は次を一つの staging generation 内で行う。

1. cold backup receiptが揃った後、source DBのSQLite writer slotを`mode=rw`の`BEGIN IMMEDIATE`で全copy期間保持し、直後に`query_only=ON`へ固定する。active writerがいれば即failする。別のread-only connectionの`backup()`でcommitted WALを含むcopyを作る。外部process確認直後にwriterが現れるraceを閉じる価値を優先してguardを残す。通常のsnapshot/verifyはwriter slotを取らず、単一read transactionのpoint-in-time snapshotを返す。
2. signals と archive working tree を copyする。legacy `.git` と `server.pid` は対象外。lock artifact、symlink、special file、権限不足、容量不足は fail-closedにする。
3. maintainer の A/B 選択どおり、新 archive に legacy と無関係な新 Git repo を作る。
4. SQLite `integrity_check`、`foreign_key_check`、schema、全 table digestに加え、agent→project、message→project/sender/thread、message→recipient/read/ack、reservation→project/agent、thread membershipを比較する。
5. `source_before`、`staged_state`、`source_after`、`source_final`、finalizer の `source_now` と `destination_now` という既存6回の照合を working-tree scope で維持する。検証回数・粒度を最適化しない。
6. working treeの全 path/content/mode、signals、33 file attachments、選んだGit開始状態を確認する。
7. fsync後、同一 filesystem上の一回のdirectory renameで `~/.agentstack/mail` を公開する。失敗時は部分treeを canonical path に残さない。file descriptor/inode/link/container identity検査で実測したsource差替を拒否するが、same-UIDの非協調filesystem writerを完全な敵対者としては扱わない。destination不在checkとrenameの間のraceも単一operator前提で明記して受け入れ、未実装の`RENAME_EXCL`を安全保証として数えない。

以下が実装済みcommand形である。pathはsymlink componentを含まないcanonical absolute pathだけを使う。**この変更では実行しておらず、14条件がGOになるまで稼働dataへ実行しない。**

```sh
"$MIGRATE_BIN" copy \
  --source-db /Users/operator/mcp_agent_mail/storage.sqlite3 \
  --source-archive /Users/operator/.mcp_agent_mail_git_mailbox_repo \
  --source-signals /Users/operator/.mcp_agent_mail/signals \
  --destination-root /Users/operator/.agentstack/mail

"$MIGRATE_BIN" verify \
  --source-db /Users/operator/mcp_agent_mail/storage.sqlite3 \
  --source-archive /Users/operator/.mcp_agent_mail_git_mailbox_repo \
  --source-signals /Users/operator/.mcp_agent_mail/signals \
  --destination-root /Users/operator/.agentstack/mail
```

manifestは`archive_policy`でworking treeのみ・legacy `.git`/`server.pid`非継承・unrelated single-root baselineを固定し、`database_policy`でmain DBのlogical comparisonとSQLite sidecar除外を明記する。`copy`の正常再実行はwrite-free `noop`、atomic publish直後の中断はowned markerをread-only再検証して`recovered`、異なる既存destinationは上書きせず失敗する。`verify`はowned markerを削除しない。

### C4: 新 service を起動し、read-only readiness を確認する

新jobをbootstrapする**直前**に、旧labelがexact rc 113、8765 listener 0、旧DB family holder 0であることをC2と同じassertionで取り直す。C2時点の古い観測だけでは開始しない。

```sh
assert_legacy_writer_absent c4 || exit 1
assert_new_writer_absent c4 || exit 1

"$SERVICE_BIN" start \
  --ownership-manifest "$NEW_OWNERSHIP" \
  > "$MAINT/c4-start.json" || exit 1

"$SERVICE_BIN" status \
  --ownership-manifest "$NEW_OWNERSHIP" \
  > "$MAINT/c4-status.json" || exit 1
assert_service_state "$MAINT/c4-start.json" job_loaded started
assert_service_state "$MAINT/c4-status.json" job_loaded -
bounded_mail_probe \
  'http://127.0.0.1:18765/mcp' 18765 \
  'sqlite+aiosqlite:////Users/operator/.agentstack/mail/storage.sqlite3' || exit 1
```

`status: job_loaded` は exact plist/program/arguments が loaded という意味だけで、MCP readiness ではない。bounded probe で新 port 18765 の `health_check`と、既存 identity の read-only `whois(include_recent_commits=false)`を確認する。この段階では `fetch_inbox`も呼ばない。notification有効時の`fetch_inbox`はsignal fileをclearし、migration baselineそのものを変え得るためである。`register_agent`、send、receipt変更、reservation変更も行わない。

`start`結果の`bootstrap_preflight`は`launchctl_print_returncode=113`かつ`launchctl_print_state=absent`でなければならない。bootstrapがEIOを返した場合、controllerは直後にexact labelを再照合する。ownershipのpath/program/argumentsと一致したloaded jobだけを`bootstrap_outcome=exact_job_already_loaded_after_eio`として二重bootstrapせず`enable → kickstart`へ進め、`bootstrap_eio_recheck`を記録する。EIO後もabsent、foreign、またはstate unknownなら後続の状態変更を行わず中止する。EIOの原因名はreceiptから推測しない。

新 root が migration baseline と同一で、旧 job/8765が停止、新 job/18765だけがreadyであることを確認する。readinessが期限内に通らなければC4 rollbackへ進む。

### C5: consumer を一括切替し、最初の1通で実動確認する

個別手編集はしない。C0でsealしたbundleと外部pinしたdigestだけを使い、次の1操作で構造化configを切り替える。

```sh
"$CONSUMERS_BIN" apply \
  --bundle "$CONSUMER_BUNDLE" \
  --expected-manifest-sha256 "$PINNED_MANIFEST_SHA256" \
  > "$MAINT/c5-consumer-apply.json" || exit 1

"$CONSUMERS_BIN" status \
  --bundle "$CONSUMER_BUNDLE" \
  --expected-manifest-sha256 "$PINNED_MANIFEST_SHA256" \
  > "$MAINT/c5-consumer-status.json" || exit 1
assert_consumer_state "$MAINT/c5-consumer-status.json" committed
```

`status=committed`以外ではconsumerを再開しない。対象は明示inventoryに入れたClaude/Codex direct config、tool permissions、AgentStack/Codex App envとinstall receipt、停止時に存在したchild resume configである。Bridge自身の client key `agentstack` は変えない。repo-managed launcher/watcher/skillsとOrrery/dashboardはC2より前に新旧env両対応artifactとしてdeploy済みであることを前提とし、C5でsourceを文字列置換しない。例外はstrict identity版のreservation hookだけで、下記restart/rebind後にexact repo artifactをdeployする。

helperは列挙済みfile内の未知aliasを拒否するが、**inventoryから漏れたfileを発見するscannerではない**。C0のlive inventory reviewが別のhard gateである。inventory schema v1は次の全fieldを明示し、pathは全てabsoluteにする（値は本番用maintenance artifactにのみ書き、repoへcommitしない）。

停止中のchild config 6件（Claude 2、Codex 4）はresume資産なので削除しない。sealed inventoryへ全件を含め、client keyを維持したままendpoint/authenticationだけを移し、更新済みmanaged launcherからのみ再開する。未コミットmachine-local観測 `2026-08-11T15:06:46 JST` では全6件が8765とlegacy envをpinしていたため、未移送の直接resumeは禁止する。live residual Codex child config 4件は同時点で全てper-tool policy tableが0件だった。helperは存在するpolicyだけを保存し、欠落policyを製造しない。

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
    "new_signals_dir": "/absolute/new/mail/signals",
    "claude_mcp_key": "mcp-agent-mail",
    "codex_mcp_key": "agent-mail"
  },
  "consumers": [
    {"kind": "claude_mcp", "path": "/absolute/copied/.claude.json"},
    {"kind": "claude_settings", "path": "/absolute/copied/.claude/settings.local.json"},
    {"kind": "codex_mcp", "path": "/absolute/copied/.codex/config.toml"}
  ]
}
```

全config置換後、**既に起動していたclientは設定file変更だけでは新endpointへ移らない**。各Claude/Codex parent、Codex App、停止時に存在したchildを`agent-start`等のmanaged launcherで明示的にrestart/rebindする。raw non-tmux Claudeは対応せず、同じmanaged経路で再起動する。各sessionの`AGENT_NAME`、または`TMUX_PANE`で明示したtargeted tmux sessionがcanonical identityと一致し、stale pane metadataとの不一致が無いことを先に確認する。続いてloaded client keyがClaudeでは`mcp-agent-mail`、Codexでは`agent-mail`のまま、endpointが127.0.0.1:18765、実HTTP `tools/list` がversioned 24-tool集合とmissing/extra 0で一致し、8765 connectionが無いことをread-onlyに確認する。providerのserverInfo identityが`agentstack-mail`でもclient keyの変更とは扱わない。確認前はtest pairを含め誰もcallしない。

restart/rebindとidentity確認が全件終わった後にだけ、strict版`check-file-reservation.sh`と`resolve-agent-name.sh`をrepoのexact digestからliveへdeployする。untargeted tmux fallbackは無く、unresolved/placeholder identityとmetadata-session不一致はHTTPを送る前にexit 2であることを負方向testで確認する。deploy前に予約guardの実動確認へ進まない。

最初の clientが `register_agent` またはwriteを成功させる直前に、maintainerが冒頭の不可逆境界を再確認する。成功した瞬間から旧 authorityへのrollbackは禁止である。

その後、専用test sender/recipientだけを許可して1通だけ送る。他の全senderはoperational smoke確認まで黙ったままにする。観測項目は次の全てである。

- request nameとresponse `name` が完全一致する。
- `send_message` が返したmessage IDをrecipientの `fetch_inbox` が返す。
- sender、recipient、subject、本文が完全一致する。
- DBのmessage/recipient edgeと新 working treeのcanonical message fileが一つ増える。
- legacy DB/archive/signalsのfingerprintがC2 quiesced sealから変わっていない。

続けて、専用test agentとprotectedなthrowaway pathでreservation guardを実動確認する。

- `file_reservation_paths`で予約を取得し、Write/Edit相当のhook payloadを渡すとexit 0になる。
- 予約をreleaseして同じpayloadを渡すとexit 2になり、対象fileは変更されない。
- production endpointではなく隔離stubでHTTP 406、JSON-RPC `error`、MCP `isError` true/型違反を返すと全てexit 2になる。
- 隔離stubの初回connection refusalだけは既定どおりexit 0になる。definitive zero回答後のtransport failureはexit 2になる。

単なる `isError: false` はoperational smoke成功にしない。全観測後に全 consumerを再開し、C6として新 authorityだけがwriterであることを再確認する。

全consumerを再開する前に、旧authorityをread-onlyで再走査し、C2 quiesced sealとcanonical bytesが一致することを確認する。

```sh
shasum -a 256 -c "$MAINT/legacy-state-quiesced.sha256" || exit 1
capture_legacy_state_snapshot "$MAINT/c5-legacy-state.json" || exit 1
cmp -s "$MAINT/legacy-state-quiesced.json" "$MAINT/c5-legacy-state.json" || exit 1
```

## 失敗時の戻し方

pre-open cold backupはmain / `-wal` / `-shm`のbyte recovery原本として保持する。ただし受け入れ判定はbytes一致でなく、scratch clone上のschema・全row・関係projection・PRAGMAの論理一致である。`rollback-assess`のstageはtoolが観測した値ではなくcaller assertionなので、出力の`cutover_stage_provenance=caller_asserted_unverified`を必ず確認する。

共通probe/assertionはC0のmaintenance shellで既に定義済みである。`bounded_mail_probe`はhealthが期待port/DBとexact一致し、C0でsealした既存agentの`whois(include_recent_commits=false)`が同名を返すまで20秒で打ち切る。register/fetch/sendは呼ばない。

### R0 — C0/C1、旧authority未停止

新artifactを使わない。display patch markerが無ければ旧authorityは動いたままでservice/config/data変更はないため、C0 readiness出力と中止理由だけをmaintenance記録へ残す。markerがある場合は、全senderを止めてから次の共通display rollbackを完走し、markerを消すまで旧dashboard/clientを再開しない。

```sh
if [ -e "$MAINT/display-patches.applied" ]; then
  rollback_display_patches || exit 1
  rm "$MAINT/display-patches.applied"
fi
```

### R1 — C2A、cold backup前

全senderを停止したまま、新job/18765が存在しないこと、旧DB holderが0であることを再確認する。これはC4の「旧exact absentを確認してから新をbootstrap」の対称操作であり、**新exact absentを確認してから旧をbootstrap**する。旧plistとC1でsealしたloaded定義receiptの外部pinが両方一致した場合だけ旧jobを戻す。

**new bootoutからlegacy bootstrapと8765 readiness完了までは、一つの不可分なrollbackである。** R4/R5から来る場合はexact owned newのstop/bootout成功とrc 113を先に確認し、R1へ引き渡す。newが一度もloadedされていないR1–R3では`assert_new_writer_absent`が同じ前半条件を満たす。newを止めた後、別のserviceやconsumerを開始せず、採取済みlegacy definitionとplist raw bytesを照合してからlegacyだけをbootstrapする。

new jobをbootoutしても、上記production override規則どおり`org.agentstack.mail => enabled` entryは残る。R1のjob不在判定はexact `launchctl print` rc 113で行い、override entryは消そうとせずmaintenance記録へ残す。

```sh
if [ -e "$MAINT/display-patches.applied" ]; then
  rollback_display_patches || exit 1
  rm "$MAINT/display-patches.applied"
fi
assert_legacy_writer_absent r1 || exit 1
assert_new_writer_absent r1 || exit 1
shasum -a 256 -c "$MAINT/legacy-launchd-definition-v1.sha256" || exit 1
shasum -a 256 -c "$MAINT/legacy-plist.sha256" || exit 1
python3 - "$LEGACY_LAUNCHD_RECEIPT" "$LEGACY_PLIST" <<'PY'
import base64, json, pathlib, sys
receipt = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert receipt["definition"]["plist_path"] == sys.argv[2]
assert base64.b64decode(receipt["definition"]["plist_bytes_base64"]) == pathlib.Path(
    sys.argv[2]
).read_bytes()
PY
shasum -a 256 -c "$MAINT/legacy-state-quiesced.sha256" || exit 1
capture_legacy_state_snapshot "$MAINT/r1-legacy-state.json" || exit 1
cmp -s "$MAINT/legacy-state-quiesced.json" "$MAINT/r1-legacy-state.json" || exit 1
launchctl bootstrap "gui/$(id -u)" "$LEGACY_PLIST" || exit 1
bounded_mail_probe \
  'http://127.0.0.1:8765/mcp' 8765 \
  'sqlite+aiosqlite:////Users/operator/mcp_agent_mail/storage.sqlite3' || exit 1
```

旧DB/archive/signalsがC2 quiesced fingerprintと一致してからsenderを再開する。

### R2 — C2B、cold backup後

まず分岐に関係なくbundle外pinを確認する。失敗したら旧DBへrestoreせずincident/no-writerにする。

```sh
shasum -a 256 -c "$MAINT/cold-backup-receipt.sha256" || exit 1
```

`copy`を一度もinvokeしていない場合は上のpin確認後にR1のlegacy tailへ進む。`copy`をinvokeし、verified C3 manifestまで公開済みなら、同じpin確認済みのbackupだけを使い、両service停止を再確認してcold restoreを実行できる。PRESENTはsibling stagingからatomic replace、receiptがABSENTとしたsidecarはatomic quarantine後に除去し、mainを最後にreplaceする。post-restore scratch論理一致、staging cleanup、target parent fsyncの後にだけterminal receiptを作る。

```sh
"$MIGRATE_BIN" cold-restore \
  --backup-dir "$COLD_BACKUP_DIR" \
  --destination-db /Users/operator/mcp_agent_mail/storage.sqlite3 \
  --restore-receipt "$MAINT/r2-production-restore.json" \
  --migration-manifest "$MIGRATION_MANIFEST" \
  --services-stopped \
  --target-kind production-source \
  --fault-injection none \
  > "$MAINT/r2-production-restore-command.json" || exit 1
python3 - "$MAINT/r2-production-restore-command.json" \
  "$MAINT/r2-production-restore.json" "$COLD_BACKUP_DIR/cold-backup-receipt.json" \
  "$MIGRATION_MANIFEST" <<'PY'
import hashlib, json, pathlib, sys
command_path, receipt_path, backup_path, manifest_path = map(pathlib.Path, sys.argv[1:])
command = json.loads(command_path.read_text(encoding="utf-8"))
receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
backup_sha = hashlib.sha256(backup_path.read_bytes()).hexdigest()
manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
assert command["status"] == "restored"
assert command["restore_receipt"] == str(receipt_path)
assert receipt["kind"] == "cold-restore"
assert receipt["restore_result"]["status"] == "restored"
assert receipt["logical_validator"]["status"] == "matched"
assert receipt["physical_validator"]["status"] == "matched"
assert receipt["physical_validator"]["post_restore_before_logical"] == (
    receipt["physical_validator"]["post_logical"]
)
assert receipt["target"]["production_source"] is True
assert receipt["services_stopped"] == {
    "asserted": True, "provenance": "caller_asserted_unverified",
}
assert receipt["backup_identity"]["receipt_sha256"] == backup_sha
assert receipt["migration_identity"]["manifest_sha256"] == manifest_sha
PY
```

上のmachine assertionが通ってからR1のlegacy tailへ進む。最初のtarget rename後に失敗した場合、toolはcanonical terminal receiptを作らず、quarantineを含み得るowned staging、`.prepared`、または`.unconfirmed`をincident evidenceとして保持する。command rc0を観測できない、`copy`をinvokeしたがverified manifestが無い、canonical receiptが無い、またはowned stagingが残る場合は、推測で旧jobを起動せずincident/no-writerにする。prepared/unconfirmedをterminalへ手動renameしない。

### R3 — C3_MIGRATION_VERIFIED

新copyは診断用に保持し、次のexact assessmentがexit 0かつ`status=reversible`、両baseline一致、両verification errorがnull、external verification requiredを返した場合だけR1のlegacy tailへ進む。`no_go`は旧jobをauthorizeしない。

```sh
"$MIGRATE_BIN" rollback-assess \
  --manifest "$MIGRATION_MANIFEST" \
  --cutover-stage C3_MIGRATION_VERIFIED \
  > "$MAINT/r3-rollback-assess.json" || exit 1
assert_rollback_state "$MAINT/r3-rollback-assess.json" C3_MIGRATION_VERIFIED
```

### R4 — C4_NEW_SERVICE_READY

exact owned new jobをstopし、任意のnonzeroを成功扱いせず、JSONの`status=stopped`と`owned=true`を確認する。その後C4 assessmentがR3と同じreversible条件を満たした場合だけR1のlegacy tailへ進む。

```sh
"$SERVICE_BIN" stop \
  --ownership-manifest "$NEW_OWNERSHIP" > "$MAINT/r4-stop.json" || exit 1
"$SERVICE_BIN" status \
  --ownership-manifest "$NEW_OWNERSHIP" > "$MAINT/r4-stopped.json" || exit 1
assert_service_state "$MAINT/r4-stop.json" stopped stopped
assert_service_state "$MAINT/r4-stopped.json" stopped -
"$MIGRATE_BIN" rollback-assess \
  --manifest "$MIGRATION_MANIFEST" \
  --cutover-stage C4_NEW_SERVICE_READY \
  > "$MAINT/r4-rollback-assess.json" || exit 1
assert_rollback_state "$MAINT/r4-rollback-assess.json" C4_NEW_SERVICE_READY
```

### R5 — C5_CLIENT_SWITCHING、first durable write前だけ

新jobをR4と同じexact sequenceでstop/status確認する。次のstandalone assessmentがreversibleの場合だけ、authority lock内で再検査するconsumer rollbackを実行する。

```sh
"$SERVICE_BIN" stop \
  --ownership-manifest "$NEW_OWNERSHIP" > "$MAINT/r5-stop.json" || exit 1
"$SERVICE_BIN" status \
  --ownership-manifest "$NEW_OWNERSHIP" > "$MAINT/r5-stopped.json" || exit 1
assert_service_state "$MAINT/r5-stop.json" stopped stopped
assert_service_state "$MAINT/r5-stopped.json" stopped -
"$MIGRATE_BIN" rollback-assess \
  --manifest "$MIGRATION_MANIFEST" \
  --cutover-stage C5_CLIENT_SWITCHING \
  > "$MAINT/r5-rollback-assess.json" || exit 1
assert_rollback_state "$MAINT/r5-rollback-assess.json" C5_CLIENT_SWITCHING
"$CONSUMERS_BIN" rollback \
  --bundle "$CONSUMER_BUNDLE" \
  --expected-manifest-sha256 "$PINNED_MANIFEST_SHA256" \
  --migration-manifest "$MIGRATION_MANIFEST" \
  --cutover-stage C5_CLIENT_SWITCHING \
  > "$MAINT/r5-consumer-rollback.json" || exit 1
"$CONSUMERS_BIN" status \
  --bundle "$CONSUMER_BUNDLE" \
  --expected-manifest-sha256 "$PINNED_MANIFEST_SHA256" \
  > "$MAINT/r5-consumer-status.json" || exit 1
assert_consumer_state "$MAINT/r5-consumer-status.json" rolled_back
```

`status=rolled_back`、terminal receipt有効、third state 0、after state 0をmachine確認した後だけR1のlegacy tailへ進む。first durable write、external edit、authority lock失敗、assessment `no_go`はいずれもR6へ進む。

### R6 — C6またはfirst durable write後（初回cutoverはfix-forward only）

**旧plist照合/bootstrap、consumer rollback、旧endpoint handshakeは行わない。** canonical stageは`C6_NEW_AUTHORITY_VERIFIED`だけであり、snapshotがfresh baselineでも`rollback-assess`は無条件`no_go`を返す。`C6_CUTOVER_COMPLETE`を含む別名は受け入れず、operatorが同じ境界に二つの名前を使わない。新authorityだけを次の順序で止め、`status=stopped, owned=true`を確認し、incident固有repair後にstartする。loaded jobへstartを直接撃ってrestart扱いにしない。

このsequenceを本番で使う前に、exact candidateを隔離root/port 18765で起動し、launchd相当の`SIGTERM`で停止して、tracebackなし、正常exit、endpoint閉鎖、SQLite main/WAL/SHMの物理mapと再open integrityをreceiptへ残す。さらにshutdown完了前のforced killを負対照として、残ったsidecarと次回startの回復結果を記録する。Ctrl-Cの`SIGINT`/exit 130はこの証跡の代用にならない。dirty working treeを使った隔離direct probeではSIGTERM clean shutdownとforced-kill後の回復を確認済みだが、clean exact candidateへ束縛したsealed receiptと、実controllerによる`stop → status=stopped → start → bounded health` receiptは未生成である。したがってservice-lifecycle conditionはNO-GOのままとする。

```sh
set +e
"$MIGRATE_BIN" rollback-assess \
  --manifest "$MIGRATION_MANIFEST" \
  --cutover-stage C6_NEW_AUTHORITY_VERIFIED \
  > "$MAINT/r6-rollback-C6_NEW_AUTHORITY_VERIFIED.json" \
  2> "$MAINT/r6-rollback-C6_NEW_AUTHORITY_VERIFIED.err"
R6_ASSESS_RC=$?
set -e
test "$R6_ASSESS_RC" -eq 1
test ! -s "$MAINT/r6-rollback-C6_NEW_AUTHORITY_VERIFIED.err"
assert_rollback_no_go \
  "$MAINT/r6-rollback-C6_NEW_AUTHORITY_VERIFIED.json" \
  C6_NEW_AUTHORITY_VERIFIED any
"$SERVICE_BIN" stop \
  --ownership-manifest "$NEW_OWNERSHIP" > "$MAINT/r6-stop.json" || exit 1
"$SERVICE_BIN" status \
  --ownership-manifest "$NEW_OWNERSHIP" > "$MAINT/r6-stopped.json" || exit 1
assert_service_state "$MAINT/r6-stop.json" stopped stopped
assert_service_state "$MAINT/r6-stopped.json" stopped -
# repairが未確定ならここで停止し、startしない。
"$SERVICE_BIN" start \
  --ownership-manifest "$NEW_OWNERSHIP" > "$MAINT/r6-start.json" || exit 1
"$SERVICE_BIN" status \
  --ownership-manifest "$NEW_OWNERSHIP" > "$MAINT/r6-loaded.json" || exit 1
assert_service_state "$MAINT/r6-start.json" job_loaded started
assert_service_state "$MAINT/r6-loaded.json" job_loaded -
bounded_mail_probe \
  'http://127.0.0.1:18765/mcp' 18765 \
  'sqlite+aiosqlite:////Users/operator/.agentstack/mail/storage.sqlite3' || exit 1
```

`start`は`status=job_loaded, owned=true, environment_drift=false, action=started`、続くstatusも`job_loaded, owned=true, environment_drift=false`をexact確認する。new jobが20秒以内にreadyにならない場合は旧を起動して二つのauthorityを作らず、new dataを保持したincident/no-writerを継続する。検証済みreverse transformが無いため、新規recordだけを旧DBへbest-effort mergeしない。

## 現在の blocker digest（non-normative）

これは進捗を読むための要約であり、別の完了条件ではない。2026-08-11の簡素化裁定後の4点だけを追う。

- **復元の実演: closed。** 67 MB production backupを、main truncate＋偽WAL/SHMで物理・論理の両方を故意破損した別inodeの`rehearsal-copy`へ既存cold-restoreで戻した。最新row ID 8829/content digest、exact candidate PIDのtarget family open、起動後full logical SHA、正常終了、本番3ファイルと8765 PIDの前後不変を確認した。
- **二重service防止: 手順固定済み。** C2A→C4を旧bootout→new bootstrapの不可分handoffとし、旧停止・新不在・DB holder不在を直前再検査する。actual authority交代は切替当日にだけ実行する。
- **戻し手順: 手順・実機tail確認済み。** new bootout→legacy bootstrapを不可分rollbackとし、legacy receiptと再起動後loaded definitionの完全一致、8765復帰、client自動再接続を確認した。production enabled overrideは戻しても残るのが正常である。
- **permission/hook selector 70件: 利用側確認中。** project `.mcp.json`へ新serverを足さず、既存`mcp-agent-mail` keyのURL/authだけを差し替える。N=1の9-tool隔離probeはpermission/trust prompt 0、error 0で、全70件は利用側担当が確認する。PluckyEinstein側の追加実装はない。

hash-lock済み依存閉包、atomic install receipt、残りの証跡handler、consumer orchestrationは切替後backlogであり、pre-cutover blockerへ戻さない。4点完了後にauthority 4遷移のcommitted testだけを追加するまでは、本番切替は未承認である。

`packages/agentstack_mail/README.md`はgenerator/rehearsal/verifier/check-onlyとcrash境界へ同期した。`claude/CLAUDE.md`と`codex/AGENTS.md`は今回の未実行runbookと矛盾するinstalled behaviorを記述していないため変更しない。
