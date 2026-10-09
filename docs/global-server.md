# global server の S1 準備

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
| 起動後の main DB / commit 前 | `test_main_database_becoming_unsafe_fences_existing_runtime` / `test_rejected_main_alias_preserves_independently_committed_wal` / `test_sqlite_files_becoming_unsafe_before_commit_roll_back` | hardlink/mode変更でHTTP/管理を拒否、別名WALのcommit保持、途中変更はrollback |
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

この検査は協調する writer が共通 lock を守る範囲を対象とします。検査した fd を閉じた後で SQLite は path を開き直すため、検査と open の間、最後の検査と commit の間は filesystem に対して原子的ではありません。同じ UID の非協調 process がその間に link や path を変える競合は残ります。直後・commit 前の再検査はこの窓を狭めて異常を検出しますが、既に隔離外の sidecar に行われた書込みまで rollback で取り消せる保証はありません。この競合自体は今回の試験では実測していません。旧 writer の全停止と実 handoff は PR7 の対象です。

## 管理 socket

owner UID の Unix socket でのみ `version=1` の global 管理契約を使います。MCP の tool に enrollment は公開しません。全 request に HTTP と同じ instance/candidate/epoch の binding を指定します。

- `inspect`: `agent_id` から canonical name、credential generation、非秘密 fingerprint を返します。token は返しません。
- `claim` / `recover`: `request_id`・`agent_id`・`expected_generation`・`new_credential` を指定します。null token の claim と既存 token の recover を分け、CAS と監査を同じ transaction にします。
- 同じ request ID と内容の再実行は同じ非秘密 receipt を返します。内容違いは拒否します。`request_status` は request ID の receipt を返します。

旧 enrollment の履歴 table は保存し、新 global receipt/audit は別 table に記録します。4a の CLI/profile はこの wire を読み、専用 HOME の credential file と stable ID を照合する作業として後続に実装します。

## 未対応と次の作業

S1 が公開するのは health/ensure/register/whois/inbox/retire/unretire の7 tool だけです。残り18 tool（正本 fixture の `unimplemented`）は S2 に残ります。送信・返信・read/ack・search・contact・summary/topic・予約と macro、archive/添付/Git writer、global signal の生成/clear、生成 Git guard の結線は未対応です。resources は公開しません。従来の未公開 resource/tool body を、新しい公開要件として勝手に追加しません。

順番は S1 → 4a → S2 → 4b → 4c です。S1 の成功は global の全機能完成や実切替完了を意味しません。現行 mode の回帰と、この7 tool/管理 socket の実 HTTP 試験を区別して検証します。
