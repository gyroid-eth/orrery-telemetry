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

working-tree scope の `agentstack-mail-migrate copy` / `verify` / `rollback-assess` と、production-shaped rehearsal / candidate-bound raw evidence runner は実装済みである。ただし台帳の`data-migration-reconciliation` evidence handlerは未実装なので、本番実行はまだNO-GOである。この手順のcommand例もGO前には実行しない。

consumer設定用の `agentstack-mail-consumers` は実装済みである。明示inventoryから全before/after imageを先に作り、外部にpinするmanifest SHA-256、whole-set CAS、同一directoryのatomic replace、write-once terminal receipt、migration baselineを再検査する1操作rollbackを持つ。ただし複数directoryを跨ぐ真のatomic syscallではない。途中状態は `status=committed` にならず、C2でconsumerを止めたまま再実行またはrollbackする契約である。実機inventoryの確定、個人設定のpreview承認、下記のOrrery/dashboard前提条件が揃うまで C2へ進まない。

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

最初に次の固定pathを同じmaintenance shellへ設定する。isolated rehearsal evidenceの生成・検算だけはfinal readinessより先に行う。本番pathへのcopy、service/config/authority操作は、readinessが`go`でなければ開始しない。

```sh
set -eu
REPO='/Users/operator/Syncthing/<vault-directory>/21_Coding Projects/claude-agent-stack'
MAINT='/Users/operator/.agentstack/cutover-maintenance'
READINESS="$REPO/packages/agentstack_mail/tests/cutover_readiness.py"
CUTOVER_MANIFEST="$REPO/packages/agentstack_mail/fixtures/differential-expected-divergences-v2.json"
EVIDENCE_INDEX="$MAINT/evidence-index.json"
EVIDENCE_ROOT="$MAINT/evidence"
LEGACY_PLIST='/Users/operator/Library/LaunchAgents/com.operator.mcp-agent-mail.plist'
NEW_OWNERSHIP="$MAINT/render/org.agentstack.mail.ownership.json"
NEW_ENV="$MAINT/render/agentstack-mail.env"
NEW_STATE_ROOT='/Users/operator/.agentstack/mail'
CANDIDATE_VENV="$MAINT/candidate-venv"
MIGRATE_BIN="$CANDIDATE_VENV/bin/agentstack-mail-migrate"
SERVICE_BIN="$CANDIDATE_VENV/bin/agentstack-mail-service"
CONSUMERS_BIN="$CANDIDATE_VENV/bin/agentstack-mail-consumers"
SERVER_BIN="$CANDIDATE_VENV/bin/agentstack-mail"
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

runnerはsource/backup/damaged/restoredの4 raw familyを同一run IDへ束縛し、built-in damageがmainを実際に変え、backup時ABSENTだったsidecarを作って除去branchを通したことを要求する。no-op damage、restore skip、PRESENT replace skip、ABSENT unlink skipは各mutation testで赤くなる。raw artifact、terminal receipt、separate verifier receipt、run directory外の3 SHA pinは、将来の`data-migration-reconciliation` handlerへの入力として保持する。`rollback-revert-procedure`は同じartifactを別名で再登録せず、C3–C6/R2–R6を再計算する独立handlerと独立recordを必要とする。最終booleanだけはevidenceに数えない。

canonical rehearsal receiptが無い、command rc0を観測できない、`.prepared`/`.unconfirmed`/ownership markerだけが残る、または初回verifier/check-onlyのどちらかが失敗した場合は未完了である。prepared/unconfirmedを手動renameしない。receipt内の`fsync`/`atomic_replace`という文字列は自己証明ではなく、それらはcode pathとEIO fault testでのみ照合する。producerが実際に走ったことの暗号学的証明ではなく、保持raw artifactsから独立再計算できるところが保証の上限である。

### 現行v1の停止点（normative）

現在のversioned manifestでは`data-migration-reconciliation`と`rollback-revert-procedure`がともに`unimplemented_v1`である。この状態ではartifactやindexへ何も書かず、非0で停止する。両conditionのversioned handlerと別々のevidence recordが実装され、readiness evaluatorが両recordを再計算して受理するまで次へ進まない。

### handler実装後のfuture skeleton（現行では実行禁止）

次のproducerは、将来`data-migration-reconciliation`用のversioned handlerが実装された後に、rehearsalの3つの外部pinとcanonical raw evidenceから同条件の1 recordだけを作るためのskeletonである。現在の`unimplemented_v1`では先頭で意図的に非0となる。これは`rollback-revert-procedure`のproducer/handlerを実装せず、未知の将来`evidence_kind`も固定していないため、現行の実行可能契約ではない。handler実装後もcondition IDを増やさず、review済みのversioned `evidence_kind`と同じ値だけを受け入れる。

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

1. 将来版で`data-migration-reconciliation`と`rollback-revert-procedure`のversioned handlerおよび別々のevidence recordが実装され、両recordを含むindexをreadiness evaluatorが再計算して受理できた場合にだけ、clean checkoutでfull candidate commit、exact manifest、digest-verified evidenceを次のexact commandへ渡す。現行v1ではここへ進まない。一つでも違えば後続のhuman確認やsmoke testで上書きせず、C0で止まる。

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
assert p["condition_count"] == 26
assert len(p["passed_condition_ids"]) == 26
assert len(set(p["passed_condition_ids"])) == 26
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
4. `distribution-artifact-release-gate`がindexへ束縛するのは現在wheel/sdistまでで、transitive dependency closureとinterpreter identityはまだ束縛しない。したがって現行v1は専用venvを作る前でNO-GOであり、以下はfuture-only skeletonとしてもそのまま実行しない。将来のdistribution/install evidenceは、interpreter identity、hash-lockされたrequirements、全dependency wheelのname/SHAを持つsealed wheelhouse manifest、install receiptを同じcandidateへ束縛する。installerは`--no-index --find-links <sealed-wheelhouse> --require-hashes -r <sealed-lock>`でcandidate wheelをlock内から導入し、`pip check`を通してからrenderへ進む。live `~/Library/LaunchAgents`ではないstaging directoryだけを使い、bare PATH entrypointやlaunchctlは使わない。

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
for executable in "$MIGRATE_BIN" "$SERVICE_BIN" "$CONSUMERS_BIN" "$SERVER_BIN"; do
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
5. 旧 DB/archive/signals の read-only fingerprintと旧 launchd plistを保存する。全consumerを`$MAINT/consumer-inventory.json`へtyped inventoryとして列挙し、次のexact commandで0600/0400のbefore/after bundleを作る。標準出力のmanifest SHA-256をbundle外へpinし、同じmaintenance shellの変数へ代入する。

```sh
"$CONSUMERS_BIN" prepare \
  --inventory "$MAINT/consumer-inventory.json" \
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
6. `agentstack-mail-consumers preview`のcontent-redactedなfile pathとbefore/after line rangeをmaintainerへ提示する。特にlive inventoryでtool permission/hookを確認した15個の `.claude/settings.local.json` は、対象fileと変更行を一件ずつ事前承認されるまでapplyしない。helperは**列挙したfile内**の旧alias、old/new併存、未知endpointをfailさせる。inventory外のfileは見えないため、別のlive inventory reviewで漏れ0を承認する。旧source tree自身の`09_MCP/mcp-agent-mail/.mcp.json`、`.codex/config.toml`、`.claude/settings.local.json`（最後のfileは旧sourceの`enabledMcpjsonServers=["mcp-agent-mail"]`だけを選ぶ開発用設定）はcutover consumerではないためexact pathで明示excludeし、理由をmaintenance記録へ残す。
7. 全 sender に開始時刻、C3の2–4分見込み、C5 test合格まで無通信が続くことを事前通知する。ProOpus 自身も、停止後は agent-mail を送受信せず同じ maintenance shell だけを使う。

### C2A: cold backup前に全 sender と旧 writer を静止する

tmux server 全体を kill しない。Claude/Codex parent・childは agent-mail call を止めて idle、`BiomatterBot`、`SeminarBot`、watcher/hook は停止または送信不能状態にする。全員の停止確認を agent-mail 停止前に済ませる。

最後の agent-mail 通知を送った後、operator は通常 shell へ移り、旧 job を止める。

```sh
launchctl bootout "gui/$(id -u)/com.operator.mcp-agent-mail"
```

次を全て確認する。一つでも hit したら copy せず、writer を特定する。

```sh
set +e
launchctl print "gui/$(id -u)/com.operator.mcp-agent-mail" \
  > "$MAINT/c2-legacy-launchctl-print.txt" 2>&1
LEGACY_PRINT_RC=$?
set -e
if [ "$LEGACY_PRINT_RC" -ne 113 ]; then
  echo "legacy job is loaded or launchd status is unknown: rc=$LEGACY_PRINT_RC" >&2
  exit 1
fi

set +e
lsof -nP -iTCP:8765 -sTCP:LISTEN \
  > "$MAINT/c2-listener-lsof.txt" 2> "$MAINT/c2-listener-lsof.err"
LISTENER_LSOF_RC=$?
set -e
if [ "$LISTENER_LSOF_RC" -eq 0 ]; then
  echo "legacy listener is still present" >&2
  exit 1
fi
if [ "$LISTENER_LSOF_RC" -ne 1 ] || [ -s "$MAINT/c2-listener-lsof.err" ]; then
  echo "cannot prove legacy listener absence" >&2
  exit 1
fi

for path in \
  /Users/operator/mcp_agent_mail/storage.sqlite3 \
  /Users/operator/mcp_agent_mail/storage.sqlite3-wal \
  /Users/operator/mcp_agent_mail/storage.sqlite3-shm; do
  if [ -e "$path" ]; then
    set +e
    lsof -- "$path" > "$MAINT/c2-db-lsof.txt" 2> "$MAINT/c2-db-lsof.err"
    DB_LSOF_RC=$?
    set -e
    if [ "$DB_LSOF_RC" -eq 0 ]; then
      echo "legacy database still has an open holder: $path" >&2
      exit 1
    fi
    if [ "$DB_LSOF_RC" -ne 1 ] || [ -s "$MAINT/c2-db-lsof.err" ]; then
      echo "cannot prove holder absence for: $path" >&2
      exit 1
    fi
  fi
done

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

C2Bのmachine-readable cold backup receiptとbundle外SHA-256 pinが揃った後だけ開始する。`cold-backup`の取得成功はrestore可能性の証明ではないため、本番C0の`data-migration-reconciliation` evidenceには、後述の非production rehearsal receiptも必要である。

working-tree scope migration は次を一つの staging generation 内で行う。

1. cold backup receiptが揃った後、source DBのSQLite writer slotを`mode=rw`の`BEGIN IMMEDIATE`で全copy期間保持し、直後に`query_only=ON`へ固定する。active writerがいれば即failする。別のread-only connectionの`backup()`でcommitted WALを含むcopyを作る。外部process確認直後にwriterが現れるraceを閉じる価値を優先してguardを残す。通常のsnapshot/verifyはwriter slotを取らず、単一read transactionのpoint-in-time snapshotを返す。
2. signals と archive working tree を copyする。legacy `.git` と `server.pid` は対象外。lock artifact、symlink、special file、権限不足、容量不足は fail-closedにする。
3. maintainer の A/B 選択どおり、新 archive に legacy と無関係な新 Git repo を作る。
4. SQLite `integrity_check`、`foreign_key_check`、schema、全 table digestに加え、agent→project、message→project/sender/thread、message→recipient/read/ack、reservation→project/agent、thread membershipを比較する。
5. `source_before`、`staged_state`、`source_after`、`source_final`、finalizer の `source_now` と `destination_now` という既存6回の照合を working-tree scope で維持する。検証回数・粒度を最適化しない。
6. working treeの全 path/content/mode、signals、33 file attachments、選んだGit開始状態を確認する。
7. fsync後、同一 filesystem上の一回のdirectory renameで `~/.agentstack/mail` を公開する。失敗時は部分treeを canonical path に残さない。file descriptor/inode/link/container identity検査で実測したsource差替を拒否するが、same-UIDの非協調filesystem writerを完全な敵対者としては扱わない。destination不在checkとrenameの間のraceも単一operator前提で明記して受け入れ、未実装の`RENAME_EXCL`を安全保証として数えない。

以下が実装済みcommand形である。pathはsymlink componentを含まないcanonical absolute pathだけを使う。**この変更では実行しておらず、26条件がGOになるまで稼働dataへ実行しない。**

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

```sh
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

pre-open cold backupはmain / `-wal` / `-shm`のbyte recovery原本として保持する。ただし受け入れ判定はbytes一致でなく、scratch clone上のschema・全row・関係projection・PRAGMAの論理一致である。`rollback-assess`のstageはtoolが観測した値ではなくcaller assertionなので、出力の`cutover_stage_provenance=caller_asserted_unverified`を必ず確認する。

共通probe/assertionはC0のmaintenance shellで既に定義済みである。`bounded_mail_probe`はhealthが期待port/DBとexact一致し、C0でsealした既存agentの`whois(include_recent_commits=false)`が同名を返すまで20秒で打ち切る。register/fetch/sendは呼ばない。

### R0 — C0/C1、旧authority未停止

新artifactを使わない。旧authorityは動いたままなのでservice/config/data変更はない。C0 readiness出力と中止理由だけをmaintenance記録へ残す。

### R1 — C2A、cold backup前

全senderを停止したまま、新job/18765が存在しないこと、旧DB holderが0であることを再確認する。`shasum -a 256 -c "$MAINT/legacy-plist.sha256"`が成功した場合だけ旧jobを戻す。

```sh
shasum -a 256 -c "$MAINT/legacy-plist.sha256" || exit 1
launchctl bootstrap "gui/$(id -u)" "$LEGACY_PLIST" || exit 1
bounded_mail_probe \
  'http://127.0.0.1:8765/mcp' 8765 \
  'sqlite+aiosqlite:////Users/operator/mcp_agent_mail/storage.sqlite3' || exit 1
```

旧DB/archive/signalsがC0 fingerprintと一致してからsenderを再開する。

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

### R6 — C6またはfirst durable write後（fix-forward only）

**旧plist照合/bootstrap、consumer rollback、旧endpoint handshakeは行わない。** canonical stageは`C6_NEW_AUTHORITY_VERIFIED`だけであり、snapshotがfresh baselineでも`rollback-assess`は無条件`no_go`を返す。`C6_CUTOVER_COMPLETE`を含む別名は受け入れず、operatorが同じ境界に二つの名前を使わない。新authorityだけを次の順序で止め、`status=stopped, owned=true`を確認し、incident固有repair後にstartする。loaded jobへstartを直接撃ってrestart扱いにしない。

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

これは進捗を読むための要約であり、別の完了条件ではない。canonicalな残件は冒頭のevaluatorが返す`missing_conditions`である。現在は少なくとも次が未完了なので、本番切替は未承認である。

- versioned generatorと手順は実装済みだが、clean candidateに対するproduction-shaped rehearsal/独立verifier/check-onlyのrelease receiptはまだ未生成。active-writer/6回照合/中断/alias/object-store/corruptionの残りraw evidenceも未完了
- 実機consumerとlive hooksのexact inventory、maintainerによる個人settings preview承認、Orrery/dashboardの切替前compatibility
- bounded MCP readiness probe
- clean candidateのwheel/sdistとfresh installed wheel verification
- 台帳で`not_implemented`のpre-cutover follow-up taskと、それぞれのdigest-bound raw evidence

`packages/agentstack_mail/README.md`はgenerator/rehearsal/verifier/check-onlyとcrash境界へ同期した。`claude/CLAUDE.md`と`codex/AGENTS.md`は今回の未実行runbookと矛盾するinstalled behaviorを記述していないため変更しない。
