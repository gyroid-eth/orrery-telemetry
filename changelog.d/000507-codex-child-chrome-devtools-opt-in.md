### Codex child の Chrome MCP を明示選択に変更

Codex child の生成設定では、`chrome-devtools` と `chrome-devtools-mcp` を直接起動する別名の server を、道具として選んだ場合だけ有効にします。

Chrome 拡張と連携する `chrome@openai-bundled` も、`--tools chrome-extension` で選んだ場合だけ有効にします。Dashboard API 世代 44 では `tools.chrome_extension: true` で選択できます。
