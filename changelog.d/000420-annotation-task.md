### agent の役割とタスクの説明を利用者が編集できるようにする

annotationに200字までの`task`を追加しました。Mailへ登録された元の説明を変えずに表示を上書きでき、空文字または`null`で保存すると元の説明へ戻ります。dashboardの一覧とNETWORKでも上書きを表示します。

`POST /api/annotate`は、省略した欄を維持し、明示的な空文字または`null`でその欄を外す部分更新になりました。role・emoji・group・taskに共通で、全欄が空になったときだけannotation全体を削除します。role・emoji・groupの既存の全項目送信は従来どおりです。新しい欄と更新の意味を API の利用側が識別できるよう、`GET /api/version`の互換世代を37から38へ上げました。

読み取り専用MCPアダプターもlegacy・freshの両方式でAPI 38を受け付けます。公開するagentの欄は増やさず、annotationとタスクの説明は返しません。未知のAPI世代は引き続き拒否します。
