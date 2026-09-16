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
