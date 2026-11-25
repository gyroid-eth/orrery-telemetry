### DeepSeek・Local の dsh の子も Claude の子と同じ利用者の skill を使う

dsh の子は、Claude の子が読む利用者の skill（`$CLAUDE_CONFIG_DIR/skills`、未設定なら `~/.claude/skills`）も読むようになった。同じ名前の製品 skill より利用者の skill が使われ、Claude と同じ結果になる。利用者の skill の directory が無い環境は従来どおり。Hermes の子は従来どおり製品の skill を使う。詳しくは [launcher の追加 harness](../docs/launchers.md#標準-spawn-の追加-harnessopt-in) を参照。

- dsh-tui では skill の探索を agent preset が持つので、起動計画の環境に `DSH_AGENTS_HOME`（Claude の設定 dir）を渡す。同じ名前（ディレクトリ名か frontmatter の `name`）の製品 skill は子の `DSH_HOME/skills` に link せず、前の起動の link は行き先が製品の skill と一致するものだけ外す。利用者の設定や skill は書き換えない。
- 起動と再開のたびに読み直すので、`CLAUDE_CONFIG_DIR` の変更や利用者の skill の追加・削除が次の起動に反映される。
- 利用者の skill の directory が解決できない（存在しない `~user` など）か、中に読めない skill があるときは、製品の skill だけで起動し、理由を子の `personal-skills.json` に残す。dsh は一つでも読めない skill があると、製品の skill ごと一覧を失うため。
- `DSH_AGENTS_HOME` を変えるので、dsh 既定の `~/.agents` は読まれなくなる。
- 製品の `/log` は、Obsidian モードの条件を満たさないとき、どの条件が欠けているかを確かめて名指すようにした。
