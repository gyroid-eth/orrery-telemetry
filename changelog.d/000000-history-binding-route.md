### 会話の履歴の確認を軽くする 1 人分の binding

legacy の dashboard に `GET /api/agents/<name>/history-binding` を足します。cockpit の会話の履歴は、中身を見せる前に binding を確かめるたびに roster 全体（稼働中の pane の読み取りを含み、live で約 1.3 秒）を待っていましたが、この route で 1 人分の binding を、`/api/agents?scope=live` のその行と同じ判断で、pane を読まずに得られます（私有の 595 人の bench で約 40ms）。API の互換世代は 35 から 36 へ上げます。fresh と global ではこの route に 404 `agent_unavailable` を返し、fresh の保護された履歴経路は変わりません。[1 人分の route](../docs/api.md#get-apiagentsnamehistory-binding)を参照してください。
