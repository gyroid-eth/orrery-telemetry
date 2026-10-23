### 保存元を維持する Mail の未公開 B1 核を追加

#213 の S′向けに、全保存元の名前解決、本人資格と対象の分離、純粋な閲覧、本人 read/ack、再試行の固定を隔離して検証する核を追加しました。既定 server・installer・hook・休眠 global mode には接続せず、本番挙動と dashboard API generation は変わりません。send/reply は宛先 decision までで、実配送・予約の絶対 path 化・切替は後続です。[実装範囲](../docs/storage-origin-b1.md)を参照してください。
