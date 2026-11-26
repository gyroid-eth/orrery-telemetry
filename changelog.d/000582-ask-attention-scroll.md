### 承認待ちの agent に「!」を出す（長い確認でも）

Claude Code の承認の画面が長く、選択肢の下に「↑ N more lines」が出ると、dashboard が承認待ちと判定できず、ORRERY に「!」が出ていませんでした。dashboard の判定を `terminal_state` の判定と共有し、スクロールの印があっても承認待ちとして扱います。通常の権限の確認・hook の確認・MCP の確認で確かめています。
