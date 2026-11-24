### スマホの Reader にコンテキスト圧縮の進行と完了を表示

- Claude Code・Codex CLI・dsh・Hermes の圧縮中は **Compacting context…** と観測できた経過時間を表示し、完了記録は **Context compacted** の 1 行で表示します。Claude Code の重複する圧縮コマンドと完了出力は畳み、ローカルコマンド出力の色制御文字は取り除きます。
- API 47 の旧運用の `/api/agents` とスマホ用 snapshot に任意の `reader_phase` と `reader_phase_elapsed` を追加しました。
