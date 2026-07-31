# claude-agent-stack

[English](README.en.md)

ローカルで動く Claude Code / Codex エージェント群のための、協調基盤とライブ telemetry ダッシュボードです。[mcp-agent-mail](https://github.com/Dicklesworthstone/mcp_agent_mail) をメッセージ・identity・file reservation の正本にし、tmux 上の実行状態、親子関係、通信履歴、コンテキスト残量を一つの画面に重ねます。

![claude-agent-stack demo](assets/demo.gif)

設計の中心は「LLM に協調を期待するだけでなく、launcher・hook・mail・可視化で運用規約を実行可能にする」ことです。

## 主な機能

- `agent-start` / `agent-start-codex` で、agent-mail identity と同名の tmux session を起動
- Claude Code hook による登録ゲート、file reservation の強制、session 記録
- agent-mail の inbox signal を実行中の Claude / Codex REPL へ再注入
- DECK / NETWORK の二つのビューで、状態、担当、通信、spawn 系譜、成果物を可視化
- dashboard から graceful EXIT、RESUME、REPLAY、role annotation、child spawn を操作
- `/delegate` と `/log` skill、Codex / Claude 向け managed instructions
- Obsidian vault を使う場合はログ、Daily Note backlink、成果物 index を統合

agent-mail を置き換えるのではなく上に重ねる構成です。mail と reservation の正本を一つに保つことで、UI を再起動しても協調状態が分裂しません。

## 動作要件

- macOS を主対象とします。launcher / hook は macOS 標準 Bash 3.2 でも動くよう実装されています
- `python3`、`tmux`、`git`、`uv`
- Claude Code または Codex CLI
- `fzf`（任意。引数なし launcher の directory picker）
- Ghostty（推奨。click-to-jump と window title。iTerm2、Terminal.app、`none` へ fallback）
- Obsidian（任意。`/log` と Output index の vault 統合）

Linux では systemd user service、利用できなければ `nohup` で dashboard を起動します。Windows native は対象外です。WSL2 では localhost dashboard は利用できますが、Ghostty による click-to-jump は使えません。

## インストール

```bash
git clone https://github.com/gyroid-eth/claude-agent-stack.git
cd claude-agent-stack
./scripts/install.sh
```

完了後に `http://127.0.0.1:8770/` を開き、診断を実行します。

```bash
~/.agentstack/bin/agentstack-doctor
```

installer は upstream agent-mail を `~/mcp_agent_mail` へ取得し、`~/.agentstack` に dashboard、launcher、hook、skill、managed instruction template、`VERSION` を配置します。生成する `env.sh` は mode `0600` で、bearer token は書き込みません。

### install tier

| 呼び出し | Tier | 内容 |
| --- | --- | --- |
| `./scripts/install.sh` | Tier 1 / default | 全 payload を導入。Claude settings の hooks・permissions・`skillsDirectories` と、Codex `AGENTS.md` / Claude `CLAUDE.md` の managed block は preview 後、`yes` のときだけ merge |
| `./scripts/install.sh --dashboard-only` | Tier 0 | dashboard と helper のみ。hooks、skills、Codex / Claude template は導入しない |
| `./scripts/install.sh --scoped` | Tier 2 placeholder | payload は導入するが、user settings / managed docs は変更しない |
| `./scripts/install.sh --dry-run` | preview | 変更予定だけを表示し、file や service を変更しない |

merge は JSON parser ベースです。既存設定を上書きせず、追加した hooks・permissions・skills directory を manifest に記録し、変更前 backup を残すのは、再実行と uninstall を安全にするためです。

主要 option:

```text
--install-dir PATH      default: ~/.agentstack
--project-key PATH      default: AGENTSTACK_PROJECT_KEY, PROJECT_KEY, repo root
--port PORT             default: 8770
--label-prefix PREFIX   default: org.agentstack
--terminal MODE         auto | ghostty | iterm | terminal | none
```

`--bin-dir` は installer の公開 option ではありません。permissions template 内の `__AGENTSTACK_BIN_DIR__` を安全に展開するため、installer が内部で `agentstack-merge-settings --bin-dir ~/.agentstack/bin` を呼びます。

version の正本は repository の `VERSION` です。installer はこれを install root にコピーし、`GET /api/version` は install 済み artifact、repository artifact、git metadata の順で解決します。

### macOS の TCC / Full Disk Access

`~/Desktop`、`~/Documents`、`~/Downloads` は macOS TCC の保護対象です。root agent を Full Disk Access のない terminal から起動すると、その terminal identity が tmux と子孫へ伝播し、子 agent だけ `EPERM` になることがあります。

- root agent を Full Disk Access 済み terminal から起動する
- または project を保護対象外へ移す
- context を変えた後は既存 tmux server / session を作り直す

launcher はこの状態を警告します。必要なら `AGENTSTACK_TCC_GUARD=0` で警告を無効化し、`AGENTSTACK_TCC_DIRS` で対象 directory を変更できます。権限エラーを chmod だけで直そうとしないのは、判定主体が file mode ではなく起動元 app だからです。

## エージェントを起動する

```bash
export PATH="$HOME/.agentstack/bin:$PATH"

agent-start ~/code/my-project
agent-start-codex ~/code/my-project
```

引数を省略すると、`fzf` があれば `AGENTSTACK_BASE_DIR`（既定 `$HOME`）以下を選べます。なければ現在 directory を使います。

```bash
export AGENTSTACK_BASE_DIR="$HOME/Obsidian/MyVault"
agent-start
```

tmux 外からは新しい named session を作り、tmux 内からは current session を rename してその場で起動します。session 名と agent-mail identity を一致させるのは、dashboard の jump、signal 配送、token recovery の照合を曖昧にしないためです。

### 科学者名と fail-closed 判定

新規 identity は `Adjective-Scientist`、たとえば `Swift-Bohr` です。形容詞は内蔵 list、科学者 suffix は `dashboard/scientist_portraits.json` から選びます。suffix が portrait key なので、個別登録なしで顔が決まります。

候補の利用可否は `available / occupied / unknown` の三値です。transport failure、auth error、timeout は `unknown` とし、空き名として扱いません。既定で `unknown` が3回続くと停止し、衝突を疑う名前を取得しない fail-closed 設計です。

### identity と token

既存 identity の再登録には、その identity の `registration_token` が必要です。top-level token は次へ mode `0600` で保存されます。

```text
${AGENTSTACK_RUNTIME_DIR:-$HOME/.claude/runtime}/agent_token_<name>
```

delegated child は加えて `child-agents/<name>.json` に child-owned state を持ちます。pre-registered child へ親 token を転送しません。`CHILD_REGISTRATION_TOKEN` という名前は歴史的なもので、top-level identity の再認証にも使われます。

```bash
AGENTSTACK_PROJECT_KEY=/path/to/project \
  ~/.agentstack/bin/agentstack-reregister "$AGENT_NAME"
```

helper は token を transcript や process argument に表示せず復元します。同名登録が失敗しても別名を作らないでください。別名は inbox と thread continuity を分断します。

launcher / child spawner は tmux session ごとの環境に `CLAUDECODE=1` を設定します。これは interactive shell の exit hook が tmux server 全体を連鎖 kill する事故を防ぐ guard です。また、継承した `AGENT_NAME`、`PARENT_AGENT`、token、reserved marker は top-level 起動前に消し、identity hijack を防ぎます。

Codex は Claude Code の hook system を持たないため、`agentstack-codex-bootstrap` が起動前の登録と tmux rename を担当します。Codex 起動時は `OPENAI_API_KEY` を除き、ChatGPT OAuth を優先します。

## Dashboard

<!-- TODO: screenshot: DECK view -->

### DECK

DECK は agent ごとの運用カードです。

- running / standby / finished / gone / retired を section 分け
- portrait、model、provider、context window、task、最後の受信指示、成果物数、attach 状態
- work / wait / approval / question と経過時間
- context 残量をカード下端の hairline で表示（緑・橙・赤）
- live pane title、agent-mail の last active、検索、`show all`
- card から History / Output panel、tmux open、running agent の二段確認 EXIT
- finished / gone は、attached client がない場合だけ二段確認 KILL / soft retire

KILL の可否は frontend の見た目ではなく server の `build_agents()` category を再利用して判定します。過去 session を running と誤判定して消さないため、分類の正本を一つにしています。

### NETWORK

<!-- TODO: screenshot: NETWORK view -->

NETWORK は spawn 系譜と agent-mail 通信を force graph で重ねます。

- node medallion に portrait、provider badge、running halo、状態 motion ring
- context 残量を node 外周の最大270度 arc として静的表示
- hover / long press tooltip に task、live state、model、last activity
- node click で詳細 panel。History は transcript と24時間 event sparkline、Output は vault の `LOG_*.md`
- ROLE ASSIGN で role / group annotation を保存・削除
- communication edge click で mail drawer。両者間の subject、importance、時刻、本文を表示
- time window slider / ALL、mail comet、spawn edge、legend
- TUNE で node size、link distance / width、repel、center、spring を調整し localStorage に保存
- 300 node を超えると label、annotation、badge、context arc を隠す dense mode

`AGENTSTACK_PROJECT_KEY` / `AGENTSTACK_VAULT` がないと edge drawer は `NOT CONFIGURED` を表示します。tmux telemetry 自体は見えるため、mail 設定不足と dashboard 全停止を区別できます。

### SELECT と一括操作

SELECT mode では drag rectangle または node click で複数選択できます。

- EXIT: running / finished へ順番に `/api/exit`
- RESUME: gone / retired へ `/api/jump`。tmux がなければ transcript resume へ fallback
- REPLAY: mail history を持つ2 agent 以上で DIGEST REPLAY

EXIT / RESUME は二段確認し、60 ms 間隔で順次送信します。一括操作を安全弁のない並列 request にしないのは、誤操作と service spike を避けるためです。

### DIGEST REPLAY

REPLAY は選択 agent の mail、spawn、exit / retire、承認待ち event を時系列再生します。

- play / pause、seek、event marker、absolute / relative clock
- 対数 scale の速度 `×1`〜`×10000`
- message card の HOLD `0.1s`〜`15s`
- GROUP-ONLY で選択 group 内の通信だけを表示
- TIME-TRAVEL は initial snapshot から node / edge / state を再構築。OFF は現在 graph 上の comet replay
- `Esc` / CLOSE で live graph snapshot と mail polling を復元

履歴範囲は event の最古・最新へ自動 fit し、短すぎる範囲は操作できる幅まで広げます。大量 event は topology と group 内 mail を優先して間引きます。

### NEW AGENT

<!-- TODO: screenshot: NEW AGENT modal -->

`+ NEW AGENT` は `/api/spawn-names` の catalog を使い、科学者、形容詞、working directory、model、parent、role / group、task、任意の isolated worktree を選びます。occupied / unknown の科学者は選択不可です。科学者を選ぶと形容詞が付き、shuffle で `WindyCurie` のような separator なしの名前を再構成します。科学者を選ばない場合は `name` を省略し、agent-mail の自動命名に委ねます。

spawn は `register_agent → annotate → send_message → spawn_child.sh --pre-registered` の順です。token file を mode `0600` で渡し、3秒後に tmux session の存在を確認します。失敗時は `dashboard/logs/spawn.log` の tail を API error に含めます。

### embed mode

`/?embed=1` または same-origin iframe では compact header の embed mode になります。parent window は次を送れます。

```js
frame.contentWindow.postMessage({type: "net-pause"}, location.origin);
frame.contentWindow.postMessage({type: "net-resume"}, location.origin);
```

pause は polling を止め、resume は現在 view を即 refresh します。hidden iframe が tmux / SQLite を継続 polling しないための契約です。

## API reference

dashboard API は既定で `127.0.0.1` に bind します。認証 layer はないため、`AGENTSTACK_BIND_HOST=0.0.0.0` は control endpoint と terminal bridge を trusted LAN / VPN へ公開する操作です。

### endpoint 一覧

| Method | Path | 用途 |
| --- | --- | --- |
| GET | `/api/version` | name、version、API revision |
| GET | `/api/spawn-names` | name status、adjective、directory、model catalog |
| GET | `/api/agents` | DECK 用の tmux + mail live rows |
| GET | `/api/graph?days=4&all=0` | NETWORK nodes、communication edges、spawn edges |
| GET | `/api/history?session=NAME&limit=220` | Claude / Codex transcript |
| GET | `/api/agent-history?name=NAME&hours=24` | 単一 agent event。`names=A,B` で replay union、`include_pane_states=1` で ask event |
| GET | `/api/edge-messages?a=A&b=B&limit=60` | 二者間 thread drawer |
| GET | `/api/messages-since?since=EPOCH&limit=80` | live mail comet |
| GET | `/api/annotations` | role / emoji / group map |
| GET | `/api/deliverables?agent=NAME` | vault 成果物 index |
| GET | `/api/custom-portraits` | custom portrait mapping |
| GET | `/api/term?session=NAME&lines=500` | tmux capture |
| GET | `/api/ptty?session=NAME` | browser terminal 用 ttyd を確保 |
| GET | `/api/mail-watcher-health` | watcher、signal backlog、直近配送結果 |
| POST | `/api/jump` | tmux open / focus、または transcript resume |
| POST | `/api/exit` | graceful `/exit` |
| POST | `/api/kill` | finished / gone の tmux kill と soft retire |
| POST | `/api/annotate` | role annotation の upsert / delete |
| POST | `/api/spawn` | child 登録、task 配送、tmux 起動 |

static resource として `/portrait?name=Curie&hi=1`、`/assets/*.svg|png` も提供します。

### request / response 例

```bash
curl -s http://127.0.0.1:8770/api/version
```

```json
{"name":"claude-agent-stack","version":"0.9.0","api":1}
```

```bash
curl -s http://127.0.0.1:8770/api/spawn-names
```

```json
{
  "names":[{"name":"Curie","portrait":true,"status":"available"}],
  "adjectives":["Windy","Curious"],
  "naming":"adjective+scientist",
  "dirs":["~","/path/to/project"],
  "models":["claude-sonnet-5","claude-opus-5","claude-haiku-4-5-20251001"],
  "default_model":"claude-sonnet-5"
}
```

```bash
curl -s -X POST http://127.0.0.1:8770/api/annotate \
  -H 'Content-Type: application/json' \
  -d '{"name":"WindyCurie","role":"docs","group":"release"}'
```

```json
{"ok":true,"annot":{"name":"WindyCurie","role":"docs","emoji":"","group":"release"}}
```

`role` と `emoji` を空にすると削除です。

```bash
curl -s -X POST http://127.0.0.1:8770/api/spawn \
  -H 'Content-Type: application/json' \
  -d '{
    "parent":"Curious-Copernicus",
    "name":"WindyCurie",
    "dir":"/path/to/project",
    "model":"claude-sonnet-5",
    "role":"docs",
    "group":"release",
    "task":"README を検証する",
    "worktree":false
  }'
```

```json
{"ok":true,"child_name":"WindyCurie","tmux_session":"WindyCurie","annot":"ok","worktree":false}
```

`name` は任意です。指定時は `available` が確認できなければ拒否します。`parent`、`task`、存在する `dir`、許可 model は必須です。

```bash
curl -s -X POST http://127.0.0.1:8770/api/exit \
  -H 'Content-Type: application/json' \
  -d '{"session":"WindyCurie"}'
```

```json
{"ok":true,"session":"WindyCurie","actions":["exit-sent"]}
```

error response は原則 `{"ok":false,"error":"..."}` で HTTP 400、spawn catalog の source が読めない場合は HTTP 503 です。

## 設定

### Dashboard / `server.py`

| 環境変数 | 既定値 | 意味 |
| --- | --- | --- |
| `AGENTSTACK_PORT` | `8770` | HTTP port |
| `AGENTSTACK_BIND_HOST` | `127.0.0.1` | bind address |
| `AGENTSTACK_MAIL_DB` | `~/mcp_agent_mail/storage.sqlite3` | agent-mail SQLite |
| `AGENTSTACK_MAIL_ENV` | `~/mcp_agent_mail/.env` | dashboard spawn が bearer token を読む file |
| `AGENTSTACK_PROJECT_KEY` | 未設定 | agent-mail project の human key |
| `AGENTSTACK_VAULT` | 未設定 | project key fallback と Output scan root |
| `AGENTSTACK_LABEL_PREFIX` | `org.agentstack` | launchd label prefix |
| `AGENTSTACK_TERMINAL` | `auto` | `ghostty` / `iterm` / `terminal` / `none` |
| `AGENTSTACK_HOOKS_DIR` | `~/.agentstack/hooks` | hook と既定 spawn script の root |
| `AGENTSTACK_RUNTIME_DIR` | `~/.claude/runtime` | token、notification state、annotation 周辺 runtime |
| `AGENTSTACK_MAIL_HOME` | `~/.mcp_agent_mail` | signal data root |
| `AGENTSTACK_SIGNALS_DIR` | `$AGENTSTACK_MAIL_HOME/signals` | mail signal root |
| `AGENTSTACK_PORTRAITS_DIR` | 未設定 | private PNG overlay directory |
| `AGENTSTACK_CUSTOM_PORTRAITS` | 未設定 | agent name → portrait key JSON |
| `AGENTSTACK_SPAWN_SCRIPT` | `$AGENTSTACK_HOOKS_DIR/spawn_child.sh` | NEW AGENT launcher |
| `AGENTSTACK_SPAWN_DIRS` | `~` | NEW AGENT の preset directories。`:` 区切り |

`AGENTSTACK_PROJECT_KEY` と `AGENTSTACK_VAULT` が両方未設定でも DECK の tmux state、terminal open、local annotation は動きます。一方、launcher の shell-side agent registration、edge mail、history / replay、dashboard spawn、project scoped retire、vault Output は動きません。mail 系だけが `NOT CONFIGURED` になるのは、local telemetry を診断に残すためです。

### Installer / launcher

| 環境変数 | 既定値 | 意味 |
| --- | --- | --- |
| `AGENTSTACK_HOME` | `~/.agentstack` | install root |
| `AGENTSTACK_MAIL_DIR` | `~/mcp_agent_mail` | upstream clone |
| `AGENTSTACK_AGENT_MAIL_REPO` | upstream GitHub URL | clone source |
| `AGENTSTACK_MCP_URL` | `http://127.0.0.1:8765/mcp` | agent-mail MCP endpoint |
| `AGENTSTACK_BASE_DIR` | `$HOME` | launcher picker root |
| `AGENTSTACK_CLAUDE_BIN` | `claude` | Claude CLI |
| `AGENTSTACK_CODEX_BIN` | `codex` | Codex CLI |
| `AGENTSTACK_CODEX_SANDBOX` | `workspace-write` | Codex sandbox |
| `AGENTSTACK_CODEX_APPROVAL` | `on-request` | Codex approval mode |
| `AGENTSTACK_VAULT` | 未設定 | Codex へ `--add-dir` する追加 writable directory |
| `AGENTSTACK_PROTECTED_ROOTS` | project key | Claude file-reservation hook の保護 root |
| `AGENTSTACK_CONTACT_POLICY` | `open` | 登録後の agent-mail contact policy。`skip` で server default |

installer が生成する `~/.agentstack/env.sh` が通常の設定箇所です。service の environment は install 時に plist / unit へ焼き込むため、変更後は installer を再実行するか service definition も更新してください。

`AGENTSTACK_MCP_URL` は launcher / hook の接続先です。現行の dashboard `/api/spawn` は `http://127.0.0.1:8765/mcp` を固定で使うため、別 endpoint を使う場合は dashboard spawn との整合も確認してください。

## カスタマイズ

### portrait overlay

```bash
export AGENTSTACK_PORTRAITS_DIR="$HOME/.agentstack/portraits_64"
export AGENTSTACK_CUSTOM_PORTRAITS="$HOME/.agentstack/custom_portraits.json"
```

overlay directory に `mybot.png` を置き、登録名を小文字 key で portrait stem へ対応させます。

```json
{"mybot":"mybot","windycurie":"Curie"}
```

`examples/custom_portraits.example.json` も参照してください。private asset を repository へ commit せず、distribution の generic portrait と分離する設計です。

### NEW AGENT directory presets

```bash
export AGENTSTACK_SPAWN_DIRS="$HOME/code:$HOME/Obsidian/MyVault:/tmp"
```

API は `~` を symbolic のまま返し、実際の spawn 時に展開します。UI は最後に選んだ directory を localStorage に保存します。

## Skills と file reservation

installer は `skills/delegate` と `skills/log` を `~/.agentstack/skills` へ配置します。

- `/delegate`: resource を宣言・予約し、Claude / Codex child、任意 model、worktree を起動して監視
- `/log`: Obsidian vault なら `05_Agents` と Daily Note へ接続した log、なければ local `logs/`

Claude Code は `check-file-reservation.sh` の PreToolUse hook で `Edit` / `Write` を hard block します。Codex には同等 hook がないため、managed `~/.codex/AGENTS.md` が reserve / renew / release discipline を指示します。registry は共通なので、Claude と Codex の reservation は相互に見えます。

## agent-mail は別 component

`mcp_agent_mail` はこの repository に同梱しません。installer が upstream を取得し、既存 clone があれば remote を確認して再利用します。データ directory と `.env` は uninstall 時も既定で保持します。

upstream の license は **MIT License with OpenAI/Anthropic Rider** です。通常の MIT 許諾に加え、OpenAI、Anthropic とその関係者に対する追加制限があります。正確な条件は取得した `~/mcp_agent_mail/LICENSE` を確認してください。本 repository 自体の条件は [LICENSE](LICENSE) が正本で、両者は別 license です。

## トラブルシューティング

### `NOT CONFIGURED`

dashboard service に `AGENTSTACK_PROJECT_KEY` または `AGENTSTACK_VAULT` がありません。`~/.agentstack/env.sh` だけでなく launchd plist / systemd unit の environment を確認し、installer を再実行して service を再起動します。

### launchd が起動しない

```bash
label=org.agentstack.agentdashboard
plist="$HOME/Library/LaunchAgents/$label.plist"
launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$plist"
launchctl enable "gui/$(id -u)/$label"
tail -f ~/.agentstack/dashboard/dashboard.log
```

同じ port を使う process があると installer は停止します。`AGENTSTACK_PORT` または `--port` を変えてください。

### Codex に通知 text は入るが送信されない

tmux injection は一回の `send-keys` に text と Enter を混ぜず、必ず二回に分けます。

```bash
tmux send-keys -t "$session" -l "$text"
sleep 0.2
tmux send-keys -t "$session" C-m
```

Codex REPL では `Enter` keysym が submit されない場合があるため `C-m` を使います。watcher は bare shell への誤注入を避け、tmux call を timeout 付き worker で実行します。

### dashboard spawn がすぐ消える

1. `dashboard/logs/spawn.log` の末尾を見る
2. `tmux has-session -t '<child-name>'` を確認する
3. `~/.local/bin/claude` または指定 CLI が service の `PATH` から見えるか確認する
4. `AGENTSTACK_SPAWN_SCRIPT`、working directory、`AGENTSTACK_PROJECT_KEY` を確認する
5. `~/.agentstack/bin/agentstack-reregister '<child-name>'` で token state を確認する

dashboard は launcher 起動後3秒で tmux session を probe します。launchd の最小 PATH では `~/.local/bin` が欠けやすいため、spawn path はこれを先頭へ補います。

### registration / inbox の認証に失敗する

別名を作らず `agentstack-reregister "$AGENT_NAME"` を実行し、`agent_token_<name>` または `child-agents/<name>.json` の存在を確認します。token が missing / stale / wrong-owner なら親または operator へ報告してください。

### tmux の scrollback が使えない

```tmux
set -g mouse on
set -g history-limit 50000
```

または `Ctrl+b [` で copy mode に入ります。`agentstack-doctor` も mouse mode を確認します。

## Upgrade / uninstall

```bash
git pull
./scripts/install.sh
~/.agentstack/bin/agentstack-doctor
```

installer は payload と `VERSION` を更新し、service を再登録し、managed merge を再び preview します。managed block は idempotent です。

```bash
~/.agentstack/bin/agentstack-uninstall
```

agent-mail clone、mail home、DB、`.env` は既定で retained path です。削除範囲は `~/.agentstack/install-state.json` が正本です。

## License

Copyright (c) 2026 gyroid. 利用条件は [LICENSE](LICENSE) を参照してください。

`mcp_agent_mail` は別 component であり、上記の Rider 付き MIT license が適用されます。
