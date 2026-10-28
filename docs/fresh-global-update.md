# 空の Mail へ切り替える手順（準備中）

[English](fresh-global-update.en.md) · [利用者単位の指示](instruction-migration.md)

**この手順だけで本番切替を完了できる段階ではありません。** 空のMail・明示client、子起動・managed harness・App adapter、画面API・PC入力・R4・終了済み整理は限定fixtureで確認しています。通常設定の保存・受渡しとMail/controller→dashboard host→Cockpitの起動・拒否・所有process停止も私有環境で確認済みです。実OS manager／Finder GUI／App／provider／login／trust／live install、最終CIと本人在席の切替は未受入です。case-insensitive な Mac の日本語pathは **nativeで確認したAPFSの限定profileだけ** が対象で、範囲外やHFS等では `CASE_RULES_UNKNOWN` の拒否が残ります。WSL の Windows 側 folder も実機未受入です。自分の作業先の支持範囲を下記で確認し、範囲外なら止めてください。未受入項目の解消と本人在席の切替判断が済むまで、以下を既存環境へ実行しません。

1台は1人の利用者が使い、その人のagentどうしは信頼する前提です。旧 Mail を統合せず、新しい Mail instance と空の DB から始めます。名前・数値IDが以前と同じでも別の登録です。旧 Mail の未読・資格・予約・通知状態・window binding・保存 resume の接続は継承しません。本文や添付の参照は、保管庫 CLI の出力から必要分を手で持ち越せます。

## agent に渡す依頼

fresh dashboardとCockpitは同じhost・UIDで、同じ確認済み `AGENTSTACK_FRESH_CLIENT_CONTEXT` を指定します。dashboardを先に起動し、再起動後はCockpitも再起動します。DBを差し替えたり復旧したりする場合は先にdashboardを停止し、作業後に再起動します。contextやprivate host接続が不明・失効の場合は止めます。

| 範囲 | 現在の確認と残作業 |
|---|---|
| 自動子・New Agent・managed/Local | 既存入口での同I内の別A・context、指定親へのMailを限定確認。実接続先・本人のlogin/trustは別確認 |
| Mail/graph・PC Terminal・R4・終了済み整理 | 有限reader、選択pane入出力・同native resume、本人確認送信、通常client拒否後の同pane整理を限定確認。実画面全体・実providerの保証ではない |
| App | 通知・waiting root wake・外部子adapterを合成fixtureで確認。実Desktop/本人trustは未受入 |
| 配布・通常起動 | 私有配布先launcherと通常設定からのMail/dashboard/Cockpit接続・停止を確認済み。開発Mail moduleを使う限定確認で、wheel/sdist・実OS manager/Finder GUI・live installは未受入 |
| 復帰・App前面化など | reactivate／退役済みresume／App GUI前面化は今回の切替対象外で、未接続のまま。稼働中の対象への通常jumpやApp通知・外部子とは別 |

API34や文書更新だけを根拠に切り替えません。

> docs/fresh-global-update.md を読んで、切替前に続ける仕事と復旧方法を整理して。未受入項目が残っていれば実行せず報告して。実行可能な版になったら、自分で行える段を進め、「本人」の段では止まって操作を具体的に説明して。資格の内容や storage.sqlite3 を直接読まず、保管庫は参照 CLI だけで読んで。

| 段 | agent が進めること | 本人の段 | 確認方法 |
|---|---|---|---|
| 1 | 続ける仕事・未読・返信待ち・担当fileを控え、旧版と起動／停止／復旧方法を記録 | 持ち越す仕事を選ぶ | 資格値を含まない引継ぎメモと復旧手順がある |
| 2 | 全writerと起動元を列挙し、停止を準備 | 在席で停止を判断。agent自身が停止対象なら保守端末を引き受ける | 作業agent・Mail・proxy・watcher・GC・自動再起動元が止まった |
| 3 | 停止後の旧一式を保全し、承認済みの版を配布 | 保存先と更新内容を確認 | 旧一式を変更しない保管と、戻す版／設定が揃う |
| 4 | 別rootを明示して初期化・health確認・foreground serve | 接続先と起動操作を確認 | 別の Mail instance、空の新登録先、旧接続との分離 |
| 5 | 新規登録・client prepare・provider準備の確認 | login、指示変更の確認、Codex hook の信頼承認 | 同じ新本人に reconnect。準備不足なら停止 |
| 6 | 保管庫 CLI で必要分を探し、新Mailへ送る／新予約後にノートへ残す | 持ち越し内容を確認 | 新ID・新時刻の受信、旧側の状態は変わらない |
| 7 | 新しい予約を取得し、別folderで送受信・通知・編集を確認 | 利用再開を判断 | 同じfileの競合拒否、本人／宛先／対象pathが一致 |
| 8 | 失敗時は新側も停止・保全し、手順で旧運用を復旧 | 復旧するかを判断 | 新旧が同時に動かず、新側の未完仕事も控えた |

## 1–3. 止める前に、戻せる形を残す

### dashboardとCockpitの明示準備（通常service切替は未完）

既存の New Agent を使うため、新側の既存clientを準備元として選び、I/Aとrootをreconnectで確認します。そのclientのprovider HOMEは既存setup／本人のCodex信頼承認で準備します。親付き起動では指定親自身の保護clientとprovider準備も必要です。browserの本文から資格やcontextを探しません。standalone=trueでは準備元への自動報告は付けず、親指定は一意な名前か明示I/Aであることを確認します。

別の保守端末では、確認済みの `orrery_python`、`orrery_install`、`orrery_context`、`orrery_work_root` を再設定してください。最後の変数は許可する作業folderです。`orrery_socket` は本人／agentが既存tmux手順で用意し、所有者・0700親directory・正規pathを確認した私有serverのsocketです（長すぎるpathなら停止）。default/live socketへ代えません。`orrery_dashboard_port` は空きを確認した非live portです。値が未確定なら起動しません。

```sh
env AGENTSTACK_PYTHON="$orrery_python" AGENTSTACK_HOME="$orrery_install" \
  AGENTSTACK_RUNTIME_MODE=fresh AGENTSTACK_CLIENT_CONFIG= AGENTSTACK_GLOBAL_SERVER_CONFIG= \
  AGENTSTACK_FRESH_CLIENT_CONTEXT="$orrery_context" \
  AGENTSTACK_FRESH_DASHBOARD_SOCKET="$orrery_socket" \
  AGENTSTACK_SPAWN_ROOTS="$orrery_work_root" AGENTSTACK_SPAWN_DIRS="$orrery_work_root" \
  AGENTSTACK_BIND_HOST=127.0.0.1 AGENTSTACK_PORT="$orrery_dashboard_port" \
  "$orrery_python" "$orrery_install/dashboard/server.py"
```

必要なprovider binaryとmanaged manifestは、それまでに確認した絶対pathを同じ端末の既存環境変数へ渡します。起動中に別clientへ切り替えません。新daemon／自動service切替は追加せず、このforeground processを止めればdashboardも止まります。

確認は `/api/version` の runtime_mode／I／capabilities、`/api/agents` のI/A、既存New Agentからstandaloneと親付き各1件、`/api/spawn-status?name=...` のnative_ready／終了記録の順です。pendingだけで起動成功としません。不明結果や準備後エラーではjob/I/Aを控え、同じ子を自動で作り直さず、既存processと準備記録を手順で確認します。provider未準備や失効は明示停止です。

現段階では worktree、browser、独自base/tools、terminalを開く指定、role/group/emoji注釈、dry_runは未接続として拒否します。必要なfolder／provider設定はagentが既存手順で準備してください。通常のClaude/Codex/dsh/Hermes/Localのstandalone／親付き自動起動を手動子作成へ置き換える意味ではありません。rosterのnative receiptだけでは現在のOS生存を証明しません。Mail／graph／Terminalの接続・上限・拒否条件は[API契約](api.md)を確認してください。

dashboardが応答してから、別の保守端末で同じ変数を再設定してCockpitを起動します。`orrery_instance`はhealthで確認した新I、`orrery_cockpit_root`は対応版checkout、`orrery_cockpit_python`はその公開依存を使える承認済みPython、`orrery_cockpit_port`は確認済みの空きport、`orrery_tmux`は確認済みの絶対binaryです。未確定なら起動しません。

```sh
orrery_instance='<healthで確認したmail_instance_id>'
orrery_cockpit_root='/absolute/path/to/matching/cockpit'
orrery_cockpit_python='/absolute/path/to/approved/cockpit-python'
orrery_cockpit_port='<confirmed-free-cockpit-port>'
orrery_tmux='/absolute/path/to/tmux'
env AGENTSTACK_RUNTIME_MODE=fresh AGENTSTACK_CLIENT_CONFIG= AGENTSTACK_GLOBAL_SERVER_CONFIG= \
  AGENTSTACK_HOME="$orrery_install" AGENTSTACK_PYTHON="$orrery_python" \
  AGENTSTACK_FRESH_CLIENT_CONTEXT="$orrery_context" \
  ORRERY_FRESH_MAIL_INSTANCE_ID="$orrery_instance" \
  ORRERY_DASHBOARD_URL="http://127.0.0.1:$orrery_dashboard_port" \
  "$orrery_cockpit_python" "$orrery_cockpit_root/bridge/orrery_backend.py" \
    --host 127.0.0.1 --port "$orrery_cockpit_port" --tmux-bin "$orrery_tmux"
```

これは明示backendの起動例です。準備済みcontextと私有socketを明示したinstallerは同じconfig/Iを保存・通常サービスへ渡し、再更新でも保持します。指定Pythonには対応Mail moduleが必要です。Cockpit scriptとFinderは共有loaderで保存設定を読み、同じI/context digest/epochのhealthを照合します。別配布先はCockpitの共有configへtelemetry_homeの絶対pathを指定します。Finderがshellのexportを自動継承するとは扱いません。同じbackendを二重起動しません。R4は[スマホ手順](mobile.md#スマホからpaneを確認して送る)の本人opt-inが別途必要です。

新旧それぞれの実行版、明示config／root、サービスの管理方法、停止・再起動の正確なコマンドを、その環境の [install](install.md)・[設定](configuration.md) と実際の登録内容から確認します。このページは launchd／systemd の名前や停止コマンドを推測しません。接続先・管理元が不明なら止め、勝手に別の DB や port を選びません。

先に仕事・返信待ちと編集担当を控えます。ノートを変更するなら、その時点の正規接続で対象pathを予約してから書きます。予約切れ・通信不能のまま編集を続けません。停止後は旧予約で作業を再開しません。

全writerと自動再起動元を停止したあと、DBだけでなく WAL／関連sidecar、archive／添付、設定・起動記録を含む旧一式を、元と区別できる保管先へ保存します。保管後はagentが書き込めない読取専用の保管庫として扱い、旧 Mail server や旧 watcher を閲覧のために再起動しません。旧 `fetch_inbox` はsignalを消すため、保管庫の参照入口に使いません。

保管庫 CLI に渡すのは、整合性が確定した **DBの保管コピー**だけです。停止確認なしのDB単体コピーや、非空WALを削除・別名化して見かけ上確定させる操作はしません。CLIは非空の `-wal`／`-journal` があれば拒否しますが、その検査だけでコピーの整合性を証明するものではありません。現段階にはコピーを確定する製品コマンドがなく、整合した保管コピーを用意できない場合は参照の段で止めます。agentが `sqlite3` や自作SQLで直接読む・checkpointする例外はありません。

準備済みfreshの通常起動経路は実装されていますが、実サービスへの切替・復旧と本人環境での受入は未完です。通常の update が自動的に新側へ切り替えるとは扱わず、承認された配布手順が揃うまでここから先を本番で実行しません。

## 4. 明示した空rootを初期化する

以下は保守端末で使うコマンドの形です。`<...>` と `/absolute/...` は実測・確認した値に置き換えます。未置換のまま実行しません。`orrery_python` はこの版の Mail package を実際にimportできる承認済みinterpreter、`orrery_install` は対応する launcher／hook が揃った配布先です。サービス用venvへ独自に依存を足しません。

```sh
orrery_python='/absolute/path/to/approved/python'
orrery_install='/absolute/path/to/matching/orrery-install'
orrery_fresh_root='/absolute/canonical/path/to/new-mail-root'
orrery_mail_port='<confirmed-free-mail-port>'
orrery_config="$orrery_fresh_root/config.json"
```

rootの親は存在する必要があります。新rootは絶対・canonical pathとし、symlinkを含めず、既存のMail領域と別にします。**root自身を事前にmkdirしません。空でも既存なら初期化は拒否します。** 失敗して領域が残った場合も削除して再試行せず、状態を保全して確認します。

```sh
"$orrery_python" -m agentstack_mail.fresh_cli init \
  --mode fresh --root "$orrery_fresh_root"
"$orrery_python" -m agentstack_mail.fresh_cli health \
  --config "$orrery_config"
```

`initialized`、`mode: fresh` と、healthの `mail_instance_id` が初期化結果と一致することを確認します。旧instanceの値をconfigへ書き戻しません。`health` の `activation_enabled: false` は、休眠global modeのactivationを有効化していない意味です。このflagを書き換える手順ではありません。`--mode` 省略時は一時領域向けのisolatedです。

別の保守端末で、確認済みの空きportを指定して前面起動します。**別端末を開くたび、冒頭の変数定義を同じ確認済みの値で再設定し、`orrery_python`／`orrery_install`／`orrery_fresh_root`／`orrery_mail_port` と、それから作る `orrery_config` を確認します。** 元shellの変数は、exportしても独立に開いた端末へ自動では引き継がれません。自動でサービス登録するコマンドではありません。

```sh
"$orrery_python" -m agentstack_mail.fresh_cli serve \
  --config "$orrery_config" --port "$orrery_mail_port"
```

接続先は `http://127.0.0.1:<同じport>/mcp` です。8765を指定できるのは明示freshだけで、稼働中のサービスを止めずに使う許可ではありません。8770／8781／8791はMail用に使えません。ここまでの開発検証は一時root・自己動的portだけで、本番8765へのbindや実root作成は未受入です。

## 5. 新規登録と保護clientの準備

人が信頼する保守端末からagentごとに登録します。登録UUIDは最初の送信前に生成して控えます。出力を失った場合も **同じUUID・同じname/program/model** で再実行し、別登録にしません。資格はprivate bindingへ保存され、コマンド結果はIDとbindingのpathを返します。binding／登録intentの内容をmodelのtool引数や引継ぎメモへ貼りません。

```sh
orrery_registration_id='<saved-registration-UUID>'
orrery_agent_name='<chosen-agent-name>'
orrery_program='<actual-program>'
orrery_model='<actual-model>'
"$orrery_python" -m agentstack_mail.fresh_cli register \
  --config "$orrery_config" --request-id "$orrery_registration_id" \
  --name "$orrery_agent_name" --program "$orrery_program" --model "$orrery_model"
```

返された `binding_path` をそのまま使い、別のclient UUIDも初回前に生成して控えます。prepareは新しい本人を登録する操作ではありません。

```sh
orrery_binding='/absolute/path/returned/as/binding_path'
orrery_client_id='<saved-client-UUID>'
orrery_endpoint="http://127.0.0.1:$orrery_mail_port/mcp"
"$orrery_python" -m agentstack_mail.fresh_client prepare \
  --config "$orrery_config" --binding "$orrery_binding" \
  --endpoint "$orrery_endpoint" --client-id "$orrery_client_id"
```

返された `context_path` を使って、同じinstance／agentに現在も接続できることを確認します。

```sh
orrery_context='/absolute/path/returned/as/context_path'
printf '%s\n' '{}' | "$orrery_python" -m agentstack_mail.fresh_client call-json \
  --context "$orrery_context" --tool reconnect
```

### 利用者単位のfresh指示を、既存setupで置く

配置先は返された `context_path` の親にある `providers/claude`／`providers/codex` です。この版の通常core payloadは `templates/fresh`、`scripts/` の既存hook準備CLI、Codex plugin source／schemasを配ります（dashboard-onlyを除く）。配布先に `templates/fresh/claude/CLAUDE.md` と `templates/fresh/codex/AGENTS.md` があることを先に確認します。ファイルがなければ止め、legacy templateで代用しません。payloadの配布はservice切替や既定mode変更ではなく、最終の案内索引・在席切替は別です。以下はClaudeの例で、Codexの **指示だけ** を準備する場合は `orrery_provider` を `codex` にします。別の保守端末では先の確認済み変数と `orrery_context` も再設定します。

```sh
orrery_provider='claude'
orrery_client_dir="${orrery_context%/context.json}"
orrery_provider_home="$orrery_client_dir/providers/$orrery_provider"
orrery_setup="$orrery_install/bin/agentstack-$orrery_provider-setup"
```

contextはprepareの返した正確な `context.json` のpathを使います。新規の場合だけ `providers` とproviderのdirectoryをそれぞれmode0700で作成します。既存なら作り直さず、本人所有の0700 directoryでsymlinkではないことを確認します。指示fileを先にtouchしたり、旧HOMEの資格・設定・bindingを一括コピーしたりしません。

既存setupのread-only planを、固定したprovider HOMEをcwdとして実行します。両provider HOME変数を同じ明示先へ渡すのはこのsetup processだけです。選択したproviderの利用者fileを作り、project単位の指示を選ぶ操作ではありません。

```sh
(
  cd "$orrery_provider_home" || exit
  env AGENTSTACK_PYTHON="$orrery_python" AGENTSTACK_HOME="$orrery_install" \
    AGENTSTACK_TEMPLATE_HOME="$orrery_install/templates/fresh" \
    CLAUDE_CONFIG_DIR="$orrery_provider_home" CODEX_HOME="$orrery_provider_home" \
    CLAUDE_HOME= AGENTSTACK_CLAUDE_MD_SCOPE=global \
    AGENTSTACK_PROJECT_KEY= AGENTSTACK_LEGACY_PROJECT_KEY= \
    "$orrery_setup" --plan --json
)
```

`target_path`／`resolved_home`、選ばれたfresh template、変更対象と `plan_id` を確認します。planはfileやlockを作りません。[指示移行の同意契約](instruction-migration.md#consent)に従い、既存内容を変えるなら **本人の端末へ渡して確認**します。新fileの作成だけなら既存engineの契約どおり追加同意は不要です。agentが `--assume-yes` を足したり、確認待ちを別writerで回避したりしません。

確認したplan IDを使い、**同じcwd・全env指定**でapplyします。planに出た `apply_argv` だけを環境指定なしで別端末へ貼りません。

```sh
orrery_instruction_plan='<verified-plan_id>'
(
  cd "$orrery_provider_home" || exit
  env AGENTSTACK_PYTHON="$orrery_python" AGENTSTACK_HOME="$orrery_install" \
    AGENTSTACK_TEMPLATE_HOME="$orrery_install/templates/fresh" \
    CLAUDE_CONFIG_DIR="$orrery_provider_home" CODEX_HOME="$orrery_provider_home" \
    CLAUDE_HOME= AGENTSTACK_CLAUDE_MD_SCOPE=global \
    AGENTSTACK_PROJECT_KEY= AGENTSTACK_LEGACY_PROJECT_KEY= \
    "$orrery_setup" --apply --plan-id "$orrery_instruction_plan" --json
)
```

`stage: complete` と対象fileを確認します。既存fileの変更は共通engineがprivateな全文backup／receiptを残し、markerの外側を保持します。変更検出・未完了receipt・同意不足で拒否されたら、表示された理由に従って止めます。指示の生成は資格の再登録やCodexのhook承認ではありません。

fresh wrapperに `instruction-configure` を追加したものではなく、旧global用configureも使いません。通常launchは現行fresh templateのmarker内を照合するだけで、不足・版不一致・legacy指示なら `INSTRUCTION_PROVIDER_CONFIG_REQUIRED` で停止します。marker外の個人指示は保持できます。template_home未指定の既存callerは従来のlegacy指示を使います。

**本人の段:** 固定provider HOMEで必要なloginを行います。実provider/loginはまだ未受入です。Codexには次の独立したhook準備も必要です。agentが承認済みplanやreceiptを作って代用せず、実HOME／既存App bindingや旧global trustを借りません。

### Codex hookを、既存CLIと本人の確認で準備する

これは上のCodex指示配置のあとに、同じ固定HOMEだけで進める手順です。実Codexでの受入は未完で、開発試験はprivate copyとfake native RPCです。旧globalの `codex-plan`／`codex-apply`、Desktop integration installer、実App bindingは使いません。別端末では確認済みの変数を再設定します。`orrery_codex` は本人が確認した実行可能なbinaryの絶対pathで、以後のlaunch／resumeにも同じ値を `AGENTSTACK_CODEX_BIN` で渡します。

```sh
orrery_codex='/absolute/path/to/confirmed/codex'
orrery_client_dir="${orrery_context%/context.json}"
orrery_provider_home="$orrery_client_dir/providers/codex"
orrery_marketplace="$orrery_provider_home/fresh-marketplace"
orrery_plugin_id='agentstack-codex-app@orrery-fresh'
(
  umask 077
  test -d "$orrery_provider_home" && test ! -L "$orrery_provider_home" || exit 1
  test ! -e "$orrery_marketplace" && test ! -L "$orrery_marketplace" || exit 1
  "$orrery_python" "$orrery_install/scripts/build-codex-app-marketplace.py" \
    "$orrery_install/integrations/codex_app" "$orrery_marketplace" \
    --marketplace-name orrery-fresh
)
```

既存builderはdestinationがあると削除するため、**未作成の専用先で1回だけ** 実行します。既にあれば削除せず停止し、前回の結果を確認します。provider HOMEは先の本人所有0700・canonical／no-symlink条件を満たす必要があります。

```sh
CODEX_HOME="$orrery_provider_home" "$orrery_codex" plugin marketplace add \
  "$orrery_marketplace" --json && \
CODEX_HOME="$orrery_provider_home" "$orrery_codex" plugin add \
  "$orrery_plugin_id" --json
```

各コマンドのrc0と応答を確認してから次へ進みます。native応答の `pluginId` が上の値と一致し、`installedPath` が同じprovider HOME内の実directoryであることを確認します。path不明・HOME外・symlink・所有者不明なら停止します。marketplace内のsource pathをinstalledPathだと推測しません。両コマンドはplugin配置だけで、hookの信頼承認ではありません。

```sh
orrery_plugin_root='/absolute/installedPath/returned/by/native-command'
orrery_hook_plan="$orrery_provider_home/hook-plan.json"
test ! -e "$orrery_hook_plan" && test ! -L "$orrery_hook_plan" && \
  "$orrery_python" "$orrery_install/scripts/codex_plugin_trust.py" plan \
    --codex-binary "$orrery_codex" --shared-codex-home "$orrery_provider_home" \
    --cwd "$orrery_fresh_root" --plugin-root "$orrery_plugin_root" \
    --plugin-id "$orrery_plugin_id" --output "$orrery_hook_plan"
```

planは `approved: false` のprivate fileを出します。既存planがある場合は上書きせず止め、本人が旧planを保全して新しい確認へ進むか判断します。**ここでagentは止まり、本人へ渡します。** 本人がbinary・HOME・cwd・plugin配置・payloadと全hook内容を確認し、その版を承認する場合だけ、同じfileの `approved` だけを `true` に編集します。agentによる自己承認、旧planの承認の流用、receiptの手作りはしません。fileの0600と配置は維持します。

本人の確認が終わってから、既存CLIでその承認を適用します。

```sh
"$orrery_python" "$orrery_install/scripts/codex_plugin_trust.py" apply \
  --approved-plan "$orrery_hook_plan" \
  --receipt "$orrery_provider_home/hook-trust-result.json" \
  --shared-codex-home "$orrery_provider_home" \
  --codex-binary "$orrery_codex" --plugin-id "$orrery_plugin_id"
```

rc0かつ `hook_trust: observed_current` が必要です。`needs_review`・配置／payload変更・応答不明なら停止し、現在の内容を本人が確認し直します。通常launchは承認を行わず、同じHOMEのplan／payload／native trustを読取照合するだけです。不足は `CODEX_HOOK_CONFIGURE_REQUIRED` 等で拒否され、別HOMEや旧経路へfallbackしません。plugin付属のMail MCPは既存launchが無効化し、同じfresh clientのproxyだけを使います。この準備だけでは実Desktopや実providerの受入を完了しません。

準備完了後の正規foreground起動の形は次のとおりです。先に旧globalの `AGENTSTACK_CLIENT_CONFIG`／`AGENTSTACK_GLOBAL_SERVER_CONFIG` が混ざっていないことを確認します。元の設定は復旧用に保全し、資格を表示して確かめません。

```sh
orrery_workdir='/absolute/path/to/work-folder'
AGENTSTACK_PYTHON="$orrery_python" AGENTSTACK_RUNTIME_MODE=fresh \
  AGENTSTACK_FRESH_CLIENT_CONTEXT="$orrery_context" \
  "$orrery_install/bin/agent-start" "$orrery_workdir"
```

Codexは上の `agent-start` を `agent-start-codex` に置き換えます。起動したmodelはbound proxyの `runtime_status` で本人を確認し、schemaにないproject／資格／本人IDを追加しません。予約pathは実際の作業folderから絶対化されます。fresh contextの欠落・破損・資格失効を旧登録へのfallbackで回避しません。

resumeは **この新clientで記録済みのnative sessionだけ** が対象です。旧Mailのresume、別clientのsession、終了が確認できないrunへ自動でつなぎません。記録が揃っている場合の引数は `agentstack-resume claude "$orrery_workdir" --session-id <recorded-native-UUID>`（Codexは `codex`）で、上と同じfresh環境変数を渡します。終了不明なら起動を止め、本人が実行状況を確認するまでreceiptを手で書き換えません。

### 自動の子: 既存preregister／spawn入口のfresh接続

切替の完了には `/delegate`、New Agent、`/api/spawn` を含む自動子起動が必要です。**Claude/Codexの共通入口、managed dsh／Hermes／Local、App adapterと画面APIの接続を限定fixtureで確認しています。手動で別agentを作れることを、自動子起動の代わりの完了条件にしません。** 実provider・実環境での起動停止・在席受入が揃うまで切替は行いません。

親の明示fresh contextから、子ごとに別の数値ID・資格・contextを準備します。同じ `child-client-name` を準備の再送単位にし、同じ引数で繰り返した場合だけ同じ子へ戻ります。name／program／model／task-descriptionや親が変わると拒否します。新しい仕事で別の子を作る場合は新しいslotを明示し、通信失敗で別名を作り直しません。

以下はClaudeの例です。Codexではprogramを `codex` にし、spawnへ `--codex` を加えます。modelは実際に使う値を指定します。本人が先に準備した同じfamilyのprovider HOMEを使うため、子ごとのhook承認を自動コピーしません。指示・binary・現行trustが不足すれば、spawn前に止まります。native transcriptはそのprovider HOMEへ、Mail本人・task・run/session記録は子ごとの領域へ保存します。自動login／hook承認や実HOME探索はありません。

```sh
orrery_child_slot='worker_01'
orrery_child_name='ChildWorker'
orrery_child_model='<actual-provider-model>'
orrery_child_task='/absolute/path/to/reserved-task.md'
orrery_child_cwd='/absolute/path/to/child-work-folder'
orrery_child_resource='/absolute/path/to/child-work-folder/target.py'
(
  export AGENTSTACK_PYTHON="$orrery_python" AGENTSTACK_RUNTIME_MODE=fresh
  export AGENTSTACK_FRESH_CLIENT_CONTEXT="$orrery_context"
  "$orrery_install/bin/agentstack-preregister-child" \
    --child-client-name "$orrery_child_slot" --name "$orrery_child_name" \
    --program claude-code --model "$orrery_child_model" \
    --task-description 'bounded child task' --prepare-only && \
  /bin/bash "$orrery_install/hooks/spawn_child.sh" \
    --child-client-name "$orrery_child_slot" --pre-registered "$orrery_child_name" \
    --embed-task --task-file "$orrery_child_task" \
    --resources "$orrery_child_resource" "$orrery_child_cwd"
)
```

task fileは事前に予約して作り、fresh Mailのprivate rootの外に置きます。本文は保存してnative providerへargvで渡し、shellのコードとして再評価しません。資格・bindingをtaskへ貼りません。準備コマンドの出力はtokenを含まないJSONで、子の名前とcontextを返します。旧 `--token-file-out` やproject指定はこのfresh入口では受けません。資源不要の場合だけ `--resources` の代わりに `--unsafe-no-resources` を明示します。`--prepare-only` をspawnへ付けると、準備・task固定・予約まででproviderは起動しません。

通常spawnは既存tmux transportに新しい子sessionを自動作成します。親のtmux socketを使い、無指定なら既存の通常socketの規則だけを使います。serverが未準備なら `FRESH_CHILD_TMUX_PREPARE_REQUIRED` で止まり、本人が既存のtmux起動手順を確認します。別paneへの入力注入や新watcher／daemonはありません。初期応答の `accepted` はhandoffの記録で、`native_ready` とは別です。同じtask／slotで再送すると同じattemptの状態を返し、二重起動しません。結果不明時はtaskやreceiptを消して再試行せず、本人が実行状況を確認します。task変更や別の子との混在も拒否します。子は渡されたtaskに従い、同instanceの指定親IDへMailで報告します。

### Codex Desktop App（切替は未完）

既存bridgeのsocket／worker／spoolとDesktop transcriptのheader照合を使い、root task／subagentを共通の子準備で別ID・contextへ結びます。CLI nonceの無いDesktopはCLI session-hookへ渡しません。通知・待機rootのcold wake・App本人から共通外部子起動への接続を以下で準備します。**実App・login・本人trust、実OS manager/Finder GUI・live配布の受入が残り、この手順だけで切替完了とはしません。** 開発試験のDesktop transcript／provider／trustは私有の合成fixtureです。

| agentの段 | 本人の段 | 確認方法 |
|---|---|---|
| 配布済みcore／App componentの版・interpreter・runner・plugin payloadと、旧bridgeの停止／復旧手順を確認 | plugin変更内容とhook承認を確認し、旧bridge停止を判断 | 同じ版のfresh adapterがあり、旧binding／資格をコピーしない |
| 別端末で確認済み変数を再設定し、下の正規runnerを前面起動 | Desktop HOME／loginを確認し、必要な承認を自分で行う | 専用runtime／HOME／template contextが一致。live入替は在席時だけ |
| hookが示すsession／subagentでbootstrapし、本人を確認してMail／予約を使う | 実Desktopで確認 | root／subagentが別ID。未準備／別本人／失効／予約不足・Move片端不足は拒否 |

別端末ごとに先の `orrery_python`／`orrery_install`／`orrery_context` を同じ確認済み値へ戻します。contextは新Mailの準備用client、HOMEは本人が確認したDesktop用の既存HOMEです。CLIの承認や旧App bindingで代用しません。旧bridgeと異なる短い絶対runtime pathを使い、既存設定が不一致なら停止します。

```sh
orrery_app_install='/absolute/path/to/installed/codex_app'
orrery_app_home='/absolute/canonical/Desktop/CODEX_HOME'
orrery_app_runtime='/absolute/short/private/fresh-app-runtime'
test -f "$orrery_app_install/src/agentstack_codex_app/fresh_bridge.py" || exit 1
test -f "$orrery_app_install/run-codex-app-bridge.sh" || exit 1
export AGENTSTACK_PYTHON="$orrery_python" AGENTSTACK_HOME="$orrery_install"
export AGENTSTACK_RUNTIME_MODE=fresh AGENTSTACK_FRESH_CLIENT_CONTEXT="$orrery_context"
export AGENTSTACK_CODEX_APP_INSTALL_DIR="$orrery_app_install"
export AGENTSTACK_CODEX_APP_RUNTIME_DIR="$orrery_app_runtime" CODEX_HOME="$orrery_app_home"
unset AGENTSTACK_CLIENT_CONFIG AGENTSTACK_GLOBAL_SERVER_CONFIG
/bin/bash "$orrery_app_install/run-codex-app-bridge.sh"
```

本人がDesktopにも同じ明示設定を適用します。1秒hookはローカル受付／spoolまでで登録HTTPを待たず、`queued`／`spooled` は準備完了ではありません。未準備を別名登録や旧transportで回避せず報告します。routeはI／A／世代・context参照・native metadataだけで、資格を別保存しません。stdioはbootstrap後に別session／subagentへ切替不能で、現在資格とrouteを毎回確認します。SubagentStopはそのsubagentの利用を止め、root Stopはturn待機として扱います。設定／HOME／transcript不一致ならreceiptを手で直さず停止し、復旧は控えた旧版／設定へ戻す手順を本人が行います。

通知は同bridge workerが現在の本人・recipientで取得し、PostToolUse hookへ短いローカルIPCで渡します。hookのstdout flush後に返るreceiptでnotification completionを行い、read／ACKや仕事の完了にはしません。flush／receipt／応答の到達が曖昧なら未完通知が残り、再通知し得ます。重要度filterは取得のLIMIT前に適用します。

cold wakeは既存のpolicy／resume方式を使い、観測済みStop後のwaiting／dormant rootだけを同じDesktop HOME／native sessionで再開します。SessionStartやworking中・停止subagentを並行resumeしません。Desktop HOMEの**本人が承認した既存 `hook-plan.json`／native trust／plugin payload**とbinary・cwd・plugin IDの一致が必要です。無ければ既存のCodex hook準備手順をその明示HOMEに対して本人の段まで進め、確認します。agentが承認receiptを作って埋めません。別端末でも確認済み `AGENTSTACK_CODEX_BINARY`／`AGENTSTACK_CODEX_APP_PLUGIN_ID` を設定します。`AGENTSTACK_CODEX_APP_COLD_WAKE=0` はwake停止で、通知／外部子の代替完了ではありません。終了code0は通知受渡しだけ、失敗／timeout／起動不明はpendingを保持します。不明なwakeは自動再起動せず、本人へ停止・調査を依頼します。

Appから外部子を起動する場合、**本人が**先に専用tmux socketを準備します。新しい短い絶対private directoryで既存tmuxを起動し、`AGENTSTACK_CODEX_APP_CHILD_SOCKET` にその実socketを設定してからbridge／Desktopを起動します。起動後の設定変更は拒否されます。default socketや別paneを探しません。この手動準備は子の登録・task移送・自動起動を置き換えません。

同じbootstrapで確認したsession／subagentを、通常の `agentstack-preregister-child` と `spawn_child.sh` の両方へ `--app-session-id`／subagentなら `--app-agent-id` として渡します。本人selector／資格は追加しません。以下は既存delegateと同じ自動子入口で、準備済みprovider／同意／trustが無ければ停止します。別端末では先の確認済み変数と明示App環境を再設定します。

```sh
orrery_app_session='session-id-from-bootstrap'
orrery_app_subagent='subagent-id-from-bootstrap'
"$orrery_install/bin/agentstack-preregister-child" --app-session-id "$orrery_app_session" --app-agent-id "$orrery_app_subagent" --child-client-name worker --name AppWorker --program claude-code --model explicit-model --task-description 'bounded task' --prepare-only
"$orrery_install/hooks/spawn_child.sh" --app-session-id "$orrery_app_session" --app-agent-id "$orrery_app_subagent" --child-client-name worker --embed-task --task-file /absolute/path/to/task.md --resources note.py /absolute/work/folder
```

root taskでは両コマンドの `--app-agent-id "$orrery_app_subagent"` を省略します。受け付けたこととnative session成立は別です。登録済みの親context／別Aの子context／指定親へのMailを確認し、起動不明を再送で別起動しません。実App・実providerの受入は在席切替時まで残ります。

### 使う前に確認する制約

- dshの接続定義`backend_file`はprepare時のpin対象外です。prepareから生成・起動完了まで接続定義を編集しません。Localの明示URL/key/model照合やHermesのHTTPS要件は維持します。
- PC historyはClaude/Codexの現在native receipt由来のuser/assistant textだけです。末尾2MiB、最大200件・1件64KiB／合計256KiBの上限があり、tool/thinkingは表示しません。dsh/Hermesは明示unavailableです。
- 本文などの必須入力欠落が`AUTH_PROOF_INCOMPLETE`になる既存の分類があります。送信本文・必須fieldを先に確認し、このcodeだけでtoken再発行やbinding変更をしません。
- 終了済み整理は同I/Aのretireと同paneの削除です。Terminal/Cockpitの表示を閉じ、所有consumer停止後も他clientが残れば拒否します。部分成功/unknownを再送で埋めず、退役済み復帰やApp前面化は対応済みとして案内しません。

## 6. 保管庫から必要分を持ち越す

保管コピーの絶対pathと検索語を明示します。**storage.sqlite3の直接読取は禁止のまま**で、次の製品CLIの出力だけをagentへ渡します。

```sh
orrery_archive_copy='/absolute/path/to/finalized-archive-copy/storage.sqlite3'
"$orrery_python" -m agentstack_mail.archive_cli \
  --archive-copy "$orrery_archive_copy" --limit 20 --offset 0 search '<literal-search-text>'
"$orrery_python" -m agentstack_mail.archive_cli \
  --archive-copy "$orrery_archive_copy" --limit 20 --offset 0 thread --message-id '<old-message-id>'
"$orrery_python" -m agentstack_mail.archive_cli \
  --archive-copy "$orrery_archive_copy" attachments --message-id '<old-message-id>'
```

検索は件名／本文の部分文字列検索で、旧FTSのquery言語ではありません。threadは選んだ旧messageと同じ保存元の範囲です。添付はpath等の参照だけで、内容を自動で開いたりコピーしたりしません。移動後の添付が元pathにあるとは限りません。

1回1〜100件、本文は1通8192文字、出力は約512KiBの予算で制限します。`has_more` と `next_offset` で続きのページを取り、`body_truncated`／`attachments_truncated`／`recipients_truncated` も確認します。ページを進めても本文の切詰め部分が取れる仕組みではなく、全文持越し・全履歴exportの保証はありません。`ARCHIVE_COPY_NOT_FINALIZED` ならsidecarを消さず停止します。

必要な本文や旧threadの引用を、出典が旧保管庫であると分かる **新Mail** として送ります。現在のproxy schemaの `send_message` に件名・本文・宛先を渡し、旧message IDを新側の `reply_message` へ渡しません。同名が曖昧なら候補つきで拒否されるので、activeな宛先の数値IDを確認して指定します。最新IDやfolderから1人を推測しません。添付の自動再送はせず、必要なら参照を本文へ残します。

再送は新ID・新時刻・現在の差出人です。旧未読、read/ACK、資格、binding、配送状態、threadの同一性は移りません。ノートへ残す場合は **書く前に新側でそのノートのpathを予約**し、次の再開段まで無予約で書きません。

## 7. 予約して、別folderから再開する

modelのbound `reserve_files` に実対象の絶対pathを渡します。相対pathを許すlocal callerでは、そのcall時の実cwdが基準です。`project_key` はpathの基準になりません。編集前の予約は900秒以上を指定し、長引く場合はrenew、終わったらreleaseします。model proxyへ資格・instance・actor・request UUID・anchorを指定しません。

少なくとも2つの作業folderで、新本人間のsend／reply、本人のpeek／read／ACK、実際の通知、同じfileの競合拒否と現在のguard coverageを確認します。過去の予約成功receiptだけで編集を許可しません。日本語やWSLの未受入をASCIIの別folderの成功で置き換えません。

APFSの日本語profileは [絶対予約の支持範囲](absolute-reservations.md) を参照してください。ASCIIとUnicode3.2で割当済みの指定CJK記号／ひらがな／カタカナ／漢字／全半角範囲に限り、濁点のNFC/NFDとASCII・全角Latinのcase別表記を同じ対象として比較します。NFKCによる幅の同一視はしません。accented Latin／Greek／新しい漢字／emoji等の範囲外や未知FSは拒否を保持し、globの探査先に範囲外名がある場合もunknownで止まることがあります。実Macの一時folderの受入と実vaultの切替確認を分け、現物の作業先を改名・ASCII folderへ移動・FS変換して回避しません。任意Unicode名／全FSの対応ではありません。

Claudeは既存hookで、tmuxは既存watcherと起動時に固定したpaneで通知します。watcherを手で準備する場合も、別の保守端末で上記の変数を同じ確認済みの値へ再設定します。`AGENTSTACK_FRESH_CLIENT_CONTEXT`／`AGENTSTACK_CLIENT_CONFIG`／`AGENTSTACK_GLOBAL_SERVER_CONFIG` を選択していないことを確認し、実装済み入口 `AGENTSTACK_PYTHON="$orrery_python" "$orrery_install/bin/agentstack-runtime-client" watch-fresh --config "$orrery_config"` を使います。1回だけの確認では末尾へ `--once` を付けます。PATHや既定python3へ任せません。通常のforeground terminalにtmux配送先が自動生成されるわけではなく、通常installerは同configだけを渡すwatcherサービスを描画し、本人contextのkeyを除去します。Appやdashboardとの接続確認も限定fixtureであり、実App/providerの受入とは分けます。

通知completionはstdout／paneへの受渡し処理の記録で、read／ACKやagentの理解・仕事完了ではありません。外部入力とDB更新を一度だけにする保証はなく、不明な結果では再通知の可能性があります。Codexの複数file patchは後のfileで拒否されても先行fileのleaseだけ延長される場合があります。編集は許可されませんが、patch全体で時刻が不変とは保証しません。ClaudeのMultiEdit／NotebookEditは現在拒否されます。

## 8. 失敗したら手順で戻す

切替前に記録した管理方法で、新provider・proxy・Mail・watcherと再起動元を止めます。**新rootも削除せず一式を保全**し、本人が戻すことを選んだ場合だけ、保存した旧実行版／設定／旧一式を用いた復旧手順を進めます。読取専用の保管コピーをそのまま書込み先にせず、元の保管を保持した復旧先を使います。正確な戻し先や起動コマンドが無ければ止めます。

停止時は仕事を控え、provider/agentと通知writerを止め、Cockpitの所有consumerを正常終了してからdashboard、最後にMailを止めます。supervisorやlaunchd/systemdなどの再起動元も控えた手順で停止します。通常SIGTERMはdashboardのprivate listenerと所有Runtimeを閉じますが、子だけを止めてもsupervisorは再起動し得ます。DBの交換・復旧は全writer停止後です。再開はMail→dashboard→Cockpitの順にし、新epochを再接続で確認します。停止timeoutやunsafe socketは手でunlinkして回避せず報告します。

新側へ書く前なら旧運用への接続を戻せます。書いたあとなら、新Mail・返信待ち・編集中の仕事を控えてから戻します。新側のMailを旧側へ自動で逆移行する仕組みはなく、無損失rollbackではありません。旧運用を再開するときも期限切れleaseに頼らず予約を取り直し、二重起動していないことを確かめます。

通信後に応答だけ失った書込みは、保存した同requestを再送できます。local callerの `fresh_client replay --context <same-context> --request-id <saved-UUID>` は元の本文・宛先・絶対pathを使い、現在の資格を再確認します。新UUIDでの再送や手でrequestを書き換える操作は、元の操作の再開ではありません。mode pinの無い旧実験contextは推測修復せず、同じowner bindingから新client UUIDでprepareし直します。この操作で旧native sessionのresumeが引き継がれるとは扱いません。

復帰・App前面化は今回の対象外です。実OS manager/Finder GUI、実provider／App／login／trust／live install、実際の作業pathの支持範囲、最終CIと本人在席の切替を確認するまで、全体完了とはしません。WSLなど未受入の環境を、この私有Macでの確認から受入済みとは扱いません。
