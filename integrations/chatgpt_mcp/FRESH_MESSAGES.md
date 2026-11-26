# 第2段階：試験用fresh Mailへの追加指示（明示的opt-in）

[Issue #395の合意](https://github.com/gyroid-eth/orrery-telemetry/issues/395#issuecomment-7195231301)に基づく独立した任意の送信アダプターです。本番fresh切替の受入が成立するまでは、試験用Mail instanceだけで使用します。通常のinstaller・daemon・UI・本番サービス・Mailの公開APIは変更しません。

従来の `python -m orrery_chatgpt_mcp` と `python -m orrery_chatgpt_mcp.server` は読み取り専用のままです。別入口 `python -m orrery_chatgpt_mcp.fresh_server` も既定OFFで、送信にはfresh専用の設定を明示します。常駐化・配布・spawn・旧方式の送信者や資格の自動移行は対象外です。

## 固定する権限

運用者が試験環境用に準備済みの保護されたfresh client contextを使います。送信は `FreshBoundClient.call('send_message', ..., request_id=...)` で、正規CLIの `agentstack_mail.fresh_client call-json --context <固定context> --tool send_message --request-id <UUID>` と同じ実装です。アダプターはcontextの作成・登録、資格の再実装、Mail DBの直接読書きを行いません。

private policyでMail instance、送信者の安定ID、1〜16件の宛先を固定します。宛先は `target_agent_id`・`expected_target_name`・`kind=to` の組です。1要求は1宛先で、名前だけの指定や同名の別IDへのfallbackはありません。17件以上は設定エラーにし、既存の宛先を削除・切り詰め・置換しません。MCP入力からcontext・資格・接続先・送信者・project・request UUIDを選ぶことはできません。

previewのwhoisは送信時の認可を代替しません。Mail側が新規送信のtransaction内で現在の資格、宛先ID・期待する名前・退役・contact policyを検査します。接続先Dashboardも同じfresh Mail instanceであることが必要です。

これは運用者が許可先への送信権を接続クライアントに事前委任する機能です。ChatGPT上の確認やpreview/commitの順序を、サーバーが本人承認として検証するわけではありません。秘密URLとTunnelへの到達権を持つ相手は、このpolicyの許可先へ送信できます。宛先allowlistは送信の範囲であり、freshの読み取りは同一instance全体のrosterを公開します。

## 試験環境での手動起動

[開発用venv](../../CONTRIBUTING.md#test-environment)に、このcheckoutのMail packageと依存を用意します。既存サービスのvenvは使い換えません。運用者が確認した試験用context、instance、送信者ID、宛先IDと名前で、次の形のpolicyを作成します。以下の値は説明用であり、確認済みの実値へ置き換えます。

```json
{
  "kind": "orrery-mcp-fresh-message-policy-v1",
  "context_path": "/absolute/private/test-fresh/clients/confirmed-client-uuid/context.json",
  "mail_instance_id": "0123456789abcdef0123456789abcdef",
  "sender_agent_id": 1,
  "recipients": [
    {"target_agent_id": 2, "expected_target_name": "ConfirmedRecipient", "kind": "to"}
  ]
}
```

policyはowner所有の0600通常ファイル、専用stateは0700の絶対canonical pathにします。symlink・hardlink・重複JSONキーを拒否します。既存の読み取り用stateや秘密URLを送信用へ流用しません。checkoutのrootから、新しいターミナルで空いているloopbackポートと試験用Dashboardを指定します。

```sh
export PYTHONPATH="$PWD/integrations/chatgpt_mcp/src:$PWD/packages/agentstack_mail/src"
export ORRERY_MCP_MODE=fresh
unset ORRERY_MCP_PROJECT_KEY
export ORRERY_MCP_STATE_DIR=/absolute/private/test-mcp-state
export ORRERY_MCP_FRESH_MESSAGE_POLICY=/absolute/private/test-message-policy.json
export ORRERY_MCP_DASHBOARD_URL=http://127.0.0.1:8770
export ORRERY_MCP_PORT=8792
export ORRERY_MCP_ENABLE_SPAWN=0
ORRERY_MCP_ENABLE_MESSAGE=0 .venv/bin/python -m orrery_chatgpt_mcp pair --show-url
export ORRERY_MCP_ENABLE_MESSAGE=1
.venv/bin/python -m orrery_chatgpt_mcp.fresh_server check
.venv/bin/python -m orrery_chatgpt_mcp.fresh_server serve
```

DashboardのURLは実際の試験用listenerに合わせ、使用中のポートは指定しません。checkは送信せず、固定権限とDashboardのinstanceを検証します。serveは検証後に127.0.0.1だけで待ち受けます。稼働中のpolicy・contextの変更や失効は新規送信前に拒否します。

Tunnelを使う場合は[既存の接続手順](README.md#secure-mcp-tunnelの設定)に従い、この試験用入口に専用Tunnelを用意します。所有するorganization/workspace、共有する利用者・グループ、runtime keyのRead/Use権限、ChatGPT接続の共有範囲を確認してください。秘密pathと固定Hostを保ち、Origin検査を解除しません。READMEのURL抽出コマンドは、その単発のPython呼出しだけ `ORRERY_MCP_ENABLE_MESSAGE=0` で実行します。稼働中freshプロセスの設定を変更する操作ではありません。Mailの実効的な本文ログ設定を確認し、秘密URLが含まれ得るTunnelログもowner-privateに保ちます。アダプターは別プロセスのログ設定を自動変更しません。

## preview・commit・status

有効時は読み取り3ツールに `orrery_message_preview`、`orrery_message_commit`、`orrery_message_status` を加えます。previewの入力は `idempotency_key`、`mail_instance_id`、`target_agent_id`、`expected_target_name`、`subject`、`body_md` だけです。宛先・件名・本文全文・digest・preview ID・request UUIDと5分の期限を返します。件名はUTF-8で200byte以内、本文は65536byte以内です。空文字や制御文字を拒否し、本文のLFとTABだけを許可します。

commitの入力は同じkey・preview ID・digestだけで、本文や宛先を差し替えられません。同じkeyを別の内容に使うと拒否します。Mailを呼ぶ前にkeyとUUIDの対応、全文とdigest、送信開始のclaimをprivate SQLite台帳へ永続保存します。同時commit、別プロセス、再起動でもclaim済み要求を再送しません。UUIDはinstance・sender・keyのhashから決まり、台帳を削除してよいという意味ではありません。

acceptedは、保存した送信内容に対応する正規send応答のinstance・sender・recipient・message ID、`decision_only=false`、`delivery_status=notification_pending`、thread・作成時刻を確認し、受領記録を保存できた状態です。正規sendは本文・宛先・request outcomeを同じtransactionで保存します。空応答やdecision-onlyはacceptedにしません。通知到達やagentの処理開始とは別で、`execution_status=unobserved`のままです。

statusは保存済みのローカル台帳と受領記録だけを読みます。MailやDashboardへの照会、受領照会API、replay、送信、再送は行わず、`readOnlyHint=true`です。応答消失や送信中の中断で確証がなければunknownを維持します。繰り返しstatusやcommitを呼んでも再実行しません。後から運用者がMail側を確認しても、その操作をこのstatusが自動観測することはありません。

台帳を読む際は固定policy、保存本文と宛先、digest、keyに対応するUUID、状態・期限・受領記録の整合性を検証します。破損は `message_state_invalid` で停止します。この検査は同じOS利用者による任意の改変を防ぐ署名ではありません。台帳は最大10000要求で新規previewを拒否し、過去の証拠を自動削除しません。台帳とprotected clientのrequestsには本文が保存されるため、0700/0600を維持して保全します。資格は台帳にコピーせず、commit/statusは本文やローカルパスを返しません。

## 停止・復旧・無効時の確認

停止は、この試験用foregroundプロセスのCtrl+Cです。送信を無効にするには停止後に `ORRERY_MCP_ENABLE_MESSAGE=0` で起動し直します。環境変数やpolicyファイルの書換えだけで、既に動くプロセスが無効になったとは扱いません。

接続した実クライアントでinitialize、notifications/initialized、tools/list、orrery_statusを順に確認します。無効時は読み取り3ツールだけ、`read_only=true`、`spawn_enabled_on_server=false`、`message_enabled_on_server=false`です。commitの直接呼出しは `isError=true`、`error.code=unknown_tool` で拒否されます。秘密なし404やhealthzだけでは送信禁止の証明になりません。有効時は6ツールとmessage flag=true、spawn flag=false、意図した送信者・許可先を確認します。

unknownや台帳不整合では台帳とclientのrequestsを保全し、運用者が元の要求と受領の有無を確認します。replayは初回が未到達なら新たに送信を成立させる操作なので、状態確認には使いません。再実行は運用者の明示的な判断に限り、アダプターは復旧送信を提供しません。台帳削除、別keyの発行、上限超過policyの切り詰めで自動復旧しないでください。

## 検証範囲

試験は一時fresh root、合成登録、専用動的port、隔離したHOMEを使います。固定権限、本文・宛先保存、UUID、再起動、応答消失、並行commit、退役、unknown維持、台帳破損、無効時拒否とMCP SDKの接続を検査します。実ChatGPT・ホストされたTunnelのACL・実agentの処理開始を証明するものではありません。
