### DeepSeek・Local の子も Claude の子と同じ利用者の skill を使う

この変更の後に起動した dsh と Hermes の子は、Claude の子が読む利用者の skill（`$CLAUDE_CONFIG_DIR/skills`、未設定なら `~/.claude/skills`）も読む。同じ名前の製品 skill より利用者の skill が優先され、Claude と同じ結果になる。利用者の skill の directory が無い環境は従来どおり。常駐 agent と、この変更より前に作った子は対象外で、profile を変えない。詳しくは [launcher の追加 harness](../docs/launchers.md#標準-spawn-の追加-harnessopt-in) を参照。

- dsh は利用者の skill の directory を `customSkillDirs` として渡す。native の順位で、子の `DSH_HOME/skills` に link した製品の skill より先に見つかる。
- Hermes は子の profile の `skills/` に利用者の skill を link し、同じ名前（ディレクトリ名か frontmatter の `name`）の製品 skill の link を外す。profile の skill は Hermes の探索で最も優先される。`config.yaml` は書かない。どの link を置いたかは profile の中に記録し、次の起動で入れ替える。
- 起動と再開のたびに読み直すので、`CLAUDE_CONFIG_DIR` の変更や利用者の skill の追加・削除が次の起動に反映される。
- 製品の `/delegate` は、自分の在りかを `$AGENTSTACK_HOME/skills/delegate/SKILL.md` と示すようにした。native の skill 一覧では、同じ名前の利用者 skill が代わりに出ることがある。
- 製品の `/log` は、Obsidian モードの条件を満たさないとき、どの条件が欠けているかを確かめて名指すようにした。
