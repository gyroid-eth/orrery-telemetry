Community-maintained / experimental。検証環境: Windows 11 build 26200、CPython 3.12.10、PowerShell、Chrome。

# ネイティブ Windows での起動境界

> English version: [windows-spawn.en.md](windows-spawn.en.md)

ネイティブ Windows では、NEW AGENT を開くと spawn が非対応であること、WSL2 が
Windows での主経路であることが表示されます。SPAWN button は無効のままです。
理由を読めるよう、dialog 自体は表示されます。

`GET /api/spawn-names` は HTTP 200 で `unavailable` の説明に加え、
`names`（name、portrait、occupancy status）、`adjectives`、`naming` を返します。
Windows はこれらを既存の launcher array と scientist JSON から、Bash を起動せず
Python で読みます。これは語彙のみの応答であり、directory、provider、model を
使用可能な起動 option として提示するものではありません。既存 dialog は選択可能な
roster ではなく、意図的に unavailable 状態のまま表示され続けます。ネイティブ
launch は未実装です。

reader は現行の単一の literal ASCII-word adjective array を、LF でも CRLF でも
受け付け、`AGENTSTACK_SCIENTISTS_JSON` が非空ならそれを、そうでなければ checkout
内の `dashboard/scientist_portraits.json` を使います。JSON は object である必要が
あり、key は launcher の ASCII alphabetic rule でソート・filter されます。Shell
expression、quoted array entry、空の語彙、adjective の重複、file の欠落や不正な
形式は fail closed します。これは汎用の Bash interpreter ではないため、source
format への変更は Windows reader と合わせてレビューする必要があります。

Catalog の読み取り失敗は HTTP 503 を返します。Name suggestion は同じ reader を
使い、data が利用できない場合は fail closed します。Windows での direct spawn
request は登録や launcher の処理より前に error を返すため、vocabulary access が
向上しても未対応の spawn path が有効化されることはありません。

既存の non-Windows catalog path は変更していません。ネイティブ catalog 取得と
ネイティブ launch は issue #9 の別作業です。この変更は vocabulary の読み取りのみを
実装するもので、support table は変更せず、Codex App Bridge の検証も行いません。

検証範囲は、subprocess 実行を伴わない実際の HTTP handler、modal の
unavailable-state handler と stale-response guard、正規の vocabulary hash、
CRLF/Unicode path、JSON override、不正な入力、occupancy/suggestion、direct spawn
の拒否、NEW AGENT dialog の Chrome での確認です。この Windows suite は完全な
ネイティブ対応を保証するものではありません。
