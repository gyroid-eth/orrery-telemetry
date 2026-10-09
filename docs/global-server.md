# global server の S1 / S2a 準備

[English](global-server.en.md)

S1 は #213 の client 対応の前提を、実 HTTP と管理 socket で確かめる隔離 server です。引数なしの `agentstack-mail` は従来の旧 mode・25 tool のままです。global は明示的な `--global-config` でだけ選びます。起動には `AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE=passthrough` が必要です。未指定時の coerce は CLI が拒否します。PR3 の pointer と DB の activation は false を保ち、installer・実サービス・実 HOME の切替は行いません。

## 自分で隔離契約を確かめる

新しい checkout で [開発用 venv](../CONTRIBUTING.md#test-environment) を用意し、次の Python を `.venv/bin/python`（`PYTHONPATH=.:packages/agentstack_mail/src`）で実行します。実 Mail の DB・実 HOME の設定・稼働中のサービスは入力にしません。

この例は [S1 の合成 fixture](../packages/agentstack_mail/tests/test_global_server.py) をそのまま使います。PR3 が合成旧 DB を schema 2 の候補へ変換し、その候補から instance/candidate を取得します。手入力の推測値や実 DB の読取りは不要です。fixture が0700の一時 root、0600の完全な config/authority、専用 HOME、空き loopback port と管理 socket を作ります。child 環境には passthrough と明示的な存在しない `AGENTSTACK_MAIL_ENV_FILE` を設定し、通常の .env を読みません。終了時にはこの例の server に TERM を送り、socket と一時 root を片付けます。

```python
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import secrets
import tempfile
from pytest import MonkeyPatch

path = Path("packages/agentstack_mail/tests/test_global_server.py")
spec = importlib.util.spec_from_file_location("s1_check", path)
fixture_module = importlib.util.module_from_spec(spec)
patch = MonkeyPatch()
temporary_home = tempfile.TemporaryDirectory(prefix="s1-home-")
fixture = None
try:
    for key in list(os.environ):
        if key.startswith(("AGENTSTACK_", "AGS_")) or key in {
            "TMUX", "CODEX_HOME", "CLAUDE_CONFIG_DIR"
        }:
            patch.delenv(key, raising=False)
    patch.setenv("HOME", temporary_home.name)
    spec.loader.exec_module(fixture_module)
    fixture = fixture_module.server.__wrapped__(patch)
    state = next(fixture)
    print("config:", json.dumps(state["config"]))
    print("authority:", (state["root"] / "authority.json").read_text())
    fixture_module.test_http_catalog_and_three_generations(state)
    before = fixture_module.call(state, "health_check")
    fixture_module.call(
        state, "register_agent",
        **fixture_module.owner(state, program="fixture", model="fixture")
    )
    after = fixture_module.call(state, "health_check")
    assert after["mutation_revision"] == before["mutation_revision"] + 1
    assert after["authority_epoch"] == before["authority_epoch"]
    assert after["candidate_generation"] == before["candidate_generation"]
    request = {"version": 1, **state["binding"], "action": "inspect", "agent_id": 2}
    print("socket request (one JSON line):", json.dumps(request) + "\n")
    inspected = fixture_module.control(state, action="inspect", agent_id=2)
    token = secrets.token_urlsafe(32)
    receipt = fixture_module.control(
        state, action="recover", request_id="documentation-recovery", agent_id=2,
        expected_generation=inspected["credential_generation"], new_credential=token
    )
    assert receipt["ok"]
    assert fixture_module.call(
        state, "whois", **fixture_module.owner(state, token=token)
    )["id"] == 2
    print("health before:", json.dumps(before))
    print("health after:", json.dumps(after))
    print("receipt (no credential):", json.dumps(receipt))
    print("S1 check: tools=7, resources=0, epoch unchanged, owner recovery OK")
finally:
    if fixture is not None:
        fixture.close()
    patch.undo()
    temporary_home.cleanup()
```

実際に使った完全な config と authority が表示されます。管理 request は `version`、3つの binding、`action`、`agent_id` を含む JSON 1行＋改行（JSONL）です。inspect は canonical name/generation/fingerprint を返し、recover は `request_id`、`expected_generation`、`new_credential` を加えます。例は新 credential を表示せず、成功 receipt と実 HTTP の whois を照合します。receipt の再実行は操作履歴の再取得であり、現在の credential の証明にはなりません。現在値は inspect と whois で確かめます。

fixture 内で使う起動入口は次の形です。上の例が自動で起動・終了します。`fixture` と `port` は生成した一時 root と空き port を表します。

```sh
env -i PATH="$PATH" HOME="$fixture/home" \
  AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE=passthrough \
  AGENTSTACK_MAIL_ENV_FILE="$fixture/missing.env" \
  .venv/bin/python -m agentstack_mail.cli \
  --global-config "$fixture/config.json" --host 127.0.0.1 --port "$port"
```

一括確認は下の file を `-n` なしで実行します。各 node は同じ [受入れ試験 file](../packages/agentstack_mail/tests/test_global_server.py) 内です。`pytest` の exit 0 と末尾の passed を確認し、失敗時は FAILED 行も読みます。

```sh
check_root="$(mktemp -d)"
mkdir "$check_root/home" "$check_root/tmux"
HOME="$check_root/home" TMUX_TMPDIR="$check_root/tmux" \
  PYTHONPATH=.:packages/agentstack_mail/src \
  .venv/bin/python -m pytest -q packages/agentstack_mail/tests/test_global_server.py
```

| 確かめること | node | 期待する結果 |
|---|---|---|
| 旧 mode | `test_default_cli_keeps_real_legacy_tools_database_and_management` | 25 tool、旧 project schema/rows を保持 |
| catalog / 3世代 | `test_http_catalog_and_three_generations` | fixture と全7 schema一致、resources空、activation=false |
| 改名 / window / 通常 write | `test_register_reconnect_stable_token_window_and_revision` | canonical ID/name、別 owner/UUID を拒否、counterのみ増加 |
| 自己 inbox | `test_inbox_privacy_and_owner_auth` | 自己 recipientのみ、BCC/token非公開、read/ack非更新 |
| 古い binding / root | `test_stale_wire_binding_rejects_http_and_management` / `test_common_authority_fences_current_http_management_and_restarted_wrapper` | STALE_RUNTIME_BINDING / WRITER_FENCED |
| updater / lock差し替え | `test_updater_exclusive_gate_prevents_new_request` / `test_replaced_startup_lock_fences_http_and_management` | EX lock中は拒否、別inodeでも拒否しrevision不変 |
| SQLite sidecar | `test_sqlite_sidecars_cannot_write_outside_isolation` | startup/HTTPでunsafeを拒否、外側sentinel不変 |
| 起動後の main DB / commit 前 | `test_main_database_becoming_unsafe_fences_existing_runtime` / `test_rejected_main_alias_preserves_independently_committed_wal` / `test_sqlite_files_becoming_unsafe_before_commit_roll_back` | hardlink/mode変更でHTTP/管理を拒否、別名が残る条件でWALのcommit保持、途中変更はrollback |
| enrollment | `test_management_recovery_cas_replay_and_secret_free_receipt` / `test_null_token_claim_is_operator_only_and_audited` | CAS/監査/同receipt再実行、旧token・古いgeneration拒否 |
| credential | `test_accepted_unicode_credential_authenticates_owner` | 新規/復旧/保存済みtokenでwhois成功、別token拒否 |


## 固定した契約

正本は package の `fixtures/global-server-s1.json` と、`global_server.py` の実 signature です。`tools/list` で能力を確認し、未対応の tool に fallback しないでください。

- `health_check` は stable `server_instance_id`、`candidate_generation`、`authority_epoch`、`mutation_revision`、対応 tool と未対応 resources を返します。
- owner の操作は `expected_server_instance_id`・`candidate_generation`・`authority_epoch` と、数値 `agent_id`・`registration_token` を使います。旧 `project_key` は任意で受けて無視します。名前や cwd、旧 tmux env は owner/routing を選びません。
- `register_agent` は既存 ID/token を照合し、改名後の canonical name と credential generation を返します。確定した window の再接続には `window_row_id` と `window_uuid` の両方を指定し、owner を照合します。既存行の owner token が無ければ新 token を付けて乗っ取りません。
- 新規登録は `agent_id` を省略して行います。同名・同じ lookup の競合は拒否します。旧 project 所属は新行に作りません。
- `whois` は自己 identity のみ、`fetch_inbox` は自己 recipient のみです。BCC の別 recipient や token を返しません。inbox は signal を clear せず、read/ack も変更しません。この接続は S2 に残ります。
- `retire_agent` / `unretire_agent` は同じ ID の owner token を要求します。`ensure_project` は何も作りません。

3つの世代を混同しないでください。`candidate_generation` は PR3 の統合候補の fingerprint、`mutation_revision` は DB の `write_generation` という書込みカウンタ、`authority_epoch` は runtime の切替世代です。登録や enrollment のたびにカウンタは増えますが、通常の書込みで authority epoch を変えません。

## 隔離設定と fence

設定は owner の private な JSON（0600）で、`kind=orrery-global-server-s1`、`activation_enabled=false`、`isolation_root`、`runtime_root`、`database`、`mail_instance_id`、`candidate_generation`、`authority_epoch`、`authority`、`authority_lock`、`management_socket` を持ちます。path は絶対 path で、DB と authority/lock/socket は owner の0700の一時 directory の下に置きます。global DB は PR3 が作った schema 2 の候補だけを開き、旧 ORM の schema 初期化を行いません。通常 HOME の DB を global 設定に指定できません。

authority の正本は `kind=orrery-global-authority-v1`、`phase=active`、`root_status=active`、`runtime_root`（隔離 directory 内の対象 state root）、`mail_instance_id`、`candidate_generation`、`authority_epoch` を持ちます。updater は同じ private regular lock file の排他 lock を取って authority を更新する契約です。server は shared lock を通信の操作と SQLite transaction の間保持し、commit 前にも現在の authority を照合します。quiescing、retired root、異なる epoch、config/DB/lock の差し替えは拒否します。

これは対応済みの writer を守る fence です。OS supervisor の停止、全 writer の列挙、旧接続の終了、実 pointer の handoff は PR7 に残ります。直接 DB に書く未知の process を、この lock だけで止めたとは扱いません。

server は起動時に検査した lock の dev/inode を固定し、操作の開始と commit 前に同じものか確かめます。起動中に lock を交換する更新はできません。正規の交換は旧 writer を全て終了させ、authority epoch と設定を更新して新 runtime を起動する handoff として PR7 で扱います。単に lock を交換して旧 runtime を継続させません。

SQLite の初回 open 前・直後と、各 transaction の open 前・直後・commit 前に、main DB と既存の `-wal`・`-shm`・`-journal` を検査します。symlink、複数 hardlink、異なる owner、公開 mode は main DB なら `DATABASE_UNSAFE`、sidecar なら `SQLITE_SIDECAR_UNSAFE` で拒否します。起動後も同じ基準を使い、途中で検出した変更は DB transaction を rollback します。credential は UTF-8 bytes の定時間比較で照合し、保存値を正規化しません。受け付けた非 ASCII token と PR3 が保存した token も、同値で認証できます。

この検査は協調する writer が共通 lock を守る範囲を対象とします。検査した fd を閉じた後で SQLite は path を開き直すため、検査と open の間、最後の検査と commit の間は filesystem に対して原子的ではありません。同じ UID の非協調 process がその間に link や path を変える競合は残ります。直後・commit 前の再検査はこの窓を狭めて異常を検出しますが、既に隔離外の sidecar に行われた書込みまで rollback で取り消せる保証はありません。別名経由の WAL commit を保持する回帰は、別名が残り、検査時に main DB の複数 link を検出できる条件です。別名を削除した後に残った WAL は対象外です。非協調 process が別名を unlink すると nlink が1に戻り、server は別名側の保留 WAL を検出できず、その後の server 書込みでその commit が失われる可能性があります。この競合自体は今回の試験では実測していません。旧 writer の全停止と実 handoff は PR7 の対象です。

## 管理 socket

owner UID の Unix socket でのみ `version=1` の global 管理契約を使います。MCP の tool に enrollment は公開しません。全 request に HTTP と同じ instance/candidate/epoch の binding を指定します。

- `inspect`: `agent_id` から canonical name、credential generation、非秘密 fingerprint を返します。token は返しません。
- `claim` / `recover`: `request_id`・`agent_id`・`expected_generation`・`new_credential` を指定します。null token の claim と既存 token の recover を分け、CAS と監査を同じ transaction にします。
- 同じ request ID と内容の再実行は同じ非秘密 receipt を返します。内容違いは拒否します。`request_status` は request ID の receipt を返します。

旧 enrollment の履歴 table は保存し、新 global receipt/audit は別 table に記録します。4a の CLI/profile はこの wire を読み、専用 HOME の credential file と stable ID を照合する作業として後続に実装します。

## 未対応と次の作業

S1 が公開するのは health/ensure/register/whois/inbox/retire/unretire の7 tool だけです。残り18 tool（正本 fixture の `unimplemented`）は S2 に残ります。送信・返信・read/ack・search・contact・summary/topic・予約と macro、archive/添付/Git writer、global signal の生成/clear、生成 Git guard の結線は未対応です。resources は公開しません。従来の未公開 resource/tool body を、新しい公開要件として勝手に追加しません。

順番は S1 → 4a → S2 → 4b → 4c です。S1 の成功は global の全機能完成や実切替完了を意味しません。現行 mode の回帰と、この7 tool/管理 socket の実 HTTP 試験を区別して検証します。

## S2a: schema3 の新規準備

S2a は明示した隔離設定 `orrery-global-server-s2a-v1` だけを対象とし、既定の旧 mode と S1 の設定・7 tool は維持します。公開契約は [S2a fixture](../packages/agentstack_mail/fixtures/global-server-s2a.json) です。health の `capabilities` は対応 ID の整列済み配列で、未対応能力へ旧 mode の fallback はしません。dashboard API の世代は変わりません。

| 追加 tool | capability | 動作 |
|---|---|---|
| `refresh_registration` | `registration_refresh_v1` | program/model/token/世代を保つ登録更新。task の null は保持、空文字は消去 |
| `verify_window_identity` | `window_verify_readonly_v1` | 正確な owner/row/UUID と期限を読取り、revision を変えない |
| `touch_window_identity` | `window_touch_v1` | 既存 window と owner の活動・有限期限を単調更新。receipt/quota の対象外 |
| `resolve_agent_identity` | `identity_resolve_v1` | canonical name から stable ID と表示情報を解決。peer の token は返さない |
| `request_contact` / `respond_contact` / `list_contacts` / `set_contact_policy` / `macro_contact_handshake` | `contact_state_v1` | 既存 stable ID の directed link と本人の policy。相手 owner の承認が必要 |

送信・返信・通知・archive/signal writer は S2b、予約と GC は S2c、実切替は PR7 です。macro は request-only で、`auto_accept=true` と welcome 本文はそれぞれ固定 reason で拒否します。approved link の期限は旧配送と同じく参考値で、期限だけで承認を取り消しません。pending の期限は再要求の更新を決め、blocked は相手 owner が明示的に解除するまで有効です。

### 隔離 fixture で試す

[開発用 venv](../CONTRIBUTING.md#test-environment) を用意し、専用 HOME で serial に実行します。2つの file は合成旧 DB から新 schema3 を準備し、本物の loopback HTTP と Unix 管理 socket を起動・終了します。外部 provider は起動しません。

```sh
check_root="$(mktemp -d)"
mkdir "$check_root/home" "$check_root/tmux"
HOME="$check_root/home" TMUX_TMPDIR="$check_root/tmux" \
  AGENTSTACK_MAIL_ENV_FILE="$check_root/absent.env" \
  PYTHONPATH=.:packages/agentstack_mail/src \
  .venv/bin/python -m pytest -q \
  packages/agentstack_mail/tests/test_global_server_s2a.py \
  tests/test_global_runtime_client_s2a.py
```

新規 preparation の operator 入口は `agentstack-mail-global-prepare --plan FILE` です。plan は0600の `orrery-s2a-preparation-plan-v1` JSON で、`activation_enabled=false`、0700の一時 `isolation_root`、新しい `candidate_name`、canonical UUID の `request_id`、`source_paths`（mail/delivery/archive/signals/history/bindings/config）、`choices`、`fence_evidence` を含めます。source と evidence は [PR3 の明示 snapshot / writer roster / fence](namespace-state-migration.md) と同じで、実 service の停止を推測した evidence を作ってはいけません。完全な合成 source と choices の生成例は [fixture](../packages/agentstack_mail/tests/fixtures/namespace_state.py)、API の呼出し例は `test_fresh_preparation_and_restart` です。

source の形式確認は immutable read-only で行い、PR3 candidate の namespace metadata/global columns を legacy source として受け入れません。user_version の数値だけでは判別しません。拒否する既存 candidate の WAL/SHM を作成・checkpoint しません。

出力は `isolation_root/s2a-candidates/candidate_name/` の固定 layout です。`preparation-owner.json`、`preparation-receipt.json`、非公開 `staging/`、完成した `candidate/` から導出し、任意の role path は受け取りません。source の厳密な保存確認後、staging の transaction で固定拡張・marker3・初期 revision1 を作り、拡張後の内容と最終差分・fence・gate を照合します。ready manifest を保存してから rename し、complete receipt を確定します。complete 前の server 起動は `PREPARATION_INCOMPLETE` です。同じ plan/UUID を再実行すると自分の途中結果を検証して完了し、別 plan/foreign root は上書きしません。source が変わった場合や owned marker より前の作成失敗で所有を証明できない場合は、旧出力を保存して新しい candidate_name を使います。

完成後は `candidate/server-config.json` を `agentstack-mail --global-config` に明示します。上の S1 例と同じ passthrough・専用 HOME・存在しない env file・空き port の条件を守ります。installed pointer、実 HOME、旧 authority は変更しません。通常 restart は検証だけで DDL・初期 revision を繰り返しません。

### schema2 を拒否されたとき

`CANDIDATE_SCHEMA_REQUIRES_REPREPARE` は既存 schema2 をその場で upgrade しないという意味です。完成した preparation receipt の無い root は DB ファイルを開く前に拒否し、main/WAL/SHM/journal を変えません。schema2 を見分けるための別検査は持ちません。receipt は準備完了と binding を示し、稼働後の DB の内容そのものは保証しないため、receipt の照合後に通常の schema3 検証を行います。旧 root を保存し、supported source から **別 workspace の新しい candidate_name** で schema3 を準備してください。未 release の master の PR3 Python API で作った schema2 も同じ扱いです。旧 S1 だけで増えた agent/message/token/window は自動移行も破棄もしません。それらの継続が必要なら root を保持して PR7 または別の明示移行を待ちます。旧 snapshot の rollback や extension の削除で pristine に見せる操作は行いません。

旧 client root は実行用として併用せず、旧 caller を止めたうえで復旧証跡として保管します。credential/context のコピーによって同じ identity の実行用 root を2つ作りません。以下の向け直しを終えてから新 caller を開始します。

新しい candidate では、新しい fixed `client_name` の root を作り、現在の instance/candidate/epoch と ID/token-state/generation を inspect してから [operator enrollment と明示 finalize](global-runtime-client.md) を実施します。旧 token や同じ数値 ID から連続性を推測しません。新しい `runtime-client.json` で whois、自己 inbox、window の照合を確かめ、次の呼出し元を向け直します。

1. `AGENTSTACK_CLIENT_CONFIG` を新しい `runtime-client.json` の絶対 path に設定します。旧値を継いだ terminal/tmux pane は明示的に更新します。
2. wrapper/hook の暗黙値はその wrapper checkout の `runtime-client.json` です。暗黙値に頼らず、試験用 wrapper と hook を呼ぶ環境に新しい `AGENTSTACK_CLIENT_CONFIG` を渡します。実 installed 設定の自動変更はありません。
3. persistent profile は新 client で固定 `profile.json` を作り直し、その profile を指定します。`client_config` が旧 root を指す profile を手書きで上書き・再利用しません。
4. `CONTEXT_CHANGED` / `WRITER_FENCED` / `STALE_RUNTIME_BINDING` / `MUTATION_PENDING_REQUIRES_RESOLUTION` が旧入口から出たら、まず env と profile の `client_config` を確認します。schema2 reason だけから新 root を自動探索しません。operator が選んだ新 candidate_name/client_name と完成 receipt を入口にします。

旧 root の pending がある場合は保持し、新 current context の認証後にのみ `resolve-mutation` で明示的に解消します。`bin/lib/runtime_client.py --context OLD_CONTEXT resolve-mutation DIGEST NEW_CONTEXT PROOF_JSON` の proof は `expected_old` / `expected_new`（server_instance_id/candidate_generation/authority_epoch）、`old_agent_id` / `new_agent_id` と `confirm=true` を含みます。DIGEST は旧 `runtime/mutation-pending.json` の SHA256 です。結果は「旧 outcome は不明・再送しない」とし、旧 local pending だけを処理します。旧 token の無条件コピー、payload の新 binding への書換え、新 UUID による自動再送はしません。

### receipt と活動、旧 mode との違い

program/model 無指定の mapped reconnect は touch/observe だけを行い、contact policy は明示した変更入口で適用します。receipt 対象の5 tool は通信前に client の固定 `runtime/mutation-pending.json` に UUID と canonical payload を保存します。`runtime/mutation.lock` は binding 非依存の local mutex です。応答喪失後も同じ intent は同 UUID を使い、別 intent は拒否します。tools/list の outputSchema と owner/binding/UUID の照合後にだけ pending を消し、不適合な応答では planned を保ちます。schema の検証関数は server/client の共通正本です。server は preimage/hash と result/digest を domain 変更と同じ transaction に保存します。現在の token/generation/binding の認証後にだけ replay でき、返る値は現在値でなく確定時の結果です。

既定 quota は owner ごと100000件で、自動削除はありません。満杯の owner の新 receipt 操作だけ `REQUEST_RECEIPT_CAPACITY_REACHED` になり、replay、別 owner、正確な window の touch は維持します。管理 inspect の `receipt_capacity` で owner の残数と keyset page を確認でき、明示 config の `operation_receipt_limit` を増やして制御した restart ができます。candidate 全体の disk quota は保証していません。touch/verify は未解消の正当な pending や保持中の mutation mutex を使わず、独立した current credential で認証します。

| 旧 mode | 明示 global S2a / S2b の判定 |
|---|---|
| 予約の重なりが block_all より先に許可することがある | directed blocked と block_all が優先。自己送信は許可 |
| requester の自動 handshake 承認がある | target owner の明示承認が必要 |
| enforcement=false で検査を止められる | 明示 global では拒否 |
| 配送が blocked link を見ない | directed blocked を拒否 |

この違いは旧 mode を変更しません。PR7 の切替案内にも載せる必要があります。auto の最近の交換は明示 config の `contact_auto_ttl_seconds`（既定86400秒）で、naive UTC / offset / Z を時点に直して比較します。活動は `max(保存値, 操作時刻)`、有限期限は `max(保存値, 操作時刻+TTL)` で、時計逆行時も戻しません。

### tracker mismatch からの出口

`RUNTIME_WRITER_MISMATCH` は通常 domain 操作を止めます。`agentstack-mail-global-incident inspect --config FILE` は同じ read snapshot で tracker/current revision・table digest を診断し、token/本文を表示しません。tracker 一致だけを免除し、schema3 の schema/integrity/私有 file 検査は維持します。receipt の無い root は DB を開かずに拒否します。不正な schema3 を修復する入口ではありません。

意図的に退役させる operator は inspect の `incident_digest`、`tracked_revision` / `current_revision` を使い、`agentstack-mail-global-incident quarantine --config FILE --request-id UUID --incident-digest DIGEST --tracked-revision N --current-revision N --confirm-quarantine` を実行します。EX 下で再照合し、固定 `global-runtime/incident.json`、authority の fencing/retirement、`quarantine.json` を記録します。各境界の中断は同 UUID で退役の完了だけを再実行します。DB/receipt/tracker を削除・巻戻し・adopt せず、その場に証拠を残します。再稼働は別の candidate と current enrollment に限ります。同 UID の非協調 process による検査と open/commit 間の競合、および外側の書込みの巻戻し不能という S1 の限界は残ります。

全件の quick_check/FK/FTS と過去 receipt の検証は起動時と明示した管理 inspect で行います。通常の書込みは同じ transaction 内で schema/profile/tracker と今回触った行・新 receipt の preimage/output/digest を検証し、replay は一致した1件だけを検証します。過去 receipt の全走査を touch や通常の書込みへ持ち込みません。read snapshot の FTS 検証は candidate DB と同じ隔離領域の固定 `.fts-validation`（0700）内に一時 disk DB を作り、成功時も例外時も検査用 directory ごと削除します。system の TMPDIR やメモリへ DB 全体を複製しません。incident の SH/EX も通常処理と同じ authority/fence/transaction 入口です。

client の pending は結果不明の証跡です。`TARGET_UNAVAILABLE`・quota 等の確定した tool 拒否では、自分の保存 bytes を mutex 下で再照合して消します。理由語と拒否時点の正本は wire fixture の `client_uuid_contract.definite_rejections` です。transport 失敗、schema/binding 不適合、未知の理由語は保持します。既存 pending に対する認証・fence 拒否は以前の commit の有無を証明しないため保持し、明示 resolution を使います。初回試行の確定した事前拒否とは区別します。同じ UUID に別の意図がある `REQUEST_ID_CONFLICT` も pending を保持し、operator の明示 resolution に回します。
