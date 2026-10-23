### 保存元を維持する Mail の未公開 B1 核を追加

#213 の S′向けに、全保存元の名前解決、本人資格と対象の分離、純粋な閲覧、本人 read/ack、再試行の固定を隔離して検証する核を追加しました。既定 server・installer・hook・休眠 global mode には接続せず、本番挙動と dashboard API generation は変わりません。send/reply は宛先 decision までで、実配送・予約の絶対 path 化・切替は後続です。[実装範囲](../docs/storage-origin-b1.md)を参照してください。

private updater の旧 binding 解決は、資格履歴を反映した私有 index の全検証後に行います。準備不足・整合性破損は owner の要解決と分けて停止します。decision には接続先と解決 revision を含め、実 whois の返値と副作用のない境界も検証します。
