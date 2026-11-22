# tmux consumer の共有境界

この draft の共有 helper は、旧 consumer の transport 固定と対象保護のためのものです。公開 Mail tool・dashboard API は増えません。既定の legacy と activation=false は維持し、専用 server での新規 global 起動はまだ有効にしません。cockpit の global terminal/protocol 移植は後続です。

## CLI と schema

入口は既存の `agentstack-runtime-client` です。JSON の正本は `bin/lib/runtime_tmux_contract.json`、検証は既存の `schema_contract.py` を共用します。

| command | 入力 | 出力 / 用途 |
|---|---|---|
| `tmux-capabilities` | なし | `contract=orrery-tmux-target-v1` と `legacy-transport` / `legacy-target-admission` / `legacy-target-diagnostics` / `legacy-window-target-admission`。版文字列だけで対応を判断しない |
| `tmux-legacy-transport` | なし | 照合済み socket・tmux の argv prefix・server PID・消す環境変数。consumer の起動時に固定する |
| `tmux-legacy-target` | `--session NAME` / `--pane %N` / `--window @N`、`--operation capture\|attach\|input\|split\|close\|kill-session\|signal` | 許可した対象の pane/PID/start と digest。拒否は exit2 と固定 reason。consumer は拒否対象に副作用を起こさない |
| `tmux-diagnostics` | なし | 同じ root reader による配置の完全性と、修復が必要な client_name / instance / check。socket は開かない |

server がまだ無い既定 legacy 起動は、接続先だけを返し server_pid は null です。socket/server/mutex を作らず、対象への操作は server の実体が確認できるまで拒否します。

全 command は読取専用です。任意 socket/root の CLI 引数、token、owner 認証の代用は受けません。返す `environment_unset` は `TMUX` と `TMUX_PANE` を消す差分です。親の全環境や credential を JSON に出しません。global owner の target/attach/compact/end は別の権限確認が必要で、この capability では提供しません。

consumer は起動時に返った socket_path / server_pid / argv を固定し、各操作でその同じ transport の admission を直前に取り直します。CLI は環境から解決するので、consumer は応答を schema で確認したうえで、起動時の値と必ず比較します。違えば副作用前に拒否します。共通の check_pinned_transport がこの比較を行います。初期 server_pid が null の場合だけ、同じ socket/argv に現れた最初の実 server PID をメモリ内で一度固定でき、その後の変更は拒否します。返った argv prefix に操作を追加し、`environment_unset` を適用します。名前だけの bare tmux に戻らず、明示 `-S` を維持します。session を消す操作は対象 session の全 pane を照合します。pane番号単独や名前 prefix は所属の証明にしません。

## 既定 endpoint と対象の保護

既定 endpoint は `TMUX_TMPDIR` と UID から決めます。継承した custom `TMUX` は UID所有/private directory の socket と実 server PID の一致を確認します。新しい global 固定 directory を legacy endpoint として使う起動は `GLOBAL_TMUX_LEGACY_CONSUMER_REFUSED` で拒否します。socket は tmux 標準の0660も許容しますが、親 directory は同 UID の0700を必須にして他者の到達を遮断します。既存 socket の chmod は行いません。

global 専用 endpoint の導出関数は、canonical client home と Mail instance の hash から短い絶対 path を作ります。epoch/TMPDIR/深いHOMEで endpoint を変えず、socket registry は作りません。この段階では新規起動から呼び出しません。

既存 env.sh の配置先は hooks/project-context.sh の literal reader で読み、env.sh 自体は実行しません。既存 config/context から RuntimeOperator が照合した client home を使い、利用者の wrapper home と隔離された client home を取り違えません。

旧対象の保護は、固定 `clients/` の context/credential/launch/live を既存の read-only snapshot 入口で照合します。新しい mutex・launch・親子台帳・SQLite handle は作りません。現在の pane/PID/start に global の既存 live が対応すれば `LEGACY_TARGET_IS_GLOBAL`。root が1つでも壊れている/partial/照合不能なら、任意名の standalone がどこにあるかを証明できないため、すべての target の副作用を拒否します。正常な root の情報を拾えても、この unknown を無視しません。helper が古い/無い場合の consumer は固定 layout の負の admission を使い、global 不在を証明できた純旧配置だけ従来の操作を続けます。

拒否対象の通知を成功消費せず、既存の理由表示/再試行に残します。配置の照合が完全で特定の global target だけを拒否した場合は、別の正常 legacy target の処理を続けます。read-only な一覧を一括停止するための境界ではありません。repair はまず tmux-diagnostics の client_name / mail_instance_id / check で原因を特定し、該当 config で agentstack-ui-client --server-config CONFIG status --client-name NAME を読みます。配置自体が不明な行は client_name が null です。root/config を修復して診断の global_complete=true を確かめ、同じ操作を再試行します。動いている root や pending を自動削除しません。他人の receipt を借りたり、未知の対象を名前で採用したりしません。

socket・pane一覧・PID/startは照合の前後で再確認します。ただし admission と外部 tmux 副作用の間を OS の原子的 transaction にするものではありません。同UIDの非協調 process による差し替えを完全に封じる権限境界とは説明しません。consumer は待機を挟まず直前に確認し、対象が変われば拒否します。

## consumer が答えとして扱う条件

stdout 全体が正しい contract を持つ単一 JSON で、command の schema・rc・reason と failure_kind が一致するときだけ答えとして扱います。rc0 は成功、rc2 は target/transport の確定した拒否です。後者を負の admission で覆してはいけません。TMUX_HELPER_FAILED は rc70 / failure_kind=helper。欠落・古い helper・空/壊れた stdout・schema/rc 不一致も helper unavailable とし、固定 layout の負の admission へ渡します。純旧不在を証明できるときだけ旧操作を続け、配置がある/不明なら拒否します。argparse の未知の command/operation の rc2・stdout 空も対象への拒否の答えではありません。

reason と分類の正本は JSON の reasons です。transport は接続先を確定できない拒否、target は当該対象を許可できない拒否、helper は内部不具合で答えが無い状態です。signal は PID/PGID へ作用する前の admission で、helper 自身は signal を送りません。

配置読取の timeout・期限超過・内部 KeyError/TypeError は root の破損を示す診断へ丸めず、TMUX_HELPER_FAILED として返します。読めた file の形式不一致や実在する不明な配置だけを target の拒否にします。

| reason | failure_kind |
|---|---|
| `TMUX_DIRECTORY_UNSAFE` | transport |
| `TMUX_SOCKET_UNSAFE` | transport |
| `TMUX_SOCKET_CHANGED` | transport |
| `TMUX_UNAVAILABLE` | transport |
| `TMUX_TARGET_UNAVAILABLE` | target |
| `TMUX_TARGET_INPUT_INVALID` | target |
| `TMUX_TARGET_CHANGED` | target |
| `TMUX_OPERATION_INVALID` | target |
| `LEGACY_TMUX_TRANSPORT_UNAVAILABLE` | transport |
| `GLOBAL_TMUX_LEGACY_CONSUMER_REFUSED` | transport |
| `LEGACY_TARGET_IS_GLOBAL` | target |
| `GLOBAL_TARGET_OWNERSHIP_UNKNOWN` | target |
| `TMUX_CLIENT_HOME_INVALID` | target |
| `TMUX_INSTANCE_REQUIRED` | target |
| `TMUX_SOCKET_PATH_TOO_LONG` | transport |
| `TMUX_HELPER_FAILED` | helper |
| `GLOBAL_ROOT_UNAVAILABLE` | target |
| `GLOBAL_PLACEMENT_UNAVAILABLE` | target |

## 実装段階

Telemetry の transport/consumer と公開 cockpit の旧対象保護を別 PR にします。共有 CLI をこの draft で固定し、cockpit の fixture にはこの実コードと installer 正本の payload を配備します。双方の merge と配布 launcher の対応前に専用起動を有効にしません。新しい rollout flag や確認台帳を作りません。

試験は一時 HOME、短い一時 directory、隔離した `-L` server を使い、実行境界は照合済みの絶対 `-S` へ固定します。既定の live tmux server、実 provider HOME、実 Mail DB は使いません。

共有 helper のコード namespace は consumer process ごとに一度だけ読みます。更新後は consumer を再起動します。env.sh の literal 読取結果は env.sh と reader の dev/inode/mtime/ctime/size/mode/UID が同じ間だけ再利用します。context/live の配置観測は呼出しごとに読み、保存しません。

`legacy-window-target-admission` は window の現在の全 pane を、共有の前後再照合で確認します。元 session や選択済み active pane への読み替えはしません。consumer の窓の復元も同じ入口を使います。
