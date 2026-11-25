### 起動記録のないエージェントにも、画面操作とブラウザーの状態を出し、選んだ権限を次の起動で使う

常駐の agent や Resume で再開した会話、別の起動 script で始めた Claude Code には子のような起動記録がなく、画面操作とブラウザーは「不明」と表示され、UI から変えられませんでした。

- 稼働中なら実際のプロセスから読みます。画面操作は Cua Driver・`orrery-screen` の子プロセスの有無で、ブラウザーは Claude の `--chrome`／`--no-chrome` と Claude in Chrome の既定値、Codex の `chrome-devtools-mcp` の子プロセスで判定します。ユーザー設定には入っているのに今のプロセスにない道具は、再起動待ちとして印を付けます。スマホの Reader の見出しにも同じ状態を出します。
- UI で選んだ権限を runtime の `agent-permissions/<name>.json` に保存し、dashboard の Resume、`agentstack-persistent run`、新しい `agentstack-agent-permissions exec` が次の起動で使います。画面操作は子と同じ `orrery-screen` を通し、ユーザー設定の Cua Driver と内蔵の computer use はその起動で拒否します。`--channels` などの元の引数は変えません。
- dashboard の Resume で起動した会話は、選ぶと同じ会話のまま再起動して使います。常駐や別の script で起動した agent は保存だけを行い、その起動方法が次に起動するときに使います。
- API 52：一覧に `computer_use_next`・`browser_use_next`・`permission_source`・`permission_save_supported`・`permission_apply` を足し、`POST /api/agent-permissions` を追加しました。Codex CLI の保存と適用はまだありません。
