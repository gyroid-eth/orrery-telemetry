# Dashboard

> English version: planned.

[前: Codex App 統合](codex-app.md) · [README に戻る](../README.md) · [次: API reference](api.md)

dashboard は既定で `http://127.0.0.1:8770/` に公開されます。tmux、agent-mail SQLite、runtime state、project log、任意の Obsidian link hint を読み合わせ、観測と安全な control operation を一つの画面にまとめます。

## DECK

<!-- TODO: screenshot: DECK view -->

DECK は agent ごとの運用カードです。

### 分類

- `running`: tmux pane 内で agent process が動作
- `standby`: session はあるが待機状態
- `finished`: agent process は終わったが session / shell は残存
- `gone`: mail record はあるが tmux session がない
- `retired`: soft-retire 済み

mail の `last_active` だけで running と判定せず、tmux process、pane state、session state を合わせます。過去 session を現在実行中と誤表示しないためです。

### カード表示

- scientist portrait
- model と provider logo
- context window と残量
- task description と最後の受信指示
- work / wait / approval / question と経過時間
- live pane title と agent-mail last active
- 成果物数と terminal attach 状態
- context 残量をカード下端の hairline で表示（緑・橙・赤）

検索と `show all` で対象を絞れます。

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

<!-- TODO: screenshot: NETWORK view -->

NETWORK は spawn 系譜と agent-mail 通信を force graph に重ねます。

### Node

- portrait medallion
- provider badge
- running halo と状態 motion ring
- context 残量を最大270度の外周 arc で表示
- hover / long press tooltip に task、live state、model、last activity
- click で詳細 panel

詳細 panel の History は transcript と24時間 event sparkline、Output は project-scoped な成果物です。ROLE ASSIGN では role / group annotation を保存または削除できます。

### Edge と mail

- spawn edge: parent-child lineage
- communication edge: agent-mail message
- edge count と direction
- edge click で二者間 mail drawer
- drawer に subject、importance、時刻、本文
- live message は comet animation

`AGENTSTACK_PROJECT_KEY` / `AGENTSTACK_VAULT` がないと mail edge と drawer は `NOT CONFIGURED` になります。tmux telemetry は残るため、mail 設定不足と dashboard 全停止を区別できます。

### 表示制御

- time window slider / ALL
- legend
- node search
- TUNE: node size、link distance / width、repel、center、spring
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

## DIGEST REPLAY

<!-- TODO: screenshot: DIGEST REPLAY -->

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

<!-- TODO: screenshot: NEW AGENT modal -->

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
- Codex は `gpt-5.6-sol / terra / luna`
- Codex の effort は `low / medium / high / xhigh`、既定 `xhigh`

server の provider / model / effort allow-list を catalog と validation の両方に使います。

### Directory

`AGENTSTACK_SPAWN_DIRS` の preset chip と exact path input を表示します。input は `/api/fs/dirs` の root-scoped typeahead で、arrow key / Enter でも選択できます。最後に使った directory は `localStorage` に保存します。

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

## Terminal bridge

カードまたは node から terminal を開くと、server は Ghostty / iTerm2 / Terminal.app への jump、または browser terminal 用 `ttyd` を確保します。

`AGENTSTACK_BIND_HOST=0.0.0.0` は terminal bridge と control endpoint も外部へ公開します。dashboard に認証 layer はないため、trusted LAN / VPN 以外では使わないでください。

## 関連文書

- [Hooks と運用 helper](hooks.md)
- [Codex App 統合](codex-app.md)
- [API reference](api.md)
- [設定](configuration.md)
- [トラブルシューティング](troubleshooting.md)
