# デザイン言語

> English version: [design.en.md](design.en.md)

[前: Dashboard](dashboard.md) · [README に戻る](../README.md)

この文書は、ORRERY Telemetry の dashboard がどう見え、どう動くかを決めている判断を言葉にしたものです。maintainer がこれまでの実装で積み上げてきたデザイン言語の正本であり、新しく決めるための提案書ではありません。

dashboard に UI を足す人、直す人、AI に UI を作らせる人は、実装の前にこの文書を読んでください。ここにある語彙で組めるものはここにある語彙で組み、合わない場合はコードを書く前に Issue で相談してください。AI に作らせるときは、このファイルをそのまま読ませてから指示するのが最短です。

cockpit（`orrery` repo）の [DESIGN.md](https://github.com/gyroid-eth/orrery/blob/master/docs/DESIGN.md) が親です。telemetry は cockpit の中に埋め込まれる計器盤であり、単体で開いても同じ言語で見えなければなりません。

書き方について 1 つ。各節には「原則」と「実装の現状」の両方を書いています。原則は新しい UI に守ってほしいこと、現状は既存コードにある例外です。現状に例外があるからといって原則を緩めないでください。逆に、例外を黙って増やさないでください。

## 1. Concept: 計器盤

ORRERY は scientist の名前を持つ agent の一団を、暗い計器盤の上で見張り、操作するための道具です。telemetry はその計器盤の観測面で、いま誰が動き、誰が誰を起動し、誰が誰に何を送ったかを、一枚で読めるようにします。

長時間の監視に耐えることが第一です。画面は静かで、光るのは意味のある場所だけ、動くのは活動と注意がある場所だけです。cyberpunk の neon や、画面全体の装飾は使いません。精密さ（telemetry）と人間味（portrait）を同居させるのが identity で、片方に寄せません。

## 2. index.html には 2 つの層がある

`dashboard/index.html` の style は 2 層です。上の層が古く、下の層が現在の言語です。

| 層 | 場所 | 中身 | 扱い |
| --- | --- | --- | --- |
| CRT phosphor 層 | `<style>` の前半（`--void` `--bone` `--cyan` `Chakra Petch`、`clip-path` の切り欠き、走査線、glow） | 初期の「蛍光管の計器」。構造と寸法の定義もここにある。NEW AGENT modal の現行の材質と control（`Cockpit spawn v2 parity` の節）も、歴史的にここに追記されている | 残置。新しい UI からは装飾を参照しない。構造（grid、bay の組み立て、modal の骨格）はここが担っているので、寸法を調べるときはここも読む |
| ORRERY surface 層 | `/* ORRERY telemetry surface */` 以降の `:root` と override | cockpit と同じ token と書体、面の材質 | これが現在の言語。新しい UI はここの語彙だけで書く |

2 層は歴史の概略で、場所だけで新旧を判別できるわけではありません。有効な値は cascade 全体で決まるので、寸法や状態を確かめるときは両方を読みます。

ORRERY surface 層は `--void` `--bone` `--cyan` `--line` などの古い名前を新しい token の別名に付け替えて（`--cyan: var(--amber)` のように）、古い CSS を上書きしています。だから古い名前を書いても新しい token の値になりますが、意味が紛らわしく、将来の撤去を妨げるので、新しい CSS では使いません。別名化は literal な色までは置き換えません。古い層に literal で書かれた色（chip の cyan 枠、ASK の赤、portrait の走査線）は今も画面に残っていて、theme の差し替えも受けません。それは意図した例外か、まだ掃除していない残りのどちらかで、新しい CSS の手本ではありません。新しい CSS で literal な色（`#ffb02e` `#36e8ff` `rgba(242,182,90,…)`）を書けば theme が効かず、`Chakra Petch` を書けば §3 の書体から外れます。

## 3. 書体

| 役割 | 書体 | 例 |
| --- | --- | --- |
| 固有名と見出し | `--serif`（Fraunces） | wordmark、agent 名、統計の数字、modal のタイトル |
| それ以外すべて | `--mono`（IBM Plex Mono） | ラベル、chip、path、時刻、本文、network の件数 |

serif は「名前を持つもの」に使います。agent の名前は人の名前なので serif、数値は計器の針なので大きく serif、ラベルは機械の刻印なので小さく mono の大文字です。この 2 書体以外を足しません。font CDN が届かないときは fallback（Georgia / ui-monospace）で崩れないことを確認します。

現状: 古い層の一部（portrait 欠測時の頭文字、drawer の見出し）に `Chakra Petch` の指定が残っていますが、その font は読み込んでいないので sans-serif に落ちています。新しい UI では書きません。

寸法（dark の実装値）:

- wordmark 20px / 600 / 字間 .12em。副題 `TMUX · ORRERY MAIL` は 9px / .2em
- ラベル・chip: 9〜9.5px、字間 .12〜.22em、大文字
- 本文: 現在の行 12px、ORD / RX 11.5px（行高 1.5）、入力 13px
- agent 名 17px / 500、統計の数字 18px / 500、NEW AGENT のタイトル 24px / 500、detail の名前 19px
- 原則: 9px 未満の文字を新しく作らない。現状: modal の節ラベル 8.5px、identity の状態 6.5px、network の役割 6.8px、EXIT / KILL 8px が既にある

## 4. 色

全体は near-monochrome です。色相は意味を持つので、用途を限定します。

| token | dark の値 | 意味 |
| --- | --- | --- |
| `--bg` | `#0b0d11` | 地 |
| `--panel` / `--panel-2` / `--elev` | `#111419` / `#161a21` / `#1c212a` | 面。上に載るほど明るい |
| `--ink` / `--ink-dim` / `--ink-faint` | `#ece5d6` / `#9a9384` / `#5f5c52` | 文字。主 / 従 / 刻印 |
| `--hair` / `--hair-2` | ink の 8.5% / 4.5% | 罫線。面の境界はこれだけ |
| `--amber` / `--amber-glow` | `#f2b65a` / 22% | 注意と選択。唯一の暖色 accent |
| `--alert` | `#d87863` | 要対応（ASK、残量僅少） |
| `--ln-local` / `--ln-remote` / `--ln-delegate` | `#5fb3a3` / `#b58be0` / `#83b06a` | spawn の系統。network で parent / child / both を表す |

規則:

- 色相は spawn の系統（誰が誰を起動したか）に予約します。新しい機能に新しい色を足しません。現状の例外は body の背景に敷いた local 色 4.5% の淡い grade だけです
- amber は「いま見てほしい所」にだけ使います。選択中の tab、focus、WORK 中のカード、注意の帯。画面の 1 割を超えたら使いすぎです
- working / waiting / idle を色だけで区別しません。動き、形、文字を併用します
- 例外は残量（context remaining）の計器です。DECK の細線は残量が減るにつれ ink → amber に変わり、NETWORK の弧は amber、20% 未満で alert になります。系統色と同じ輪には載せません
- portrait の色に UI の状態を依存させません。portrait は grayscale で、状態は portrait の外側（ring、LED、chip）に出します
- 現状: NETWORK の介入グリフだけは `!` を赤、`?` を cyan の literal で描いています。2 つの意味を見分けるための意図した例外で、他に広げません

## 5. 面と階層

画面の情報階層は上から順に決まっています。新しい要素は、必ずどの階層に入るかを宣言してから作ります。

1. header（wordmark、DECK / NETWORK 切替、RUNNING / STANDBY / AGENTS の統計、MAIL の健全性、検索、履歴範囲、NEW AGENT）
2. sector 見出し（`ACTIVE AGENTS [ 27 ]` のような mono 大文字の行と、右へ消える罫線）
3. bay（agent card）
4. 補助の帯（履歴の sparkline、残量など。agent card より弱く、折りたためる）
5. overlay（NEW AGENT modal、agent detail、edge thread drawer、Tune）

### header と grid

- header は sticky、最小 58px、padding 9px 22px、要素間 22px。統計は「数字 → ラベル → 2px の下線」の縦積みで、間隔 16px。DECK / NETWORK の切替と MAIL は pill（角丸 999px、hairline。地は切替が `--panel` 74%、MAIL が 62%）。検索は幅 226px。NEW AGENT は amber 22% の地に amber 32% の枠、角丸 7px、amber の文字
- main は padding 22px 22px 0、`repeat(auto-fill, minmax(440px, 1fr))`、間隔 10px。760px 以下は 1 列で左右 13px、統計を隠す
- sector 見出しは margin 16px 2px 4px、9px / .22em、`--ink-dim`、左に 7px の amber 菱形、右へ `--hair` の罫線が消えていく
- cockpit に埋め込まれたとき（`body.embed`）は header が 44px になり、wordmark、統計、MAIL を隠す。cockpit 側が持つ情報を二重に出さない

### bay（agent card）の解剖

```text
┌ portrait 56px ┐  ✳ AgentName            LAST 1M41S
│ grayscale     │  ↯ asked for Agent-Name
└───────────────┘  [ GPT 6 ] [ ● LINKED ] [ ● ATTACHED ]
──────────────────────────────────────── ctxline（残量の細線）
│ 現在の行（左に amber の縦罫）
ORD  親からの依頼の要約
RX   直近に受け取った mail の 1 行
──────────────────────────────────────── foot
● ONLINE  LAST 1D 前  ZSH                      [ ↵ EXIT ]
```

- 面は `--panel` に上端だけ白 2.5% の grade、罫線は `--hair`、角丸 9px、影は `0 10px 32px rgba(0,0,0,.12)`。padding 14px 17px 13px、行間 9px。古い層の切り欠き（clip-path）と角の amber は出しません
- 上段は横並びで間隔 13px。portrait は 56px、9px の切り欠き、grayscale に contrast 1.06、無いときは名前の頭 2 文字。右の identity 列は 名前 → 要求名の注記（登録名が違うときだけ）→ chip の順で間隔 8px。provider の logo は名前の左 13px、止まっているときは opacity .55
- chip は 9.5px、字間 1.5px、padding 2px 7px、間隔 6px で折り返す。原則は中立色で、分類（category）が amber、重要度（importance）が alert。現状: `LINKED` `ATTACHED` と context window の chip は amber の文字に cyan 由来の枠と glow、delivery の chip も amber で、古い層の名残です。新しい chip はこれを真似ません
- 「現在の行」は左に 2px の amber 縦罫を持ちます。padding 7px 11px、1 行で省略。ここが agent の「いま」です。live の情報が無いときは `— STANDBY · no live activity —` を italic の hairline で出します。amber の縦罫はこの行の印で、他の要素に使いません（現状: sparkline の tooltip と detail の tool 吹き出しにも同じ縦罫があります）
- key-value（ORD / RX）のキーは amber の 9.5px 大文字、幅 34px 固定、値との間隔 10px。値は dark で `#c8c2b1`、light では `--ink-dim`。データがあるときだけ行を出します
- ctxline は稼働中で残量が取れているときだけ、高さ 2px、幅が残量%。50% 以上は ink-dim → ink、20〜50% は ink-dim → amber、20% 未満は amber
- foot は上に `--hair-2`、padding-top 9px、10px / 字間 1.5px。左に `● ONLINE`（止まっていれば `○ SHELL`）と経過時間と shell 名。右に高さ 17px の EXIT（稼働中）か KILL（finished / gone で attach されていないとき）。どちらも 2 段階（arm → confirm、5 秒で解除）
- 状態による沈め方: idle / infra / warmup は card 全体を opacity .5、hover で .85。finished / gone / retired は card は沈めず、provider の logo を .4 / .4 / .28 にします。消しません
- hover で 2px 浮き、`--panel-2` に amber 28% の内枠。クリックで detail を開きます。DECK に「選択」の状態はありません（選択 ring は NETWORK だけ）

### 補助の帯

agent card の上や間に入る帯（履歴、残量、通知）は、agent card より弱い階層です。ここは原則だけで、残量の帯はまだ実装されていません（#31 で提案中）。寸法を実装値として扱わないでください。

- 高さは agent card の 1 段目を押し下げない範囲に収め、折りたためるようにします。折りたたんだ状態でも要点の数字は header の脇に残します
- 古い値を最新値のように見せません。取得時刻を必ず併記し、取れていない provider は `WAITING FOR UPDATE` のように、いつの値かを文字で言います
- provider が返した window だけを出します。5h / 7d のような枠を ORRERY 側で仮定して空欄を補完しません

## 6. 状態と動き

| 状態 / 出来事 | DECK（bay） | NETWORK（node） |
| --- | --- | --- |
| running | provider logo が amber に灯り 2.8s で呼吸（opacity .7 ↔ .57）。WORK 中は card に 135° の amber tint（5.5% → 1.8%）。所要時間か残量が取れているときだけ右上に 9px 角の LED（1s の点滅）と所要時間、時間が無ければ `WORKING` | portrait、縁、dot が 1.2 倍。WORK 中は残量の弧の外を motion ring（dash 30 / 70）が 1.5s で周回 |
| waiting（入力待ち） | 前の turn の所要時間が取れているときだけ右上に `LAST` と時間。`--ink-dim`、opacity .55 で静止。取れなければ何も出さない | 走査なし。弧は残量の値だけ。1.2 倍のまま |
| ask（要対応） | 右上 `APPROVAL` が赤で 1s、LED は .6s。枠が赤、影が 1.6s で脈動 | 赤い実線の環に italic の `!`。文字が 1.25s で bounce |
| question | 右上 `?` が amber で .9s の点滅 | cyan の実線の環に italic の `?`。文字が 1.6s で bounce |
| finished / gone / retired | logo を .4 / .4 / .28（§5） | 全体 .4 / .42 / .28。portrait を .78 / .72 倍に縮め、陰を .54 / .54 / .66 |
| mail が届いた | RX 行が更新 | 辺の上を comet が走り、受信 node に弧が広がる |
| 選択 | なし（クリックで detail） | ring が amber |

- 動きは活動と注意を示すために使います。running や ask の間は定常でも動き続けますが、飾りとしての motion は入れません
- 周期は要素ごとに決まっていて、1s 未満のものは点滅の LED（.6s〜1s）、question（.9s）、確認の arm（.8s）、送信中の SPAWN の矢印（.7s）です。現在の行の caret は 1.05s。新しい要素に 1s 未満の周期を足さない
- `prefers-reduced-motion` は CSS 6 か所と JS 2 か所で見ています。現状で止まらないものが 2 種類あります。DECK の logo の呼吸と question の表示は reduce 側の指定そのものが無く、状態 LED と `APPROVAL` は reduce 側の指定より詳細度の高い規則が勝っています。新しい動きは必ず reduce で止め、止めても文字と形で状態が読めるようにします

## 7. NETWORK view

- node は portrait の medallion。基準半径は 13（SVG 単位、Tune の NSIZE と zoom で拡大）。縁は系統色で 1.55、残量の弧は半径 +5、線 1.55、最大 270°、motion ring は半径 +9、線 1.6。残量が取れなければ弧を描かず、portrait が無ければ dot
- label は serif（名前 10.5px、running は 11px）と mono（役割 6.8px、件数 9px）。要求名と登録名が違う node は mono 10px に amber の点線下線。301 node 以上では名前・役割・件数・badge・弧を隠す（dense）。多数の node で label が重なるときは、常時表示を増やさず hover / focus で近傍を強調して開示します
- 辺は `--ink-dim` の細線で opacity .22。spawn の辺は `--ln-delegate` の実線で 1.25 / .4。mail の件数は辺の中点に mono で載せ、文字の下に地の色を焼き込んで可読性を保ちます
- 背景は 48px の格子と中央の淡い楕円。格子は `--hair-2` で、地より目立ちません
- Tune、legend、sel-toggle などの浮遊パネルは同じガラス材（`--panel` 90%、hairline、角丸 9px、影 0 16px 42px 黒 18%）。legend の popover だけは大きいので角丸 10px、白 9% の枠、blur 26px、影 0 30px 90px 黒 52% と、少し重い材質です

## 8. overlay（NEW AGENT modal ほか）

NEW AGENT、agent detail、edge thread drawer は同じガラス材を共有します。共有するのは材質で、配置と寸法は overlay ごとに違います。以下の配置、寸法、control は NEW AGENT のものです。

- 材質は中立です。白 9% の 1px 枠、角丸 13px、地は白 4.5% から 140px で透明になる grade を `rgba(17,19,25,.66)` に重ね、blur 26px と saturate 1.12、影 `0 30px 90px rgba(0,0,0,.52)`。古い層の amber 枠は上書きで消え、四隅の角は `display:none` にしてあり、出しません
- backdrop は `rgba(8,9,13,.62)` に blur 10px（NEW AGENT と detail だけ）。NEW AGENT の frame は中央、幅 `min(790px, 100vw - 48px)`、最大高 92vh で内側を scroll。detail は幅 `min(1180px, 100vw - 48px)`、高さ `min(88vh, 900px)`。drawer は右端に固定（上 68px、下 14px、幅 `min(390px, 100vw - 18px)`）で、角丸は左側だけ、右の枠は無し
- header は padding 17px 19px。kicker `NEW AGENT` 8px、タイトル `LAUNCH AN AGENT` は serif 24px / 500、副題 `IDENTITY · ENGINE · DIRECTORY · TASK` は mono 8.5px。右上の × は 30px 角
- body は 2 列 grid（間隔 12px 16px、padding 18px 19px）で主要 4 節は全幅。identity / engine / directory は `--hair` の枠と角丸 10px、黒系 30% の地、padding 12px。task は枠なし。節ラベルは 8.5px / .14em で、左に 11×1px の amber の短い線（`▸` ではありません）
- identity は任意。AUTO NAME の preview（44px の丸 portrait）と横 scroll の scientist 列（幅 68px、42px の丸 portrait、角丸 8px）。選択は amber 22% の地と 55% の枠、使えないものは opacity .3
- engine は provider の tab（最小幅 108px）と model の card（最小幅 155px、間隔 7px）、必要な provider には effort の chip。選択肢は catalog から来るので固定で列挙しません
- directory は preset の pill と typeahead の入力を横並び。task は必須の textarea（最小高 96px、行高 1.55）。入力は 13px、padding 8px 11px、角丸 7px、地は `rgba(11,13,17,.52)`。focus で amber の枠と薄い glow
- 4 節の下に折りたたみの ADVANCED。foot は上に罫線、padding 13px 19px。CANCEL は hairline の枠だけ、SPAWN は透明に amber 40% の枠で、hover / focus で amber に塗る。主操作を常時塗らないのは、塗りを「押せる」合図に取っておくためです
- 背景クリック、`Esc`、× で閉じます（送信中は閉じない）。現状: 閉じたあとの focus 復帰は未実装です。破壊的操作（KILL / EXIT）は card 上では 2 段階（arm → confirm、5 秒で解除）です。現状: detail にも EXIT があり、こちらは 1 段階で送信します。原則は 2 段階で、detail 側は追いついていません

## 9. light theme

telemetry には dark と light の 2 つの theme があります。light は warm-paper のクリーム地で、文字、面、罫線、accent がすべて dark とは別の値になります。maintainer が日常に使うのは light です。

light の palette と適用処理は telemetry に同梱されています。`dashboard/theme_core.js` が OKLCH の seed から palette を導出し、`dashboard/theme_controller.js` が `html[data-color-theme="light"]` と token の値を書き込み、`dashboard/theme_light.css` が light 専用の補正を当てます。既定は dark で、いまの切替経路は 2 つです:

- cockpit（`orrery`）に埋め込まれたとき: host が same-origin の `postMessage` で theme を通知し、controller が適用します
- 単体で開いたとき: 利用者向けの切替 UI はまだありません。開発者 console から次で切り替えられます

```js
window.AgentStackColorTheme.apply({preference: 'light', resolved: 'light'})
window.AgentStackColorTheme.apply({preference: 'dark', resolved: 'dark'})
```

予定: 単体でも header から dark と light を切り替えられるようにします（設定の保存と、埋め込み時は cockpit 側が勝つこと、を含む）。あわせて、古い層に literal で残っている色を token に置き換えます。cockpit はいま、埋め込み時にその literal を実行時に書き換えて凌いでいます。

theme が成り立つ仕組みは 1 つだけです。色は名前（§4 の token）で書かれていて、theme はその名前の中身を差し替えます。だから §4 と §10 の「色は token で書く。literal な値を書かない」が theme の規則そのもので、守れているかは `grep` で確かめられます。2026-08-10 に light で 14 か所の文字が読めなくなったのは、古い層の literal 色が差し替わらなかったためです。

theme の差で影響が出る 2 点は、どちらも §4 と §6 の規則の範囲内です:

- 走査線、粒子、vignette、glow のような暗い地の上でしか意味を持たない演出は、light 側で弱めるか消してあります（走査線 .035、粒子 .018、vignette なし）。新しい演出を足さないのは §6 の規則どおりです
- amber は light では地と輝度が近く、文字にすると読めなくなります。amber を「注意と選択」の面に使い文字色にしないのは §4 の規則どおりです。現状: ORD / RX のキー、選択中の tab、SPAWN の文字は amber で、light 側は暗い amber に差し替えて読めるようにしています

CSS を変えたら theme manifest を更新して検査します。cockpit は telemetry の CSS の digest を照合して theme を当てるので、manifest が古いままだと単体の dark は正常でも埋め込み時に theme が拒否されます:

```bash
python3 scripts/dashboard_theme_manifest.py --write
python3 scripts/dashboard_theme_manifest.py --check
```

仕組みの詳細は [Dashboard の Theme axis bridge](dashboard.md#theme-axis-bridge) にあります。

## 10. UI を足すときの作法

1. どの階層（§5）に入るかを 1 行で宣言する
2. token（§4）と 2 書体（§3）だけで組む。新しい色相、新しい書体、literal な色を足さない
3. 状態は色だけで伝えない（§6）。動きは活動と注意にだけ使い、reduce で止める
4. 帯は折りたためるようにし、agent card を押し下げない（§5）
5. 古い値には時刻を添える。取れない・使えないは文字で言う（fail-closed の文言）
6. chrome のラベルは簡潔な英語の大文字（`USAGE · LEFT`、`WAITING FOR UPDATE`）。agent が生成した内容は原文の locale を保つ
7. 9px 未満の文字を作らない
8. 何かを削るなら消さずに沈める（opacity）。履歴と系譜は消えないことに価値がある。履歴範囲や dense のように「いま見せない」は別で、それは表示の範囲の話です

## 11. 変更前の確認

visual change を PR にする前に、次を自分の目で見ます。

1. dark と light の両方で screenshot を撮り、読めない文字がない。light は console の `window.AgentStackColorTheme.apply({preference: 'light', resolved: 'light'})` で出す（§9）。cockpit に埋め込んだときの最終確認は maintainer が行う
2. `prefers-reduced-motion` で新しい動きが止まり、主要な状態が文字と形で読める
3. 1440×900 の viewport（maintainer の基準。standalone で確認）で、帯を折りたたんだ状態なら先頭の agent card の名前と現在の行が初期画面に収まる
4. 新しい CSS に literal な色、`Chakra Petch`、`--cyan` `--bone` `--void` `--line` の古い名前がない（`grep` で確かめる）
5. agent がいない、mail が空、portrait が無い、provider が取れない、の各縮退が空白にならない
6. 新しく足した overlay や control は keyboard だけで開き、操作し、`Esc` で閉じられる（既存の bay はクリック可能な div で、keyboard の到達性は未整備。新規分の受入条件であって既存画面の保証ではない）
7. CSS を変えたら `scripts/dashboard_theme_manifest.py --write` と `--check` を通し、manifest の差分を PR に含める（§9）

## 関連文書

- cockpit の [DESIGN.md](https://github.com/gyroid-eth/orrery/blob/master/docs/DESIGN.md)（親。概念と cockpit 側の layout）
- [Dashboard](dashboard.md)（機能と操作）
- [設定](configuration.md)（`AGENTSTACK_*` 環境変数）
- [CONTRIBUTING.md](../CONTRIBUTING.md)（PR の出し方）
