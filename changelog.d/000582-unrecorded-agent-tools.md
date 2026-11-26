### 起動記録のないエージェントの画面操作・ブラウザーの状態を表示

稼働中の Claude Code／Codex CLI は、起動記録がなくても認識できたローカルの道具と起動フラグを Cockpit とスマホの Reader に表示します。プロセスの引数や画面 relay のモードを確認できない場合は、Unknown とその理由を表示します。

API 52 の `/api/agents` に `permission_source`、`computer_use_reason`、`browser_use_reason` を追加しました。詳しくは [API の権限表示](../docs/api.md#get-apiagents) を参照してください。
