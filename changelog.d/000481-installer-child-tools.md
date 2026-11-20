### NEW AGENT と dsh・Hermes に installer の既定の道具を適用

API 41 から、標準の `/api/spawn`（NEW AGENT）で `base` と `tools` を省略すると、Claude・Codex・dsh・Hermes の子に `AGENTSTACK_CHILD_DEFAULT_TOOLS` を適用します。CLI の dsh・Hermes も同じ設定を使います。明示指定を優先し、使えない既定の項目は通知して外します。resume は起動時に記録した選択を使います。[既定の道具](../docs/delegation.md#installer-の既定の道具)

installer の共通の一覧に `;dsh=` などを付けると、provider ごとの一覧を設定できます。`--child-default-tools 'mcp:cua-driver;dsh='` は dsh 以外に `cua-driver` を渡します。

複数行の依頼を `/api/spawn` から送ったとき、依頼メールの件名を1行に整えます。依頼文はメール本文にそのまま載せます。
