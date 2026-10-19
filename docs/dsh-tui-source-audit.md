# dsh-TUI 固定版の source 監査

通常の起動・設定・補助commandでも秘密の継承や予定外HTTPが起こり得るため、
鍵漏れ・利用者fileへの書込・誤った成功報告に関係する入口を管理対象にする。

2026-10-19の静的監査。公開tarballのbytesと展開sourceを読んだ結果であり、
TUIの実起動、通信実測、sandbox・hook・native権限の受入ではない。
監査中に取得・API・鍵・登録・service起動は行っていない。

## 固定した対象と確認結果

| 対象 | 固定版・出典 | 今回の確認 |
| --- | --- | --- |
| 公式dsh | `@deepseek-ai/dsh@0.2.1-rc.2`、[tarball](https://registry.npmjs.org/@deepseek-ai/dsh/-/dsh-0.2.1-rc.2.tgz) | 保持済み25,130 bytesのSHA-512が下記integrityと一致 |
| 独立したTUI | `@deepseek-harness-tui/dsh-tui@0.16.1`、[tarball](https://registry.npmjs.org/@deepseek-harness-tui/dsh-tui/-/dsh-tui-0.16.1.tgz) | 保持済み18,473,587 bytesのSHA-512が下記integrityと一致 |
| TUI source | [repository](https://github.com/ccch1mneyyy/dsh-TUI)、package metadataのgitHead `73ff99fbed56563bf154d719effd09f9f9725349` | source位置は展開済みpackageのJSを基準にした |
| Node | 製品の最低版24.2、絶対実体を固定する | TUIの`package.json:122–123`は`^22.19 || >=24`、dsh metadataにはenginesなし。版宣言だけでは組合せの動作を証明しない |

dshのintegrity:

```text
sha512-tvY+yhfKRifthbqPe/OK0AYxM5ZkvqfYq/jMYgJcEUxiRRL1Vwra3Ti5TB6AXgSWQdiPHYh/Vp6SJa9yHwMNuw==
```

TUIのintegrity:

```text
sha512-xk8Nviy55RvLnAdjYj04DekUu6D1Pc0kEa5R+2EXoL3H/h6ZEzv8L0bxtSTrWxtNdXd4ufOXMu4TQ7HjrpYhbg==
```

TUIのlicenseはMIT（`package.json:18`）。公式dshと同じ保守主体とは扱わない。
tarballの一致はその二つのpackageだけを確認したもので、依存lock・管理patch・
実際のNode/Python/proxyまで固定した証拠ではない。

以下、`T:` はTUI package root、`D:` はdsh package root。
元TSは[公式dshの固定tag](https://github.com/deepseek-ai/deepseek-harness/tree/dsh-v0.2.1-rc.2)
を参照する。行番号は固定版に対する位置である。

## 接続の形と鍵の行き先

`T:bin/dsh-tui.js:1658–1670` のhost entryから、
`lib/types/dsh-adapter/host-entry.js:25–32,115–150,219` がdsh rootを読み込み、
TUIと同じprocessへcomposeする経路がある。別経路の同file`:39–50`は
`spawn('dsh', ..., env:process.env)`。単にACPで隔離した画面ではない。

管理する単一のdsh runtimeには同居TUIも含まれ、そのPIDだけがAPI keyを持つ。
supervisorは引渡し時に値を扱うが、env・argv・file・ログへ残さない。
tmux server/session、Mail proxy、launcher、補助processにはkeyを渡さない。

| 補助起動 | sourceの実際 | 段Aの扱い |
| --- | --- | --- |
| clipboard | `T:lib/types/utils/clipboard.js:244–246` のspawnはenv省略 | 機能を止める。後に許可する場合は最小envを再生成 |
| external editor | `T:lib/types/utils/externalEditor.js:155,170–180` のspawnもenv省略 | 段Aでは無効 |
| updater/restart | `T:lib/types/update.js:955,2005` に起動経路 | 自動更新・restart・backend切替を無効 |
| link opener / file reveal | `T:lib/types/screens/Chat.js:1745–1753`、`utils/openExternal.js:40–53,112–135` | 通常のURL操作からenv省略のdetached open/xdg-openを起動。段Aでは無効。既存browser本体への継承までは断定しない |
| 共通exec / copy | `T:lib/types/utils/execFileNoThrow.js:18–28`、`ink/termio/osc.js:95–105` | env省略spawnがtmux buffer/clipboard copyにも使われる。read側だけでなくcopy側も停止対象。将来は同じ最小env境界へ |
| profile bootstrap | `T:bin/dsh-tui.js:3–25,94` にprofile委譲・plugin追加・latest fallback | 固定entry/profileだけを使い、自動取得へ進まない |

## 読み書き・直接action・追加socket

| 対象 | 確認したsource | 実際にある経路 / 管理条件 |
| --- | --- | --- |
| TUI root | `T:lib/types/utils/paths.js:15–19` | `homedir()`優先の`~/.dsh-tui`。明示data-rootの管理patchが必要。HOMEを別用途へ置き換えない |
| 履歴・設定 | `history.js:6–9,40,163`、`tuiSettingsFile.js:16,53,108,150`（いずれも`T:lib/types/`） | history.jsonl、settings.json、profile patchやlegacy settings。専用rootへ閉じて共有設定importを止める |
| 初回import | `T:lib/types/dsh-adapter/tui-settings.js:33–70` | profile patch/legacy settingsを取り込む。専用DATA_DIRだけでは閉じない |
| session | `T:cordis.yml:97,236` | resume/session rootのenv参照。段Aでは旧sessionを自動選択しない |
| HOME資格fallback | `T:lib/types/utils/credentials.js:38–46`、`bin/dsh-tui.js:686–687` | 専用DSH_HOMEのほか実HOMEの`.dsh/.credentials.yaml`を読む。既存doctorを管理runtimeで使わない |
| 会話migration | `T:lib/types/dsh-adapter/migrate/adapters/claude-code.js:60,73,82`、`codex.js:28,52,61` | 実HOMEの既存会話を直接読む。migrateを無効化 |
| `/init` / `/export` | `T:lib/types/screens/Chat.js:3447–3467` → `dsh-adapter/channel/reports.js:100–101,108–117` | cwdへ直接writeFileSync。model toolのguardを通らないためcommandとbackend action双方で止める |
| compile-cache | `T:bin/dsh-tui.js:816–830` | DATA_DIRを通らず実HOMEへ書く。global CLIを迂回し、必要なら明示専用cacheを指定 |
| debug | `T:lib/types/utils/debug.js:10–32` | stderr、専用mouse log。管理root以外へ出さず、値を記録しない検査が必要 |
| npm/cache | `T:lib/types/update.js:265,694,1005` | HOMEの`.npmrc`・standalone cache・DSH_HOME fallback。更新経路を止める |
| IDE / context添付 | `T:lib/types/dsh-adapter/channel/core/compose.js:177–187`、`ide-channel.js:335–352,447`、`channel/input-delivery.js:133–149` | env/lock由来のloopback WebSocketと@mention/IDE/context展開。inject socketとは別入口。段Aでは接続・添付展開とも無効 |
| inject channel | `T:lib/types/dsh-adapter/inject-channel.js:30–44,155–225`、`plugin.js:2425–2444` | session socketとservers.json、append/submit。管理modeで機能自体を無効化 |

inject channelの対象sourceに認証tokenによる検査は見つからず、bind失敗も
sessionを失敗させない（`plugin.js:2420–2424`）。この入口をMail配送に転用しない。
正規watcherから製品handoffを経由する接続は別の受入対象である。

## 通信の入口

| 通信 | source | 管理mode |
| --- | --- | --- |
| provider API | 公式`packages/llm/llm-deepseek/src/config.ts:114–118` | defaultは`https://api.deepseek.com/anthropic`。利用者が選んだ固定route以外へfallbackしない |
| retry | 公式`packages/llm/llm/src/retry-policy.ts:40–41,156–181` | 省略時は既定5回。明示maxRetries=0、要求数は別に送信前制限 |
| recap / btw / auto recap | `T:lib/types/screens/Chat.js:3914–3956,1245–1283`、`dsh-adapter/channel/session-metadata.js:172–215`、`dsh-adapter/sideQuestion.js:119–127` | 通常command/自動recapが直接llm.streamを呼ぶ。command・backend・auto設定を停止し、provider admissionを補助streamにも適用。主turnのpre-stepだけに依存しない |
| update check | `T:lib/types/update.js:25–27,303,391,431,455`、`dsh-adapter/plugin.js:2461–2464` | npm registryとGitHub API/releases。起動時checkも含め無効化 |
| onboarding残高 | `T:lib/types/screens/Onboarding.js:58–74` → `dsh-adapter/channel/reports.js:19–31` → `deepseekBalance.js:1,19–21` | `https://api.deepseek.com/user/balance`へ鍵付きfetch。推論課金とは断定しないが予定外HTTPなので無効化 |
| OAuth callback | `T:lib/types/dsh-adapter/oauth/deepseek.js:25` | loopback callbackを含む。既存OAuth画面を管理runtimeへ出さない |

これはsourceから確認した到達経路であり、通信先を実際にすべて観測した一覧ではない。
request数・max output・timeoutと、事後usageのtoken値も別の制御として扱う。

## hookと入力guardの限界

公式raw `packages/core/tools/src/index.ts:144–154`にtools/pre-execute（宣言154）、
`:155–165`にexecute（165）、`:166–177`にpost-execute（177）、`:192–199`にresult（199）がある。
model toolとTUIの直接actionは別経路であり、Mail proxyだけでFSを守れない。

`T:lib/types/dsh-adapter/decision-guard.js:169–211` はgrant不足で登録がno-opに
なり、`extension-events.js:6–10,61–73` は例外/timeoutをno opinionとして続行する。
`channel/input-delivery.js:263–269,306–321` は拒否がなければ入力を配送する。
負荷やplugin再読込でも起こり得るため、provider admissionとFS/action側のgateを
独立させる。DecisionEventsだけを安全上の最終判断には使わない。

細工を要する同時symlink差替え等のために独立した仕組みを増やさない。
通常のpath/scope制限と、世代専用worktree・失効した成果の採用拒否を境界とする。

## ロゴと未導入時の扱い

[専用asset](../integrations/dsh/assets/deepseek.svg)は、cockpitの
[provider badge PR #59](https://github.com/gyroid-eth/orrery/pull/59)と同じbone色の
アイコンを使う。公式配布元は[DeepSeek公式integrationのlogo directory](https://github.com/deepseek-ai/awesome-deepseek-integration/tree/main/docs/_logo%20svg)、
[元SVG](https://raw.githubusercontent.com/deepseek-ai/awesome-deepseek-integration/main/docs/_logo%20svg/ICON.svg)。
青`#4d6bfe`をbone`#ece2cc`へ置換したもので、geometryは同じ。
viewBoxは`0 0 71.69 52.76`。以前候補にした横長wordmarkとは別のassetである。

今回のコピーはcockpitのassetとbytes一致、SHA-256は
`99fc0ef8d8247f1947d2ca1179d529a997141a338dcd203dfcce31184916e102`。
通常installerのコピー対象外に置き、配信一覧・catalog・UIへは未登録。
opt-in無効の環境へロゴの表示枠を追加しない。

## 保守停止時の退路と未確認

固定版と小さなmanaged patchで始め、更新は依存/入口を再確認してから明示実施する。
保守停止・互換破壊で維持できなくなれば、公式dsh adapterと同じtask/turn/Mail/
完了receipt契約を使う薄いORRERY UIへ交換する。現時点で代替UIを同時に作らない。

この監査で確認したのはtarball integrity、source上の入口、コピーしたSVGのbytes。
managed patch、実効tool/action集合、補助processのenv、通信拒否、TUI/cockpitの入力、
Mail接続、runtime停止は未受入。古いNodeで空のexit0になり得るentry
（`D:lib/bin.js:252`のimport.meta.main）も、固定Nodeの実機能検査と複合完了証拠で
扱う必要がある。設計の記述を実測成功に読み替えない。
