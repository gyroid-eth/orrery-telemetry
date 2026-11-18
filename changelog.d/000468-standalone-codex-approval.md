### NEW AGENT から親なしで立てた Codex は、普段の Codex の承認設定で起動

NEW AGENT で親を指定せずに新しく起動した Codex には `--ask-for-approval` を付けず、有効な設定の `approval_policy` か Codex の既定値を使います。親付きの起動（delegate の子など）は従来どおり既定の `never` と、明示した `AGENTSTACK_CODEX_CHILD_APPROVAL` を使います。dashboard からの再開は対象外で、従来の承認設定のままです。詳しくは [delegation](../docs/delegation.md) を参照してください。
