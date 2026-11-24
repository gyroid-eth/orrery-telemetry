### Mac の子の Computer use を Cua Driver に統一

- NEW AGENT、`/api/spawn`、`--tools` の macOS の画面操作で、Claude Code、Codex CLI、dsh、Hermes、Antigravity CLI は既存の `orrery-screen` を使います。起動ごとの Off／View／Control と呼び出し履歴を引き継ぎます。
- 生成する Codex CLI のホームでは、同梱の GUI 操作経路を外します。Hermes の子は組み込みの Cua Driver toolset と relay の同時読み込みを避けます。
