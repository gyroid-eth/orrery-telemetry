### 停止済みの子の権限を一つずつ変更して再開できます

Dashboard API 世代 45 の `/api/jump` に `permission` を追加しました。停止と会話の一致を確認できた Claude／Codex CLI の子について、元の `base` と他の道具・承認を維持したまま、Computer use または Browser use を切り替えます。起動に必要な構成を用意できない場合は理由を返します。[再開 API](../docs/api.md#post-apijump)
