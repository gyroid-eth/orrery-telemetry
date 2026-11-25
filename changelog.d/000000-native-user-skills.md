### DeepSeek・Local の子も Claude の子と同じ利用者の skill を使う

dsh と Hermes の子は、Claude の子が読む利用者の skill（`$CLAUDE_CONFIG_DIR/skills`、未設定なら `~/.claude/skills`）も読むようになった。同じ名前の製品 skill より利用者の skill が優先され、Claude と同じ結果になる。利用者の skill の directory が無い環境は従来どおり。詳しくは [launcher の追加 harness](../docs/launchers.md#標準-spawn-の追加-harnessopt-in) を参照。

- dsh は利用者の skill の directory を `customSkillDirs` として渡す。native の順位で、子の `DSH_HOME/skills` に link した製品の skill より先に見つかる。
- Hermes は子の profile の `skills.external_dirs` の先頭に置き、同じ名前の製品 skill の link を外す。以前の起動で足した directory だけを入れ替え、それ以外の項目は保持する。
- 起動と再開のたびに読み直すので、`CLAUDE_CONFIG_DIR` の変更や利用者の skill の追加・削除が次の起動に反映される。
- 製品の `/log` は、Obsidian モードの条件を満たさないとき、どの条件が欠けているかを確かめて名指すようにした。
- Hermes の設定を書き換える処理が、ブロック形式のリストを置き換えたときに YAML を壊さないようにした。
