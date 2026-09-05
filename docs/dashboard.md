# Dashboard

> English version: [dashboard.en.md](dashboard.en.md)

[前: Codex App 統合](codex-app.md) · [README に戻る](../README.md) · [次: API reference](api.md)

dashboard は既定で `http://127.0.0.1:8770/` に公開されます。tmux、agent-mail SQLite、runtime state、project log、任意の Obsidian link hint を読み合わせ、観測と安全な control operation を一つの画面にまとめます。

起動時には `pkill` で前回の browser terminal（ttyd）の残存プロセスを整理します。
`pkill` がない環境では、この整理だけをスキップして HTTP server を起動します。
この処理は Windows native の installer や Codex App Bridge を対応済みにするものではありません。

ここでいう agent-mail / mail-watcher は、AgentStack 内のエージェント間メッセージを扱う機構です。利用者のメールアカウント、メールクライアント、受信箱には一切アクセスしません。

## やりたいことから探す

| やりたいこと | 最短の操作 |
| --- | --- |
| 今動いているエージェントを見る | [DECK](#deck) を開き、`ACTIVE AGENTS` のカードを見る。カードをクリックすると task、live state、History、Output、terminal 操作をまとめて確認できます。 |
| 親子関係を見る | [NETWORK](#network) に切り替える。parent と child は spawn edge で結ばれ、node をクリックすると個別の詳細 panel が開きます。 |
| エージェント同士が何を話したか読む | NETWORK の communication edge をクリックする。右側の mail drawer に、その2者間の subject、importance、時刻、本文が表示されます。[mail 設定がない場合](#edge-と-mail)は `NOT CONFIGURED` になります。 |
| 複数のエージェントをまとめて操作する | NETWORK 上部の `Select` を有効にし、node をクリックするか空白部分を矩形 drag する。選択後に画面下部へ出る action bar で、running / finished agent は `Exit N`、2人以上は `Replay N` を選べます。EXIT は同じ button をもう一度押す二段確認です。 |
| 終了したエージェントを resume する | tmux 型の Claude / Codex CLI agent では、DECK の `show all` または NETWORK の `ALL` で過去 agent を出し、カード / node → 詳細 panel → `OPEN TMUX` と進む。tmux session がなければ `/api/jump` が保存済み transcript の resume に切り替わります。NETWORK の `Select` で gone / retired node を選び、画面下部の `Resume N` を二度押す経路もあります。resume には transcript、元の cwd、対応 CLI、terminal adapter が必要です。 |
| 終わったエージェントを見る | NETWORK の time window を `ALL` にするか、DECK の `show all` を有効にする。DECK は直近30日の `gone` / `retired` card を表示します。[完了後の見え方](#child-完了後の表示)も参照してください。 |

NETWORK は選択中の time window 外にある node を表示しないことがあります。現在の graph に見えないことだけでは task failure を意味しないため、`ALL`、DECK の `show all`、親へ届く完了報告を確認してください。

## DECK

最初は DECK を開きます。**1エージェントが1枚のカード**として並ぶため、誰が何をしているか、入力待ちで止まっていないかを個別に読めます。

次の画像では、上部のカウンタが3体、カードも3枚です。カウンタは全体の数、カードは各エージェントの状態を示します。

![DECK の初期状態。3体のエージェントが1枚ずつのカードで表示されている](images/deck_start.png)

本ページの6枚の画面写真は隔離したデモ環境の実画面です。エージェント名、task、mail、transcript はすべて架空で、実在の作業内容ではありません。

### カードを上から読む

| 表示 | 読み方 |
| --- | --- |
| 名前の下の `GPT 5.6`、`SONNET 5` など | 使用中のモデル。`◷ 200K`、`◷ 1M` がある場合は、そのモデルが使える作業メモリ全体の大きさです。 |
| モデルの下の細い横線 | **残りの作業メモリ（context remaining）**。長いほど余裕があり、残り20%未満では赤寄りになります。telemetry を取得できないカードには出ません。 |
| 黒い帯 | terminal で観測した現在の表示。作業中か待機中かを読む手掛かりです。 |
| `ORD` | そのエージェントに与えられている直近の指示です。 |
| `RX` | agent-mail で直近に受信した指示です。送信者、件名、重要度が並びます。まだ受信がなければ行自体がありません。 |
| `● ONLINE` | agent process が稼働中です。**いま仕事を進めているという意味とは限りません**。 |
| 右上の状態表示 | 黄色の秒数は作業中、薄い `LAST …` は入力待ち、`?` と `APPROVAL` は人の介入待ちです。 |
| `↩ EXIT` | 稼働中のエージェントへ graceful な `/exit` を送ります。誤操作防止のため二度押しで確定します。 |

次の画像では12枚に増え、Warm-Lovelace などのカードに `RX` が現れています。`ORD` と見比べると「担当」と「最後に誰から何を受け取ったか」を分けて読めます。

![DECK で12体に増え、複数のカードに ORD と RX が表示されている](images/deck_growing.png)

### ヘッダのカウンタとカードの状態を分ける

ヘッダの3項目は、個々の作業内容ではなく**数**です。

| カウンタ | 数えているもの |
| --- | --- |
| `RUNNING` | agent process が稼働しているカード数 |
| `STANDBY` | process が稼働していない active agent のカード数 |
| `AGENTS` | active agent の総数 |

したがって、`RUNNING 12` だけを見ても12体すべてが仕事を進めているとは限りません。**全体の数はヘッダ、誰が作業中・待機中・介入待ちかは各カードの右上と枠**で確認します。

### 人の介入待ちを見逃さない

次の画像で見る場所は、Bright-Curie カード右上の `?` と、Swift-Noether カードの赤い枠と `APPROVAL` です。

![DECK の介入待ち。Bright-Curie に質問マーク、Swift-Noether に赤枠の APPROVAL が表示されている](images/deck_humanloop.png)

| 合図 | 意味 | 解除方法 |
| --- | --- | --- |
| `?` | エージェントがユーザーへの質問で止まっています。 | terminal で選択肢や回答を入力します。 |
| 赤枠の `APPROVAL` | permission の承認待ちで止まっています。 | terminal で許可または拒否を選びます。 |

どちらも解除するまで先へ進みません。ただし process 自体は生きているため、この画像でもヘッダは `RUNNING 12` のままです。これが、ヘッダの数とカードの状態を分けて読む理由です。

### 該当 terminal を開く

カードをクリックすると、そのエージェントの詳細パネルが開きます。tmux 型の Claude / Codex CLI agent なら、パネル右上の `OPEN TMUX` をクリックすると対応 terminal を前面化または新しく開いて attach します。NETWORK でも、ノードをクリックした後は同じ手順です。

### 分類

- `running`: tmux pane 内で agent process が動作
- `standby`: session はあるが待機状態
- `finished`: agent process は終わったが session / shell は残存
- `gone`: mail record はあるが tmux session がない
- `retired`: soft-retire 済み

mail の `last_active` だけで running と判定せず、tmux process、pane state、session state を合わせます。過去 session を現在実行中と誤表示しないためです。

### 検索

上部の `FILTER · name / task` は、名前だけでなく **task description、live pane title、最後に受け取った指示の subject と送信者**も対象にします。何をしていた agent かを覚えていれば、名前を思い出せなくても引けます。

既定では running と finished しか出ません。`show all` を有効にすると直近30日の `gone` / `retired` も対象に入るので、**終了した agent を検索で見つけて resume する**という使い方ができます。過去の文脈を持った相手を取っておいて、必要になったら再開する形です（手順は[やりたいことから探す](#やりたいことから探す)の「終了したエージェントを resume する」、見え方は[Child 完了後の表示](#child-完了後の表示)）。

### カード操作

- History panel: Claude / Codex transcript
- Output panel: project または設定 root の `LOG_*.md` と成果物
- terminal open / focus
- running agent への二段確認 EXIT。`/api/exit` 自体は finished agent も受け付ける
- tmux client が attach していない finished / gone agent の KILL / soft retire

KILL の可否は frontend の見た目だけで決めず、server の `build_agents()` category を再検証します。attached client がある session では UI が KILL button を隠し、API を直接呼んでも server が `refusing to kill (detach first)` で hard refusal します。

### Child 完了後の表示

正常な completion flow では、`/delegate` で起動した child が終了前に agent-mail の完了報告を親へ送ります。親はその報告を読み、成果物を検証してから利用者へ結果を返します。child の REPL が終了した後は launcher の cleanup が reservation を解放し、remote identity を soft-retire し、child runtime の credential と state を削除します。その command の終了に伴い tmux session も閉じます。

このため、完了した child のカードは DECK の通常表示から消えますが、失敗ではありません。`show all` を有効にすると、直近30日の `gone` / `retired` agent もカードとして表示されます。

![DECK show all — FINISHED / GONE / RETIRED の各セクション](img/deck-show-all.jpg)

NETWORK は現在の稼働状態と選択した time window を重ねる表示です。完了や retire だけを理由に node を即座に隠すわけではありませんが、last activity が window 外になると child node と、それに接続する spawn / mail edge は表示されません。現在の window に見えないことだけでは task failure を意味しません。履歴を確認する場合は NETWORK の `ALL`、個別の終了状態を確認する場合は DECK の `show all` を使います。

## Output / deliverables

Output は vault 専用ではありません。`AGENTSTACK_DELIVERABLE_ROOTS` があれば `:` 区切りの root 群、なければ project の `logs/` を再帰走査し、frontmatter の `agent:` が選択 agent と一致する `LOG_*.md` を mtime 降順で最大25件表示します。

project base は絶対 path の `AGENTSTACK_PROJECT_KEY`、絶対 path の `AGENTSTACK_VAULT`、dashboard の cwd / git root の順に fallback します。明示した deliverable root は既定 root を置き換えます。

検出 item が `AGENTSTACK_VAULT` 内なら `obsidian://` link にし、それ以外の generic project / shared log は非リンク項目として表示します。Obsidian がなくても一覧と成果物数は利用できます。

## Codex App runtime

[Codex App 統合](codex-app.md)を導入すると、dashboard は Bridge の allowlist 済み snapshot を tmux state と並べて読みます。同じ agent-mail 名の row を `surface: codex-app` として昇格し、`Codex App · <state>` または `Codex App · wake:<status>` を live 表示します。

- `registering / working / waiting / blocked`: running 扱い
- `dormant / degraded`: finished 扱い
- active 系 state でも snapshot 更新が10分以上ない: stale な running 表示を避けるため dormant 扱い
- capability: `open` のみ

Codex App runtime には tmux pane がありません。terminal attach、dashboard の EXIT / KILL / wake は行わず、jump / resume action は macOS の ChatGPT app を前面化します。inbox の cold wake と delivery retry は dashboard ではなく Bridge が所有します。

## NETWORK

NETWORK は「誰から生まれたか」と「誰と通信したか」を一枚の force graph に重ねます。まず、**ノードはエージェント、線は関係**と読んでください。

次の初期状態では3ノードに対して `0 links · 0 spawn` なので、まだ親子関係も通信もありません。右側には配置を調整する `TUNE` パネルが見えます。

![NETWORK の初期状態。3ノードで通信線と spawn 線はまだない](images/net_start.png)

### Node

- 1ノードが1エージェントです。肖像の横の小さな badge は provider を示します。
- 肖像を囲む**系統色のリング**は spawn 上の立場です。色の対応は左上の `?`（凡例）で parent / child / both と確認できます。`both` は、親から仕事を受け、さらに子へ委任している中間ノードです。
- 外周の円弧は**残りの作業メモリ（context remaining）**です。最大270度で、短くなるほど残量が少ないことを示します。
- hover、または touch で長押しすると task、live state、model、last activity が出ます。
- ノードをクリックすると詳細パネルが開きます。tmux 型 agent は、そこから `OPEN TMUX` をクリックして該当 terminal へ移動できます。

詳細 panel の History は transcript と24時間 event sparkline、Output は project-scoped な成果物です。ROLE ASSIGN では role / group annotation を保存または削除できます。

![node 詳細 panel — CTX / STATE / History / ROLE ASSIGN / transcript](img/agent-detail.jpg)

### Edge と mail

- spawn edge: parent-child lineage
- communication edge: agent-mail message
- communication edge 上の数字: その2者間の通信回数
- communication edge の矢印: 通信方向
- edge click で二者間 mail drawer
- drawer に subject、importance、時刻、本文
- live message は comet animation

次の画像は、12ノードに `6 links · 6 spawn` ができた段階です。系統色のリングで親・子・中間を見分け、線上の `1` からその組み合わせの通信が1回だと読めます。

![NETWORK で12ノードへ増え、親子の spawn 線と通信回数1の線が表示されている](images/net_growing.png)

通信が増えると、同じ2者間の線上の数字も増えます。次の画像では `29 links · 9 spawn` となり、中央付近に `2` や `3` が見えます。数字はノード数や子の数ではなく、**通信回数**です。

![NETWORK の通信が増え、エッジ上に通信回数2や3が表示されている](images/net_humanloop.png)

`AGENTSTACK_PROJECT_KEY` / `AGENTSTACK_VAULT` がないと mail edge と drawer は `NOT CONFIGURED` になります。tmux telemetry は残るため、mail 設定不足と dashboard 全停止を区別できます。

![edge click 後の mail drawer — 二者間の subject / importance / 時刻 / 本文](img/network-edge-drawer.jpg)

### 表示制御

- time window slider / ALL
- legend
- node search
- TUNE: `NODE SIZE`、`LINK DIST`、`LINK WIDTH`、`REPEL`、`CENTER`、`LINK FORCE`。右側の slider でノード同士の配置を見やすく調整
- TUNE 値を `localStorage` に保存
- 300 node 超で dense mode

dense mode は label、annotation、provider badge、context arc を隠し、大規模 graph の描画負荷を抑えます。

## SELECT と一括操作

SELECT mode では drag rectangle または node click で複数選択できます。

| 操作 | 対象 | 動作 |
| --- | --- | --- |
| EXIT | running / finished | `/api/exit` で graceful `/exit` |
| RESUME | gone / retired | `/api/jump`。tmux がなければ transcript resume |
| REPLAY | mail history を持つ2 agent 以上 | DIGEST REPLAY |

EXIT / RESUME は二段確認し、60 ms 間隔で順次送信します。誤操作と service spike を避けるため、安全弁のない並列 request にはしません。

![SELECT mode — 矩形 drag で13体を選択し、下部に EXIT / RESUME / REPLAY](img/network-select.jpg)

## DIGEST REPLAY

![DIGEST REPLAY — 選択した11エージェントの event を時系列再生](img/digest-replay.jpg)

選択 agent の mail、spawn、exit / retire、承認待ち event を時系列再生します。

- play / pause
- seek と event marker
- absolute / relative clock
- 対数 scale の速度 `×1`〜`×10000`
- message card の HOLD `0.1s`〜`15s`
- GROUP-ONLY
- TIME-TRAVEL

TIME-TRAVEL ON は initial snapshot から node、edge、state を再構築します。OFF は現在 graph 上で comet だけを再生します。

履歴範囲は event の最古・最新へ auto-fit し、短すぎる範囲は操作可能な幅まで広げます。大量 event は topology と group 内 mail を優先して間引きます。

`Esc` / CLOSE で live graph snapshot と mail polling を復元します。

## NEW AGENT

![NEW AGENT modal — identity / engine / directory / task の launch manifest](img/new-agent.jpg)

`+ NEW AGENT` は identity、engine、directory、task を順に確認する launch manifest です。通常項目を一方向に並べ、parent / role / group / isolation は ADVANCED に畳むことで、standalone agent の起動を最短経路にしています。

### Identity

- `AUTO`: server が shared vocabulary から空き `Adjective-Scientist` を検証し、その explicit `name` を登録 request に送る
- scientist rail: portrait と `available / occupied / unknown`
- scientist 選択: `/api/suggest-name` が空き adjective を付けて live registry で検証
- SHUFFLE: 同じ scientist で別の verified name を再提案
- `occupied / unknown`: 選択不可
- roster 外または空き候補なし: HTTP 409 として別 scientist / AUTO を促す

scientist rail の `available` は bare surname ではなく、134 adjective のどれかとの組み合わせに空きがあることを意味します。adjective は agent-mail 正典 `SIMPLE_ADJECTIVES` と同期し、client は local で未検証名を作りません。AUTO でも server が最大75候補を live registry で fail-closed 検証し、空きを確認できなければ spawn を拒否します。

### Engine

- Claude / Codex provider tab
- provider ごとの model card と用途 guide
- Claude は Sonnet / Opus / Haiku
- Codex は `gpt-5.6-sol / terra / luna / gpt-6-astra`
- Codex の effort は `low / medium / high / xhigh`、既定 `xhigh`

server の provider / model / effort allow-list を catalog と validation の両方に使います。

### Directory

`AGENTSTACK_SPAWN_DIRS` の preset chip と exact path input を表示します。preset は installer の `--spawn-dirs` で永続化します（shell の `export` は service に届きません。[configuration.md](configuration.md) の「Spawn directory」参照）。input は `/api/fs/dirs` の root-scoped typeahead で、arrow key / Enter でも選択できます。最後に使った directory は `localStorage` に保存します。

typeahead は `AGENTSTACK_SPAWN_ROOTS` の外へ出ず、hidden directory、`..`、root 外への symlink を候補にしません。

### Task と ADVANCED

task は必須、最大4000文字です。ADVANCED の開閉状態は `localStorage` に保存します。

- parent: 既定は `STANDALONE · independent agent`
- role: 任意、最大40文字
- group: 任意、最大24文字
- isolation: isolated worktree と base revision

parent を選ばないと `standalone: true` を送り、`PARENT_AGENT` のない独立 agent として起動します。parent を選ぶと通常 child になり、task を child inbox へ送り、parent を CC した audit trail を残します。

### Spawn 順序

1. `register_agent` で child identity と専用 token を作成
2. role / group annotation（best effort）
3. 通常 child だけ、parent を sender にして task message と CC audit trail を作成
4. token を mode `0600` の one-shot file に保存
5. `spawn_child.sh --pre-registered` を background 起動
6. launcher の readiness verdict を最大120秒待つ
7. live tmux session を再確認

失敗時は tmux session と token / child credential file を cleanup し、`dashboard/logs/spawn.log` の末尾を API error の `detail` に含めます。登録済み identity は server に削除権限がないため保持され、response の `registration_retained: true` で明示されます。

Codex では `--codex --model <model> --effort <effort>` を渡します。non-git directory の trust dialog は `C-m` で最大10回受理を試み、残り続ける場合は fail-fast します。

すべての POST は JSON body が必須です。browser は same-origin、CLI は `Origin` / `Sec-Fetch-Site` を付けない request だけを受け付けます。

### Isolated worktree

`worktree: true` では child ごとに:

```text
/tmp/cc-worktrees/<child-name>
branch: exp/<child-name>
```

を使います。`worktree_base` を省略すると `HEAD` です。task message には元 project key、branch、base、directory を明記し、worktree path を agent-mail project key と誤認しないようにします。

## Embed mode

`/?embed=1` または same-origin iframe では compact header の embed mode になります。

parent window から:

```js
frame.contentWindow.postMessage({type: "net-pause"}, location.origin);
frame.contentWindow.postMessage({type: "net-resume"}, location.origin);
```

を送れます。

- `net-pause`: DECK / NETWORK / mail health polling を停止
- `net-resume`: polling を再開し、現在 view を即 refresh

hidden iframe が tmux / SQLite を継続 polling しないための契約です。message は same-origin だけを受け付けます。

### Theme axis bridge

same-origin の parent は、dashboard だけに1軸の A/B override を送れます。parent 側が選択履歴と preset を所有し、iframe は永続化しません。

```js
frame.contentWindow.postMessage({
  type: "agentstack-theme-axis",
  version: 1,
  axis: "glow",
  value: 1,
}, location.origin);
```

`axis` は `dim-contrast`、`small-text`、`tracking`、`glow`、`background` のいずれかです。数値は finite かつ `0 <= value <= 1` でなければならず、範囲外は clamp せず `invalid-value` として reject します。`null` は override style node と一時属性を物理的に除去します。初期表示は bridge の CSS を一切適用しないため、従来の描画経路のままです。

receiver は `event.source === window.parent` と `event.origin === location.origin` の両方を確認します。処理した request には parent へ次の envelope を返します。

```js
{
  type: "agentstack-theme-axis-result",
  version: 1,
  ok: true,
  requested: {axis: "glow", value: 1},
  state: {axis: "glow", value: 1},
  source: {unit: "declaration", expected: sourceRecords.length, matched: sourceMatched},
  mutation: {unit: "effect-component", expected: snapshot.length, applied: appliedCount},
  effect: {unit: "effect-component", evaluated: 30, changed: 26,
    rendered: 28, inViewport: 26, visibleExpected: 26,
    visibleReached: 26, visibleChanged: 26, deferred: 4},
  reason: null,
}
```

上の識別子は envelope の対応関係を示す擬似コードです。正本は [`scripts/dashboard_theme_manifest.py`](../scripts/dashboard_theme_manifest.py) が `dashboard/index.html` から生成する [`dashboard/theme_effect_manifest.json`](../dashboard/theme_effect_manifest.json) と、そこから生成して HTML に埋め込む runtime inventory です。前者は selector / property / line / component を含む review 用 record、後者は同じ record の安定 ID list と規則/source digest を持ちます。runtime の `source.expected` は軸ごとの `records.length` から導出し、独立した数値定数を持ちません。CSS を変更したら次を実行し、record list、規則 digest、source digest の差分を一緒に review します。

```bash
python3 scripts/dashboard_theme_manifest.py --write
python3 scripts/dashboard_theme_manifest.py --check
```

`source` は token / declaration の coverage、`mutation` は apply 直前の immutable snapshot から導出した token-write / element / effect-component の compiled 適用数です。source unit は軸ごとに厳密で、`dim-contrast` / `background` は `token-write`、`small-text` / `tracking` / `glow` は `declaration` です。`effect` の membership は generated eligibility の pre-apply live match だけで固定し、適用前・適用後・endpoint の値から対象を増減しません。そのうち rendered・nonzero box・viewport 内を `visibleExpected`、cascade 後の computed が requested derivation へ到達したものを `visibleReached` と数えます。元から requested 値だった member も reached です。`visibleChanged` は可視 member の canonical post が pre と異なる数、`changed` は全 evaluated member の同じ差分を数える独立 no-op gate です。hidden / zero-box / offscreen は `deferred` に数えます。non-null apply は `visibleExpected > 0`、`visibleReached === visibleExpected`、`visibleChanged > 0`、`changed > 0` をすべて満たす場合だけ成功します。0や量子化以下は `no-effective-change`、可視対象0は `no-visible-targets`、requested derivation に到達しない member があれば `effect-count-mismatch` として reject し、直前の valid axis を復元します。面や単位を合算しません。`glow` の effect unit は live `effect-component` です。同一 surface に対象 radial layer が2つあれば2件と数え、CSS rule 1件が可視 element 10件へ match すれば10件と数えます。selector と keyframe の source component は `emissive | elevation | focus | state` に分け、色 halo だけを弱めて elevation と focus を維持します。

### Atomic theme profile bridge

`small-text` と `tracking` は complete five-axis vector を使う1つの atomic profile として同時適用できます。全キーは必須で、non-null の組み合わせは空、`small-text` / `tracking` の任意 subset、または legacy 1軸だけです。

```js
frame.contentWindow.postMessage({
  type: "agentstack-theme-profile",
  version: 1,
  requestId: "theme-42",
  values: {
    "dim-contrast": null,
    "small-text": 0.5,
    tracking: 0.25,
    glow: null,
    background: null,
  },
}, location.origin);
```

receiver は全 experimental override を外した同じ A snapshot から両軸を導出します。baseline font size を `s0`、baseline letter-spacing ratio を `r0` とすると、small の final size は `s(vs)`、tracking ratio は `lerp(r0, min(r0, 0.08), vt)`、final spacing はその ratio と `s(vs)` の積です。前の profile render を次の baseline にしないため、UI で small→tracking と tracking→small の順に操作しても complete final vector が同じなら computed size / weight / spacing は一致します。適用後に追加された DOM node も override を一時無効化した A baseline から両軸を同時導出し、各軸の独立 envelope へ加算します。

成功時は次の response を返します。`requested` / `applied` は常に exact five-key map、`axes` は non-null 軸の exact set です。

```js
{
  type: "agentstack-theme-profile-result",
  version: 1,
  requestId: "theme-42",
  surface: "telemetry",
  requested: {"dim-contrast": null, "small-text": 0.5,
    tracking: 0.25, glow: null, background: null},
  applied: {"dim-contrast": null, "small-text": 0.5,
    tracking: 0.25, glow: null, background: null},
  status: "applied",
  axes: {
    "small-text": {status: "applied", source: {}, mutation: {}, effect: {}},
    tracking: {status: "applied", source: {}, mutation: {}, effect: {}},
  },
}
```

1軸でも source / mutation / effect guard に失敗すれば profile 全体を reject し、`applied` には receiver が復元した last-valid complete map を返します。schema 違反も state を変更しません。legacy `agentstack-theme-axis` v1 は対象1軸以外を null にした complete profile と同じで、legacy `value: null` は全軸 reset です。result reply が失われた場合、parent coordinator は fresh `requestId` の last-valid complete profile を compensating request として送れます。

embed の初期化完了時と reload 後には、dashboard が `agentstack-theme-axis-ready` と `agentstack-theme-profile-ready`（ともに version 1、`surface: "telemetry"`）を same-origin parent へ送ります。recovery はこの明示 signal の後に compensating profile を送り、その result が acknowledged になるまで完了扱いにしません。ready が来ない場合の timeout と UI failure 表示は parent coordinator が担当します。

## Terminal bridge

カードまたは node から terminal を開くと、server は Ghostty / iTerm2 / Terminal.app への jump、または browser terminal 用 `ttyd` を確保します。

`AGENTSTACK_BIND_HOST=0.0.0.0` は terminal bridge と control endpoint も外部へ公開します。dashboard に認証 layer はないため、trusted LAN / VPN 以外では使わないでください。

## デモ（サーバー不要）

公開デモ [agentstack-demo.pages.dev](https://agentstack-demo.pages.dev/) は、この `index.html` そのものを Python も SQLite も tmux も無しで動かしたものです。コピーは作っていません。画面を動かしている通信は `fetch` による GET だけなので、`dashboard/demo/demo_api.js` が `fetch` を横取りし、台本（`story_*.js`・配役・生死・mail 本文・作業ログ）から時刻に応じた応答を組み立てます。書き込み系 API は無効です。

- `http://127.0.0.1:8770/?demo=1` で、手元の dashboard でも同じデモを再生できます（`demo_tour.js` が字幕と対象のリングを重ねるだけで、製品の挙動は変えません）
- 台本の書き方は [`dashboard/demo/STORY_CONTRACT.md`](../dashboard/demo/STORY_CONTRACT.md)。公開してよい肖像の台帳は `PORTRAITS_CLEARED.txt` で、build はそこに無い肖像を同梱しません
- 静的 bundle は `bash dashboard/demo/build.sh [outdir]` で作り、どのパスに置いても動きます。全 script に内容ハッシュの版番号を付けるので、CDN の古い写しと新しい HTML が組になる壊れ方をしません

## 関連文書

- [Hooks と運用 helper](hooks.md)
- [Codex App 統合](codex-app.md)
- [API reference](api.md)
- [設定](configuration.md)
- [トラブルシューティング](troubleshooting.md)
