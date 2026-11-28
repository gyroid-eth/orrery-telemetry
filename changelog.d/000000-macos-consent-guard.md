### macOS の同意ダイアログを要求する操作を事前に拒否

tmux 内の Claude Code、dsh、Hermes、Antigravity CLI が、画面収録や Automation の同意を要求するコマンドを実行前に止めます。拒否時は Cua Driver とブラウザ用の経路を案内します。

ORRERY が起動・再開する Codex では、macOS の撮影・Automation・Keychain と WSL の Secret Service を呼ぶ指定コマンドを exec policy で拒否します。このルールには hook の承認操作が要りません。
