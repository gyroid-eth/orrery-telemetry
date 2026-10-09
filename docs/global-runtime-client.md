# global runtime client の4a準備

[English](global-runtime-client.en.md)

4a は [隔離 S1 server](global-server.md) を呼ぶ共通 client と登録・復旧・待機の entrypoint を準備します。設定が無ければ従来の旧 mode のままです。installer は helper を配置するだけで、global 設定を生成しません。activation は false で、実サービス・実 HOME・Mail DB の切替は行いません。

## 隔離設定

`AGENTSTACK_CLIENT_CONFIG` に owner の0600 JSONを明示します。cwd・旧 project env・tmux の name は宛先を選びません。省略時にだけ wrapper root の `runtime-client.json` を確認し、それも無ければ旧経路です。明示した設定が無い・不正・fenced のとき、旧経路へ fallback しません。

設定の kind は `orrery-runtime-client-s1`、mode は `global`、`activation_enabled=false`。`wrapper_root` はこの helper の install/checkout root、`isolation_root` は0700の専用一時 directoryです。以下を指定します。

- `agentstack_home` は必ず `isolation_root/agentstack` です。固定の `clients` 親の直下を、単一成分の `client_name`（`[a-z0-9][a-z0-9_-]{0,63}`）で選びます。これは local slot 名で、Mail の宛先名ではありません。client root は `agentstack_home/clients/client_name` から導出します。home・親・client root・その `runtime` directory は0700で用意し、context は固定の `client_root/runtime-client.json` です。任意の home 配置は `CLIENT_HOME_MISMATCH`、不正な slot 名は `CLIENT_NAME_INVALID` で拒否します。
- S1 と同じ `runtime_root`、`authority`、`authority_lock`、`management_socket`、loopback の `mcp_url`。
- `expected_server_instance_id`、`candidate_generation`、`authority_epoch`。mutation revision は普通の書込みで変わるため、固定した世代として扱いません。
- `lock_identity` は共通 lock の `[st_dev, st_ino]`。wrapper 再起動でも保存した inode を照合します。
- 固定の `client_root/credential.json` は0600 JSON。kind は `orrery-global-credential-v1`、同じ3 binding と `agent_id`、`credential_generation`、`registration_token` を保存します。token を argv に入れません。
- `identity` は stable `agent_id`、0以上の `credential_generation`、表示用 `name`。window の再接続を使うときは確定した `window_row_id` と `window_uuid` の両方を保存します。

新規 identity には新しい `client_root` を用意します。使用済みディレクトリの metadata/session が別 identity に属する場合は `RUNTIME_DIR_BELONGS_TO_OTHER_IDENTITY` で network 前に拒否し、自動で削除・置換しません。新規登録の前だけ identity を null にし、`agentstack-runtime-client register NAME PROGRAM MODEL` を使います。whois で名前の空きを推測せず、原子的な register の `NAME_CONFLICT` を示します。未知の配送結果は pending journal を残して再登録を拒否します。新しい名前や token を自動で作って回避しません。既存 row の token が無ければ operator の claim が必要です。

## PR4a で決めた固定 local layout

固定の親の直下にある単一成分の `client_name` だけを選びます。旧 `client_root` / `runtime_dir` / `credential_file` の任意 path 指定と、導出した `client_root/runtime-client.json` 以外の context は `FIXED_LAYOUT_CONTEXT_REQUIRED` で拒否します。global の profile 保存には path を渡しません。`--journal` や `save-profile` の引数は `FIXED_LAYOUT_PATH_NOT_SUPPORTED` で登録前に拒否し、成功時は固定の `profile_path` を返します。

| role | client_root からの固定 path |
| --- | --- |
| context | `runtime-client.json` |
| credential | `credential.json` |
| 登録 pending | `credential.registration-pending.json` |
| 復旧 pending | `credential.enrollment-pending.json` |
| 登録 metadata | `runtime/registration-metadata.json` |
| profile | `profile.json` |
| 自分の session 記録 | `runtime/session_index/self.json` |

client root は兄弟なので入れ子になりません。固定の親と Mail runtime_root の双方向の重なり、親の中の authority/lock/socket を拒否し、private な親から root の chain に symlink がないことを検査します。兄弟・子孫・provider HOME/履歴は探索しません。数千の provider file や過去の記録があっても ready の判定は変わりません。1 root の現在の session 記録は `runtime/session_index/self.json` の1 slotで、stable agent ID は内容に保存し、他の固定出力と一緒に照合します。無関係な履歴ファイルを routing や所有権の根拠にしません。

1か所の preflight plan が、この全出力を network 前に検査します。client と Mail の root の双方向の重なり、client root 内の authority/lock/socket は `CLIENT_ROOT_OVERLAP`、alias・不正 directory・外国形式も拒否します。writer は固定 slot だけを書き、未知 role や別 role の directory を指定できません。保存先の不正を登録 commit 後に初めて発見する動作をなくします。network 成功後の disk I/O 失敗は別の不確定結果で、pending journal/receipt を残し、同じ identity を明示 finalize します。remote commit を原子的に巻き戻したとは表示しません。

## 実際の entrypoint

`agentstack-reregister` は保存済み ID/token で再接続し、server の canonical name を表示します。旧名前の引数は routing に使いません。program/model の明示引数・env・provider model は登録に反映し、成功した値を private な `client_root/runtime/registration-metadata.json`（`orrery-global-registration-metadata-v1`）に保存します。context は変えないので、別 process の通常再登録で待機中の client を止めません。旧 context の `registration_metadata` は読込み互換だけ維持します。無指定の再接続は保存値で再送します。保存値が無い既存 identity は S1 whois が program/model を返さないため register を呼ばず、`reconnect_mode=observe-only` / `registration_verified=false` を返し、server row を上書きしません。この場合の reregister は `observed ... (registration not refreshed; pass program/model)`、exit 3 です。明示または保存値で register を行った場合だけ `registered`、exit 0 になります。contact policy は `tools/list` を見て、その能力が無い S1 には呼びません。`agentstack-await-reply` は同じ owner の inbox を読み、従来の sender/after-id/timeout の動作を保ちます。一時的な `TRANSPORT_FAILED` は timeout まで再試行し、authority の変更は即停止します。S1 では read/ack と signal clear を変更しません。

provider bootstrap は source した shell に canonical identity を export します。global の `agent-start`、`agent-start-codex`、`agent-start-gemini` は明示した context で登録して provider を起動します。この隔離準備経路は現在の terminal で実行し、新しい tmux session を自動生成しません。旧 mode の picker/tmux 起動は従来どおりです。試験では provider を shell stub に置き換え、実 LLM を起動しません。

名前解決と session policy は同じ context を照合します。session index の新形式は schema 3 / `global-self` で、instance・agent ID・credential generation・authority epoch と window を持ちます。別 agent の登録結果を自分の session に保存しません。旧 schema 2 は旧経路に残します。read-only inspect の window は `window_verification=local-only-server-unverified` と表示し、ready を立てません。明示または保存した program/model で register する再接続時には S1 register で server 照合し `server-verified-by-register` を返します。observe のみの再接続は server 未照合のままです。read-only window capability と、program/model を変更せず登録を更新する能力は S2 の候補です。

credential・authority・lock・config・管理 socket・pending journal の保存先が同じ path / 解決先 / dev・inode を指す場合は、各呼出し前後に `CONTEXT_PATH_ROLE_CONFLICT` で拒否します。profile にこれらの保存先を使うことも拒否します。implicit context が broken symlink の場合も不在扱いにせず停止します。client context・credential・journal・metadata・profile・session index は Mail の `runtime_root` 配下と、その symlink の解決先を保存先にできません。既存の出力先は role の magic/schema と binding・owner を確認し、SQLite・任意テキスト・別 owner の形式は network 前に拒否します。symlink・複数 hardlink も拒否します。新しい0600の一時ファイルは O_EXCL で作り、rename の直前にも既存出力先の形式を再検査します。非協調の同 UID actor が最後の検査と rename の間に名前を差し替える競合まで原子的に排除する保証ではありません。

保存形式の magic は context の `orrery-runtime-client-s1`、credential の `orrery-global-credential-v1`、登録 pending の `orrery-global-registration-pending-v1`、復旧 pending の `orrery-global-enrollment-pending-v1`、metadata の `orrery-global-registration-metadata-v1`、profile の `orrery-global-client-profile-v1` です。session index は schema 3 / `global-self` です。型・binding・owner が違う既存ファイルは自動で移行や上書きをせず、入力先を直す operator 操作が必要です。

released 版の raw token（任意の identity sidecar）、旧 persistent profile、schema 2 / self の session index を global の固定保存先に置くと、`LEGACY_FILE_REQUIRES_IMPORT` で network 前に拒否し、bytes を維持します。これらの明示 operator import は PR7 の担当で、4a は自動変換しません。

## operator の復旧と profile

`agentstack-enroll --global-context CONFIG inspect` は S1 の管理 socket に接続します。`claim REQUEST_ID EXPECTED_GENERATION` と `recover REQUEST_ID EXPECTED_GENERATION` は operator が明示して実行する入口です。モデルの認証失敗から自動実行しません。新 credential を0600の pending journal に保存してから CAS を要求し、同じ入力の再実行は同じ値を使います。receipt の再取得だけで成功とはせず、現在の inspect の generation/fingerprint と whois 認証を照合してから local credential と context を有効にします。専用 HOME に替えても明示した context が同じ ID を選びます。

`agentstack-runtime-client save-profile` は project を持たない `orrery-global-client-profile-v1` を保存します。`agentstack-persistent inspect|reconnect --profile PROFILE` と `agentstack-daemon inspect --profile PROFILE` が binding を照合します。新 profile の proxy/daemon 実行は4cが担当します。4a の `run` は `GLOBAL_PROXY_RUNTIME_REQUIRES_PR4C` で拒否し、ready を true にしません。global context の無い旧 profile の実行は維持します。global context と旧 profile を混ぜた場合は `LEGACY_PROFILE_CONTEXT_CONFLICT` で拒否します。operator の `agentstack-daemon create --connection CONFIG --name NAME --operator` は同じ登録・profile 保存処理を使い、project を入力しません。`finalize-create --connection CONFIG --agent-id ID --operator` は、保存した pending credential を指定 stable ID で照合し、再登録せずに同じ profile を確定します。daemon 自体は起動しません。

明示した `finalize-create` と claim/recover の再実行に限り、固定 pending journal の identity を復旧用 owner として照合します。journal の binding/ID、現在の credential の token/generation、認証した server row が一致する場合だけ復旧します。通常の identity=null 入口は使用済み root を引き続き拒否します。有効化の書込み順は credential → context → 登録 metadata（存在する場合）の1か所に統一し、各 slot 保存後の中断を回帰で確認します。再実行は register/CAS を追加せず同じ ID に戻します。登録 pending は有効化後に消し、それ以降の profile 保存失敗は検証済み identity の明示 finalize で修復できます。request 前に保存した journal だけでは server row の存在を証明しません。不一致なら local bytes と server を変更しません。 credential 更新 pending には更新前の credential の世代と fingerprint も保存し、保存前後のどちらの途中状態かを照合します。未 claim の row に古い local credential が残る場合も、明示 claim の同じ pending と CAS receipt で確認します。 local credential が無い null-token row の claim も支えます。この場合、pending の更新前 generation/fingerprint は両方 null です。再実行は credential が依然として無い状態、またはその pending の新 credential が保存済みの状態だけを受け入れます。

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

S2a の明示 schema3 server に接続すると、能力に応じて program/model を保つ登録更新・server 側の read-only window 照合・receipt の UUID 引継ぎを使います。S1 の動きは維持します。新 candidate と client root を選ぶときは [schema2 の拒否と呼出し側の向け直し](global-server.md#schema2-を拒否されたとき)を確認してください。

S2a の program/model 無指定 reconnect は活動更新と照合だけを行い、contact policy を自動適用しません。変更は明示した `set_contact_policy` tool、または `AGENTSTACK_CONTACT_POLICY` を指定した `runtime_client.py --context CONFIG policy` で行います。容量上限や既存 pending は mapped reconnect を止めず、policy の明示変更だけに適用します。

receipt の応答は tools/list の outputSchema と owner/binding/UUID を確認してから pending を消します。schema 不適合なら planned を保ち、同 UUID で正常応答を replay します。server と stdlib client は同じ `schema_contract.py` を使い、installer はその正本を wrapper の `bin/lib/` にコピーします。client 用の別 validator や新しい外部依存はありません。

canonical preimage・既定値・TTL 補正も同じ stdlib 正本を使います。tool の分類と固定 reason は wire fixture から読み、installer はその fixture も helper と同じ `bin/lib/` にコピーします。確定した拒否（相手不存在・quota 等）では自分の pending bytes を再照合して解除し、別の意図の操作へ進めます。通信失敗・応答不適合・未知 reason は保持します。既存 pending の認証/fence 拒否は以前の結果を証明しないため保持します。固定 reason とその拒否時点の一覧は `client_uuid_contract.definite_rejections` が正本です。
