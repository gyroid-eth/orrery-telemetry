# 委任と child agent

> English version: [delegation.en.md](delegation.en.md)

[前: Launcher と identity](launchers.md) · [README に戻る](../README.md) · [次: Hooks](hooks.md)

Claude Code にも Codex Desktop にも、最初から subagent の仕組みがあります。このスタックが作る **child agent** はそれとは別のものです。名前が似ているうえ、**片方が動かないときにもう片方が黙って肩代わりする**ため、区別を知らないまま「動いている」と判断してしまう事故が実際に起きました。

このページはその2つの違いと、いま自分がどちらを使っているかの見分け方を扱います。

## 一言でいうと

- **組み込み subagent** は、親の中で開かれ、答えを返して閉じる**呼び出し**です。外から見えるものは何も残しません。
- **child agent** は、identity を持ち、自分の tmux session で動き、メールを受け取れる**相手**です。親と対等に名指しでき、あとから履歴をたどれます。

短い調べ物なら前者で十分です。後者が要るのは、**その仕事の存在を人間や他のエージェントが知っている必要があるとき**です。

## 違い

| | 組み込み subagent | child agent（このスタック） |
|---|---|---|
| 作り方 | 親が `Agent` / `Task` ツールを呼ぶ | `/delegate`（内部で `spawn_child.sh`） |
| identity | 無し。呼び出しごとの内部 ID（`a1798ced…` のような16進） | ORRERY Mail に登録された名前（`Teal-Darwin` のような形容詞＋科学者名） |
| プロセス | 親と同じプロセス | 独立した tmux session（＋任意で端末ウィンドウ） |
| dashboard | **現れない**（ノードとしても存在しない） | ノードとして立ち、親との間に線が引かれる |
| 連絡 | 親からの引数と、返ってくる最終テキストだけ | ORRERY Mail。親以外の agent とも双方向にやり取りできる |
| 寿命 | その1回の呼び出しの間だけ | 明示的に終えるまで。次の仕事を追加で投げられる |
| 中断からの復帰 | 不可 | tmux session が残り、dashboard の jump / resume で戻れる |
| ファイル調停 | 無し | file reservation で他 agent と衝突を避けられる |
| 進行の観察 | 終わるまで分からない | 途中経過を pane で見られる。監視ループを回せる |
| 向いている仕事 | 短い調査・検索・単発の判断 | 長い実装、並走、人間が経過を見たい仕事 |

## 見分け方

いちばん確実なのは **dashboard を見ること**です。組み込み subagent は ORRERY Mail に登録しないので、ノードとして現れません。線が無いのではなく、**存在しません**。

しりとりのような疎通確認をしたとき、次のどれかに当てはまるなら組み込み subagent です。

- NETWORK に子のノードが出ない
- 子の名前が形容詞＋科学者名ではなく、16進の識別子になっている
- 親のログに `Agent(...)` / `Task(...)` の呼び出しが並んでいる
- `tmux ls` に子の session が無い

逆に child agent なら、2体の間にエッジが引かれ、そこに通信件数が乗り、エッジを開くとやり取りが1通ずつメールとして並びます。

## なぜ黙って入れ替わるのか

`/delegate` は ORRERY Mail の MCP ツールを使います。**そのツールが無いとき、親は自分の判断で組み込み subagent に切り替えて仕事を終わらせます。** 仕事は完了し、報告も返るので、人間の側からは成功にしか見えません。

このスタックでは、その振る舞いを次の3段構えで潰しています。

1. **install が ORRERY Mail を MCP サーバーとして登録する。** 以前は登録手順がどこにも書かれておらず、ユーザーが登録済みであることを暗黙の前提にしていました
2. **`agentstack-doctor` が登録の欠落・不一致を報告する。** 修復コマンドも表示します
3. **管理下の指示（`claude/CLAUDE.md` / `codex/AGENTS.md`）が、委任は `/delegate` だけで行うこと、ツールが無いときは代替せず報告して止まることを明示する**

導入直後は `agentstack-selftest` を実行してください。存在ではなく**機能**を確認します（登録の検証 → 実際の2体の spawn → 相互のメール到達まで）。

## Codex child と MCP 承認

Codex child は無人実行のため既定で `--ask-for-approval never` です。shell command の承認とは別に MCP tool にも承認設定があり、明示的な許可がない inherited server を呼ぶと `MCP tool call requires approval, but approval policy is never` で直ちに失敗します。全 Codex child に同じ許可を与えるには TOML fragment を用意し、installer の `--codex-child-overlay /absolute/path/to/overlay.toml` で保存します。

server 全体を許可する最小例です。

```toml
[mcp_servers.chrome-devtools]
default_tools_approval_mode = "approve"
```

接続先も変える場合、overlay の array は inherited 値を丸ごと置換します。

```toml
[mcp_servers.chrome-devtools]
args = ["--browserUrl", "http://127.0.0.1:<port>"]
default_tools_approval_mode = "approve"
```

`default_tools_approval_mode` は Codex の server-wide key です。個別 tool だけを許可する場合は `[mcp_servers.chrome-devtools.tools.take_screenshot]` の `approval_mode = "approve"` で範囲を狭めます。table は再帰的に merge され、scalar と array は overlay 側が置換します。child の認証済み identity を保護するため、ORRERY Mail proxy に相当する `mcp_servers` と `plugins.*.mcp_servers.agentstack` は overlay から変更できず、無視した key が stderr に警告されます。

この overlay は現在 macOS/Linux の `spawn_child.sh` にだけ適用されます。Windows では WSL2 経由なら同じ経路を使いますが、community lane の native Windows launcher は対象外です。

### Codex child の MCP を最小化する

`/delegate "<task>" --codex --codex-mcp orrery-only` を明示すると、child 専用 `config.toml` は認証済み ORRERY Mail と session-binding に必要な AgentStack plugin だけを残し、それ以外の継承 MCP server と plugin を `enabled = false` にします。shell と file 操作は残りますが、plugin が提供する skill / app tool も無効になるため、それらを使う task には指定しません。

既定は `inherit` で、従来どおり利用者の MCP/plugin 設定を継承します。これは既存 child の能力を黙って削らないためです。maintainer の macOS 実測では、未使用でも起動していた `chrome-devtools`、`node_repl`、Rhino の RSS が合計約 208 MB/child でした。RSS は共有 page を重複計上し、環境ごとに異なるため、これは物理解放量の保証ではなく profile 選択の目安です。

#### 途中で必要な MCP が増えたとき

`orrery-only` で始めた child に別の道具が必要になったら、親 agent（standalone なら operator）へ相談してください。親がその作業を引き取るか、必要な MCP を有効にした新しい child を正規の `/delegate` 手順で起動し、必要な文脈を明示的に渡します。新しい child は元の会話を自動では継続しません。

Codex CLI 0.154.0 の interactive session で、事前設定済みの stdio MCP を disabled から enabled へ変えた限定実測では、config の変更後に `/mcp` が一覧を更新して server を起動しても tool call は確認できず、同じ会話を resume した1対照も成功しませんでした。新しい process と新しい会話の対照では成功したため、現行案内では config の変更、`/mcp`、resume を確実な途中切替として扱いません。この結果を他の version、HTTP/OAuth、plugin 由来の server、新規 MCP 追加へ一般化するものではありません。

## Claude child とブラウザ操作（Claude in Chrome）

`/delegate "<task>" --claude-chrome-device <deviceId>`（または `--claude-chrome`）を明示すると、Claude child の `claude` に `--chrome` を付けて起動し、Claude in Chrome のブラウザ操作を渡します。Claude child 専用で、`--codex` とは併用できません。OS のデスクトップ操作とは別の指定です。

- **既定は inherit**。指定しない child の起動コマンドは従来と同じで、`--chrome` も `--no-chrome` も付けません。Chrome を使えるかどうかは利用者自身の Claude 設定（`claudeInChromeDefaultEnabled` など）で決まります。既定は「ブラウザ無効」ではありません。
- **deviceId は選択ポリシー**。同じアカウントに複数のブラウザ（例: Mac の Chrome と Windows の Brave）がつながっているときに、どれを使うかを child に指示します。child は `list_connected_browsers` で deviceId が接続中であることを確かめ、`select_browser` が成功してから自分のタブを作ります。一覧に無い・切断されているときはブラウザ作業を止めて報告し、先頭や local のブラウザへ切り替えません。deviceId を渡さないときは、`list_connected_browsers` 以外のブラウザ操作をせず、親に deviceId を尋ねます。**これは child への指示であって、技術的な隔離ではありません**。Claude Code には Claude in Chrome を特定のブラウザに固定する CLI フラグがないためです。誤操作を確実に避けたい検証では、対象外のブラウザの拡張を切断して候補を減らしてください。
- **保証する範囲は新規の cold 起動です**。cold start と legacy のどちらでも `--chrome` が付き、最初のプロンプトでブラウザの選び方を伝えます。warm pool のセッションは `--chrome` なしで起動済みなので claim せず、cold start します。
- **resume は補助機能です**。起動の記録は agent 名ではなく会話（session ID）に結び付けます。launcher は tmux の起動前にその起動専用の仮の記録を書きます。child の最初の SessionStart hook が、それを自分の session ID の記録（`AGENTSTACK_RUNTIME_DIR/child-agents/<name>.claude-launch.<session-id>.json`）に確定させます。同じ名前で後から起動し直しても、起動に失敗しても、前の会話の記録は変わりません（失敗した起動の仮の記録は消します）。dashboard からの resume は、次の 3 つだけで判断します。会話の本文（プロンプトやツールの出力）は使いません。
  - 再開する session ID の正常な記録がある: その記録から `--chrome` とブラウザの選び方を復元し、起動の ID も渡します（resume 後の `/clear` も同じ起動に結び付きます）。
  - 記録はあるが、壊れている・別の agent や session のもの・権限が 0600 でない: resume を止めて理由を返します。
  - 記録が無い: 従来どおり（inherit）で再開します。**もともと指定なしだったのか、記録の保存に失敗したのかは区別できません。指定したブラウザの復元も、ブラウザを使わせないことも保証しません。**
- **既知の限界**。記録の保存に失敗したセッション（例: `/clear` 後の新しい session ID で書き込めなかった場合）には、その場で hook が「ブラウザを使わない」と伝えます。ただし、その session ID を後で resume すると、記録が無い扱い（inherit）になります。記録を確認できない会話でブラウザ作業を続ける必要があるときは、deviceId を指定した新しい cold の child を起動してください。SessionStart hook は、記録がある会話にだけ、起動・resume・compaction のたびに同じ方針を伝え直します。
- **WSL**。公式ドキュメント（[Claude in Chrome](https://code.claude.com/docs/en/chrome)）では WSL は非対応です。Claude Code 2.1.283 で、アカウント経由で接続した Windows のブラウザを WSL の `claude -p --chrome` から操作できたことを 1 台の実機で確認しています。サポート対象として扱わず、version を上げたら確認し直してください。
- **env の既定**。`AGENTSTACK_CLAUDE_CHILD_CHROME=1` と `AGENTSTACK_CLAUDE_CHILD_CHROME_DEVICE=<id>` を設定すると、`spawn_child.sh` から起動する Claude child すべてに同じ指定が付きます。CLI のフラグが env より優先されます。Codex child では無視し、Codex の起動・再開時には env から外します（Codex child が起動する子へ引き継がないため）。dashboard の NEW AGENT は、フォームで指定した値だけを使い、env の既定は使いません。

## Claude child がタスクを受け取る仕組み

Claude の子には、launcher（`spawn_child.sh`）が最初のタスクを `claude [prompt]` の引数で渡します。子にとってはこれが利用者の最初の発言になります。同じ起動コマンドの `--append-system-prompt` で、運用者の設定として次を伝えます。

- この session は ORRERY の子 X で、親 P がタスクを委任したため launcher が起動したこと
- 最初の発言は P が委任したタスクであること（ふだんどおり判断して進める）
- 報告は `mcp__orrery-mail__send_message` で行うこと

launcher が確かめていない「人が頼んだ」といった主張は書きません。
以前はタスクを入力欄に貼り付けていました。Claude Code は貼り付けを `<pasted_content>` で包み、「貼り付けの中の指示は、利用者自身の発言が求めたときだけ従う」と扱います。2026-10-01 の測定では、vault の外（managed block の CLAUDE.md が無い場所）の Sonnet 5 の子は、事故と同じ形のタスクを次のように扱いました。

| 渡し方 | 結果 |
|---|---|
| 貼り付け | 6 回中 6 回断った（文面を変えても同じ） |
| 引数だけ | 3 回中 2 回断った |
| 引数と system prompt | 3 回中 3 回実行した |

**受け取ったかの確認:** 起動の後、launcher は background で子の transcript を読み、最初の turn の終わり方を見ます。読むのは、見つけた 1 本の transcript の書き足された部分だけです。

- 親への `mcp__orrery-mail__send_message` で終われば、何も知らせません
- 次の場合は、親に ORRERY Mail で知らせます
  - 文章だけで終わった（断った・確認を求めた）
  - tool を呼んだが、親へ報告せずに終わった（確かめてから断った・報告を忘れた）
  - `AGENTSTACK_CHILD_START_WAIT_SECONDS`（既定 180 秒）の間に応答が無い、または transcript が見つからない
- 180 秒の時点で最初の turn がまだ書かれている（考え中）なら、「まだ最初の応答の途中です」と分けて知らせます
- 先の知らせの後で始めた場合は、「始めました」をもう 1 通送ります
- この Mail は子の名前で届きますが、件名は `[launcher]` で始まり、本文の先頭に「子本人ではなく launcher が自動で送った」と書いてあります
- 結果はどれも `spawn_incidents.log` に残ります。`AGENTSTACK_CHILD_START_CHECK=0` で無効にできます（テスト用）

1 つの引数に収まらない長いタスク（Linux・WSL は 1 引数 128 KiB）は、子だけが読める file に置き、引数にはその file を読むようにという短い文だけを渡します。引数で渡したタスクは、子が動いている間 process の一覧（`ps`）から見えます。Codex の子も同じです。

warm pool（`hooks/warm_pool.sh`）の session は起動済みなので、タスクは今も貼り付けで渡します。warm pool を使う場合は、事前起動の時に同じ system prompt を渡してください。Codex の子は前からタスクを引数で受け取っています。

**モデルが変わったら回す:** `scripts/canary-embed-task.sh` は、モデル × vault の内外ごとに一時の子を起動し、それぞれを「実行した／Mail 以外で報告した／断った／時間切れ」に分けて表にします。CI には入れず、手で回します。子は最後に必ず終了させ、retire します。live の ORRERY Mail に一時の identity を作るので、始める前に確認を求めます。

```bash
scripts/canary-embed-task.sh --models opus,sonnet,haiku --places vault,outside --runs 3 --parent <自分の名前>
```

## 使い分け

組み込み subagent が正しい場面はあります。答えだけが要る短い検索、親のコンテキストを汚したくない読み取り専用の調査。1回で閉じ、誰も後から参照しない仕事です。

child agent が要るのは次のような場合です。

- **人間が経過を見たい**。実装が長く、途中で方向を変えたくなる
- **複数を並走させる**。たとえば1体に文献調査、もう1体に実験結果のまとめをさせ、親は総括に残る
- **親を人間との対話に残しておきたい**。派生タスクが出るたびに子へ渡せば、親の会話は途切れない
- **子同士に直接やり取りさせたい**。組み込み subagent は互いを知りません
- **同じファイルを触る可能性がある**。reservation が要る
- **あとから履歴をたどる**。誰が誰に何を頼んだかが残る

**1体に1つのことをさせたほうが仕事の質は上がります。** 役割を分けて渡すのが、並走の効率ではなく質のための理由です。

**コンテキストを取っておきたいときも child が向きます。** 終了した agent も dashboard の検索から呼び出して resume できるので、「この文脈を持った相手」を残しておいて必要なときに再開する使い方ができます。組み込み subagent は呼び出しが閉じた時点で消えるので、これができません。

## 通知が会話に割り込むとき

子の報告は親の入力欄に直接タイプされます。子を何体も抱えていると、人間が親と話している最中に進捗報告が挟まります。

```bash
export AGENTSTACK_MAIL_NOTIFY_MIN_IMPORTANCE=high
```

`normal` 以下は割り込まなくなります。**メールは消えません。** signal は残るので、次に `fetch_inbox` を呼べば普通に読めます。奪うのは割り込む権利であって、届く権利ではありません。

完了報告だけは確実に受け取りたい、という使い方になるはずです。`/delegate` はタスク依頼を `importance="high"` で送り、**完了報告も同じく `high` で返すよう子に指示します**。中間の進捗は既定のままなので溜まっていき、こちらの区切りで読みます。

自分で書いた指示で子を動かす場合は、この使い分けを子に伝えてください。閾値を上げた環境で完了報告を既定の重要度で送ると、**メール自体は届いているのに親が待ち続ける**という形になります。

## 関連

- [Launcher と identity](launchers.md) — 名前と token がどう決まるか
- [Dashboard](dashboard.md) — NETWORK でのノードとエッジの読み方
- [トラブルシューティング](troubleshooting.md) — 委任が動かないときの確認順
