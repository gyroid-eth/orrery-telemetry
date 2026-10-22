# dsh / Hermes の非有効な設定例

> English: [README.en.md](README.en.md)

**最初にDeepSeek・Localを追加する方へ：** [初期設定ガイド](https://github.com/gyroid-eth/orrery-telemetry/blob/master/docs/native-setup.md)で、APIキーの取得・OSへの保存・runtime追加の順序を確認してください。この文書は、設定例をコピーしてつなぐ工程の詳細です。

例の版: 1。これは設定補助用の例です。installは接続先の有効設定や鍵を作りません。
ここにあるmanifestをそのままサービスへ登録しないでください。

## B標準：runtime参照入りの例を使う

harnessを任意導入したinstallは、検証済みのcurrent/所有receiptからruntime参照を
埋めた `resolved/<snapshot-id>/` の4JSONを作り、完了時にその場所を表示します。
manifestは常に `enabled: false` です。両harness未導入なら静的例だけを配り、
片側だけならsnapshotのmanifestもそのharnessだけを参照します。
未導入側のJSONにはplaceholderが残るため、そのまま有効化しないでください。

1. 利用者がharness・backend・正式model ID・新しい私有設定の保存先を選びます。
2. 既存agentへ「このREADMEに従い非秘密設定だけ準備して」と依頼できます。
   agentは表示されたsnapshotを**別の新規dir**へコピーし、dir0700/JSON0600にします。
   既存設定を上書きせず、必要なら変更案を先に確認します。
3. コピー先のmanifestの `harnesses.dsh.config_file` と
   `harnesses.hermes.config_file`、dsh-configの `backend_file` をコピー先の
   **絶対path**へ直します。使うharnessだけなら不要なentryを省きます。
   JSON内の環境変数やplaceholderは自動展開されません。
4. backend URL・model・非秘密の資格参照を本人の選択へ合わせます。
   鍵値をagentへ貼ったり、JSON/env/argvへ記録したりしません。
5. 本人が下記のOS秘密保管へ登録し、agentへは「登録済み/失敗」と非秘密refだけを返します。
6. 本人が利用者コピーの有効化を承認した後、そのコピーだけを `enabled: true` にします。
   既存の `hooks/harness_child.py validate --manifest <コピー先manifestの絶対path>
   --harness <dsh|hermes> --backend deepseek --model <正式ID>` を確認済みPythonで使えます。
   これは静的参照検査で、資格取得・接続・Mail成功の試験ではありません。
7. 本人承認の下で既存installerの `--managed-spawn-config <絶対path>` へ参照を渡します。
   install/service操作を伴うためagentが無断で実行しません。設定後に標準のNew Agentから
   起動し、親子Mail・通常終了・同identityのresumeを別に受け入れます。

両harnessでは4file/13参照（runtime由来10＋例内peer参照3）。本人の手書き回数ではなく、
installとagentが合わせる項目です。Bはruntime参照の手転記を省きますが、保存先・接続先・
秘密入力・有効化承認・実受入を自動で済ませるものではありません。

## A受け皿：未導入・model変更・調整

同dirの [manifest](manifest.example.json)、[dsh config](dsh-config.example.json)、
[dsh backends](dsh-backends.example.json)、[Hermes config](hermes-config.example.json)
は未完成の静的例です。`<ABS_...>` を残して起動できません。既存agentは確認済みの
current/receiptとHermesのcommitted PM factsを使い、非秘密参照を合わせられます。
世代名/venv名を推測したり、実profile/認証store/記憶や広いruntimeを探索したりしません。
導入が不足・不整合なら止めて導入の修復を依頼します。

静的4JSONはMacの資格形式です。WSLでAを使うときは次節の置換を必ず行います。
model変更時も主/helperは同じbackendの正式IDだけを許可し、effortや要求数/寿命の
試し用固定値を足しません。例には巨大なcontext/output上限を設定していません。

### Local / Strataへ変更する場合

Bの標準例はDeepSeek用です。Localはbackend IDを `strata` とし、利用者コピーの
次のfieldを同じ接続先へ揃えます。runtime参照とコピー先の絶対pathはそのまま整合させます。

| file | 変更するfield |
| --- | --- |
| manifest | `backends.strata.model` / `models` に利用者が確認した正式model IDを設定 |
| dsh-backends | `backends.strata` に変更し、`model` と `base_url` を正式ID・利用者指定のHTTPS URLへ設定 |
| Hermes config | `profiles.strata` に変更し、`model` / `base_url` / `api_mode` を設定。modeは接続先が提供する `chat_completions` または `anthropic_messages`。`provider` は組込み名と衝突しない任意名（例 `orrery-strata-manual`） |
| OS資格ref | Macはdshの `backends.strata.keychain` とHermesの `profiles.strata.key_cmd` を本人指定の同じ資格へ。WSLはmanifestの `backends.strata.credential_ref` を1正本とし、対応するkeychain/key_cmdを削除 |

DeepSeekのURL・model・資格selectorを残したままLocalとして使いません。endpointのpathや
API modeは推測せず、利用者が確認したserver仕様に合わせます。dshのこの設定には
`api_mode` の切替fieldはないため、Hermes用fieldを足して同じprotocolになるとは扱いません。
不要なDeepSeek entryはコピーから省き、下記の `--model` も `backends.strata.models` に含めます。

確認済みPythonで `hooks/harness_child.py validate --manifest <コピー先manifestの絶対path>
--harness <dsh|hermes> --backend strata --model <正式ID>` を実行します。
これは静的検査で、Mac/WSLのDeepSeek/Local×dsh/Hermesの4通りの標準UI起動・接続の
実機受入とは別です。秘密入力と有効化の本人承認は上の手順と同じです。

## MacとWSLの資格参照

**Mac:** dshはdsh-backendsのkeychain.service/account、Hermesはprofileのkey_cmdで
同じ会話用selector `orrery-dsh-deepseek` / `api-key` を参照します。本人がKeychain Access等の
OSの非表示入力で登録します。agentはsecurityのlookupを実行せず、鍵の表示を求めません。
このselectorは既存Mac残高workerの `deepseek-api` と別です。会話成功は残高の取得成功や
同一accountの証明ではなく、残高は別の未受入項目です。二重鍵登録を求める手順ではありません。

**WSL / LinuxのSecret Service経路:** #330対応sourceを含む製品のLinux snapshotでは、
manifest.backends.deepseekへ次の1正本だけを置き、dshのkeychainとHermesのkey_cmdを省きます。
静的Aを手で補助する場合も、この2つの旧fieldを削除してから同じ形へ置換します。

```json
{
  "credential_ref": {
    "store": "secret-service",
    "attributes": {"service": "orrery-dsh-deepseek", "account": "api-key"}
  }
}
```

本人が同じattributesのSecret Service項目へ秘密を保存します。必要なSecret Service、
D-Bus session、unlock、`/usr/bin/secret-tool` の準備はOS側の工程です。agentにlookupや
内部readerの直接実行をさせず、秘密登録を自動化しません。両harness/guard/quotaは同refを
製品内部のreaderへ投影し、旧Mac資格との併記はエラーです。MacへSS refを足したり、
失敗時に別storeへfallbackしたりしません。

非有効例の生成はSecret Serviceの存在/解除/取得成功を検査しません。Ubuntu WSLの実Node/PM、
秘密入力、標準起動/Mail/終了/exact resumeは実機受入前です。旧sourceへのコピーだけでは
WSL機能は増えません。macOS legacyとLinux legacyの静的準備を、全OS/modeの保証と混同しません。

## 更新・削除・証拠の範囲

既存例/snapshotは更新でも上書きせず、不足分と別内容の新snapshotだけを追加します。
既存保持の表示は最新/使用可の検証を意味しません。古い例を使い続けるか、利用者が退避して
新版を新規配置するかを選びます。installは未記録の既存例を所有化しません。
uninstall/purgeは記録hashと一致する未編集の例だけを削除し、編集済み・リンク・未記録file・
利用者コピーを保持します。snapshotの有効化や利用者設定の自動移行はありません。

公開DeepSeek endpoint候補はdshが `/anthropic`、Hermes Chatが `/v1`。
固定source上の投影と、標準例の全bytesによる実wire受入は別です。dshの最終wire、本人の秘密入力、
新規利用者のMac/WSL標準例の通し受入は未確認で、配布/所有のsynthetic試験だけで合格としません。
