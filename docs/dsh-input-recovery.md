# dsh 入力の受領と回復

中断・ACK喪失・busyは通常起こり得、二重入力と重複要求につながるため、
受領とsubmitの記録を分け、不明な送信を自動再送しない。

`integrations/dsh/input_receiver.mjs` はtrusted pluginのcoreである。
TUI/Cordisへのmount、IPC、永続journal、正規watcher、cockpit transportには未接続。
owner認証・process検査・proxyを別実装せず、共通側が検証したbindingと通知を受ける。

## Mailからの入力

呼出し元は `binding`（identity/ownerGeneration/sessionId）、`journal`、
`admit`、`submit` を渡す。journalは同じowner/sessionに属する専用領域に保存し、
`write` はdurable完了後に返す。今回はmemory fixtureで契約を検査するだけで、
新しいdisk storeを実装していない。

- `receive` は宛先を照合し、message IDごとに一度だけqueueへ保存する。
  同ID再通知には保存済み受領receiptを返す。ACKは保存成功後だけ。
- busy中はFIFO保留。trusted hostは受領ACK後又はidle eventで `drain` を呼ぶ。
  `drain` は独立admissionを通した一件だけをsubmitする。本文は予算やscopeの承認にならない。
- submit前にintent、native受領確認後にsubmittedを保存する。共通admissionの
  private owner/token値をjournalへ写さず、turnとbudget generationだけを残す。
- submit例外・receipt不一致・再起動時の未確定intentはunknownとして止める。
  native receiptの照合なしに再submitしない。正規hostがturn完了を確認してbusyを解除する。
- 停止中は受領成功を返さない。fetch_inboxのread状態は変更しない。

このcoreのserializeは同一instance内だけ。既存製品handoffの単一ownerと永続journalへ
接続する必要があり、複数writerの認証/lockをこのmoduleへ追加しない。
legacyのend-to-end exactly-once実行は約束しない。

## 人間のpasteと回復

`sameInputTarget` はsocket/session/pane/owner世代/runtime PID/input epochが一致し、
readyであるときだけ続行可能とする。これは共通側のowner/process検証の代用品ではない。
実transportは各chunk/CRの直前に同じ固定対象を再照合し、変化時は残りを送らない。

`pasteOutcome` は**実際に到達確認したbytes数**を受ける。送信を試みたbytes数や
`send-keys wait=False` の呼出し成功だけでは到達確認にしない。

| 結果 | 意味 | 回復 |
| --- | --- | --- |
| not-sent | 0 bytes・submitなしを確認 | 対象を再確認して新規入力 |
| partial | 一部又は全pasteの到達は既知、CRは送っていない | 同じdraftを人間が確認/clearしてから新規入力。続きを自動送信しない |
| unknown | 到達量又はsubmit結果が不明 | native turn/受領receiptを照合するまで再送しない |
| submitted | 全pasteとsubmitの受領を確認 | 再送しない |

どの回復結果でも自動再送/追加CRを許可しない。fixtureは途中pane切替・owner更新・
終了・ACK喪失を再現し、残り/CR/二重submitが出ないことを検査する。
これは実PTYでの原子的配送を証明するものではなく、共有transport接続時に同じ消費側を検査する。

```sh
python3 tests/test_dsh_input_receiver.py
```

Node標準runnerで10件を実行する。一致対象の通常pasteと、intentのdurable完了前は
submitしないdeferred writeの対照を含む。既存Nodeがなければskipを明示し、取得しない。
runtimeのNode pin検査とは別のoffline module試験であり、provider/TUIを起動しない。
