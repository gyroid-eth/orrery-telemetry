### Codex の起動確認で観測不能を死亡と誤判定しない

Codex 子の起動確認は、その launch の receipt と rollout に記録された task 開始を先に確認します。pane や process の観測が sandbox の権限拒否・timeout 等で取得できない場合は、死亡と断定せず「起動未確認」として同じ子と復旧情報を保持し、固定の理由 code と終了値を残します。明示的な session 不在は引き続き失敗です。確認不能を理由に task 再送・重複 spawn・別 transport への切替は行いません。
