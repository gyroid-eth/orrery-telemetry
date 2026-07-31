# Dashboard

> English version: planned.

<!-- NOTE: spawn v2 改修中・確定後に更新 -->

[前: Launcher と identity](launchers.md) · [README に戻る](../README.md) · [次: API reference](api.md)

dashboard は既定で `http://127.0.0.1:8770/` に公開されます。tmux、agent-mail SQLite、runtime state、Obsidian vault を読み合わせ、観測と安全な control operation を一つの画面にまとめます。

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
- Output panel: vault の `LOG_*.md` と成果物
- terminal open / focus
- running / finished agent への二段確認 EXIT
- finished / gone agent の KILL / soft retire

KILL の可否は frontend の見た目だけで決めず、server の `build_agents()` category を再検証します。attached client がある session は warning を返します。

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

詳細 panel の History は transcript と24時間 event sparkline、Output は vault の成果物です。ROLE ASSIGN では role / group annotation を保存または削除できます。

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

<!-- NOTE: spawn v2 改修中・確定後に更新 -->

<!-- TODO: screenshot: NEW AGENT modal -->

`+ NEW AGENT` は child の登録、task 配送、annotation、tmux 起動を dashboard から行う control panel です。

### 現在の server 契約

`GET /api/spawn-names` は次を返します。

- scientist と `available / occupied / unknown`
- adjective list
- directory preset
- Claude model
- provider catalog
- Codex model と effort `low / medium / high / xhigh`

`POST /api/spawn` は `provider` を `claude` または `codex` から選び、provider ごとの model / effort allow-list を検証します。指定名の `-` は除去され、たとえば `Sunny-Curie` は `SunnyCurie` になります。科学者を選ばず `name` を省略すると、agent-mail の自動命名に委ねます。

現在、server 側は provider / effort 対応済みですが、checked-in UI は旧 payload の modal から段階的に移行中です。確定仕様は [API reference](api.md#get-apispawn-names) の server 契約を優先してください。

### Spawn 順序

1. `register_agent` で child identity と専用 token を作成
2. role / group annotation（best effort）
3. parent を sender にして child inbox へ task message を送信
4. parent 自身を CC にして監査 trail と watcher 通知を残す
5. token を mode `0600` file に保存
6. `spawn_child.sh --pre-registered` を background 起動
7. 3秒後に tmux session の存在を probe

失敗時は `dashboard/logs/spawn.log` の末尾を API error の `detail` に含めます。Codex では `--codex --model <model> --effort <effort>` を spawner へ渡します。

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

- [API reference](api.md)
- [設定](configuration.md)
- [トラブルシューティング](troubleshooting.md)
