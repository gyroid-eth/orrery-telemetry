# global runtime client の4a準備

[English](global-runtime-client.en.md)

4a は [隔離 S1 server](global-server.md) を呼ぶ共通 client と登録・復旧・待機の entrypoint を準備します。設定が無ければ従来の旧 mode のままです。installer は helper を配置するだけで、global 設定を生成しません。activation は false で、実サービス・実 HOME・Mail DB の切替は行いません。

## 隔離設定

`AGENTSTACK_CLIENT_CONFIG` に owner の0600 JSONを明示します。cwd・旧 project env・tmux の name は宛先を選びません。省略時にだけ wrapper root の `runtime-client.json` を確認し、それも無ければ旧経路です。明示した設定が無い・不正・fenced のとき、旧経路へ fallback しません。

設定の kind は `orrery-runtime-client-s1`、mode は `global`、`activation_enabled=false`。`wrapper_root` はこの helper の install/checkout root、`isolation_root` は0700の専用一時 directoryです。以下を指定します。

- client の session/profile bookkeeping 用の private な `runtime_dir`。Mail state を示す S1 の `runtime_root` と分け、両方を隔離 root 内に置きます。
- S1 と同じ `runtime_root`、`authority`、`authority_lock`、`management_socket`、loopback の `mcp_url`。
- `expected_server_instance_id`、`candidate_generation`、`authority_epoch`。mutation revision は普通の書込みで変わるため、固定した世代として扱いません。
- `lock_identity` は共通 lock の `[st_dev, st_ino]`。wrapper 再起動でも保存した inode を照合します。
- `credential_file` は一時 root 内の0600 JSON。kind は `orrery-global-credential-v1`、同じ3 binding と `agent_id`、`credential_generation`、`registration_token` を保存します。token を argv に入れません。
- `identity` は stable `agent_id`、0以上の `credential_generation`、表示用 `name`。window の再接続を使うときは確定した `window_row_id` と `window_uuid` の両方を保存します。

新規登録の前だけ identity を null にし、`agentstack-runtime-client register NAME PROGRAM MODEL` を使います。whois で名前の空きを推測せず、原子的な register の `NAME_CONFLICT` を示します。未知の配送結果は pending journal を残して再登録を拒否します。新しい名前や token を自動で作って回避しません。既存 row の token が無ければ operator の claim が必要です。

## 実際の entrypoint

`agentstack-reregister` は保存済み ID/token で再接続し、server の canonical name を表示します。旧名前の引数は routing に使いません。contact policy は `tools/list` を見て、その能力が無い S1 には呼びません。`agentstack-await-reply` は同じ owner の inbox を読み、従来の sender/after-id/timeout の動作を保ちます。S1 では read/ack と signal clear を変更しません。

provider bootstrap は source した shell に canonical identity を export します。global の `agent-start`、`agent-start-codex`、`agent-start-gemini` は明示した context で登録して provider を起動します。この隔離準備経路は現在の terminal で実行し、新しい tmux session を自動生成しません。旧 mode の picker/tmux 起動は従来どおりです。試験では provider を shell stub に置き換え、実 LLM を起動しません。

名前解決と session policy は同じ context を照合します。session index の新形式は schema 3 / `global-self` で、instance・agent ID・credential generation・authority epoch と window を持ちます。別 agent の登録結果を自分の session に保存しません。旧 schema 2 は旧経路に残します。read-only inspect の window は `window_verification=local-only-server-unverified` と表示し、ready を立てません。再接続時には S1 register で server 照合し `server-verified-by-register` を返します。read-only window capability は S2 の候補です。

## operator の復旧と profile

`agentstack-enroll --global-context CONFIG inspect` は S1 の管理 socket に接続します。`claim REQUEST_ID EXPECTED_GENERATION` と `recover REQUEST_ID EXPECTED_GENERATION` は operator が明示して実行する入口です。モデルの認証失敗から自動実行しません。新 credential を0600の pending journal に保存してから CAS を要求し、同じ入力の再実行は同じ値を使います。receipt の再取得だけで成功とはせず、現在の inspect の generation/fingerprint と whois 認証を照合してから local credential と context を有効にします。専用 HOME に替えても明示した context が同じ ID を選びます。

`agentstack-runtime-client save-profile /absolute/private/profile.json` は project を持たない `orrery-global-client-profile-v1` を保存します。`agentstack-persistent inspect|reconnect --profile PROFILE` と `agentstack-daemon inspect --profile PROFILE` が binding を照合します。新 profile の proxy/daemon 実行は4cが担当します。4a の `run` は `GLOBAL_PROXY_RUNTIME_REQUIRES_PR4C` で拒否し、ready を true にしません。global context の無い旧 profile の実行は維持します。global context と旧 profile を混ぜた場合は `LEGACY_PROFILE_CONTEXT_CONFLICT` で拒否します。operator の `agentstack-daemon create --connection CONFIG --name NAME --journal PROFILE --operator` は同じ登録・profile 保存処理を使い、project を入力しません。`finalize-create --connection CONFIG --journal PROFILE --agent-id ID --operator` は、保存した pending credential を指定 stable ID で照合し、再登録せずに同じ profile を確定します。daemon 自体は起動しません。

## fence と残る作業

共通 lock の shared lock を操作中に保持し、起動時・通信前後に設定と authority を照合します。quiescing・retired root・異なる epoch/root/instance・lock inode の交換は拒否します。旧 mode を明示した fenced context も、起動前の同じ照合で止まります。active の S1 context を legacy として指定しても `LEGACY_CONTEXT_REQUIRES_PR7` で拒否します。lock を外して旧 curl に戻る処理を writer fence とは扱いません。設定無しの既定の旧 mode は従来どおり動き、旧 transport と authority の実 handoff は未実装です。設定無しの旧 wrapper を排除したとは扱いません。OS supervisor の停止、全 writer collector、実 pointer handoff は PR7 に残ります。同 UID の非協調 process による filesystem の名前交換は原子的に防げません。

S2 の送信・返信・予約・contact、4b の child manifest/resume/cleanup と bundled skill、4c の proxy/session binding/daemon/wake/配送世代は未接続です。Gemini stdio は context/authority 確認後に `GLOBAL_STDIO_REQUIRES_PR4C`、native Windows は明示 context/authority 確認後に `GLOBAL_S1_UNIX_CONTRACT_UNSUPPORTED` で拒否し、旧 project routing へ fallback しません。Windows の Unix socket/flock 代替は別の設計課題です。WSL は共通 Unix client を使います。一般の作業 directory や transcript の cwd は保存できますが、Mail routing の project に戻しません。

## 確認手順

[開発 venv](../CONTRIBUTING.md#test-environment) と新しい checkout を使います。`tests/test_global_runtime_client.py` は正本 S1 fixture が合成旧 DB を PR3 candidate に変換し、実 HTTP と管理 socket を専用 HOME・空き port で起動します。手元の DB を指定する必要はありません。server は各試験の finally で TERM/必要なら kill し、socket と一時 root を片付けます。

```sh
check_root=$(mktemp -d)
mkdir -m 700 "$check_root/home" "$check_root/tmux"
HOME="$check_root/home" TMUX_TMPDIR="$check_root/tmux" \
  PYTHONPATH=.:packages/agentstack_mail/src .venv/bin/python -m pytest -q \
  tests/test_global_runtime_client.py tests/test_await_reply.py \
  tests/test_reregister_diagnostics.py tests/test_persistent_agents.py
```

新契約・default旧経路・provider stub・世代/owner/window・復旧再実行を分けて評価します。全体の試験は同じ head を固定した別環境で1本ずつ流します。

## 自分で実 HTTP と管理 socket を確かめる

次の例を checkout の root で `PYTHONPATH=.:packages/agentstack_mail/src .venv/bin/python` に渡します。正本の合成 fixture を使い、保存 token と stable ID を実 server で照合します。token 自体は表示せず、最後に自分の server/socket/root を片付けます。

```python
import importlib.util
import os
from pathlib import Path
import tempfile
from pytest import MonkeyPatch

patch = MonkeyPatch()
private_home = tempfile.TemporaryDirectory(prefix='client-check-home-')
fixture = None
try:
    for key in list(os.environ):
        if key.startswith(('AGENTSTACK_', 'AGS_')) or key in {'TMUX', 'TMUX_PANE', 'CODEX_HOME', 'CLAUDE_CONFIG_DIR'}:
            patch.delenv(key, raising=False)
    patch.setenv('HOME', private_home.name)
    patch.setenv('AGENTSTACK_MAIL_ENV_FILE', str(Path(private_home.name) / 'absent.env'))
    path = Path('tests/test_global_runtime_client.py')
    spec = importlib.util.spec_from_file_location('client_check', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fixture = module.prepared.__wrapped__(patch)
    state = next(fixture)
    client = module.client(state)
    observed = client.reconnect()
    inspected = client.management('inspect', agent_id=observed['agent_id'])
    assert observed['agent_id'] == inspected['agent_id']
    assert client.local_owner() == state['token']
    assert client.call('health_check')['activation_enabled'] is False
    print('stable ID / preserved token / actual HTTP and socket: OK')
    print('client context:', state['client_path'])
    print('canonical name:', observed['name'])
finally:
    if fixture is not None:
        fixture.close()
    patch.undo()
    private_home.cleanup()
```

## 4a の入口と後続の担当

| 経路 | 4a の対応 | 残るもの |
|---|---|---|
| project context / launch / 共通 register・名前・contact | 明示 context を旧 env 読込より先に選ぶ。名前 preflight は使わず原子的登録。能力無しの contact 呼出を省く | 保護する作業 folder は一般の cwd の概念として残す |
| register/reregister / Claude・Codex・Gemini bootstrap / await | stable ID/token と authority を照合。専用 HOME、改名、window の register 照合 | 子の preregister・manifest は4b |
| resolve name / identity policy / session index | 同じ context と自己応答だけを schema 3 に保存 | model の登録 guard/reminder/skill は4b、proxy の external binding は4c |
| persistent / daemon profile / enrollment | 共通 global profile の保存・inspect・reconnect、明示 operator CAS | proxy/daemon 実行・wake は4c |
| Gemini stdio | authority の照合と固定 reason の拒否 | stdio proxy は4c |
| native Windows launcher | context/authority の検査と Unix 契約対象外の固定 reason | global transport の platform 設計は別。WSL は Unix client |

window の read-only server 照合 capability は S2 への候補です。S1 の fixture と wire を4aの都合で変更しません。
