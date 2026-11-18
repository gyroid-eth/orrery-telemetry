### NEW AGENT から親なしで立てた Codex は、普段の承認設定で起動・再開

NEW AGENT で親を指定しない起動と、その記録があるセッションを dashboard から再開するときは `--ask-for-approval` を省略します。Codex は有効な設定の `approval_policy` か自身の既定値を使います。親付きの起動（delegate の子など）と、親なし起動の記録がないセッションの再開では、既定の `never` または明示した `AGENTSTACK_CODEX_CHILD_APPROVAL` を使います。詳しくは [delegation](../docs/delegation.md) を参照してください。
