### 子 agent の sandbox を既定でオフに

更新後に新しく起動・再開する親付きの子は、既定で provider の sandbox を使わず、OS ユーザーの権限でコマンドを実行します。sandbox を使うには installer に `--child-sandbox on` を渡します。Hermes で隔離を使うには、Hermes 側で隔離型の terminal backend を設定します。Codex の一時 Chrome は、既定の承認方針でも選択したブラウザーの既知の道具を使えます。

### すべての子 agent の指示に、道具に拒否された場合の対応を記載

MCP の道具に拒否されたら、作業を止めて理由を報告します。別の道具やシェルで同じ操作を実行したり、権限設定を変えたりしないよう、Claude Code、Codex、dsh、Hermes、Antigravity の子に共通の指示を渡します。
