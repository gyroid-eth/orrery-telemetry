### DeepSeek・Local の dsh の子も Claude の子と同じ利用者の skill を使う

dsh の子は、Claude の子が読む利用者の skill（`$CLAUDE_CONFIG_DIR/skills`、未設定なら `~/.claude/skills`）も読むようになった。同じ名前の製品 skill より利用者の skill が優先され、Claude と同じ結果になる。利用者の skill の directory が無い環境は従来どおり。Hermes の子は従来どおり製品の skill を使う。詳しくは [launcher の追加 harness](../docs/launchers.md#標準-spawn-の追加-harnessopt-in) を参照。

- 利用者の skill の directory を、起動ごとの native config の `customSkillDirs` として渡す。native の順位で、子の `DSH_HOME/skills` に link した製品の skill より先に見つかる。子の profile や利用者の設定は書かない。
- 起動と再開のたびに読み直すので、`CLAUDE_CONFIG_DIR` の変更や利用者の skill の追加・削除が次の起動に反映される。
- 製品の `/log` は、Obsidian モードの条件を満たさないとき、どの条件が欠けているかを確かめて名指すようにした。
