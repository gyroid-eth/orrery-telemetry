### Codex の子で利用者の hook が毎回確認待ちになる問題を直す

Codex の子は専用の CODEX_HOME で利用者の `hooks.json` を別のパスから読むため、利用者が信頼済みの同じ hook も「N hooks need review」で止まり、確認されるまで動きませんでした。子の `config.toml` に、利用者の CODEX_HOME の hook に与えた信頼を子のパスで引き継ぎます。

信頼は hook の内容の hash で、Codex が読み込み時に照合し直すため、内容が変わった hook は引き続き確認待ちになります。利用者が信頼していない hook は追加せず、子のパスに既にある記録は保持し、利用者の設定ファイルは変更しません。worktree の project hook の引き継ぎ（#174）と同じ方式です。
