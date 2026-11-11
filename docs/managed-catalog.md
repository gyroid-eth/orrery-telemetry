# Native catalog and effort capabilities

> English: [managed-catalog.en.md](managed-catalog.en.md)

## 現在の組合せ

[provider・model・harness の説明](getting-started.md#provider-model-harness)から、次の表で使う経路を選びます。API の4組は設定が整えば選択できる候補であり、手元の資格・接続先・model での実機成功を意味しません。

| provider ID / 接続先 | harness | model と準備条件 |
| --- | --- | --- |
| `claude` / Claude Code の既存経路 | Claude Code（`claude-code`） | CLI のログインと既存 model catalog を使う。新しい汎用 Anthropic API 接続の設定ではない |
| `codex` / Codex CLI の既存経路 | Codex CLI（`codex-cli`） | CLI のログイン・利用権限と版に応じた model catalog を使う |
| `deepseek` / 設定済み DeepSeek backend | `dsh` | 固定 DeepSeek adapter と資格の参照、設定済みの正式 model ID が必要 |
| `deepseek` / 設定済み DeepSeek backend | `hermes` | 明示 profile と資格の参照が必要。公式 DeepSeek の対象 `/anthropic` route は thinking を保持する `/v1` Chat Completions に投影する |
| `strata` / 画面では Local | `dsh` | 既存 adapter に合う API と資格の参照、設定済みの正式 model ID が必要。任意の Local server を保証しない |
| `strata` / 画面では Local | `hermes` | Chat Completions / Anthropic Messages の route を明示設定する。手動 effort の能力公開と、実 Local server の受理・効果の確認は別 |

API を使う4組の実機での確認範囲は現在 macOS の legacy mode です。Ubuntu WSL の追加の静的経路は次節に記載します。同じ backend の `models`（未指定なら `model` 1件）にある正式 ID を主・補助 model の候補とし、選んだ model × harness ごとに利用可否を検査します。別 provider の資格や未設定の model を推測して補いません。

[Codex App Bridge](codex-app.md) と [Antigravity / Gemini](antigravity.md) は別の任意統合です。この表から dsh / Hermes との追加の組合せを保証したり、未対応の API を `claude` / `codex` として使えたりするわけではありません。

runtime の任意導入は `install.sh --enable-dsh --dsh-node /absolute/node` / `--enable-hermes` で選びます。[導入と削除](install.md)を参照してください。

## catalog と起動

Ubuntu WSL 向けに、明示した Secret Service 参照を持つ Linux の静的な選択経路を追加しています。
`backends.<id>.credential_ref` を1つの正本として両 harness と残高 reader に渡します。
[参照の条件](install.md#ubuntu-wsl-の-secret-service-参照実機受け入れ前)を確認してください。
実 WSL の構築・runtime・認証・Mail・終了/resume は受け入れ前で、Linux 全般の動作を保証するものではありません。

`dashboard.managed_catalog.standard_catalog(config_file, *, mode)` は標準 spawn の選択肢を返します。`hooks.harness_child.selection()` と同じ世代・設定の静的な検査を使い、model ごとに利用可能性と固定理由を返します。鍵・登録・provider API には接続しません。起動時にも共通 launcher が検査します。

provider ID は deepseek/strata、harness ID は dsh/hermes。manifest の同じ backend の正式 model を主と補助の候補に使い、generator の対応能力に従って公開します。未 opt-in は空配列、legacy 以外は unsupported_mode、Mac/Linux 以外は unsupported_platform です。Linux では明示した Secret Service 参照と静的に準備された reader/runtime が必要です。私有 URL・account・実行 path を公開しません。

設定が整って catalog に並んでも、起動や作業の成功は別に確かめます。Telemetry 同梱 NEW AGENT でも、catalog で available な DeepSeek / Local の model と Harness（dsh または Hermes）を選べます。cockpit を追加した環境の New Agent / Harness / Lineage と Mail の[通し確認](getting-started.md#5-しりとりで通しの確認をする)を参照してください。画面を使わない起動には [API reference](api.md) の provider・model・harness・親・認証の契約を使います。

## effort の能力と既定

manifestの手書きefforts/default_effortを能力の根拠にはしません。選択modelのharness entryがefforts/default_effort/effort_sourceを持ち、pickerと明示POSTの検証は同じentryを使います。同梱 NEW AGENT と追加の New Agent の画面では、未操作・Use default のとき、catalog が報告した有効な具体 effort を送り、既定が null・不明・候補外なら effort を省略します。候補先頭を既定と推測しません。API で effort 自体を省略した場合は、ハーネスの設定を保持します。provider共通配列は既定の利用可能harnessからの互換投影だけです。

**dsh**: install/updateは保存済み又は指定した manifestのmodelを対象に、固定Node・runtimeのローカルresolveModelInfoから段と既定を取得します。鍵・profile・discovery・モデルAPIを使わず、runtime世代のevidence/effort-capabilities.jsonへ保存します。runtime/modelが変わったcache・取得失敗はunknown（null/null/unknown）。選択肢だけを無効にし、未指定の起動を妨げません。opt-in offでは取得/投影しません。

dshの報告はsource=harnessで、Strataのserver報告ではありません。固定adapterの4段を自動取得し、Strataの公開sourceでmaxがhighと同じ段であることと、配置版を照合していないことを区別します。

**Hermes**: 固定版の native source をデータとして静的に読み、実効 route に合わせて公開します。Chat Completions の経路では `none`・`minimal`・`low`・`medium`・`high`・`xhigh`・`max`、対象 model の Anthropic Messages の manual thinking 経路では `low`・`medium`・`high`・`xhigh` を使います。manual の4段は Claude・Haiku・Kimi の別方針には適用せず、native の構造が変わった時や読み取れない時は unknown にします。詳しくは[モデルごとの能力](native-capabilities.md)を参照してください。

Hermes の公開既定は `default_effort=null` です。未指定や「Use default」は `--reasoning` を送らず、保存済みの全体・model 別設定や harness の既定に任せます。一定の段を推測表示しません。Chat の `none` と違い、manual 経路の `none` は明示的な thinking 無効化にならないため選択肢にしません。

旧manifestやprivate backendに残る試行low/highは利用者設定の移行対象です。installerはprivate設定を無断書換せず、標準起動の未指定 effort 経路はこれらをハーネス既定として投影しません。

## native の web_search

managed spawn manifest の `web_search` で dsh・Hermes の検索先を共通に設定できます。
この節を省略した製品の既定は従来どおりです。接続先の model や backend ID に依存しません。

```json
{
  "web_search": {
    "provider": "exa",
    "exa": {
      "credential_ref": {
        "store": "keychain",
        "service": "orrery-web-search-exa",
        "account": "your-account"
      }
    },
    "searxng": {"url": "http://search.example.invalid:8080"}
  }
}
```

本人の端末で `security add-generic-password -U -s orrery-web-search-exa -a your-account -w` を実行し、末尾の `-w` が出す入力待ちに Exa の鍵を入れてください（値をコマンドに書かない）。
Linux / WSL では同じ `credential_ref` に既存の `{"store":"secret-service","attributes":{"service":"orrery-web-search-exa","account":"your-account"}}` を使います。

起動ごとに切り替える場合、native の `POST /api/spawn` に `"web_search":"searxng"` または `"web_search":"exa"` を付けます。
内部 launcher の対応する指定は `--web-search searxng|exa` です。URL と資格の参照は manifest の同じ設定を使い、指定は保存されるので resume にも渡ります。

dsh は固定 runtime の `@deepseek-ai/dsh-web-search-exa` と小さな SearXNG provider を使い、検索先を固定します。
Hermes は native の Exa と資格 helper（`secrets.command`）を使い、子専用 config に helper の非秘密参照を写します。
managed Hermes の固定 runtime は、上流で版が固定された `exa` extra を install 時に準備します。既存の install も新しい世代へ更新し、私有設定の runtime 参照を更新後の `current.json` に合わせてから新しい子を起動してください。子の初回検索で SDK を別の環境へ入れる手順には頼りません。
Hermes の SearXNG には native の HTTP・結果整形を使う薄い provider を足し、両 harness とも「結果0件かつ `unresponsive_engines` がある」応答は engine 名つきのエラーにします。
検索先を指定した Hermes では匿名検索への自動救済を使わず、失敗理由をそのまま返します。
鍵の値は起動計画・設定ファイル・引数に保存しません。鍵無しや検索 service の停止は子の起動を止めず、検索時にエラーになります。
PR の検証は fake HTTP と偽の資格によるものです。実資格・実サービスの4組の tool-call は別途受け入れで確認します。
既知の差: dsh の上流 `dsh-web-search-exa@0.2.1-rc.2` は `contents:{highlights:{highlightsPerUrl:N}}` を使い、最初の有効な highlight だけを snippet にします（Exa 公式推奨は `contents:{highlights:true}`、Hermes は推奨どおり）。両 harness の検索内容は query ベースで、公式が推奨する `objective`（何を上位にし、除外し、抽出するか）は検索引数にありません。

## 現在の制限

- **手動設定**: B（runtime 参照入りの非有効な例の配布）と A（既存 agent による非秘密設定の補助）は、[日本語 README](../examples/native-harnesses/README.md)・[English README](../examples/native-harnesses/README.en.md) に案内があります。コピー後の参照整合、本人の秘密入力と有効化承認が必要です。実資格・標準例を使う Mac / WSL の全 DeepSeek / Local × dsh / Hermes の組合せは通し受け入れ前で、共通 provider ログイン CLI はありません。
- **OS と画面**: API 起動の catalog は legacy mode に限定されます。Mac に加え Linux の明示した Secret Service 経路を静的に検査しますが、実 WSL の受け入れは未完です。既存 Claude Code / Codex CLI の WSL2 手順や同梱 NEW AGENT の候補表示を、API 経路の対応証拠にはしません。
- **Local effort**: Hermes の manual 4段は要求を組み立てる能力です。実 Local server の受理・効果は未受け入れです。固定版は temperature を1、出力上限を budget+4096 以上にするため、context 制約で拒否される場合があります。fallback して会話が続いたことを、選んだ effort が効いた証拠にしません。
- **Localの接続条件**: キーを保存・参照しない構成は現在非対応です。dshは固定adapterと互換のHTTP/HTTPS API、HermesはHTTPSと対応するAPI形式が必要です。キー不要・HTTPのみのサーバーを、任意のharnessでそのまま使えるものではありません。[初期設定の条件](native-setup.md#1-最初に使う組合せを選ぶ)を確認してください。
- **記憶と resume**: Hermes の新しい子は project と canonical 名ごとの安定した独立 HOME を使い、正規 resume は保存 pin と exact native session ID を照合して同じ HOME を使います。旧共有 HOME の pin は `native_resume_legacy_profile` で停止し、旧データや pin は自動移行しません。合成 provider を使う実 CLI で native store の保存・同 HOME / ID の復帰・別子との非共有を確認しています。resume 時に最新 memory を初回 system prompt へ再注入するものではなく、実 provider・full Mail の通し受け入れは未完です。会話履歴の resume と memory の分離・復帰は別の確認です。
- **Telegram**: fresh の interactive Hermes TUI と同梱 plugin の入口は[常駐 agent](persistent-agents.md#fresh-の-hermes-telegraminteractive)で案内します。登録・provider の準備と本人の plugin 承認が必要です。合成試験と実 Telegram／WSL の受け入れは別で、後者は未完です。
