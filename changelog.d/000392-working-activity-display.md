### 作業中の表示と最後の記録時刻を分けて読む

DECK は所要時間や context が取れない作業状態にも `WORKING` を表示します。ライトテーマの作業badge文字は既存の amber token で表示し、背景との区別を保ちます。footer と detail は最後に記録されたイベントを `LAST EVENT` / `last event` と明示し、長い tool 実行中に時刻が古いままでも現在の状態と区別できます。表示用の要約が無い場合は `NO LIVE SUMMARY` と示し、作業中に待機・停止と矛盾する文言を出しません。

既存 provider の状態分類（Codex App の登録中を含む）を表示し、実際の進捗を保証しません。`last_active` の計算・履歴期間・並び順と API 世代34 は変更しません。
